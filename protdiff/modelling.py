"""
Modelling
"""
import os
import re
import shutil
import time
import glob
from pathlib import Path
import json
import inspect
import logging
import math
import functools
from typing import *

import torch
from torch import nn
from torch.nn import functional as F

import pytorch_lightning as pl

from transformers import BertConfig
from transformers.models.bert.modeling_bert import (
    BertPreTrainedModel,
    BertEncoder,
)
from transformers.activations import get_activation
from transformers.optimization import get_linear_schedule_with_warmup

from . import losses

LR_SCHEDULE = Optional[Literal["OneCycleLR", "LinearWarmup"]]
TIME_ENCODING = Literal["gaussian_fourier", "sinusoidal"]
LOSS_KEYS = Literal["l1", "smooth_l1"]
DECODER_HEAD = Literal["mlp", "linear"]


class GaussianFourierProjection(nn.Module):
    """
    Gaussian random features for encoding time steps.
    Built primarily for score-based models.

    Source:
    https://colab.research.google.com/drive/120kYYBOVa1i0TD85RjlEkFjaWDxSFUx3?usp=sharing#scrollTo=YyQtV7155Nht
    """

    def __init__(self, embed_dim: int, scale: float = 2 * torch.pi):
        super().__init__()
        # Randomly sample weights during initialization. These weights are fixed
        # during optimization and are not trainable.
        w = torch.randn(embed_dim // 2) * scale
        assert w.requires_grad == False
        self.register_buffer("W", w)

    def forward(self, x: torch.Tensor):
        """
        takes as input the time vector and returns the time encoding
        time (x): (batch_size, )
        output  : (batch_size, embed_dim)
        """
        if x.ndim > 1:
            x = x.squeeze()
        elif x.ndim < 1:
            x = x.unsqueeze(0)
        x_proj = x[:, None] * self.W[None, :] * 2 * torch.pi
        embed = torch.cat([torch.sin(x_proj), torch.cos(x_proj)], dim=-1)
        return embed


class SinusoidalPositionEmbeddings(nn.Module):
    """
    Positional embeddings
    """

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def forward(self, time: torch.Tensor) -> torch.Tensor:
        device = time.device
        half_dim = self.dim // 2
        embeddings = math.log(10000) / (half_dim - 1)
        # half_dim shape
        embeddings = torch.exp(torch.arange(half_dim, device=device) * -embeddings)
        # outer product (batch, 1) x (1, half_dim) -> (batch x half_dim)
        embeddings = time[:, None] * embeddings[None, :]
        # sin and cosine embeddings
        embeddings = torch.cat((embeddings.sin(), embeddings.cos()), dim=-1)
        return embeddings


class PositionalEncoding(nn.Module):
    """
    Positional embedding for BERT.
    Source: https://pytorch.org/tutorials/beginner/transformer_tutorial.html
    """

    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model)
        )
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer("pe", pe)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: Tensor, shape [batch_size, seq_len, embedding_dim]
        """
        assert len(x.shape) == 3
        orig_shape = x.shape
        # x is a tensor of shape (batch_size, seq_len, embedding_dim)
        # permute to be (seq_len, batch_size, embedding_dim)
        x = x.permute(1, 0, 2)
        x += self.pe[: x.size(0)]
        # permute back to (batch_size, seq_len, embedding_dim)
        x = x.permute(1, 0, 2)
        assert x.shape == orig_shape, f"{x.shape} != {orig_shape}"
        return self.dropout(x)


class BertEmbeddings(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.position_embedding_type = getattr(
            config, "position_embedding_type", "absolute"
        )
        if self.position_embedding_type == "absolute":
            self.position_embeddings = nn.Embedding(
                config.max_position_embeddings, config.hidden_size
            )
            self.register_buffer(
                "position_ids",
                torch.arange(config.max_position_embeddings).expand((1, -1)),
            )

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        # position_ids (1, len position emb) is contiguous in memory and exported when serialized

    def forward(
        self,
        input_embeds: torch.Tensor,
        position_ids: torch.LongTensor,
    ) -> torch.Tensor:
        assert position_ids is not None, "`position_ids` must be defined"
        embeddings = input_embeds
        if self.position_embedding_type == "absolute":
            position_embeddings = self.position_embeddings(position_ids)
            embeddings += position_embeddings

        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class AnglesPredictor(nn.Module):
    """
    Predict angles from the embeddings. For BERT, the MLM task is done using an
    architecture like
    d_model -> dense -> d_model -> activation -> layernorm -> dense -> d_output
    https://github.com/huggingface/transformers/blob/v4.21.1/src/transformers/models/bert/modeling_bert.py#L681

    activation should be given as nn.ReLU for example -- NOT nn.ReLU()
    """

    def __init__(
        self,
        d_model: int,
        d_out: int = 4,
        activation: Union[str, nn.Module] = "gelu",
        eps: float = 1e-12,
    ) -> None:
        super().__init__()
        self.d_model = d_model
        self.d_out = d_out
        self.dense1 = nn.Linear(d_model, d_model)

        if isinstance(activation, str):
            self.dense1_act = get_activation(activation)
        else:
            self.dense1_act = activation()
        self.layer_norm = nn.LayerNorm(d_model, eps=eps)

        self.dense2 = nn.Linear(d_model, d_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.dense1(x)
        x = self.dense1_act(x)
        x = self.layer_norm(x)
        x = self.dense2(x)
        return x


class BertForDiffusion(BertPreTrainedModel, pl.LightningModule):
    """
    BERT designed to be used with continuous inputs instead of tokens

    Reference: https://github.com/huggingface/transformers/blob/f681437203baa7671de3174b0fa583c349d9d5e1/src/transformers/models/bert/modeling_bert.py#L870

    Decoder: linear = single linear decoding of per-position embeddings
             mlp = two-layer MLP to decode per-position embeddings
    """

    # Define loss functions and their wrapped angular versions
    nonangular_loss_fn_dict = {
        "l1": F.l1_loss,
        "smooth_l1": F.smooth_l1_loss,
    }
    angular_loss_fn_dict = {
        "l1": losses.radian_l1_loss,
        "smooth_l1": functools.partial(
            losses.radian_smooth_l1_loss, beta=torch.pi / 10
        ),
    }
    # To have legacy models still work with these
    loss_autocorrect_dict = {
        "radian_l1_smooth": "smooth_l1",
    }

    def __init__(
        self,
        config,
        ft_is_angular: List[bool] = [False, True, True, True],
        ft_names: Optional[List[str]] = None,
        time_encoding: TIME_ENCODING = "gaussian_fourier",
        decoder: DECODER_HEAD = "mlp",
        lr: float = 5e-5,
        loss: Union[Callable, LOSS_KEYS] = "smooth_l1",
        l2: float = 0.0,
        l1: float = 0.0,
        circle_reg: float = 0.0,
        epochs: int = 1,
        steps_per_epoch: int = 250,  # Dummy value
        lr_scheduler: LR_SCHEDULE = None,
        write_preds_to_dir: Optional[str] = None,
    ) -> None:
        """
        dim should be the dimension of the inputs
        """
        super().__init__(config)
        self.config = config
        if self.config.is_decoder:
            raise NotImplementedError
        n_inputs = len(ft_is_angular)
        self.n_inputs = n_inputs

        if ft_names is not None:
            self.ft_names = ft_names
        else:
            self.ft_names = [f"ft{i}" for i in range(n_inputs)]
        assert (
            len(self.ft_names) == n_inputs
        ), f"Got {len(self.ft_names)} names, expected {n_inputs}"
        # Store information about leraning rates and loss
        self.learning_rate = lr
        # loss function is either a callable or a list of callables
        if isinstance(loss, str):
            logging.info(
                f"Mapping loss {loss} to list of losses corresponding to angular {ft_is_angular}"
            )
            if loss in self.loss_autocorrect_dict:
                logging.info(
                    "Autocorrecting {} to {}".format(
                        loss, self.loss_autocorrect_dict[loss]
                    )
                )
                loss = self.loss_autocorrect_dict[loss]
            self.loss_func = [
                self.angular_loss_fn_dict[loss]
                if is_angular
                else self.nonangular_loss_fn_dict[loss]
                for is_angular in ft_is_angular
            ]
        else:
            logging.warning(
                f"Using pre-given callable loss: {loss}. This may not handle angles correctly!"
            )
            self.loss_func = loss
        pl.utilities.rank_zero_info(f"Using loss: {self.loss_func}")
        if isinstance(self.loss_func, (tuple, list)):
            assert (
                len(self.loss_func) == self.n_inputs
            ), f"Got {len(self.loss_func)} loss functions, expected {self.n_inputs}"

        self.l1_lambda = l1
        self.l2_lambda = l2
        self.circle_lambda = circle_reg
        self.epochs = epochs
        self.steps_per_epoch = steps_per_epoch
        self.lr_scheduler = lr_scheduler

        # Needed to project the low dimensional input to hidden dim
        self.inputs_to_hidden_dim = nn.Linear(
            in_features=n_inputs, out_features=config.hidden_size
        )
        self.embeddings = BertEmbeddings(config)
        self.encoder = BertEncoder(config)

        # Set up the network to project token representation to our four outputs
        if decoder == "linear":
            self.token_decoder = nn.Linear(config.hidden_size, n_inputs)
        elif decoder == "mlp":
            self.token_decoder = AnglesPredictor(config.hidden_size, n_inputs)
        else:
            raise ValueError(f"Unrecognized decoder: {decoder}")

        # Set up the time embedder
        if time_encoding == "gaussian_fourier":
            self.time_embed = GaussianFourierProjection(config.hidden_size)
        elif time_encoding == "sinusoidal":
            self.time_embed = SinusoidalPositionEmbeddings(config.hidden_size)
        else:
            raise ValueError(f"Unknown time encoding: {time_encoding}")
        pl.utilities.rank_zero_info(f"Using time embedding: {self.time_embed}")

        # Initialize weights and apply final processing
        self.init_weights()

        # Set up the output directory for writing predictions
        self.write_preds_to_dir = write_preds_to_dir
        self.write_preds_counter = 0
        if self.write_preds_to_dir:
            os.makedirs(self.write_preds_to_dir, exist_ok=True)

        # Epoch counters and timers
        self.train_epoch_counter = 0
        self.train_epoch_last_time = time.time()

    @classmethod
    def from_dir(
        cls,
        dirname: str,
        ft_is_angular: Optional[Sequence[bool]] = None,
        load_weights: bool = True,
        idx: int = -1,
        best_by: Literal["train", "valid"] = "valid",
        copy_to: str = "",
        **kwargs,
    ) -> "BertForDiffusion":
        """
        Builds this model out from directory. Legacy mode is for loading models
        before there were separate folders for training and validation best models.
        idx indicates which model to load if multiple are given
        """
        train_args_fname = os.path.join(dirname, "training_args.json")
        with open(train_args_fname, "r") as source:
            train_args = json.load(source)
        config = BertConfig.from_json_file(os.path.join(dirname, "config.json"))

        if ft_is_angular is None:
            ft_is_angular = {
                "canonical": [False, False, False, True, True, True, True, True, True],
                "canonical-full-angles": [True, True, True, True, True, True],
                "canonical-minimal-angles": [True, True, True, True],
                "cart-coords": [False, False, False],
            }[train_args["angles_definitions"]]
            logging.info(f"Auto constructed ft_is_angular: {ft_is_angular}")

        model_args = dict(
            config=config,
            ft_is_angular=ft_is_angular,
            time_encoding=train_args["time_encoding"],
            decoder=train_args["decoder"],
            lr=train_args["lr"],
            loss=train_args["loss"],
            l2=train_args["l2_norm"],
            l1=train_args["l1_norm"],
            circle_reg=train_args["circle_reg"],
            min_epochs=train_args["min_epochs"],
            lr_scheduler=train_args["lr_scheduler"],
            **kwargs,
        )

        if load_weights:
            epoch_getter = lambda x: int(
                re.findall(r"epoch=[0-9]+", os.path.basename(x)).pop().split("=")[-1]
            )
            subfolder = f"best_by_{best_by}"
            # Sort checkpoints by epoch -- last item is latest epoch
            ckpt_names = sorted(
                glob.glob(os.path.join(dirname, "models", subfolder, "*.ckpt")),
                key=epoch_getter,
            )
            logging.info(f"Found {len(ckpt_names)} checkpoints")
            ckpt_name = ckpt_names[idx]
            logging.info(f"Loading weights from {ckpt_name}")
            retval = cls.load_from_checkpoint(checkpoint_path=ckpt_name, **model_args)
        else:
            retval = cls(**model_args)
            logging.info(f"Loaded unitialized model from {dirname}")

        # If specified, copy out the requisite files to the given directory
        if copy_to:
            logging.info(f"Copying minimal model file set to: {copy_to}")
            os.makedirs(copy_to, exist_ok=True)
            copy_to = Path(copy_to)
            with open(copy_to / "training_args.json", "w") as sink:
                json.dump(train_args, sink)
            config.save_pretrained(copy_to)
            if load_weights:
                # Create the direcotry structure
                ckpt_dir = copy_to / "models" / subfolder
                os.makedirs(ckpt_dir, exist_ok=True)
                shutil.copyfile(ckpt_name, ckpt_dir / os.path.basename(ckpt_name))

        return retval

    def forward(
        self,
        inputs: torch.Tensor,
        timestep: torch.Tensor,  # Tensor of shape batch_length with time indices
        attention_mask: torch.Tensor,
        position_ids: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ):
        r"""
        encoder_hidden_states  (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
            Sequence of hidden-states at the output of the last layer of the encoder. Used in the cross-attention if
            the model is configured as a decoder.
        encoder_attention_mask (`torch.FloatTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Mask to avoid performing attention on the padding token indices of the encoder input. This mask is used in
            the cross-attention if the model is configured as a decoder. Mask values selected in `[0, 1]`:
            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.
        past_key_values (`tuple(tuple(torch.FloatTensor))` of length `config.n_layers` with each tuple having 4 tensors of shape `(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains precomputed key and value hidden states of the attention blocks. Can be used to speed up decoding.
            If `past_key_values` are used, the user can optionally input only the last `decoder_input_ids` (those that
            don't have their past key value states given to this model) of shape `(batch_size, 1)` instead of all
            `decoder_input_ids` of shape `(batch_size, sequence_length)`.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
            `past_key_values`).
        """
        # pl.utilities.rank_zero_debug("Train status", self.training)
        # pl.utilities.rank_zero_debug(
        #     "Inputs", inputs.device, timestep.device, attention_mask.device
        # )
        # pl.utilities.rank_zero_debug(
        #     "Inputs", inputs.dtype, timestep.dtype, attention_mask.dtype
        # )

        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        input_shape = inputs.size()
        batch_size, seq_length, *_ = input_shape
        logging.debug(f"Detected batch {batch_size} and seq length {seq_length}")

        assert attention_mask is not None

        # If position IDs are not given, auto-generate them
        if position_ids is None:
            # [1, seq_length]
            position_ids = (
                torch.arange(
                    seq_length,
                )
                .expand(batch_size, -1)
                .type_as(timestep)
            )

        # pl.utilities.rank_zero_debug(
        #     "Position IDs", position_ids.device, position_ids.dtype
        # )

        # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads. This code is taken
        # from hugggingface modeling_utils
        assert (
            attention_mask.dim() == 2
        ), f"Attention mask expected in shape (batch_size, seq_length), got {attention_mask.shape}"
        extended_attention_mask = attention_mask[:, None, None, :]
        extended_attention_mask = extended_attention_mask.type_as(attention_mask)
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        # msk = torch.ones(size=(self.config.num_attention_heads,))
        # msk = msk.type_as(inputs)
        # head_mask = self.get_head_mask(msk, self.config.num_hidden_layers)

        assert len(inputs.shape) == 3  # batch_size, seq_length, features
        inputs_upscaled = self.inputs_to_hidden_dim(inputs)  # Batch * seq_len * dim

        # Pass through embeddings
        inputs_upscaled = self.embeddings(inputs_upscaled, position_ids=position_ids)

        # timestep is (batch, 1), squeeze to (batch,)
        # embedding gets to (batch, embed_dim) -> unsqueee to (batch, 1, dim)
        time_encoded = self.time_embed(timestep.squeeze(dim=-1)).unsqueeze(1)
        inputs_with_time = inputs_upscaled + time_encoded
        encoder_outputs = self.encoder(
            inputs_with_time,
            attention_mask=extended_attention_mask,
            # head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = encoder_outputs[0]
        per_token_decoded = self.token_decoder(sequence_output)
        return per_token_decoded

    def _get_loss_terms(
        self, batch, write_preds: Optional[str] = None
    ) -> List[torch.Tensor]:
        """
        Returns the loss terms for the model. Length of the returned list
        is equivalent to the number of features we are fitting to.
        """
        known_noise = batch["known_noise"]
        predicted_noise = self.forward(
            batch["corrupted"],
            batch["t"],
            attention_mask=batch["attn_mask"],
            position_ids=batch["position_ids"],
        )
        assert (
            known_noise.shape == predicted_noise.shape
        ), f"{known_noise.shape} != {predicted_noise.shape}"

        # Indexes into batch then indices along sequence length
        # attn_mask has shape (batch, seq_len) --> where gives back
        # two lists of values, one for each dimension
        # known_noise has shape (batch, seq_len, num_fts)
        unmask_idx = torch.where(batch["attn_mask"])
        assert len(unmask_idx) == 2
        loss_terms = []
        for i in range(known_noise.shape[-1]):
            loss_fn = (
                self.loss_func[i]
                if isinstance(self.loss_func, list)
                else self.loss_func
            )
            logging.debug(f"Using loss function {loss_fn}")
            # Determine whether the loss accepts circle_penalty
            # https://stackoverflow.com/questions/23228664/how-to-check-which-arguments-a-function-method-takes
            loss_args = inspect.getfullargspec(loss_fn)
            if (
                "circle_penalty" in loss_args.args
                or "circle_penalty" in loss_args.kwonlyargs
            ):
                logging.debug(f"Loss function {loss_fn} accepts circle_penalty")
                l = loss_fn(
                    predicted_noise[unmask_idx[0], unmask_idx[1], i],
                    known_noise[unmask_idx[0], unmask_idx[1], i],
                    circle_penalty=self.circle_lambda,
                )
            else:
                logging.debug(f"Loss function {loss_fn} does not accept circle_penalty")
                l = loss_fn(
                    predicted_noise[unmask_idx[0], unmask_idx[1], i],
                    known_noise[unmask_idx[0], unmask_idx[1], i],
                )
            loss_terms.append(l)

        if write_preds is not None:
            with open(write_preds, "w") as f:
                d_to_write = {
                    "known_noise": known_noise.cpu().numpy().tolist(),
                    "predicted_noise": predicted_noise.cpu().numpy().tolist(),
                    "attn_mask": batch["attn_mask"].cpu().numpy().tolist(),
                    "losses": [l.item() for l in loss_terms],
                }
                json.dump(d_to_write, f)

        return torch.stack(loss_terms)

    def training_step(self, batch, batch_idx):
        """
        Training step, runs once per batch
        """
        loss_terms = self._get_loss_terms(batch)
        # pl.utilities.rank_zero_debug("Training stacked and gathered loss", loss_terms)
        avg_loss = torch.mean(loss_terms)

        # L1 loss implementation
        if self.l1_lambda > 0:
            l1_penalty = sum(torch.linalg.norm(p, 1) for p in self.parameters())
            avg_loss += self.l1_lambda * l1_penalty

        assert len(loss_terms) == len(self.ft_names)
        loss_dict = {
            f"train_loss_{val_name}": val
            for val_name, val in zip(self.ft_names, loss_terms)
        }
        loss_dict["train_loss"] = avg_loss
        self.log_dict(loss_dict)  # Don't seem to need rank zero or sync dist

        return avg_loss

    def training_epoch_end(self, outputs) -> None:
        """Log the average training loss over the epoch"""
        losses = torch.stack([o["loss"] for o in outputs])
        mean_loss = torch.mean(losses)
        t_delta = time.time() - self.train_epoch_last_time
        pl.utilities.rank_zero_info(
            f"Train loss at epoch {self.train_epoch_counter} end: {mean_loss:.4f} ({t_delta:.2f} seconds)"
        )
        # Increment counter and timers
        self.train_epoch_counter += 1
        self.train_epoch_last_time = time.time()

    def validation_step(self, batch, batch_idx) -> Dict[str, torch.Tensor]:
        """
        Validation step
        """
        with torch.no_grad():
            loss_terms = self._get_loss_terms(
                batch,
                write_preds=os.path.join(
                    self.write_preds_to_dir, f"{self.write_preds_counter}_preds.json"
                )
                if self.write_preds_to_dir
                else None,
            )
            self.write_preds_counter += 1
        avg_loss = torch.mean(loss_terms)

        # Log each of the loss terms
        assert len(loss_terms) == len(self.ft_names)
        loss_dict = {
            f"val_loss_{val_name}": self.all_gather(val)
            for val_name, val in zip(self.ft_names, loss_terms)
        }
        loss_dict["val_loss"] = avg_loss
        # with rank zero it seems that we don't need to use sync_dist
        self.log_dict(loss_dict, rank_zero_only=True)

        return {"val_loss": avg_loss}

    def validation_epoch_end(self, outputs) -> None:
        """Log the average validation loss over the epoch"""
        # Note that this method is called before zstraining_epoch_end().
        losses = torch.stack([o["val_loss"] for o in outputs])
        mean_loss = torch.mean(losses)
        pl.utilities.rank_zero_info(
            f"Valid loss at epoch {self.train_epoch_counter} end: {mean_loss:.4f}"
        )

    def configure_optimizers(self) -> Dict[str, Any]:
        """
        Return optimizer. Limited support for some optimizers
        """
        optim = torch.optim.AdamW(
            self.parameters(),
            lr=self.learning_rate,
            weight_decay=self.l2_lambda,
        )
        retval = {"optimizer": optim}
        if self.lr_scheduler:
            if self.lr_scheduler == "OneCycleLR":
                retval["lr_scheduler"] = {
                    "scheduler": torch.optim.lr_scheduler.OneCycleLR(
                        optim,
                        max_lr=1e-2,
                        epochs=self.epochs,
                        steps_per_epoch=self.steps_per_epoch,
                    ),
                    "monitor": "val_loss",
                    "frequency": 1,
                    "interval": "step",
                }
            elif self.lr_scheduler == "LinearWarmup":
                # https://huggingface.co/docs/transformers/v4.21.2/en/main_classes/optimizer_schedules#transformers.get_linear_schedule_with_warmup
                # Transformers typically do well with linear warmup
                warmup_steps = int(self.epochs * 0.1)
                pl.utilities.rank_zero_info(
                    f"Using linear warmup with {warmup_steps}/{self.epochs} warmup steps"
                )
                retval["lr_scheduler"] = {
                    "scheduler": get_linear_schedule_with_warmup(
                        optim,
                        num_warmup_steps=warmup_steps,
                        num_training_steps=self.epochs,
                    ),
                    "frequency": 1,
                    "interval": "epoch",  # Call after 1 epoch
                }
            else:
                raise ValueError(f"Unknown lr scheduler {self.lr_scheduler}")
        pl.utilities.rank_zero_info(f"Using optimizer {retval}")
        return retval


def main():
    """on the fly testing"""
    import datasets
    from torch.utils.data import default_collate

    clean_dset = datasets.CathCanonicalAnglesDataset(toy=True)
    noised_dset = datasets.NoisedAnglesDataset(clean_dset, "angles")
    for k, v in noised_dset[0].items():
        print(k, v.shape)
    x = default_collate([noised_dset[i] for i in range(8)])

    # # Create model
    # device = torch.device("cuda")
    cfg = BertConfig()
    model = BertForDiffusion(cfg)
    # print(model)
    y = model.forward(x["corrupted"], x["t"].squeeze())
    print(y.shape)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
