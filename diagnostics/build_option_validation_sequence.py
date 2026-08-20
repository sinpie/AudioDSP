#!/usr/bin/env python3
"""Build one low-level 4-channel acoustic validation sequence for all FIRs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import struct

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve


RATE = 48_000


def load_matrix_order(matrix_tool: Path) -> list[dict[str, object]]:
    spec = importlib.util.spec_from_file_location("audiodsp_option_matrix", matrix_tool)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load matrix definition: {matrix_tool}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.variants()


def read_fir(path: Path) -> np.ndarray:
    rate, samples = wavfile.read(path)
    if rate != RATE or samples.shape != (32_768, 2) or samples.dtype != np.float32:
        raise RuntimeError(f"invalid FIR contract: {path} / {rate} / {samples.shape} / {samples.dtype}")
    if not np.isfinite(samples).all():
        raise RuntimeError(f"non-finite FIR: {path}")
    return samples.astype(np.float64)


def log_sweep(seconds: float, level_dbfs: float) -> np.ndarray:
    count = round(seconds * RATE)
    time_axis = np.arange(count, dtype=np.float64) / RATE
    low, high = 20.0, 20_000.0
    ratio = math.log(high / low)
    phase = 2.0 * math.pi * low * seconds / ratio * (np.exp(time_axis * ratio / seconds) - 1.0)
    values = np.sin(phase) * (10.0 ** (level_dbfs / 20.0))
    fade = min(round(0.08 * RATE), count // 8)
    window = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade, dtype=np.float64) / max(1, fade - 1))
    values[:fade] *= window
    values[-fade:] *= window[::-1]
    return values


def pcm24_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0 - 1.0 / (2**23))
    signed = np.rint(clipped * (2**23)).astype(np.int32)
    unsigned = signed.astype(np.uint32)
    packed = np.empty((*signed.shape, 3), dtype=np.uint8)
    packed[..., 0] = unsigned & 0xFF
    packed[..., 1] = (unsigned >> 8) & 0xFF
    packed[..., 2] = (unsigned >> 16) & 0xFF
    return packed.tobytes(order="C")


def wav_header(frames: int, channels: int = 4) -> bytes:
    block_align = channels * 3
    data_bytes = frames * block_align
    fmt = struct.pack("<HHIIHH", 1, channels, RATE, RATE * block_align, block_align, 24)
    return (
        b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", data_bytes)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix-dir", type=Path, required=True)
    parser.add_argument("--matrix-tool", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--level-dbfs", type=float, default=-18.0)
    parser.add_argument("--sweep-seconds", type=float, default=3.0)
    parser.add_argument("--limit", type=int, choices=range(1, 69), default=None, help="preflight only: build the first N variants")
    parser.add_argument("--variant-id", action="append", default=[], help="build only this variant ID; may be repeated for selective acoustic retry")
    parser.add_argument("--include-unfiltered-reference", action="store_true", help="prepend direct L+Woofer and R+Woofer comparison sweeps")
    parser.add_argument("--unfiltered-attenuation-db", type=float, default=-6.0, help="extra safety attenuation for the louder direct comparison")
    args = parser.parse_args()
    if not -18.0 <= args.unfiltered_attenuation_db <= 0.0:
        raise RuntimeError("--unfiltered-attenuation-db must be between -18 and 0 dB")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((args.matrix_dir / "manifest.json").read_text(encoding="utf-8"))
    passed = {item["id"]: item for item in manifest["variants"] if item.get("status") == "PASS"}
    order = load_matrix_order(args.matrix_tool)
    if args.limit is not None and args.variant_id:
        raise RuntimeError("--limit and --variant-id cannot be combined")
    if args.limit is not None:
        order = order[:args.limit]
    elif args.variant_id:
        requested = set(args.variant_id)
        known = {str(item["id"]) for item in order}
        unknown = sorted(requested - known)
        if unknown:
            raise RuntimeError(f"unknown variant IDs: {unknown}")
        order = [item for item in order if item["id"] in requested]
    expected = [str(item["id"]) for item in order]
    missing = [identifier for identifier in expected if identifier not in passed]
    if missing:
        raise RuntimeError(f"matrix is incomplete; missing PASS variants: {missing}")
    sequence_definitions: list[dict[str, object]] = list(order)
    if args.include_unfiltered_reference:
        sequence_definitions.insert(0, {
            "id": "unfiltered-reference",
            "dimension": "reference",
            "value": None,
            "options": dict(passed["baseline"]["options"]),
        })

    sweep = log_sweep(args.sweep_seconds, args.level_dbfs)
    wavfile.write(args.output_dir / "validation_reference_float32.wav", RATE, sweep.astype(np.float32))
    pre_frames = round(0.50 * RATE)
    post_frames = round(1.00 * RATE)
    between_frames = round(0.40 * RATE)
    global_frames = round(2.0 * RATE)
    convolved_frames = len(sweep) + 32_768 - 1
    side_frames = pre_frames + convolved_frames + post_frames
    total_frames = global_frames * 2 + len(sequence_definitions) * (side_frames * 2 + between_frames)
    output_wav = args.output_dir / "all_options_low_level_validation_4ch_pcm24.wav"
    metadata: dict[str, object] = {
        "schema": 1,
        "sample_rate": RATE,
        "channels": ["Front L", "Front R", "Rear/Woofer L", "Rear/Woofer R"],
        "encoding": "PCM signed 24-bit little-endian",
        "level_dbfs": args.level_dbfs,
        "sweep_seconds": args.sweep_seconds,
        "pre_seconds": pre_frames / RATE,
        "post_seconds": post_frames / RATE,
        "between_sides_seconds": between_frames / RATE,
        "global_silence_seconds": global_frames / RATE,
        "total_frames": total_frames,
        "total_seconds": total_frames / RATE,
        "filter_variants": len(order),
        "includes_unfiltered_reference": args.include_unfiltered_reference,
        "unfiltered_attenuation_db": args.unfiltered_attenuation_db if args.include_unfiltered_reference else None,
        "variants": [],
    }
    maximum_peak = 0.0
    current_frame = global_frames
    silence_pre = np.zeros((pre_frames, 4), dtype=np.float64)
    silence_post = np.zeros((post_frames, 4), dtype=np.float64)
    silence_between = np.zeros((between_frames, 4), dtype=np.float64)
    silence_global = np.zeros((global_frames, 4), dtype=np.float64)
    with output_wav.open("wb") as handle:
        handle.write(wav_header(total_frames))
        handle.write(pcm24_bytes(silence_global))
        for definition in sequence_definitions:
            identifier = str(definition["id"])
            is_filter_variant = definition["dimension"] != "reference"
            if is_filter_variant:
                directory = args.matrix_dir / "filters" / identifier
                front = read_fir(directory / "Front_LR_32768.wav")
                rear = read_fir(directory / "Woofer_LR_32768.wav")
            else:
                front = np.zeros((32_768, 2), dtype=np.float64)
                rear = np.zeros((32_768, 2), dtype=np.float64)
                direct_gain = 10.0 ** (args.unfiltered_attenuation_db / 20.0)
                front[0, :] = direct_gain
                rear[0, :] = direct_gain
            item: dict[str, object] = {
                "id": identifier,
                "dimension": definition["dimension"],
                "value": definition["value"],
                "options": definition["options"],
                "is_filter_variant": is_filter_variant,
                "additional_attenuation_db": 0.0 if is_filter_variant else args.unfiltered_attenuation_db,
                "front_sha256": passed[identifier]["front_sha256"] if is_filter_variant else None,
                "rear_sha256": passed[identifier]["rear_sha256"] if is_filter_variant else None,
                "sides": {},
            }
            for side, channel in (("left", 0), ("right", 1)):
                block = np.zeros((convolved_frames, 4), dtype=np.float64)
                block[:, channel] = fftconvolve(sweep, front[:, channel], mode="full")
                block[:, channel + 2] = fftconvolve(sweep, rear[:, channel], mode="full")
                peak = float(np.max(np.abs(block)))
                maximum_peak = max(maximum_peak, peak)
                if peak >= 0.98:
                    raise RuntimeError(f"validation sequence would clip: {identifier}/{side} peak={peak}")
                segment_start = current_frame
                sweep_input_start = segment_start + pre_frames
                output_end = sweep_input_start + convolved_frames
                item["sides"][side] = {
                    "segment_start_frame": segment_start,
                    "sweep_input_start_frame": sweep_input_start,
                    "output_end_frame": output_end,
                    "segment_end_frame": segment_start + side_frames,
                    "digital_peak_dbfs": round(20.0 * math.log10(max(peak, 1e-15)), 3),
                }
                handle.write(pcm24_bytes(silence_pre))
                handle.write(pcm24_bytes(block))
                handle.write(pcm24_bytes(silence_post))
                current_frame += side_frames
                if side == "left":
                    handle.write(pcm24_bytes(silence_between))
                    current_frame += between_frames
            metadata["variants"].append(item)
        handle.write(pcm24_bytes(silence_global))
        current_frame += global_frames
    if current_frame != total_frames:
        raise RuntimeError(f"frame accounting mismatch: {current_frame} != {total_frames}")
    metadata["maximum_digital_peak_dbfs"] = round(20.0 * math.log10(max(maximum_peak, 1e-15)), 3)
    metadata["output_wav"] = output_wav.name
    (args.output_dir / "validation_sequence.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(output_wav),
        "variants": len(order),
        "sequence_entries": len(sequence_definitions),
        "seconds": metadata["total_seconds"],
        "maximum_digital_peak_dbfs": metadata["maximum_digital_peak_dbfs"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
