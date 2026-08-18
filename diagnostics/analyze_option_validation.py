#!/usr/bin/env python3
"""Analyze the one-pass low-level acoustic validation of every option FIR."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.ndimage import gaussian_filter1d
from scipy.signal import correlate, correlation_lags
from scipy.stats import spearmanr


RATE = 48_000
BANDS = {
    "bass_30_120": (30.0, 120.0),
    "low_mid_120_500": (120.0, 500.0),
    "mid_high_500_10000": (500.0, 10_000.0),
    "audible_30_10000": (30.0, 10_000.0),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_audio(path: Path) -> tuple[int, np.ndarray]:
    rate, values = wavfile.read(path)
    if rate != RATE:
        raise RuntimeError(f"unexpected recording rate: {rate}")
    if values.ndim > 1:
        values = values[:, 0]
    if np.issubdtype(values.dtype, np.integer):
        scale = float(2 ** (np.iinfo(values.dtype).bits - 1))
        values = values.astype(np.float64) / scale
    else:
        values = values.astype(np.float64)
    return rate, values


def calibration(path: Path) -> tuple[np.ndarray, np.ndarray]:
    frequencies, corrections = [], []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("*", "#", ";")):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        try:
            frequency, correction = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if frequency > 0:
            frequencies.append(frequency)
            corrections.append(correction)
    if len(frequencies) < 10:
        raise RuntimeError("calibration file has too few frequency points")
    order = np.argsort(frequencies)
    return np.asarray(frequencies)[order], np.asarray(corrections)[order]


def block_power(values: np.ndarray, block: int) -> np.ndarray:
    count = len(values) // block
    trimmed = values[: count * block].reshape(count, block)
    centered = trimmed - np.mean(trimmed, axis=1, keepdims=True)
    return np.mean(np.square(centered), axis=1)


def locate_playback_offset(recording: np.ndarray, metadata: dict[str, object], reference: np.ndarray) -> dict[str, object]:
    unfiltered = next((item for item in metadata["variants"] if item["id"] == "unfiltered-reference"), None)
    if unfiltered is not None:
        # The first direct full-range sweep is a much stronger timing marker
        # than the later heavily attenuated filters.  Correlate only its
        # bounded 0..3 s startup window so a long recording never needs one
        # enormous FFT.  The returned lag includes the ~40 ms acoustic path,
        # which is harmless inside the 200 ms analysis guard and preferable to
        # guessing it away.
        expected_start = int(unfiltered["sides"]["left"]["sweep_input_start_frame"])
        search_end = min(len(recording), expected_start + round(3.0 * RATE) + len(reference) + RATE)
        search = recording[expected_start:search_end]
        if len(search) >= len(reference):
            scores = correlate(search - np.mean(search), reference - np.mean(reference), mode="valid", method="fft")
            maximum_lag = min(len(scores) - 1, round(3.0 * RATE))
            lag = int(np.argmax(np.abs(scores[: maximum_lag + 1])))
            return {
                "offset_frames": lag,
                "offset_seconds": lag / RATE,
                "block_frames": None,
                "correlation_score": round(float(abs(scores[lag])), 6),
                "recording_noise_power": None,
                "method": "bounded direct-sweep FFT correlation; includes acoustic propagation delay",
            }
    block = round(0.05 * RATE)
    recorded_power = block_power(recording, block)
    expected_frames = int(metadata["total_frames"])
    expected_blocks = math.ceil(expected_frames / block)
    expected = np.zeros(expected_blocks, dtype=np.float64)
    for variant in metadata["variants"]:
        for side in ("left", "right"):
            item = variant["sides"][side]
            first = int(item["sweep_input_start_frame"]) // block
            last = math.ceil(int(item["output_end_frame"]) / block)
            expected[first:last] = 1.0
    # USB microphones can emit a large cold-start transient in the first
    # recorded second.  A low percentile across the whole sequence is a more
    # robust noise floor than assuming the initial global silence is clean.
    baseline = float(np.percentile(recorded_power, 10.0))
    observed = np.maximum(0.0, recorded_power - baseline)
    observed /= max(float(np.percentile(observed, 95)), 1e-30)
    scores = correlate(observed, expected, mode="full", method="fft")
    lags = correlation_lags(len(observed), len(expected), mode="full")
    maximum_lag_blocks = round(3.0 * RATE / block)
    allowed = (lags >= 0) & (lags <= maximum_lag_blocks)
    selected = int(np.argmax(np.where(allowed, scores, -np.inf)))
    lag_blocks = int(lags[selected])
    return {
        "offset_frames": lag_blocks * block,
        "offset_seconds": lag_blocks * block / RATE,
        "block_frames": block,
        "correlation_score": round(float(scores[selected]), 6),
        "recording_noise_power": baseline,
        "method": "50 ms robust power-envelope correlation",
    }


def ac_rms(values: np.ndarray) -> float:
    if len(values) == 0:
        return 0.0
    centered = values - float(np.mean(values))
    return float(np.sqrt(np.mean(np.square(centered))))


def robust_noise_rms(values: np.ndarray) -> float:
    """Use median 50 ms background power so one household transient is audited but does not define the floor."""
    block = round(0.05 * RATE)
    if len(values) < block:
        return ac_rms(values)
    count = len(values) // block
    blocks = values[: count * block].reshape(count, block)
    blocks = blocks - np.mean(blocks, axis=1, keepdims=True)
    return float(np.sqrt(np.median(np.mean(np.square(blocks), axis=1))))


def response_curve(
    recorded: np.ndarray,
    reference: np.ndarray,
    cal_frequency: np.ndarray,
    cal_db: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    length = 1 << (max(len(recorded), len(reference)) - 1).bit_length()
    y = np.fft.rfft(recorded, length)
    x = np.fft.rfft(reference, length)
    regularization = float(np.max(np.square(np.abs(x)))) * 1e-9
    transfer = y * np.conj(x) / (np.square(np.abs(x)) + regularization)
    bins = np.fft.rfftfreq(length, 1.0 / RATE)
    frequencies = 20.0 * np.power(1000.0, np.arange(512, dtype=np.float64) / 511.0)
    magnitude = np.interp(frequencies, bins, np.abs(transfer))
    levels = 20.0 * np.log10(np.maximum(magnitude, 1e-15))
    levels += np.interp(np.log(frequencies), np.log(cal_frequency), cal_db)
    points_per_octave = 1.0 / float(np.median(np.diff(np.log2(frequencies))))
    levels = gaussian_filter1d(levels, sigma=points_per_octave / 12.0, mode="nearest")
    return frequencies, levels


def aligned_metrics(frequency: np.ndarray, response: np.ndarray, target: np.ndarray) -> dict[str, object]:
    anchor = (frequency >= 500.0) & (frequency <= 2_000.0)
    offset = float(np.median(response[anchor] - target[anchor]))
    residual = response - target - offset
    result: dict[str, object] = {"alignment_offset_db": round(offset, 4)}
    for name, (low, high) in BANDS.items():
        mask = (frequency >= low) & (frequency <= high)
        values = residual[mask]
        result[name] = {
            "mae_db": round(float(np.mean(np.abs(values))), 3),
            "median_db": round(float(np.median(values)), 3),
            "p90_abs_db": round(float(np.percentile(np.abs(values), 90)), 3),
        }
    return result


def target_for_side(result_state: dict[str, object], side: str, frequency: np.ndarray) -> np.ndarray:
    graph = result_state["result"]["graphs"][side]
    graph_frequency = np.asarray(graph["frequency"], dtype=np.float64)
    key = "effective_target_db" if graph.get("effective_target_db") else "target_db"
    return np.interp(np.log(frequency), np.log(graph_frequency), np.asarray(graph[key], dtype=np.float64))


def relative_band(frequency: np.ndarray, levels: np.ndarray, low: float, high: float) -> float:
    selected = (frequency >= low) & (frequency <= high)
    anchor = (frequency >= 500.0) & (frequency <= 2_000.0)
    return float(np.median(levels[selected]) - np.median(levels[anchor]))


def monotonic_check(entries: list[dict[str, object]], dimension: str, band: tuple[float, float]) -> dict[str, object]:
    selected = [item for item in entries if item["dimension"] in ("baseline", dimension)]
    result: dict[str, object] = {}
    for side in ("left", "right"):
        pairs = []
        for item in selected:
            value = item["options"][dimension]
            frequency = np.asarray(item["acoustic"][side]["frequency_hz"])
            levels = np.asarray(item["acoustic"][side]["response_db"])
            pairs.append((float(value), relative_band(frequency, levels, *band)))
        pairs.sort()
        enough_values = len(pairs) >= 2 and len({pair[0] for pair in pairs}) >= 2
        correlation = float(spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic) if enough_values else None
        result[side] = {
            "evaluated": enough_values,
            "spearman": round(correlation, 4) if correlation is not None else None,
            "pass": not enough_values or (math.isfinite(correlation) and correlation >= 0.85),
            "values": [[round(a, 3), round(b, 3)] for a, b in pairs],
            "note": None if enough_values else "not applicable: selective validation contains fewer than two distinct values",
        }
    result["pass"] = all(result[side]["pass"] for side in ("left", "right"))
    return result


def write_svg(path: Path, entries: list[dict[str, object]]) -> None:
    width, height = 1180, 520
    left, right, top, bottom = 68, 20, 48, 62
    plot_w, plot_h = width - left - right, height - top - bottom
    values = []
    for item in entries:
        values.append(float(item["acoustic"]["left"]["target_fit"]["audible_30_10000"]["mae_db"]))
        values.append(float(item["acoustic"]["right"]["target_fit"]["audible_30_10000"]["mae_db"]))
    upper = max(8.0, math.ceil(max(values) / 2.0) * 2.0)
    x = lambda index: left + index / max(1, len(entries) - 1) * plot_w
    y = lambda value: top + (upper - value) / upper * plot_h
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#101827"/>',
        '<text x="68" y="29" fill="#f8fafc" font-family="sans-serif" font-size="18">AudioDSP all-option low-level acoustic target MAE</text>',
    ]
    for value in np.linspace(0, upper, 5):
        yy = y(float(value))
        lines.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#334155"/>')
        lines.append(f'<text x="{left-8}" y="{yy+4:.1f}" text-anchor="end" fill="#94a3b8" font-family="sans-serif" font-size="11">{value:.1f}</text>')
    for side, color in (("left", "#4ade80"), ("right", "#60a5fa")):
        points = " ".join(f"{x(i):.1f},{y(float(item['acoustic'][side]['target_fit']['audible_30_10000']['mae_db'])):.1f}" for i, item in enumerate(entries))
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
    for index, item in enumerate(entries):
        if index % 5 == 0:
            lines.append(f'<text transform="translate({x(index):.1f},{height-46}) rotate(60)" fill="#94a3b8" font-family="sans-serif" font-size="9">{item["id"]}</text>')
    lines.append('<text x="960" y="29" fill="#4ade80" font-family="sans-serif" font-size="12">Left</text><text x="1010" y="29" fill="#60a5fa" font-family="sans-serif" font-size="12">Right</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequence-dir", type=Path, required=True)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--matrix-dir", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    metadata = json.loads((args.sequence_dir / "validation_sequence.json").read_text(encoding="utf-8"))
    _, reference = read_audio(args.sequence_dir / "validation_reference_float32.wav")
    _, recording = read_audio(args.recording)
    cal_frequency, cal_db = calibration(args.calibration)
    timing = locate_playback_offset(recording, metadata, reference)
    offset = int(timing["offset_frames"])
    results: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    for definition in metadata["variants"]:
        identifier = definition["id"]
        is_filter_variant = bool(definition.get("is_filter_variant", True))
        state_identifier = identifier if is_filter_variant else "baseline"
        state = json.loads((args.matrix_dir / "filters" / state_identifier / "result-state.json").read_text(encoding="utf-8"))
        entry: dict[str, object] = {
            "id": identifier,
            "dimension": definition["dimension"],
            "value": definition["value"],
            "options": definition["options"],
            "front_sha256": definition["front_sha256"],
            "rear_sha256": definition["rear_sha256"],
            "is_filter_variant": is_filter_variant,
            "digital_self_validation_pass": bool(state["result"]["self_validation"]["overall_pass"]) if is_filter_variant else None,
            "digital_audit": {
                "front_phase": state["result"]["graphs"]["left"].get("phase"),
                "woofer_phase": state["result"]["graphs"].get("woofer", {}).get("phase"),
                "time_alignment": state["result"].get("time_alignment"),
            } if is_filter_variant else None,
            "acoustic": {},
        }
        for side in ("left", "right"):
            segment = definition["sides"][side]
            segment_start = offset + int(segment["segment_start_frame"])
            input_start = offset + int(segment["sweep_input_start_frame"])
            output_end = offset + int(segment["output_end_frame"])
            segment_end = min(len(recording), offset + int(segment["segment_end_frame"]))
            if segment_start < 0 or output_end > len(recording):
                raise RuntimeError(f"recording does not contain {identifier}/{side}")
            pre = recording[segment_start + round(0.05 * RATE): max(segment_start, input_start - round(0.05 * RATE))]
            active = recording[input_start + round(0.2 * RATE): min(output_end, input_start + len(reference) - round(0.2 * RATE))]
            noise_rms = robust_noise_rms(pre)
            raw_noise_rms = ac_rms(pre)
            transient_excess_db = 20.0 * math.log10(max(raw_noise_rms, 1e-30) / max(noise_rms, 1e-30))
            active_rms = ac_rms(active)
            signal_power = max(0.0, active_rms * active_rms - noise_rms * noise_rms)
            snr_db = 10.0 * math.log10(max(signal_power, 1e-30) / max(noise_rms * noise_rms, 1e-30))
            response_window = recording[input_start:segment_end]
            ref_window = np.pad(reference, (0, max(0, len(response_window) - len(reference))))[: len(response_window)]
            frequency, levels = response_curve(response_window, ref_window, cal_frequency, cal_db)
            target = target_for_side(state, side, frequency)
            target_fit = aligned_metrics(frequency, levels, target)
            peak = float(np.max(np.abs(recording[segment_start:segment_end])))
            acoustic = {
                "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-15)), 3),
                "noise_rms_dbfs": round(20.0 * math.log10(max(noise_rms, 1e-15)), 3),
                "raw_background_rms_dbfs": round(20.0 * math.log10(max(raw_noise_rms, 1e-15)), 3),
                "background_transient_excess_db": round(transient_excess_db, 3),
                "background_transient_detected": transient_excess_db >= 6.0,
                "active_rms_dbfs": round(20.0 * math.log10(max(active_rms, 1e-15)), 3),
                "snr_db": round(snr_db, 3),
                "usable": snr_db >= 3.0 and peak < 0.891,
                "recommended": snr_db >= 10.0 and peak < 0.891,
                "frequency_hz": [round(float(v), 3) for v in frequency],
                "response_db": [round(float(v), 4) for v in levels],
                "target_db": [round(float(v), 4) for v in target],
                "target_fit": target_fit,
            }
            entry["acoustic"][side] = acoustic
            rows.append({
                "id": identifier,
                "dimension": definition["dimension"],
                "value": definition["value"],
                "side": side,
                "snr_db": acoustic["snr_db"],
                "peak_dbfs": acoustic["peak_dbfs"],
                "usable": acoustic["usable"],
                "recommended": acoustic["recommended"],
                "background_transient_detected": acoustic["background_transient_detected"],
                "mae_30_120_db": target_fit["bass_30_120"]["mae_db"],
                "mae_120_500_db": target_fit["low_mid_120_500"]["mae_db"],
                "mae_500_10000_db": target_fit["mid_high_500_10000"]["mae_db"],
                "mae_30_10000_db": target_fit["audible_30_10000"]["mae_db"],
            })
        entry["acoustic_pass"] = all(entry["acoustic"][side]["usable"] for side in ("left", "right"))
        results.append(entry)

    baseline = next((item for item in results if item["id"] == "baseline"), None)
    family_checks: dict[str, dict[str, object]] = {}
    if any(item["dimension"] == "woofer_trim_db" for item in results):
        family_checks["woofer_trim_monotonic_38_67_hz"] = monotonic_check(results, "woofer_trim_db", (38.0, 67.0))
    if any(item["dimension"] == "bass_tilt_db" for item in results):
        family_checks["bass_tilt_monotonic_30_120_hz"] = monotonic_check(results, "bass_tilt_db", (30.0, 120.0))
    if any(item["dimension"] == "treble_tilt_db" for item in results):
        family_checks["treble_tilt_monotonic_5_10_khz"] = monotonic_check(results, "treble_tilt_db", (5_000.0, 10_000.0))
    center = next((item for item in results if item["id"] == "spatial_mode-center"), None)
    if center is not None and baseline is not None:
        family_checks["fixed_position_spatial_modes_digitally_identical"] = {
            "pass": baseline["front_sha256"] == center["front_sha256"] and baseline["rear_sha256"] == center["rear_sha256"],
            "note": "equal and center weighting must be identical when positions 2/3 explicitly reuse position 1",
        }
    phase_entries = [item for item in results if item["dimension"] in ("baseline", "phase_choice")]
    if len(phase_entries) > 1:
        explicit = True
        reasons: dict[str, object] = {}
        for item in phase_entries:
            front_phase = item["digital_audit"]["front_phase"] or {}
            woofer_phase = item["digital_audit"]["woofer_phase"] or {}
            if item["options"]["phase_mode"] == "magnitude":
                valid = "minimum phase" in str(front_phase.get("method", ""))
            else:
                valid = bool(front_phase.get("common_lr_phase")) and (
                    float(front_phase.get("applied_strength", 0.0)) > 0.0
                    or bool(front_phase.get("disabled_reason"))
                )
            explicit = explicit and valid
            reasons[item["id"]] = {
                "front_applied_strength": front_phase.get("applied_strength"),
                "front_disabled_reason": front_phase.get("disabled_reason"),
                "woofer_effective_mode": woofer_phase.get("effective_mode", "magnitude" if item["options"]["phase_mode"] == "magnitude" else None),
                "woofer_reason": woofer_phase.get("reason"),
            }
        pairs = {(item["front_sha256"], item["rear_sha256"]) for item in phase_entries}
        family_checks["phase_choices_explicit_effect_or_safe_fallback"] = {
            "pass": explicit,
            "distinct_filter_pairs": len(pairs),
            "all_identical": len(pairs) == 1,
            "variants": reasons,
            "note": "identical WAVs are valid only when each requested phase mode records an explicit safety fallback",
        }
    unfiltered = next((item for item in results if item["id"] == "unfiltered-reference"), None)
    if unfiltered is not None and baseline is not None:
        before_after: dict[str, object] = {}
        for side in ("left", "right"):
            before = unfiltered["acoustic"][side]["target_fit"]
            after = baseline["acoustic"][side]["target_fit"]
            audible_improvement = float(before["audible_30_10000"]["mae_db"]) - float(after["audible_30_10000"]["mae_db"])
            bass_improvement = float(before["bass_30_120"]["mae_db"]) - float(after["bass_30_120"]["mae_db"])
            before_after[side] = {
                "unfiltered_audible_mae_db": before["audible_30_10000"]["mae_db"],
                "filtered_audible_mae_db": after["audible_30_10000"]["mae_db"],
                "audible_mae_improvement_db": round(audible_improvement, 3),
                "unfiltered_bass_mae_db": before["bass_30_120"]["mae_db"],
                "filtered_bass_mae_db": after["bass_30_120"]["mae_db"],
                "bass_mae_improvement_db": round(bass_improvement, 3),
                "pass": audible_improvement >= -0.5 and bass_improvement >= -1.0,
            }
        before_after["pass"] = all(bool(before_after[side]["pass"]) for side in ("left", "right"))
        before_after["note"] = "same fixed microphone; baseline Harman/Strong FIR must not materially regress aligned target MAE versus direct L+Woofer/R+Woofer"
        family_checks["baseline_filtered_vs_unfiltered"] = before_after
    filter_results = [item for item in results if item["is_filter_variant"]]
    summary = {
        "timing": timing,
        "recording": str(args.recording),
        "artifact_sha256": {
            "recording": sha256(args.recording),
            "validation_playback": sha256(args.sequence_dir / str(metadata["output_wav"])),
            "validation_reference": sha256(args.sequence_dir / "validation_reference_float32.wav"),
            "microphone_calibration": sha256(args.calibration),
        },
        "microphone_calibration": str(args.calibration),
        "sequence": metadata,
        "matrix": {
            "variants": len(filter_results),
            "digital_passed": sum(bool(item["digital_self_validation_pass"]) for item in filter_results),
            "digital_failed": sum(not bool(item["digital_self_validation_pass"]) for item in filter_results),
        },
        "acoustic": {
            "sweeps": len(rows),
            "usable": sum(bool(row["usable"]) for row in rows),
            "recommended": sum(bool(row["recommended"]) for row in rows),
            "minimum_snr_db": round(min(float(row["snr_db"]) for row in rows), 3),
            "median_snr_db": round(float(np.median([float(row["snr_db"]) for row in rows])), 3),
            "maximum_peak_dbfs": round(max(float(row["peak_dbfs"]) for row in rows), 3),
            "background_transient_sweeps": sum(bool(row["background_transient_detected"]) for row in rows),
        },
        "family_checks": family_checks,
        "overall_pass": (
            all(item["digital_self_validation_pass"] for item in filter_results)
            and all(item["acoustic_pass"] for item in results)
            and all(check["pass"] for check in family_checks.values())
        ),
        "variants": results,
    }
    (args.output_dir / "acoustic_validation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output_dir / "acoustic_validation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write_svg(args.output_dir / "acoustic_validation.svg", results)
    print(json.dumps({
        "overall_pass": summary["overall_pass"],
        "matrix": summary["matrix"],
        "acoustic": summary["acoustic"],
        "family_checks": family_checks,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
