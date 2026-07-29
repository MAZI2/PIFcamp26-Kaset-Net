#!/usr/bin/env python3

import argparse
import pathlib

import numpy as np
from scipy.io import wavfile
from scipy import signal


DEFAULT_INPUT = pathlib.Path(__file__).resolve().parent.parent / "clip.wav"
DEFAULT_OUTPUT_DIR = pathlib.Path(__file__).resolve().parent / "out"


def read_audio(path):
    rate, data = wavfile.read(path)

    if data.dtype != np.int16:
        raise ValueError(f"Expected 16-bit PCM WAV, got {data.dtype}")

    audio = data.astype(np.float64) / 32768.0

    if audio.ndim == 1:
        audio = audio[:, None]

    return rate, audio


def write_audio(path, rate, audio):
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = np.asarray(audio)
    audio = np.clip(audio, -1.0, 0.999969)

    if audio.shape[1] == 1:
        audio = audio[:, 0]

    wavfile.write(path, rate, (audio * 32768.0).astype(np.int16))


def remove_dc(audio):
    return audio - np.mean(audio, axis=0, keepdims=True)


def butter_filter(audio, rate, cutoff_hz, kind, order=4):
    sos = signal.butter(order, cutoff_hz, btype=kind, fs=rate, output="sos")
    return signal.sosfiltfilt(sos, audio, axis=0)


def notch(audio, rate, freq_hz, quality=35.0):
    b, a = signal.iirnotch(freq_hz, quality, fs=rate)
    return signal.filtfilt(b, a, audio, axis=0)


def notch_harmonics(audio, rate, base_hz=50.0, max_hz=1200.0, quality=35.0):
    filtered = audio
    freq = base_hz

    while freq <= max_hz:
        filtered = notch(filtered, rate, freq, quality=quality)
        freq += base_hz

    return filtered


def notch_many(audio, rate, freqs, quality=25.0):
    filtered = audio

    for freq in freqs:
        filtered = notch(filtered, rate, freq, quality=quality)

    return filtered


def soften_clipped_samples(audio, clip_level=0.985):
    softened = audio.copy()

    for channel in range(audio.shape[1]):
        samples = softened[:, channel]
        clipped = np.abs(samples) >= clip_level
        indexes = np.flatnonzero(clipped)

        for index in indexes:
            left = index - 1
            right = index + 1

            while left >= 0 and clipped[left]:
                left -= 1

            while right < len(samples) and clipped[right]:
                right += 1

            if left >= 0 and right < len(samples):
                span = right - left
                position = (index - left) / span
                samples[index] = samples[left] + ((samples[right] - samples[left]) * position)
            elif left >= 0:
                samples[index] = samples[left]
            elif right < len(samples):
                samples[index] = samples[right]

    return softened


def spectral_gate(audio, rate, strength=0.65, min_gain=0.25, quiet_percentile=20):
    output_channels = []

    for channel in range(audio.shape[1]):
        samples = audio[:, channel]
        freqs, times, spectrum = signal.stft(
            samples,
            fs=rate,
            nperseg=2048,
            noverlap=1536,
            boundary="zeros",
        )

        magnitudes = np.abs(spectrum)
        frame_level = np.sqrt(np.mean(magnitudes * magnitudes, axis=0))
        cutoff = np.percentile(frame_level, quiet_percentile)
        quiet = frame_level <= cutoff

        if not np.any(quiet):
            quiet = np.ones_like(frame_level, dtype=bool)

        noise = np.median(magnitudes[:, quiet], axis=1, keepdims=True)
        gain = 1.0 - strength * (noise / (magnitudes + 1e-9))
        gain = np.clip(gain, min_gain, 1.0)

        _, cleaned = signal.istft(
            spectrum * gain,
            fs=rate,
            nperseg=2048,
            noverlap=1536,
            input_onesided=True,
        )

        output_channels.append(cleaned[:len(samples)])

    return np.column_stack(output_channels)


def despike(audio, threshold=0.16, kernel=7):
    cleaned = audio.copy()

    for channel in range(audio.shape[1]):
        samples = audio[:, channel]
        median = signal.medfilt(samples, kernel_size=kernel)
        residual = samples - median
        local_level = signal.medfilt(np.abs(residual), kernel_size=kernel * 4 + 1)
        limit = np.maximum(threshold, local_level * 8.0)
        spikes = np.abs(residual) > limit
        cleaned[spikes, channel] = median[spikes]

    return cleaned


def normalize_headroom(audio, peak=0.95):
    current_peak = np.max(np.abs(audio))

    if current_peak <= 0:
        return audio

    if current_peak <= peak:
        return audio

    return audio * (peak / current_peak)


def report(label, audio, rate):
    peak = np.max(np.abs(audio))
    rms = np.sqrt(np.mean(audio * audio))
    dc = np.mean(audio, axis=0)
    clipped = np.sum(np.abs(audio) >= 0.999)
    duration = len(audio) / rate
    print(
        f"{label}: {duration:.2f}s peak={peak:.4f} "
        f"rms={rms:.4f} dc={','.join(f'{v:.5f}' for v in dc)} "
        f"near_clip={int(clipped)}"
    )


def print_spectral_peaks(audio, rate, label):
    mono = np.mean(audio, axis=1)
    size = min(len(mono), rate * 6)
    window = mono[:size] * np.hanning(size)
    spectrum = np.abs(np.fft.rfft(window))
    freqs = np.fft.rfftfreq(size, 1.0 / rate)

    mask = (freqs >= 20) & (freqs <= 12000)
    indexes = np.argpartition(spectrum[mask], -12)[-12:]
    masked_freqs = freqs[mask]
    masked_spectrum = spectrum[mask]
    peaks = sorted(
        [(masked_freqs[index], masked_spectrum[index]) for index in indexes],
        key=lambda item: item[1],
        reverse=True,
    )

    print(f"\nStrongest frequency areas for {label}:")
    for freq, magnitude in peaks[:12]:
        print(f"  {freq:8.1f} Hz  magnitude={magnitude:.1f}")


def process(input_path, output_dir):
    rate, original = read_audio(input_path)
    report("original", original, rate)
    print_spectral_peaks(original, rate, "original")

    base = remove_dc(original)
    base = butter_filter(base, rate, 60.0, "highpass")

    hum = notch_harmonics(base, rate, base_hz=50.0, max_hz=1200.0)
    report("04 hum notched", hum, rate)
    write_audio(output_dir / "04_hum_notched_50hz_harmonics.wav", rate, normalize_headroom(hum))

    hiss_low = butter_filter(hum, rate, 6500.0, "lowpass")
    report("05 lowpass", hiss_low, rate)
    write_audio(output_dir / "05_hum_notched_lowpass_6k5.wav", rate, normalize_headroom(hiss_low))

    light_gate = spectral_gate(hum, rate, strength=0.55, min_gain=0.35, quiet_percentile=18)
    report("06 spectral light", light_gate, rate)
    write_audio(output_dir / "06_spectral_gate_light.wav", rate, normalize_headroom(light_gate))

    strong_gate = spectral_gate(hiss_low, rate, strength=0.85, min_gain=0.12, quiet_percentile=25)
    report("07 spectral strong", strong_gate, rate)
    write_audio(output_dir / "07_spectral_gate_strong.wav", rate, normalize_headroom(strong_gate))

    declicked = despike(hum)
    declicked = spectral_gate(declicked, rate, strength=0.65, min_gain=0.22, quiet_percentile=20)
    report("08 despike gate", declicked, rate)
    write_audio(output_dir / "08_despike_spectral_gate.wav", rate, normalize_headroom(declicked))

    targeted = notch_many(base, rate, [150.5, 495.0, 590.0], quality=18.0)
    targeted = butter_filter(targeted, rate, 8000.0, "lowpass")
    report("09 targeted tones", targeted, rate)
    write_audio(output_dir / "09_targeted_tone_notches.wav", rate, normalize_headroom(targeted))

    repaired = soften_clipped_samples(original)
    repaired = remove_dc(repaired)
    repaired = butter_filter(repaired, rate, 60.0, "highpass")
    repaired = notch_many(repaired, rate, [150.5, 495.0, 590.0], quality=18.0)
    repaired = despike(repaired, threshold=0.12)
    repaired = spectral_gate(repaired, rate, strength=0.78, min_gain=0.16, quiet_percentile=25)
    report("10 repaired aggressive", repaired, rate)
    write_audio(output_dir / "10_repaired_aggressive.wav", rate, normalize_headroom(repaired))

    print(f"\nWrote stronger variants to {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="Generate stronger cassette cleanup variants.")
    parser.add_argument("input", nargs="?", type=pathlib.Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    process(args.input, args.out)


if __name__ == "__main__":
    main()
