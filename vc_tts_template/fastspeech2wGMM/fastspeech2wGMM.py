from typing import Dict, Optional

import torch.nn as nn

from vc_tts_template.fastspeech2.encoder_decoder import Encoder, Decoder
from vc_tts_template.fastspeech2.layers import PostNet
from vc_tts_template.fastspeech2.varianceadaptor import VarianceAdaptor
from vc_tts_template.fastspeech2wGMM.prosody_model import ProsodyExtractor, ProsodyPredictor
from vc_tts_template.utils import make_pad_mask


class FastSpeech2wGMM(nn.Module):
    """ FastSpeech2wGMM """

    def __init__(
        self,
        max_seq_len: int,
        num_vocab: int,  # pad=0
        # encoder
        encoder_hidden_dim: int,
        encoder_num_layer: int,
        encoder_num_head: int,
        conv_filter_size: int,
        conv_kernel_size_1: int,
        conv_kernel_size_2: int,
        encoder_dropout: float,
        # prosody extractor
        prosody_emb_dim: int,
        extra_conv_kernel_size: int,
        extra_conv_n_layers: int,
        extra_gru_n_layers: int,
        extra_global_gru_n_layers: int,
        # prosody predictor
        gru_hidden_dim: int,
        gru_n_layers: int,
        pp_conv_out_channels: int,
        pp_conv_kernel_size: int,
        pp_conv_n_layers: int,
        pp_conv_dropout: float,
        pp_zoneout: float,
        num_gaussians: int,
        global_gru_n_layers: int,
        global_d_gru: int,
        global_num_gaussians: int,
        # variance predictor
        variance_predictor_filter_size: int,
        variance_predictor_kernel_size: int,
        variance_predictor_dropout: int,
        pitch_feature_level: int,  # 0 is frame 1 is phoneme
        energy_feature_level: int,  # 0 is frame 1 is phoneme
        pitch_quantization: str,
        energy_quantization: str,
        n_bins: int,
        # decoder
        decoder_hidden_dim: int,
        decoder_num_layer: int,
        decoder_num_head: int,
        decoder_dropout: float,
        n_mel_channel: int,
        # other
        encoder_fix: bool,
        global_prosody: bool,
        stats: Dict,
        speakers: Optional[Dict] = None,
        emotions: Optional[Dict] = None,
        accent_info: int = 0,
    ):
        super(FastSpeech2wGMM, self).__init__()
        self.encoder = Encoder(
            max_seq_len,
            num_vocab,
            encoder_hidden_dim,
            encoder_num_layer,
            encoder_num_head,
            conv_filter_size,
            [conv_kernel_size_1, conv_kernel_size_2],
            encoder_dropout,
            accent_info,
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
        self.prosody_extractor = ProsodyExtractor(
            n_mel_channel,
            prosody_emb_dim,
            conv_kernel_size=extra_conv_kernel_size,
            conv_n_layers=extra_conv_n_layers,
            gru_n_layers=extra_gru_n_layers,
            global_prosody=global_prosody,
            global_gru_n_layers=extra_global_gru_n_layers,
        )
        self.prosody_predictor = ProsodyPredictor(
            encoder_hidden_dim,
            gru_hidden_dim,
            prosody_emb_dim,
            pp_conv_out_channels,
            conv_kernel_size=pp_conv_kernel_size,
            conv_n_layers=pp_conv_n_layers,
            conv_dropout=pp_conv_dropout,
            gru_layers=gru_n_layers,
            zoneout=pp_zoneout,
            num_gaussians=num_gaussians,
            global_prosody=global_prosody,
            global_gru_layers=global_gru_n_layers,
            global_d_gru=global_d_gru,
            global_num_gaussians=global_num_gaussians,
        )
        self.decoder = Decoder(
            max_seq_len,
            decoder_hidden_dim,
            decoder_num_layer,
            decoder_num_head,
            conv_filter_size,
            [conv_kernel_size_1, conv_kernel_size_2],
            decoder_dropout
        )
        self.prosody_linear = nn.Linear(
            prosody_emb_dim,
            encoder_hidden_dim,
        )
        self.decoder_linear = nn.Linear(
            encoder_hidden_dim,
            decoder_hidden_dim,
        )
        self.mel_linear = nn.Linear(
            decoder_hidden_dim,
            n_mel_channel,
        )
        self.postnet = PostNet(
            n_mel_channel
        )

        self.speaker_emb = None
        if speakers is not None:
            n_speaker = len(speakers)
            self.speaker_emb = nn.Embedding(
                n_speaker,
                encoder_hidden_dim,
            )
        self.emotion_emb = None
        if emotions is not None:
            n_emotion = len(emotions)
            self.emotion_emb = nn.Embedding(
                n_emotion,
                encoder_hidden_dim,
            )

        self.encoder_fix = encoder_fix
        self.global_prosody = global_prosody
        self.speakers = speakers
        self.emotions = emotions
        self.accent_info = accent_info

    def forward(
        self,
        ids,
        speakers,
        emotions,
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
        divide_value = 2 if self.accent_info > 0 else 1
        src_masks = make_pad_mask((src_lens / divide_value).long(), max_src_len // divide_value)
        # PAD??????, ??????????????????????????????????????????True??????????????????mask?????????
        # ?????????, attention???, -inf???fill?????????????????????????????????.
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
        if self.emotion_emb is not None:
            output = output + self.emotion_emb(emotions).unsqueeze(1).expand(
                -1, max_src_len, -1
            )
        is_inference = True if p_targets is None else False

        if self.global_prosody is False:
            prosody_target = self.prosody_extractor(mels, d_targets)
            prosody_prediction, pi_outs, sigma_outs, mu_outs = self.prosody_predictor(
                output, target_prosody=prosody_target, is_inference=is_inference
            )
        else:
            prosody_target, g_prosody_target = self.prosody_extractor(mels, d_targets)
            prosody_prediction, pi_outs, sigma_outs, mu_outs, g_pi, g_sigma, g_mu = self.prosody_predictor(
                output, target_prosody=prosody_target, target_global_prosody=g_prosody_target,
                is_inference=is_inference
            )
        if is_inference is True:
            output = output + self.prosody_linear(prosody_prediction)
        else:
            output = output + self.prosody_linear(prosody_target)
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

        output, mel_masks = self.decoder(self.decoder_linear(output), mel_masks)
        output = self.mel_linear(output)

        postnet_output = self.postnet(output) + output

        if self.global_prosody is True:
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
                prosody_target,
                pi_outs,
                mu_outs,
                sigma_outs,
                g_prosody_target,
                g_pi,
                g_mu,
                g_sigma,
            )
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
            prosody_target,
            pi_outs,
            mu_outs,
            sigma_outs,
        )
