# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# BSD 3-Clause License
#
# Copyright (c) 2021, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#     this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from typing import List, Optional

import torch
from omegaconf import DictConfig

from nemo.collections.asr.parts.utils import adapter_utils
from nemo.collections.tts.modules.transformer import _LayerNorm
from nemo.collections.tts.parts.utils.helpers import binarize_attention_parallel, regulate_len
from nemo.core.classes import NeuralModule, adapter_mixins, typecheck
from nemo.core.neural_types.elements import (
    EncodedRepresentation,
    Index,
    LengthsType,
    LogprobsType,
    MelSpectrogramType,
    ProbsType,
    RegressionValuesType,
    TokenDurationType,
    TokenIndex,
    TokenLogDurationType,
)
from nemo.core.neural_types.neural_type import NeuralType


def average_features(pitch, durs):
    durs_cums_ends = torch.cumsum(durs, dim=1).long()
    durs_cums_starts = torch.nn.functional.pad(durs_cums_ends[:, :-1], (1, 0))
    pitch_nonzero_cums = torch.nn.functional.pad(torch.cumsum(pitch != 0.0, dim=2), (1, 0))
    pitch_cums = torch.nn.functional.pad(torch.cumsum(pitch, dim=2), (1, 0))

    bs, l = durs_cums_ends.size()
    n_formants = pitch.size(1)
    dcs = durs_cums_starts[:, None, :].expand(bs, n_formants, l)
    dce = durs_cums_ends[:, None, :].expand(bs, n_formants, l)

    pitch_sums = (torch.gather(pitch_cums, 2, dce) - torch.gather(pitch_cums, 2, dcs)).float()
    pitch_nelems = (torch.gather(pitch_nonzero_cums, 2, dce) - torch.gather(pitch_nonzero_cums, 2, dcs)).float()

    pitch_avg = torch.where(pitch_nelems == 0.0, pitch_nelems, pitch_sums / pitch_nelems)
    return pitch_avg


class ConvReLUNorm(torch.nn.Module, adapter_mixins.AdapterModuleMixin):
    def __init__(self, in_channels, out_channels, kernel_size=1, dropout=0.0, speaker_size=384, condition_lnorm=False):
        super(ConvReLUNorm, self).__init__()
        self.conv = torch.nn.Conv1d(in_channels, out_channels, kernel_size=kernel_size, padding=(kernel_size // 2))
        self.norm = _LayerNorm(out_channels, spk_emb_dim=speaker_size, condition=condition_lnorm)
        self.dropout = torch.nn.Dropout(dropout)

    def forward(self, signal, conditioning=None):
        out = torch.nn.functional.relu(self.conv(signal))
        out = self.norm(out.transpose(1, 2), conditioning).transpose(1, 2)
        out = self.dropout(out)

        if self.is_adapter_available():
            out = self.forward_enabled_adapters(out.transpose(1, 2)).transpose(1, 2)

        return out


class TemporalPredictor(NeuralModule):
    """Predicts a single float per each temporal location"""

    def __init__(self, input_size, filter_size, kernel_size, dropout, n_layers=2, condition_lnorm=False):
        super(TemporalPredictor, self).__init__()

        self.layers = torch.nn.ModuleList()
        for i in range(n_layers):
            self.layers.append(
                ConvReLUNorm(
                    input_size if i == 0 else filter_size,
                    filter_size,
                    kernel_size=kernel_size,
                    dropout=dropout,
                    speaker_size=input_size,
                    condition_lnorm=condition_lnorm,
                )
            )
        self.fc = torch.nn.Linear(filter_size, 1, bias=True)
        self.filter_size = filter_size

    @property
    def input_types(self):
        return {
            "enc": NeuralType(('B', 'T', 'D'), EncodedRepresentation()),
            "enc_mask": NeuralType(('B', 'T', 1), TokenDurationType()),
            "conditioning": NeuralType(('B', 'T', 'D'), EncodedRepresentation(), optional=True),
        }

    @property
    def output_types(self):
        return {
            "out": NeuralType(('B', 'T'), EncodedRepresentation()),
        }

    def forward(self, enc, enc_mask, conditioning=None):
        if conditioning is not None:
            enc = enc + conditioning

        out = enc * enc_mask
        out = out.transpose(1, 2)

        for layer in self.layers:
            out = layer(out, conditioning=conditioning)

        out = out.transpose(1, 2)
        out = self.fc(out) * enc_mask
        return out.squeeze(-1)


class TemporalPredictorAdapter(TemporalPredictor, adapter_mixins.AdapterModuleMixin):

    # Higher level forwarding
    def add_adapter(self, name: str, cfg: dict):
        cfg = self._update_adapter_cfg_input_dim(cfg)
        for conv_layer in self.layers:  # type: adapter_mixins.AdapterModuleMixin
            conv_layer.add_adapter(name, cfg)

    def is_adapter_available(self) -> bool:
        return any([conv_layer.is_adapter_available() for conv_layer in self.layers])

    def set_enabled_adapters(self, name: Optional[str] = None, enabled: bool = True):
        for conv_layer in self.layers:  # type: adapter_mixins.AdapterModuleMixin
            conv_layer.set_enabled_adapters(name=name, enabled=enabled)

    def get_enabled_adapters(self) -> List[str]:
        names = set([])
        for conv_layer in self.layers:  # type: adapter_mixins.AdapterModuleMixin
            names.update(conv_layer.get_enabled_adapters())

        names = sorted(list(names))
        return names

    def _update_adapter_cfg_input_dim(self, cfg: DictConfig):
        cfg = adapter_utils.update_adapter_cfg_input_dim(self, cfg, module_dim=self.filter_size)
        return cfg


"""
Register any additional information
"""
if adapter_mixins.get_registered_adapter(TemporalPredictor) is None:
    adapter_mixins.register_adapter(base_class=TemporalPredictor, adapter_class=TemporalPredictorAdapter)


class FastPitchModule(NeuralModule, adapter_mixins.AdapterModuleMixin):
    def __init__(
        self,
        encoder_module: NeuralModule,
        decoder_module: NeuralModule,
        duration_predictor: NeuralModule,
        pitch_predictor: NeuralModule,
        energy_predictor: NeuralModule,
        aligner: NeuralModule,
        speaker_encoder: NeuralModule,
        n_speakers: int,
        symbols_embedding_dim: int,
        pitch_embedding_kernel_size: int,
        energy_embedding_kernel_size: int,
        n_mel_channels: int = 80,
        max_token_duration: int = 75,
        speaker_emb_condition_prosody: bool = False,
        speaker_emb_condition_decoder: bool = False,
        speaker_emb_condition_aligner: bool = False,
    ):
        super().__init__()

        self.encoder = encoder_module
        self.decoder = decoder_module
        self.duration_predictor = duration_predictor
        self.pitch_predictor = pitch_predictor
        self.energy_predictor = energy_predictor
        self.aligner = aligner
        self.speaker_encoder = speaker_encoder
        self.learn_alignment = aligner is not None
        self.use_duration_predictor = True
        self.binarize = False
        self.speaker_emb_condition_prosody = speaker_emb_condition_prosody
        self.speaker_emb_condition_decoder = speaker_emb_condition_decoder
        self.speaker_emb_condition_aligner = speaker_emb_condition_aligner

        # TODO: combine self.speaker_emb with self.speaker_encoder
        if n_speakers > 1:
            self.speaker_emb = torch.nn.Embedding(n_speakers, symbols_embedding_dim)
        else:
            self.speaker_emb = None

        self.max_token_duration = max_token_duration
        self.min_token_duration = 0

        self.pitch_emb = torch.nn.Conv1d(
            1,
            symbols_embedding_dim,
            kernel_size=pitch_embedding_kernel_size,
            padding=int((pitch_embedding_kernel_size - 1) / 2),
        )

        if self.energy_predictor is not None:
            self.energy_emb = torch.nn.Conv1d(
                1,
                symbols_embedding_dim,
                kernel_size=energy_embedding_kernel_size,
                padding=int((energy_embedding_kernel_size - 1) / 2),
            )

        # Store values precomputed from training data for convenience
        self.register_buffer('pitch_mean', torch.zeros(1))
        self.register_buffer('pitch_std', torch.zeros(1))

        self.proj = torch.nn.Linear(self.decoder.d_model, n_mel_channels, bias=True)

    @property
    def input_types(self):
        return {
            "text": NeuralType(('B', 'T_text'), TokenIndex()),
            "durs": NeuralType(('B', 'T_text'), TokenDurationType()),
            "pitch": NeuralType(('B', 'T_audio'), RegressionValuesType()),
            "energy": NeuralType(('B', 'T_audio'), RegressionValuesType(), optional=True),
            "speaker": NeuralType(('B'), Index(), optional=True),
            "pace": NeuralType(optional=True),
            "spec": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType(), optional=True),
            "attn_prior": NeuralType(('B', 'T_spec', 'T_text'), ProbsType(), optional=True),
            "mel_lens": NeuralType(('B'), LengthsType(), optional=True),
            "input_lens": NeuralType(('B'), LengthsType(), optional=True),
            "gst_ref_spec": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType(), optional=True),
            "gst_ref_spec_lens": NeuralType(('B'), LengthsType(), optional=True),
            "speaker_embedding": NeuralType(('B', 'D'), RegressionValuesType(), optional=True),
        }

    @property
    def output_types(self):
        return {
            "spect": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType()),
            "num_frames": NeuralType(('B'), TokenDurationType()),
            "durs_predicted": NeuralType(('B', 'T_text'), TokenDurationType()),
            "log_durs_predicted": NeuralType(('B', 'T_text'), TokenLogDurationType()),
            "pitch_predicted": NeuralType(('B', 'T_text'), RegressionValuesType()),
            "attn_soft": NeuralType(('B', 'S', 'T_spec', 'T_text'), ProbsType()),
            "attn_logprob": NeuralType(('B', 'S', 'T_spec', 'T_text'), LogprobsType()),
            "attn_hard": NeuralType(('B', 'S', 'T_spec', 'T_text'), ProbsType()),
            "attn_hard_dur": NeuralType(('B', 'T_text'), TokenDurationType()),
            "pitch": NeuralType(('B', 'T_audio'), RegressionValuesType()),
            "energy_pred": NeuralType(('B', 'T_text'), RegressionValuesType()),
            "energy_tgt": NeuralType(('B', 'T_audio'), RegressionValuesType()),
        }

    @typecheck()
    def forward(
        self,
        *,
        text,
        durs=None,
        pitch=None,
        energy=None,
        speaker=None,
        pace=1.0,
        spec=None,
        attn_prior=None,
        mel_lens=None,
        input_lens=None,
        gst_ref_spec=None,
        gst_ref_spec_lens=None,
        speaker_embedding=None,
    ):

        if not self.learn_alignment and self.training:
            assert durs is not None
            assert pitch is not None

        # Calculate speaker embedding
        if self.speaker_emb is None or speaker is None:
            spk_emb = None
        else:
            spk_emb = self.speaker_emb(speaker).unsqueeze(1)

        if self.speaker_encoder is not None:
            spk_emb = self.speaker_encoder(
                spk_emb=spk_emb,
                gst_ref_spec=gst_ref_spec,
                gst_ref_spec_lens=gst_ref_spec_lens,
                speaker_embedding=speaker_embedding,
            )

        # Input FFT
        enc_out, enc_mask = self.encoder(input=text, conditioning=spk_emb)

        # Predict duration
        if self.speaker_emb_condition_prosody:
            log_durs_predicted = self.duration_predictor(enc_out, enc_mask, conditioning=spk_emb)
        else:
            log_durs_predicted = self.duration_predictor(enc_out, enc_mask)

        durs_predicted = torch.clamp(torch.exp(log_durs_predicted) - 1, 0, self.max_token_duration)

        attn_soft, attn_hard, attn_hard_dur, attn_logprob = None, None, None, None
        if self.learn_alignment and spec is not None:
            text_emb = self.encoder.word_emb(text)
            if self.speaker_emb_condition_aligner and not isinstance(spk_emb, int):
                attn_soft, attn_logprob = self.aligner(
                    spec, text_emb.permute(0, 2, 1), enc_mask == 0, attn_prior, conditioning=spk_emb
                )
            else:
                attn_soft, attn_logprob = self.aligner(spec, text_emb.permute(0, 2, 1), enc_mask == 0, attn_prior)
            attn_hard = binarize_attention_parallel(attn_soft, input_lens, mel_lens)
            attn_hard_dur = attn_hard.sum(2)[:, 0, :]

        # Predict pitch
        if self.speaker_emb_condition_prosody:
            pitch_predicted = self.pitch_predictor(enc_out, enc_mask, conditioning=spk_emb)
        else:
            pitch_predicted = self.pitch_predictor(enc_out, enc_mask)

        if pitch is not None:
            if self.learn_alignment and pitch.shape[-1] != pitch_predicted.shape[-1]:
                # Pitch during training is per spectrogram frame, but during inference, it should be per character
                pitch = average_features(pitch.unsqueeze(1), attn_hard_dur).squeeze(1)
            elif not self.learn_alignment:
                # If alignment is not learnt attn_hard_dur is None, hence durs_predicted
                pitch = average_features(pitch.unsqueeze(1), durs_predicted).squeeze(1)
            pitch_emb = self.pitch_emb(pitch.unsqueeze(1))
        else:
            pitch_emb = self.pitch_emb(pitch_predicted.unsqueeze(1))

        enc_out = enc_out + pitch_emb.transpose(1, 2)

        # Predict energy
        if self.energy_predictor is not None:
            energy_pred = self.energy_predictor(prosody_input, enc_mask).squeeze(-1)

            if energy is not None:
                # Average energy over characters
                if self.learn_alignment:
                    energy_tgt = average_features(energy.unsqueeze(1), attn_hard_dur)
                else:
                    energy_tgt = average_features(energy.unsqueeze(1), durs_predicted)
                energy_tgt = torch.log(1.0 + energy_tgt)
                energy_emb = self.energy_emb(energy_tgt)
                energy_tgt = energy_tgt.squeeze(1)
            else:
                energy_emb = self.energy_emb(energy_pred.unsqueeze(1))
                energy_tgt = None

            enc_out = enc_out + energy_emb.transpose(1, 2)
        else:
            energy_pred = None
            energy_tgt = None

        if self.learn_alignment and spec is not None:
            len_regulated, dec_lens = regulate_len(attn_hard_dur, enc_out, pace)
        elif spec is None and durs is not None:
            len_regulated, dec_lens = regulate_len(durs, enc_out, pace)
        # Use predictions during inference
        elif spec is None:
            len_regulated, dec_lens = regulate_len(durs_predicted, enc_out, pace)
        else:
            raise ValueError(
                f"Something unexpected happened when 'spec' is not None and 'self.learn_alignment' is False."
            )

        # Output FFT
        if self.speaker_emb_condition_decoder:
            dec_out, _ = self.decoder(input=len_regulated, seq_lens=dec_lens, conditioning=spk_emb)
        else:
            dec_out, _ = self.decoder(input=len_regulated, seq_lens=dec_lens)
        spect = self.proj(dec_out).transpose(1, 2)
        return (
            spect,
            dec_lens,
            durs_predicted,
            log_durs_predicted,
            pitch_predicted,
            attn_soft,
            attn_logprob,
            attn_hard,
            attn_hard_dur,
            pitch,
            energy_pred,
            energy_tgt,
        )

    def infer(self, *, text, pitch=None, speaker=None, energy=None, pace=1.0, volume=None):
        # Calculate speaker embedding
        if self.speaker_emb is None or speaker is None:
            spk_emb = 0
        else:
            spk_emb = self.speaker_emb(speaker).unsqueeze(1)

        # Input FFT
        enc_out, enc_mask = self.encoder(input=text, conditioning=spk_emb)

        # Predict duration and pitch
        if self.speaker_emb_condition_prosody:
            log_durs_predicted = self.duration_predictor(enc_out, enc_mask, conditioning=spk_emb)
        else:
            log_durs_predicted = self.duration_predictor(enc_out, enc_mask)

        durs_predicted = torch.clamp(
            torch.exp(log_durs_predicted) - 1.0, self.min_token_duration, self.max_token_duration
        )
        if self.speaker_emb_condition_prosody:
            pitch_predicted = self.pitch_predictor(enc_out, enc_mask, conditioning=spk_emb) + pitch
        else:
            pitch_predicted = self.pitch_predictor(enc_out, enc_mask) + pitch

        pitch_emb = self.pitch_emb(pitch_predicted.unsqueeze(1))
        enc_out = enc_out + pitch_emb.transpose(1, 2)

        if self.energy_predictor is not None:
            if energy is not None:
                assert energy.shape[-1] == text.shape[-1], f"energy.shape[-1]: {energy.shape[-1]} != len(text)"
                energy_emb = self.energy_emb(energy)
            else:
                energy_pred = self.energy_predictor(prosody_input, enc_mask).squeeze(-1)
                energy_emb = self.energy_emb(energy_pred.unsqueeze(1))
            enc_out = enc_out + energy_emb.transpose(1, 2)

        # Expand to decoder time dimension
        len_regulated, dec_lens = regulate_len(durs_predicted, enc_out, pace)
        volume_extended = None
        if volume is not None:
            volume_extended, _ = regulate_len(durs_predicted, volume.unsqueeze(-1), pace)
            volume_extended = volume_extended.squeeze(-1).float()

        # Output FFT
        if self.speaker_emb_condition_decoder:
            dec_out, _ = self.decoder(input=len_regulated, seq_lens=dec_lens, conditioning=spk_emb)
        else:
            dec_out, _ = self.decoder(input=len_regulated, seq_lens=dec_lens)
        spect = self.proj(dec_out).transpose(1, 2)
        return (
            spect.to(torch.float),
            dec_lens,
            durs_predicted,
            log_durs_predicted,
            pitch_predicted,
            volume_extended,
        )


class FastPitchSSLModule(NeuralModule):
    def __init__(
        self,
        encoder_module: NeuralModule,
        decoder_module: NeuralModule,
        duration_predictor: NeuralModule,
        pitch_predictor: NeuralModule,
        symbols_embedding_dim: int,
        pitch_embedding_kernel_size: int,
        n_mel_channels: int = 80,
        max_token_duration: int = 75,
    ):
        super().__init__()

        self.encoder = encoder_module
        self.decoder = decoder_module
        self.duration_predictor = duration_predictor
        self.pitch_predictor = pitch_predictor

        self.max_token_duration = max_token_duration
        self.min_token_duration = 0

        if self.pitch_predictor is not None:
            self.pitch_emb = torch.nn.Conv1d(
                1,
                symbols_embedding_dim,
                kernel_size=pitch_embedding_kernel_size,
                padding=int((pitch_embedding_kernel_size - 1) / 2),
            )

        # Store values precomputed from training data for convenience
        self.register_buffer('pitch_mean', torch.zeros(1))
        self.register_buffer('pitch_std', torch.zeros(1))

        self.proj = torch.nn.Linear(self.decoder.d_model, n_mel_channels, bias=True)

    @property
    def input_types(self):
        return {
            "enc_out": NeuralType(('B', 'T', 'D'), EncodedRepresentation()),
            "enc_mask": NeuralType(('B', 'T', 'D'), EncodedRepresentation()),
            "durs": NeuralType(('B', 'T_text'), TokenDurationType(), optional=True),
            "pitch": NeuralType(('B', 'T_audio'), RegressionValuesType(), optional=True),
            "pace": NeuralType(optional=True),
        }

    @property
    def output_types(self):
        return {
            "spect": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType()),
            "num_frames": NeuralType(('B'), TokenDurationType()),
            "durs_predicted": NeuralType(('B', 'T_text'), TokenDurationType()),
            "log_durs_predicted": NeuralType(('B', 'T_text'), TokenLogDurationType()),
            "pitch_predicted": NeuralType(('B', 'T_text'), RegressionValuesType()),
            "pitch": NeuralType(('B', 'T_audio'), RegressionValuesType()),
        }

    @typecheck()
    def forward(self, *, enc_out=None, enc_mask=None, durs=None, pitch=None, pace=1.0):

        log_durs_predicted, durs_predicted = None, None
        if self.duration_predictor is not None:
            log_durs_predicted = self.duration_predictor(enc_out, enc_mask)
            durs_predicted = torch.clamp(torch.exp(log_durs_predicted) - 1, 0, self.max_token_duration)

        # Predict pitch
        pitch_predicted = None
        if self.pitch_predictor is not None:
            pitch_predicted = self.pitch_predictor(enc_out, enc_mask)
            if pitch is not None:
                if pitch.shape[-1] != enc_out.shape[1]:
                    # during inference, we send the averaged pitch over each token so we don't need to average here
                    # TODO: have a flag to indicate whether the pitch is already averaged or not
                    pitch = average_features(pitch.unsqueeze(1), durs).squeeze(1)

                pitch_emb = self.pitch_emb(pitch.unsqueeze(1))
            else:
                pitch_emb = self.pitch_emb(pitch_predicted.unsqueeze(1))

            enc_out = enc_out + pitch_emb.transpose(1, 2)

        if durs is not None:
            len_regulated, dec_lens = regulate_len(durs, enc_out, pace)
        else:
            # Use predictions during inference
            assert self.duration_predictor is not None, "Duration predictor cannot be none if durs is not provided"
            len_regulated, dec_lens = regulate_len(durs_predicted, enc_out, pace)

        # Output FFT
        dec_out, _ = self.decoder(input=len_regulated, seq_lens=dec_lens)
        spect = self.proj(dec_out).transpose(1, 2)
        return (
            spect,
            dec_lens,
            durs_predicted,
            log_durs_predicted,
            pitch_predicted,
            pitch,
        )
