import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import List

import librosa
import numpy as np
import pyworld as pw
from fastdtw import fastdtw
from pydub import AudioSegment, silence

sys.path.append("../..")
from recipes.fastspeech2VC.utils import pydub_to_np
from scipy.interpolate import interp1d
from scipy.spatial.distance import cityblock
from tqdm import tqdm
from vc_tts_template.dsp import logmelspectrogram


def get_parser():
    parser = argparse.ArgumentParser(
        description="Preprocess for Fastspeech2VC",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("utt_list", type=str, help="utternace list")
    parser.add_argument("src_wav_root", type=str, help="source wav root")
    parser.add_argument("tgt_wav_root", type=str, help="target wav root")
    parser.add_argument("out_dir", type=str, help="out directory")
    parser.add_argument("--n_jobs", type=int, default=1, help="Number of jobs")
    parser.add_argument("--sample_rate", type=int)
    parser.add_argument("--silence_thresh_h", type=int, help="silence thresh of head")
    parser.add_argument("--silence_thresh_t", type=int, help="silence thresh of tail")
    parser.add_argument("--chunk_size", type=int, help="silence chunk size")
    parser.add_argument("--filter_length", type=int)
    parser.add_argument("--hop_length", type=int)
    parser.add_argument("--win_length", type=int)
    parser.add_argument("--n_mel_channels", type=int)
    parser.add_argument("--mel_fmin", type=int)
    parser.add_argument("--mel_fmax", type=int)
    parser.add_argument("--clip", type=float)
    parser.add_argument("--log_base", type=str)
    parser.add_argument("--is_continuous_pitch", type=int)
    parser.add_argument("--reduction_factor", type=int)
    parser.add_argument("--sentence_duration", type=int)
    parser.add_argument("--min_silence_len", type=int)
    return parser


def make_novoice_to_zero(audio: AudioSegment, silence_thresh: float, min_silence_len: int) -> AudioSegment:
    """????????????????????????????????????, 0??????????????????.
    """
    silences = silence.detect_silence(audio, min_silence_len=min_silence_len, silence_thresh=silence_thresh)

    audio_new = AudioSegment.empty()
    s_index = 0
    for silence_ in silences:
        audio_new += audio[s_index:silence_[0]]
        audio_new += AudioSegment.silent(duration=silence_[1]-silence_[0])
        s_index = silence_[1]

    audio_new += audio[s_index:]

    return audio_new


def delete_novoice(input_path, silence_thresh_h, silence_thresh_t, chunk_size):
    """???????????????????????????????????????????????????.
    Args:
      input_path: wav??????????????????path
      output_path: wav?????????????????????????????????. ??????????????????input_path???basename????????????.
      chunk_size: ???????????????????????????????????????. ??????default????????????????????????.
    """
    audio = AudioSegment.from_wav(input_path)
    assert len(audio) > 0, f"{input_path}??????????????????????????????????????????"

    # ??????: https://stackoverflow.com/questions/29547218/
    # remove-silence-at-the-beginning-and-at-the-end-of-wave-files-with-pydub
    def detect_leading_silence(sound, silence_threshold=-50.0, chunk_size=10):
        trim_ms = 0  # ms

        assert chunk_size > 0  # to avoid infinite loop
        while sound[trim_ms:trim_ms+chunk_size].dBFS < silence_threshold and trim_ms < len(sound):
            trim_ms += chunk_size

        return trim_ms

    start_trim = detect_leading_silence(audio, silence_threshold=silence_thresh_h, chunk_size=chunk_size)
    end_trim = detect_leading_silence(audio.reverse(),
                                      silence_threshold=silence_thresh_t, chunk_size=chunk_size)

    audio_cut = audio[start_trim:len(audio)-end_trim]

    audio_cut = make_novoice_to_zero(audio_cut, silence_thresh_t, chunk_size)

    assert len(audio_cut) > 0, f"{input_path}????????????cut???????????????????????????. ??????????????????????????????"
    return pydub_to_np(audio_cut)


def continuous_pitch(pitch: np.ndarray) -> np.ndarray:
    # 0?????????????????????nan???????????????, ??????????????????????????????.
    nonzero_ids = np.where(pitch > 1e-6)[0]
    interp_fn = interp1d(
        nonzero_ids,
        pitch[nonzero_ids],
        fill_value=(pitch[nonzero_ids[0]], pitch[nonzero_ids[-1]]),
        bounds_error=False,
    )
    pitch = interp_fn(np.arange(0, len(pitch)))

    return pitch


def process_utterance(wav, sr, n_fft, hop_length, win_length,
                      n_mels, fmin, fmax, clip_thresh, log_base,
                      is_continuous_pitch):
    pitch, t = pw.dio(
        wav.astype(np.float64),
        sr,
        frame_period=hop_length / sr * 1000,
    )
    pitch = pw.stonemask(wav.astype(np.float64),
                         pitch, t, sr)
    if np.sum(pitch != 0) <= 1:
        return None, None, None
    mel_spectrogram, energy = logmelspectrogram(
        wav,
        sr,
        n_fft,
        hop_length,
        win_length,
        n_mels,
        fmin,
        fmax,
        clip=clip_thresh,
        log_base=log_base,
        need_energy=True
    )
    energy = np.log(energy+1e-6)

    if is_continuous_pitch is True:
        no_voice_indexes = np.where(energy < -5.0)
        pitch[no_voice_indexes] = 0.0
        pitch = continuous_pitch(pitch)

    pitch = np.log(pitch+1e-6)

    return (
        mel_spectrogram,  # (T, mel)
        pitch,
        energy
    )


def reduction(x: np.ndarray, reduction_factor: int) -> np.ndarray:
    """1D or 2D?????????.
    2D?????????: (time, *) ?????????.
    """
    n_dim = len(x.shape)

    if n_dim > 2:
        raise ValueError("?????????2?????????array??????????????????????????????.")

    if n_dim == 1:
        x = x[:(x.shape[0]//reduction_factor)*reduction_factor]
        x = x.reshape(x.shape[0]//reduction_factor, reduction_factor)

    else:
        x = x.T
        x = x[:, :(x.shape[1]//reduction_factor)*reduction_factor]
        x = x.reshape(x.shape[0], x.shape[1]//reduction_factor, reduction_factor)

    x = x.mean(-1)

    return x.T


def get_s_e_index_from_bools(array):
    """True, False???bool????????????, True?????????index?????????????????????????????????index???
    ?????????????????????.
    """
    index_list = []
    flg = 0
    s_ind = 0
    for i, v in enumerate(array):
        if (flg == 0) and v:
            s_ind = i
            flg = 1
        elif (flg == 1) and (not v):
            index_list.append([s_ind, i])
            flg = 0

    if v:
        # ?????????True???????????????????????????.
        index_list.append([s_ind, i+1])

    return index_list


def calc_duration(ts_src: List[np.ndarray], target_path: str, diagonal_index: np.ndarray = None) -> np.ndarray:
    """
    Args:
      ts_src: ????????????????????????????????????.
        ???????????????, (time, d)????????????????????????.
        ????????????target, ??????sorce??????????????????.
      diagonal_index: ?????????????????????index. False, True????????????????????????.
        ???????????????, source???target???time index???x, y???????????????????????????, ????????????????????????????????????.

    source : target = ??? : 1 ????????????
        - ????????????source????????????1, ???????????????0??????????????????????????????.
        - ??????; -1, -2 ???????????????, mean??????????????????????????????, -1, -2??????????????????????????????????????????????????????. ????????????????????????????????????????????????.
        - ??????: ??????, ?????????????????????????????????????????????????????????????????????. target???????????????????????????????????????????????????.

    source: target = 1 : ???????????????
        - ?????????????????????, source???????????????????????????????????????.
    """
    t_src, s_src = ts_src
    duration = np.ones(s_src.shape[0])

    # alignment??????.
    _, path = fastdtw(t_src, s_src, dist=cityblock)

    # x???path??????????????????, ????????????????????????.
    patht = np.array(list(map(lambda l: l[0], path)))
    paths = np.array(list(map(lambda l: l[1], path)))

    b_p_t, b_p_s = 0, 0  # ?????????.
    count = 0
    for p_t, p_s in zip(patht[1:], paths[1:]):
        if b_p_t == p_t:
            # ??????, target?????????????????????????????????, s:t=???:1???????????????.
            duration[p_s] = 0  # ??????????????????, p_t?????????????????????p_s????????????????????????.
        if b_p_s == p_s:
            # source???????????????????????????, s:t=1:????????????????????????.
            count += 1
        elif count > 0:
            # count > 0???, ?????????????????????????????????, ????????????????????????????????????.
            duration[b_p_s] += count
            count = 0

        b_p_t = p_t
        b_p_s = p_s

    duration[b_p_s] += count if count > 0 else 0

    if diagonal_index is not None:
        assert s_src.shape[0] == len(
            diagonal_index), f"s_src.shape: {s_src.shape}, len(diagonal_index): {len(diagonal_index)}"
        index_list = get_s_e_index_from_bools(diagonal_index)
        for index_s_t in index_list:
            duration_part = duration[index_s_t[0]:index_s_t[1]]
            if np.sum(duration_part) > len(duration_part):
                # target???????????????????????????????????????.
                mean_ = np.sum(duration_part) // len(duration_part)
                rest_ = int(np.sum(duration_part) % (len(duration_part)*mean_))
                duration_part[:] = mean_
                duration_part[:rest_] += 1
            else:
                # source????????????????????????.
                s_index = int(np.sum(duration_part))
                duration_part[:s_index] = 1
                duration_part[s_index:] = 0

            duration[index_s_t[0]:index_s_t[1]] = duration_part

    assert np.sum(duration) == t_src.shape[0], f"""{target_path}??????duration??????????????????????????????\n
    duration: {duration}\n
    np.sum(duration): {np.sum(duration)}\n
    t_src.shape: {t_src.shape}"""

    return duration


def get_duration(
    utt_id, src_wav, tgt_wav, sr, n_fft, hop_length, win_length,
    fmin, fmax, clip_thresh, log_base, reduction_factor,
    return_mel=False
):
    src_mel, energy = logmelspectrogram(
        src_wav, sr, n_fft, hop_length, win_length,
        20, fmin, fmax, clip=clip_thresh, log_base=log_base,
        need_energy=True
    )
    tgt_mel = logmelspectrogram(
        tgt_wav, sr, n_fft, hop_length, win_length,
        20, fmin, fmax, clip=clip_thresh, log_base=log_base,
        need_energy=False
    )
    source_mel = reduction(src_mel, reduction_factor)
    energy = reduction(energy, reduction_factor)
    target_mel = reduction(tgt_mel, reduction_factor)
    duration = calc_duration([target_mel, source_mel], utt_id, np.log(energy+1e-6) < -5.0)
    if return_mel is True:
        return duration, source_mel, target_mel
    return duration


def get_sentence_duration(
    utt_id, src_wav, tgt_wav, sr, n_fft, hop_length, win_length,
    fmin, fmax, clip_thresh, log_base, reduction_factor,
    min_silence_len, silence_thresh
):
    duration, src_mel, tgt_mel = get_duration(utt_id, src_wav, tgt_wav, sr, n_fft, hop_length, win_length,
                                              fmin, fmax, clip_thresh, log_base, reduction_factor, return_mel=True
                                              )
    if tgt_wav.dtype in [np.float32, np.float64]:
        tgt_wav = (tgt_wav * np.iinfo(np.int16).max).astype(np.int16)

    t_audio = AudioSegment(
        tgt_wav.tobytes(),
        sample_width=2,
        frame_rate=sr,
        channels=1
    )

    t_silences = np.array(
        silence.detect_silence(t_audio, min_silence_len=min_silence_len, silence_thresh=silence_thresh)
    )

    src_sent_durations = []
    tgt_sent_durations = []

    t_silences = (t_silences / 1000 * sr // hop_length // reduction_factor).astype(np.int16)

    for i, (s_frame, _) in enumerate(t_silences):
        if s_frame == 0:
            continue
        if len(tgt_sent_durations) == 0:
            tgt_sent_durations.append(s_frame)
        else:
            tgt_sent_durations.append(s_frame - np.sum(tgt_sent_durations))

        if i == (len(t_silences) - 1):
            tgt_sent_durations.append(len(tgt_mel) - s_frame)

    if len(tgt_sent_durations) == 0:
        tgt_sent_durations = [len(tgt_mel)]
    assert (np.array(tgt_sent_durations) > 0).all(), f"""
        tgt_sent_durations: {tgt_sent_durations}
        t_silences: {t_silences}
        tgt_mel_len: {len(tgt_mel)}
    """
    assert np.sum(tgt_sent_durations) == len(tgt_mel)

    snt_sum = 0
    s_duration_cum = duration.cumsum()
    for i, snt_d in enumerate(tgt_sent_durations):
        snt_sum += snt_d
        snt_idx = np.argmax(s_duration_cum > snt_sum)
        if i == (len(tgt_sent_durations) - 1):
            snt_idx = len(src_mel)
        if len(src_sent_durations) == 0:
            src_sent_durations.append(snt_idx)
        else:
            src_sent_durations.append(snt_idx - np.sum(src_sent_durations))
    assert (np.array(src_sent_durations) > 0).all(), src_sent_durations
    assert np.sum(src_sent_durations) == len(src_mel)

    return np.array(src_sent_durations), np.array(tgt_sent_durations)


def preprocess(
    src_wav_file,
    tgt_wav_file,
    sr,
    silence_thresh_h,
    silence_thresh_t,
    chunk_size,
    n_fft,
    hop_length,
    win_length,
    n_mels,
    fmin,
    fmax,
    clip_thresh,
    log_base,
    is_continuous_pitch,
    reduction_factor,
    sentence_duration,
    min_silence_len,
    in_dir,
    out_dir,
):
    assert src_wav_file.stem == tgt_wav_file.stem

    src_wav, sr_src = delete_novoice(src_wav_file, silence_thresh_h, silence_thresh_t, chunk_size)
    tgt_wav, sr_tgt = delete_novoice(tgt_wav_file, silence_thresh_h, silence_thresh_t, chunk_size)

    src_wav = librosa.resample(src_wav, sr_src, sr)
    tgt_wav = librosa.resample(tgt_wav, sr_tgt, sr)

    utt_id = src_wav_file.stem

    src_mel, src_pitch, src_energy = process_utterance(
        src_wav, sr, n_fft, hop_length, win_length,
        n_mels, fmin, fmax, clip_thresh, log_base,
        is_continuous_pitch
    )
    if src_pitch is None:
        return src_wav_file, None
    tgt_mel, tgt_pitch, tgt_energy = process_utterance(
        tgt_wav, sr, n_fft, hop_length, win_length,
        n_mels, fmin, fmax, clip_thresh, log_base,
        is_continuous_pitch
    )
    if tgt_pitch is None:
        return None, tgt_wav_file
    duration = get_duration(utt_id, src_wav, tgt_wav, sr, n_fft, hop_length, win_length,
                            fmin, fmax, clip_thresh, log_base, reduction_factor)

    np.save(
        in_dir / "mel" / f"{utt_id}-feats.npy",
        src_mel.astype(np.float32),
        allow_pickle=False
    )
    np.save(
        in_dir / "pitch" / f"{utt_id}-feats.npy",
        src_pitch.astype(np.float32),
        allow_pickle=False
    )
    np.save(
        in_dir / "energy" / f"{utt_id}-feats.npy",
        src_energy.astype(np.float32),
        allow_pickle=False
    )
    np.save(
        out_dir / "mel" / f"{utt_id}-feats.npy",
        tgt_mel.astype(np.float32),
        allow_pickle=False,
    )
    np.save(
        out_dir / "pitch" / f"{utt_id}-feats.npy",
        tgt_pitch.astype(np.float32),
        allow_pickle=False,
    )
    np.save(
        out_dir / "energy" / f"{utt_id}-feats.npy",
        tgt_energy.astype(np.float32),
        allow_pickle=False,
    )
    np.save(
        out_dir / "duration" / f"{utt_id}-feats.npy",
        duration.astype(np.int16),
        allow_pickle=False,
    )
    if sentence_duration is True:
        src_sent_durations, tgt_sent_durations = get_sentence_duration(
            utt_id, src_wav, tgt_wav, sr, n_fft, hop_length, win_length,
            fmin, fmax, clip_thresh, log_base, reduction_factor,
            min_silence_len, silence_thresh_t
        )
        np.save(
            in_dir / "sent_duration" / f"{utt_id}-feats.npy",
            src_sent_durations.astype(np.int16),
            allow_pickle=False
        )
        np.save(
            out_dir / "sent_duration" / f"{utt_id}-feats.npy",
            tgt_sent_durations.astype(np.int16),
            allow_pickle=False,
        )
    return None, None


if __name__ == "__main__":
    args = get_parser().parse_args(sys.argv[1:])

    with open(args.utt_list) as f:
        utt_ids = [utt_id.strip() for utt_id in f]

    src_wav_files = [Path(args.src_wav_root) / f"{utt_id}.wav" for utt_id in utt_ids]
    tgt_wav_files = [Path(args.tgt_wav_root) / f"{utt_id}.wav" for utt_id in utt_ids]

    in_dir = Path(args.out_dir) / "in_fastspeech2VC"
    out_dir = Path(args.out_dir) / "out_fastspeech2VC"

    in_mel_dir = Path(args.out_dir) / "in_fastspeech2VC" / "mel"
    in_pitch_dir = Path(args.out_dir) / "in_fastspeech2VC" / "pitch"
    in_energy_dir = Path(args.out_dir) / "in_fastspeech2VC" / "energy"
    in_sent_duration_dir = Path(args.out_dir) / "in_fastspeech2VC" / "sent_duration"
    out_mel_dir = Path(args.out_dir) / "out_fastspeech2VC" / "mel"
    out_pitch_dir = Path(args.out_dir) / "out_fastspeech2VC" / "pitch"
    out_energy_dir = Path(args.out_dir) / "out_fastspeech2VC" / "energy"
    out_duration_dir = Path(args.out_dir) / "out_fastspeech2VC" / "duration"
    out_sent_duration_dir = Path(args.out_dir) / "out_fastspeech2VC" / "sent_duration"

    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    in_mel_dir.mkdir(parents=True, exist_ok=True)
    in_pitch_dir.mkdir(parents=True, exist_ok=True)
    in_energy_dir.mkdir(parents=True, exist_ok=True)
    in_sent_duration_dir.mkdir(parents=True, exist_ok=True)
    out_mel_dir.mkdir(parents=True, exist_ok=True)
    out_pitch_dir.mkdir(parents=True, exist_ok=True)
    out_energy_dir.mkdir(parents=True, exist_ok=True)
    out_duration_dir.mkdir(parents=True, exist_ok=True)
    out_sent_duration_dir.mkdir(parents=True, exist_ok=True)

    failed_src_lst = []
    failed_tgt_lst = []

    with ProcessPoolExecutor(args.n_jobs) as executor:
        futures = [
            executor.submit(
                preprocess,
                src_wav_file,
                tgt_wav_file,
                args.sample_rate,
                args.silence_thresh_h,
                args.silence_thresh_t,
                args.chunk_size,
                args.filter_length,
                args.hop_length,
                args.win_length,
                args.n_mel_channels,
                args.mel_fmin,
                args.mel_fmax,
                args.clip,
                args.log_base,
                args.is_continuous_pitch > 0,
                args.reduction_factor,
                args.sentence_duration > 0,
                args.min_silence_len,
                in_dir,
                out_dir,
            )
            for src_wav_file, tgt_wav_file in zip(src_wav_files, tgt_wav_files)
        ]
        for future in tqdm(futures):
            src_wav_file, tgt_wav_file = future.result()
            if src_wav_file is not None:
                failed_src_lst.append(str(src_wav_file)+'\n')
            if tgt_wav_file is not None:
                failed_tgt_lst.append(str(tgt_wav_file)+'\n')

    with open(in_dir.parent / "failed_src_lst.txt", 'w') as f:
        f.writelines(failed_src_lst)
    with open(in_dir.parent / "failed_tgt_lst.txt", 'w') as f:
        f.writelines(failed_tgt_lst)
