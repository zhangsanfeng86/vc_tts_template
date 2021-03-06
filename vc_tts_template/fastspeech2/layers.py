import torch
import torch.nn as nn
from torch.nn import functional as F

from vc_tts_template.fastspeech2.sublayers import MultiHeadAttention, PositionwiseFeedForward


class FFTBlock(torch.nn.Module):
    """FFT Block
    構造はまさに[Transformer](https://qiita.com/norihitoishida/items/2fead107792b504eaccf#attention%E3%81%AE%E5%88%86%E9%A1%9E-3-multiplicity-%E5%A4%9A%E9%87%8D%E5%BA%A6)
    になっている!! いいね.
    """

    def __init__(self, d_model, n_head, d_k, d_v, d_inner, kernel_size, dropout=0.1):
        super(FFTBlock, self).__init__()
        # わかりやすい構造. 2つから構成されている.
        self.slf_attn = MultiHeadAttention(n_head, d_model, d_k, d_v, dropout=dropout)
        self.pos_ffn = PositionwiseFeedForward(
            d_model, d_inner, kernel_size, dropout=dropout
        )

    def forward(self, enc_input, mask=None, slf_attn_mask=None):
        # maskを使うのはattentionのほう.
        enc_output, enc_slf_attn = self.slf_attn(
            enc_input, enc_input, enc_input, mask=slf_attn_mask
        )
        enc_output = enc_output.masked_fill(mask.unsqueeze(-1), 0)

        enc_output = self.pos_ffn(enc_output)
        enc_output = enc_output.masked_fill(mask.unsqueeze(-1), 0)

        return enc_output, enc_slf_attn


class ConvNorm(torch.nn.Module):
    # PostNetで利用.
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=1,
        stride=1,
        padding=None,
        dilation=1,
        bias=True,
        w_init_gain="linear",
    ):
        super(ConvNorm, self).__init__()

        if padding is None:
            assert kernel_size % 2 == 1
            padding = int(dilation * (kernel_size - 1) / 2)

        self.conv = torch.nn.Conv1d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=bias,
        )

    def forward(self, signal):
        conv_signal = self.conv(signal)

        return conv_signal


class PostNet(nn.Module):
    """
    PostNet: Five 1-d convolution with 512 channels and kernel size 5

    いたってシンプルなconv層. 分かれてるのは, 入出力のchannelを合わせているだけ.
    """

    def __init__(
        self,
        n_mel_channels=80,
        postnet_embedding_dim=512,
        postnet_kernel_size=5,
        postnet_n_convolutions=5,
    ):

        super(PostNet, self).__init__()
        self.convolutions = nn.ModuleList()

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(
                    n_mel_channels,
                    postnet_embedding_dim,
                    kernel_size=postnet_kernel_size,
                    stride=1,
                    padding=int((postnet_kernel_size - 1) / 2),
                    dilation=1,
                    w_init_gain="tanh",
                ),
                nn.BatchNorm1d(postnet_embedding_dim),
            )
        )

        for i in range(1, postnet_n_convolutions - 1):
            self.convolutions.append(
                nn.Sequential(
                    ConvNorm(
                        postnet_embedding_dim,
                        postnet_embedding_dim,
                        kernel_size=postnet_kernel_size,
                        stride=1,
                        padding=int((postnet_kernel_size - 1) / 2),
                        dilation=1,
                        w_init_gain="tanh",
                    ),
                    nn.BatchNorm1d(postnet_embedding_dim),
                )
            )

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(
                    postnet_embedding_dim,
                    n_mel_channels,
                    kernel_size=postnet_kernel_size,
                    stride=1,
                    padding=int((postnet_kernel_size - 1) / 2),
                    dilation=1,
                    w_init_gain="linear",
                ),
                nn.BatchNorm1d(n_mel_channels),
            )
        )

    def forward(self, x):
        x = x.contiguous().transpose(1, 2)

        for i in range(len(self.convolutions) - 1):
            x = F.dropout(torch.tanh(self.convolutions[i](x)), 0.5, self.training)
        x = F.dropout(self.convolutions[-1](x), 0.5, self.training)

        x = x.contiguous().transpose(1, 2)
        return x


class WordEncoder(nn.Module):
    def __init__(
        self,
        n_src_vocab,
        d_word_vec,
        padding_idx,
        accent_info,
    ) -> None:
        super().__init__()

        self.src_word_emb = nn.Embedding(
            n_src_vocab, d_word_vec, padding_idx=padding_idx
        )
        if accent_info > 0:
            self.src_accent_emb = nn.Embedding(
                n_src_vocab, d_word_vec, padding_idx=padding_idx
            )

        self.accent_info = accent_info

    def forward(self, src_seq):
        if self.accent_info < 1:
            return self.src_word_emb(src_seq)
        text_seq, accent_seq = self.src2txtaccent(src_seq)
        text_emb = self.src_word_emb(text_seq)
        accent_emb = self.src_accent_emb(accent_seq)

        return text_emb + accent_emb

    def src2txtaccent(self, x):
        # expected: (B, len*2), len=the length of text
        text, accent = torch.tensor_split(x.view(x.size(0), -1, 2), 2, dim=-1)
        return text.squeeze(-1), accent.squeeze(-1)
