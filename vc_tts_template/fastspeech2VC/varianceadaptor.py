import sys

import torch
import torch.nn as nn

sys.path.append('.')
from vc_tts_template.utils import make_pad_mask, pad
from vc_tts_template.tacotron.decoder import ZoneOutCell

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class VarianceAdaptor(nn.Module):
    def __init__(
        self,
        encoder_hidden_dim: int,
        variance_predictor_filter_size: int,
        variance_predictor_kernel_size_d: int,
        variance_predictor_layer_num_d: int,
        variance_predictor_kernel_size_p: int,
        variance_predictor_layer_num_p: int,
        variance_predictor_kernel_size_e: int,
        variance_predictor_layer_num_e: int,
        variance_predictor_dropout: int,
        stop_gradient_flow_d: bool,
        stop_gradient_flow_p: bool,
        stop_gradient_flow_e: bool,
        reduction_factor: int,
        pitch_AR: bool = False,
        lstm_layers: int = 2,
    ):
        super(VarianceAdaptor, self).__init__()
        self.reduction_factor = reduction_factor
        self.pitch_AR = pitch_AR

        # duration, pitch, energyで共通なのね.
        self.duration_predictor = VariancePredictor(
            encoder_hidden_dim,
            variance_predictor_filter_size,
            variance_predictor_kernel_size_d,
            variance_predictor_layer_num_d,
            variance_predictor_dropout,
        )
        if pitch_AR is False:
            self.pitch_predictor = VariancePredictor(
                encoder_hidden_dim,
                variance_predictor_filter_size,
                variance_predictor_kernel_size_p,
                variance_predictor_layer_num_p,
                variance_predictor_dropout,
                reduction_factor
            )
        else:
            self.pitch_predictor = VarianceARPredictor(  # type: ignore
                encoder_hidden_dim,
                variance_predictor_filter_size,
                variance_predictor_kernel_size_p,
                variance_predictor_layer_num_p,
                variance_predictor_dropout,
                reduction_factor,
                lstm_layers
            )
        self.energy_predictor = VariancePredictor(
            encoder_hidden_dim,
            variance_predictor_filter_size,
            variance_predictor_kernel_size_e,
            variance_predictor_layer_num_e,
            variance_predictor_dropout,
            reduction_factor
        )
        self.length_regulator = LengthRegulator()
        self.pitch_conv1d_1 = Conv_emb(self.reduction_factor, encoder_hidden_dim)
        self.pitch_conv1d_2 = Conv_emb(self.reduction_factor, encoder_hidden_dim)
        self.energy_conv1d_1 = Conv_emb(self.reduction_factor, encoder_hidden_dim)
        self.energy_conv1d_2 = Conv_emb(self.reduction_factor, encoder_hidden_dim)

        self.pitch_stop_gradient_flow = stop_gradient_flow_p
        self.energy_stop_gradient_flow = stop_gradient_flow_e
        self.duration_stop_gradient_flow = stop_gradient_flow_d

    def forward(
        self,
        x,
        src_mask,
        src_max_len,
        src_pitch,
        src_energy,
        src_duration=None,
        mel_mask=None,
        max_len=None,
        pitch_target=None,
        energy_target=None,
        p_control=1.0,
        e_control=1.0,
        d_control=1.0,
    ):
        # まずは, durationを計算する.
        if self.duration_stop_gradient_flow is True:
            log_duration_prediction = self.duration_predictor(x.detach(), src_mask)
        else:
            log_duration_prediction = self.duration_predictor(x, src_mask)

        # convして, 次元を合わせる
        if self.reduction_factor > 1:
            src_pitch = self.reshape_with_reduction_factor(src_pitch, src_max_len)
            src_energy = self.reshape_with_reduction_factor(src_energy, src_max_len)

        pitch_conv = self.pitch_conv1d_1(src_pitch)
        energy_conv = self.energy_conv1d_1(src_energy)

        if src_duration is not None:
            duration_rounded = src_duration
        else:
            duration_rounded = torch.clamp(  # 最小値を0にする. マイナスは許さない.
                (torch.round(torch.exp(log_duration_prediction) - 1) * d_control),
                min=0,
            )

        x, mel_len = self.length_regulator(x, duration_rounded, max_len)
        pitch, _ = self.length_regulator(pitch_conv, duration_rounded, max_len)
        energy, _ = self.length_regulator(energy_conv, duration_rounded, max_len)

        if mel_mask is None:
            mel_mask = make_pad_mask(mel_len)
            max_len = max(mel_len.cpu().numpy())

        pitch = pitch + x.detach() if self.pitch_stop_gradient_flow is True else pitch + x
        energy = energy + x.detach() if self.energy_stop_gradient_flow is True else energy + x

        # pitch, energy: (B, T//r, d_enc)
        if self.pitch_AR is False:
            pitch_prediction = self.pitch_predictor(pitch, mel_mask) * p_control
        else:
            pitch_prediction = self.pitch_predictor(pitch, mel_mask, pitch_target) * p_control

        energy_prediction = self.energy_predictor(energy, mel_mask) * e_control
        # pitchを, また次元増やしてhiddenに足す.
        if pitch_target is not None:
            pitch = self.reshape_with_reduction_factor(pitch_target, max_len)
            energy = self.reshape_with_reduction_factor(energy_target, max_len)
        else:
            pitch = self.reshape_with_reduction_factor(pitch_prediction, max_len)
            energy = self.reshape_with_reduction_factor(energy_prediction, max_len)

        pitch = self.pitch_conv1d_2(pitch)
        energy = self.energy_conv1d_2(energy)

        x = x + pitch + energy
        return (
            x,
            pitch_prediction,
            energy_prediction,
            log_duration_prediction,
            duration_rounded,
            mel_len,
            mel_mask,
        )

    def reshape_with_reduction_factor(self, x, max_len):
        assert len(x.size()) == 2
        x = x[:, :max_len*self.reduction_factor]
        x = x.unsqueeze(-1).contiguous().view(x.size(0), -1, self.reduction_factor)
        return x


class LengthRegulator(nn.Module):
    def __init__(self):
        super(LengthRegulator, self).__init__()

    def LR(self, x, duration, max_len):
        output = list()
        mel_len = list()
        for batch, expand_target in zip(x, duration):
            expanded = self.expand(batch, expand_target)
            output.append(expanded)
            # expandedのlenがまさに出力したいmelの幅になる.
            mel_len.append(expanded.shape[0])

        # ここでは, まだoutputは長さバラバラのlistであることに注意.
        # 長さを揃えなきゃ.
        if max_len is not None:
            # max_lenがあるなら, それでpad.
            output = pad(output, max_len)
        else:
            # targetがないならmax_lenもないですね.
            # その場合は自動で一番長い部分を探してくれる.
            output = pad(output)

        return output, torch.LongTensor(mel_len).to(device)

    def expand(self, batch, predicted):
        out = list()

        for i, vec in enumerate(batch):
            expand_size = predicted[i].item()
            out.append(vec.expand(max(int(expand_size), 0), -1))
        out = torch.cat(out, 0)  # listをtorchの2次元へ, かな.

        return out

    def forward(self, x, duration, max_len=None):
        # durationがpredictか, targetのdurationです.
        output, mel_len = self.LR(x, duration, max_len)
        return output, mel_len


class VariancePredictor(nn.Module):
    def __init__(
        self,
        encoder_hidden_dim: int,
        variance_predictor_filter_size: int,
        variance_predictor_kernel_size: int,
        variance_predictor_layer_num: int,
        variance_predictor_dropout: int,
        reduction_factor: int = 1,
    ):
        super(VariancePredictor, self).__init__()

        self.input_size = encoder_hidden_dim
        self.filter_size = variance_predictor_filter_size
        self.kernel = variance_predictor_kernel_size
        self.layer_num = variance_predictor_layer_num
        self.conv_output_size = variance_predictor_filter_size
        self.dropout = variance_predictor_dropout
        self.reduction_factor = reduction_factor

        conv_layers = []

        for i in range(self.layer_num):
            if i == 0:
                conv = Conv(
                    self.input_size,
                    self.filter_size,
                    kernel_size=self.kernel,
                    padding=(self.kernel - 1) // 2,  # same sizeに.
                )
            else:
                conv = Conv(
                    self.filter_size,
                    self.filter_size,
                    kernel_size=self.kernel,
                    padding=(self.kernel - 1) // 2,  # same sizeに.
                )
            conv_layers += [conv, nn.ReLU(), nn.LayerNorm(self.filter_size),
                            nn.Dropout(self.dropout)]

        self.conv_layer = nn.Sequential(*conv_layers)

        self.linear_layer = nn.Linear(self.conv_output_size, self.reduction_factor)

    def forward(self, encoder_output, mask):
        # encoder_output: (B, T//r, d_enc)
        out = self.conv_layer(encoder_output)
        out = self.linear_layer(out)
        # out: (B, T//r, r)

        if mask is not None:
            if self.reduction_factor > 1:
                out = out.masked_fill(mask.unsqueeze(-1).expand(mask.size(0), mask.size(1), self.reduction_factor), 0.0)
                out = out.contiguous().view(out.size(0), -1, 1)
                out = out.squeeze(-1)

            else:
                out = out.squeeze(-1)
                out = out.masked_fill(mask, 0.0)

        # out: (B, T)
        return out


class VarianceARPredictor(nn.Module):
    def __init__(
        self,
        encoder_hidden_dim: int,
        variance_predictor_filter_size: int,
        variance_predictor_kernel_size: int,
        variance_predictor_layer_num: int,
        variance_predictor_dropout: int,
        reduction_factor: int = 1,
        lstm_layers: int = 2,
        zoneout: float = 0.1
    ):
        super(VarianceARPredictor, self).__init__()

        self.input_size = encoder_hidden_dim
        self.filter_size = variance_predictor_filter_size
        self.kernel = variance_predictor_kernel_size
        self.layer_num = variance_predictor_layer_num
        self.dropout = variance_predictor_dropout
        self.reduction_factor = reduction_factor

        conv_layers = []

        for i in range(self.layer_num):
            if i == 0:
                conv = Conv(
                    self.input_size,
                    self.filter_size,
                    kernel_size=self.kernel,
                    padding=(self.kernel - 1) // 2,  # same sizeに.
                )
            else:
                conv = Conv(
                    self.filter_size,
                    self.filter_size,
                    kernel_size=self.kernel,
                    padding=(self.kernel - 1) // 2,  # same sizeに.
                )
            conv_layers += [conv, nn.ReLU(), nn.LayerNorm(self.filter_size),
                            nn.Dropout(self.dropout)]

        self.conv_layer = nn.Sequential(*conv_layers)

        # 片方向 LSTM
        self.lstm = nn.ModuleList()
        for layer in range(lstm_layers):
            lstm = nn.LSTMCell(
                self.filter_size*2 if layer == 0 else self.filter_size,
                self.filter_size,
            )
            self.lstm += [ZoneOutCell(lstm, zoneout)]

        self.prenet = nn.Linear(1, self.filter_size)

        self.linear_layer = nn.Linear(self.filter_size*2, self.reduction_factor)

    def forward(self, encoder_output, mask, target=None):
        # encoder_output: (B, T//r, d_enc)
        # target: (B, T)

        if (self.reduction_factor > 1) and (target is not None):
            target = target[
                :, self.reduction_factor - 1:: self.reduction_factor
            ].unsqueeze(-1)
        # target: (B, T//r)

        encoder_output = self.conv_layer(encoder_output)

        # LSTM の状態をゼロで初期化
        h_list, c_list = [], []
        for _ in range(len(self.lstm)):
            h_list.append(self._zero_state(encoder_output))
            c_list.append(self._zero_state(encoder_output))

        # デコーダの最初の入力
        go_frame = encoder_output.new_zeros(encoder_output.size(0), 1)
        prev_out = go_frame
        outs = []

        # main loop
        for t in range(encoder_output.size()[1]):
            # Pre-Net
            prenet_out = self.prenet(prev_out)

            # LSTM
            xs = torch.cat([encoder_output[:, t, :], prenet_out], dim=1)
            h_list[0], c_list[0] = self.lstm[0](xs, (h_list[0], c_list[0]))
            for i in range(1, len(self.lstm)):
                h_list[i], c_list[i] = self.lstm[i](
                    h_list[i - 1], (h_list[i], c_list[i])
                )
            hcs = torch.cat([h_list[-1], encoder_output[:, t, :]], dim=1)
            outs.append(self.linear_layer(hcs).unsqueeze(1))

            # 次の時刻のデコーダの入力を更新
            if target is None:
                prev_out = outs[-1][:, :, -1]  # (B, 1)
            else:
                # Teacher forcing
                prev_out = target[:, t]

        outs = torch.cat(outs, dim=1)
        # outs: (B, T//r, r)

        if mask is not None:
            if self.reduction_factor > 1:
                outs = outs.masked_fill(
                    mask.unsqueeze(-1).expand(mask.size(0), mask.size(1), self.reduction_factor), 0.0
                )
                outs = outs.contiguous().view(outs.size(0), -1, 1)
                outs = outs.squeeze(-1)

            else:
                outs = outs.squeeze(-1)
                outs = outs.masked_fill(mask, 0.0)

        # outs: (B, T)
        return outs

    def _zero_state(self, hs):
        init_hs = hs.new_zeros(hs.size(0), self.lstm[0].hidden_size)
        return init_hs


class Conv(nn.Module):
    """
    Convolution Module
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=1,
        stride=1,
        padding=0,
        dilation=1,
        bias=True,
        w_init="linear",
    ):
        super(Conv, self).__init__()

        self.conv = nn.Conv1d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=bias,
        )

    def forward(self, x):
        x = x.contiguous().transpose(1, 2)
        x = self.conv(x)
        x = x.contiguous().transpose(1, 2)

        return x


class Conv_emb(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=1,
        stride=1,
        padding=0,
        dilation=1,
        bias=True,
        dropout=0.2
    ):
        super().__init__()
        self.conv = Conv(
            in_channels,
            out_channels,
            kernel_size,
            stride,
            padding,
            dilation,
            bias
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        x = self.conv(x)
        x = self.dropout(x)
        return x
