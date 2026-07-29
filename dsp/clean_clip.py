#!/usr/bin/env python3

import argparse
import array
import math
import pathlib
import wave


DEFAULT_INPUT = pathlib.Path(__file__).resolve().parent.parent / "clip.wav"
DEFAULT_OUTPUT_DIR = pathlib.Path(__file__).resolve().parent / "out"


def clamp_sample(value):
    return max(-32768, min(32767, int(round(value))))


def read_wav(path):
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        rate = wav_file.getframerate()
        frames = wav_file.getnframes()
        raw = wav_file.readframes(frames)

    if sample_width != 2:
        raise ValueError(f"Only 16-bit PCM WAV is supported, got {sample_width * 8}-bit")

    samples = array.array("h")
    samples.frombytes(raw)

    if samples.itemsize != 2:
        raise RuntimeError("Unexpected host sample size")

    if not is_little_endian():
        samples.byteswap()

    return {
        "channels": channels,
        "sample_width": sample_width,
        "rate": rate,
        "samples": list(samples),
    }


def write_wav(path, audio, samples):
    path.parent.mkdir(parents=True, exist_ok=True)

    pcm = array.array("h", (clamp_sample(sample) for sample in samples))

    if not is_little_endian():
        pcm.byteswap()

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(audio["channels"])
        wav_file.setsampwidth(audio["sample_width"])
        wav_file.setframerate(audio["rate"])
        wav_file.writeframes(pcm.tobytes())


def is_little_endian():
    return array.array("h", [1]).tobytes()[0] == 1


def channel_values(samples, channels, channel):
    return samples[channel::channels]


def describe(label, audio, samples):
    channels = audio["channels"]
    rate = audio["rate"]
    duration = len(samples) / (rate * channels)
    peak = max((abs(sample) for sample in samples), default=0)
    rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))

    dc_parts = []
    for channel in range(channels):
        values = channel_values(samples, channels, channel)
        dc = sum(values) / max(1, len(values))
        dc_parts.append(f"ch{channel + 1}={dc:.1f}")

    print(
        f"{label}: {rate} Hz, {channels} ch, {duration:.2f}s, "
        f"peak={peak}, rms={rms:.1f}, dc=({', '.join(dc_parts)})"
    )


def remove_dc(audio, samples):
    channels = audio["channels"]
    offsets = []

    for channel in range(channels):
        values = channel_values(samples, channels, channel)
        offsets.append(sum(values) / max(1, len(values)))

    cleaned = []
    for index, sample in enumerate(samples):
        cleaned.append(clamp_sample(sample - offsets[index % channels]))

    return cleaned


def highpass(audio, samples, cutoff_hz=70.0):
    channels = audio["channels"]
    rate = audio["rate"]
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    dt = 1.0 / rate
    alpha = rc / (rc + dt)

    previous_input = [0.0] * channels
    previous_output = [0.0] * channels
    output = [0] * len(samples)

    for index, sample in enumerate(samples):
        channel = index % channels
        filtered = alpha * (previous_output[channel] + sample - previous_input[channel])
        previous_input[channel] = sample
        previous_output[channel] = filtered
        output[index] = clamp_sample(filtered)

    return output


def estimate_noise_floor(audio, samples, block_ms=25.0, percentile=0.15):
    channels = audio["channels"]
    rate = audio["rate"]
    block_frames = max(1, int(rate * (block_ms / 1000.0)))
    block_samples = block_frames * channels
    levels = []

    for start in range(0, len(samples), block_samples):
        block = samples[start:start + block_samples]

        if not block:
            continue

        rms = math.sqrt(sum(sample * sample for sample in block) / len(block))
        levels.append(rms)

    if not levels:
        return 0.0

    levels.sort()
    index = int(max(0, min(len(levels) - 1, (len(levels) - 1) * percentile)))
    return levels[index]


def soft_noise_reduce(audio, samples, floor_rms=None, block_ms=12.0):
    channels = audio["channels"]
    rate = audio["rate"]
    block_frames = max(1, int(rate * (block_ms / 1000.0)))
    block_samples = block_frames * channels

    if floor_rms is None:
        floor_rms = estimate_noise_floor(audio, samples)

    threshold = max(120.0, floor_rms * 2.2)
    full_level = max(threshold * 4.0, threshold + 1.0)
    output = []
    last_gain = 1.0

    for start in range(0, len(samples), block_samples):
        block = samples[start:start + block_samples]

        if not block:
            continue

        rms = math.sqrt(sum(sample * sample for sample in block) / len(block))

        if rms <= threshold:
            target_gain = 0.28
        elif rms >= full_level:
            target_gain = 1.0
        else:
            ratio = (rms - threshold) / (full_level - threshold)
            target_gain = 0.28 + (ratio * 0.72)

        gain = (last_gain * 0.65) + (target_gain * 0.35)
        last_gain = gain

        output.extend(clamp_sample(sample * gain) for sample in block)

    return output


def process(input_path, output_dir):
    audio = read_wav(input_path)
    original = audio["samples"]

    describe("original", audio, original)

    dc_removed = remove_dc(audio, original)
    describe("dc removed", audio, dc_removed)
    write_wav(output_dir / "01_dc_removed.wav", audio, dc_removed)

    highpassed = highpass(audio, dc_removed)
    describe("high-pass", audio, highpassed)
    write_wav(output_dir / "02_highpass.wav", audio, highpassed)

    reduced = soft_noise_reduce(audio, highpassed)
    describe("noise reduced", audio, reduced)
    write_wav(output_dir / "03_noise_reduced.wav", audio, reduced)

    print(f"Wrote test files to {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="First-pass cleanup for cassette recorder WAV clips.")
    parser.add_argument("input", nargs="?", type=pathlib.Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    process(args.input, args.out)


if __name__ == "__main__":
    main()
