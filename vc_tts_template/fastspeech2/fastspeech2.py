from typing import Dict

import torch.nn as nn

from vc_tts_template.fastspeech2.encoder_decoder import Encoder, Decoder
from vc_tts_template.fastspeech2.layers import PostNet
from vc_tts_template.fastspeech2.varianceadaptor import VarianceAdaptor
from vc_tts_template.utils import make_pad_mask


class FastSpeech2(nn.Module):
    """ FastSpeech2 """

    def __init__(
        self,
        max_seq_len: int,
        num_vocab: int,  # pad=0
        encoder_hidden_dim: int,
        encoder_num_layer: int,
        encoder_num_head: int,
        conv_filter_size: int,
        conv_kernel_size: int,
        encoder_dropout: float,
        variance_predictor_filter_size: int,
        variance_predictor_kernel_size: int,
        variance_predictor_dropout: int,
        pitch_feature_level: int,  # 0 is frame 1 is phoneme
        energy_feature_level: int,  # 0 is frame 1 is phoneme
        pitch_quantization: str,
        energy_quantization: str,
        n_bins: int,
        decoder_hidden_dim: int,
        decoder_num_layer: int,
        decoder_num_head: int,
        decoder_dropout: float,
        n_mel_channel: int,
        multi_speaker: int,  # 0 is false 1 is true
        encoder_fix: bool,
        stats: Dict,
        speakers: Dict,
    ):
        super(FastSpeech2, self).__init__()
        self.encoder = Encoder(
            max_seq_len,
            num_vocab,
            encoder_hidden_dim,
            encoder_num_layer,
            encoder_num_head,
            conv_filter_size,
            conv_kernel_size,
            encoder_dropout
        )
        self.variance_adaptor = VarianceAdaptor(
            encoder_hidden_dim,
            variance_predictor_filter_size,
            variance_predictor_kernel_size,
            variance_predictor_dropout,
            pitch_feature_level,
            energy_feature_level,
            pitch_quantization,
            energy_quantization,
            n_bins,
            stats  # type: ignore
        )
        self.decoder = Decoder(
            max_seq_len,
            decoder_hidden_dim,
            decoder_num_layer,
            decoder_num_head,
            conv_filter_size,
            conv_kernel_size,
            decoder_dropout
        )
        self.mel_linear = nn.Linear(
            decoder_hidden_dim,
            n_mel_channel,
        )
        self.postnet = PostNet(
            n_mel_channel
        )

        self.speaker_emb = None
        if multi_speaker > 0:
            n_speaker = len(speakers)
            self.speaker_emb = nn.Embedding(
                n_speaker,
                encoder_hidden_dim,
            )

        self.encoder_fix = encoder_fix

    def forward(
        self,
        ids,
        speakers,
        texts,
        src_lens,
        max_src_len,
        mels=None,
        mel_lens=None,
        max_mel_len=None,
        p_targets=None,
        e_targets=None,
        d_targets=None,
        p_control=1.0,
        e_control=1.0,
        d_control=1.0,
    ):
        src_masks = make_pad_mask(src_lens, max_src_len)
        # PAD前の, 元データが入っていない部分がTrueになっているmaskの取得
        # これは, attentionで, -infをfillするために使いたいので.
        mel_masks = (
            make_pad_mask(mel_lens, max_mel_len)
            if mel_lens is not None
            else None
        )

        output = self.encoder(texts, src_masks)

        if self.encoder_fix is True:
            output = output.detach()

        if self.speaker_emb is not None:
            output = output + self.speaker_emb(speakers).unsqueeze(1).expand(
                -1, max_src_len, -1
            )

        (
            output,
            p_predictions,
            e_predictions,
            log_d_predictions,
            d_rounded,
            mel_lens,
            mel_masks,
        ) = self.variance_adaptor(
            output,
            src_masks,
            mel_masks,
            max_mel_len,
            p_targets,
            e_targets,
            d_targets,
            p_control,
            e_control,
            d_control,
        )

        output, mel_masks = self.decoder(output, mel_masks)
        output = self.mel_linear(output)

        postnet_output = self.postnet(output) + output

        return (
            output,
            postnet_output,
            p_predictions,
            e_predictions,
            log_d_predictions,
            d_rounded,
            src_masks,
            mel_masks,
            src_lens,
            mel_lens,
        )