#!/usr/bin/env python3
"""Build a conservative tonal refinement from an AudioDSP sweep A/B report.

The refinement deliberately leaves 0-120 Hz untouched, uses only broad
correction above that point, and preserves the source FIR phase at every FFT
bin.  It is intended for a listening preview after a low-level sweep, not as a
replacement for a multi-position room calibration.
"""

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


def read_comparison(path: Path) -> dict[str, dict[str, np.ndarray]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result: dict[str, dict[str, np.ndarray]] = {}
    for side in ("left", "right"):
        selected = [row for row in rows if row["side"] == side]
        if not selected:
            raise ValueError(f"comparison CSV has no {side} rows")
        result[side] = {
            key: np.asarray([float(row[key]) for row in selected], dtype=np.float64)
            for key in (
                "frequency_hz",
                "raw_db",
                "predicted_fir_db",
                "target_relative_db",
                "theoretical_filter_db",
            )
        }
    return result


def aligned_residual(
    frequency: np.ndarray, response: np.ndarray, target: np.ndarray
) -> tuple[np.ndarray, float]:
    anchor = (frequency >= 500.0) & (frequency <= 2_000.0)
    offset = float(np.median(response[anchor] - target[anchor]))
    return response - target - offset, offset


def band_metrics(
    frequency: np.ndarray, response: np.ndarray, target: np.ndarray
) -> dict[str, object]:
    residual, offset = aligned_residual(frequency, response, target)
    metrics: dict[str, object] = {"alignment_offset_db": round(offset, 4)}
    for name, (low, high) in BANDS.items():
        mask = (frequency >= low) & (frequency <= high)
        values = residual[mask]
        metrics[name] = {
            "mae_db": round(float(np.mean(np.abs(values))), 3),
            "p90_p10_db": round(
                float(np.percentile(values, 90) - np.percentile(values, 10)), 3
            ),
            "max_excess_db": round(float(np.max(values)), 3),
            "median_db": round(float(np.median(values)), 3),
        }
    return metrics


def smooth_correction(
    frequency: np.ndarray, predicted: np.ndarray, target: np.ndarray
) -> np.ndarray:
    residual, _ = aligned_residual(frequency, predicted, target)
    points_per_octave = 1.0 / float(np.median(np.diff(np.log2(frequency))))
    # sigma=1/4 octave produces an approximately 0.59-octave FWHM.  This is
    # intentionally much broader than narrow room nulls or measurement noise.
    smoothed = gaussian_filter1d(
        residual, sigma=0.25 * points_per_octave, mode="nearest"
    )
    anchor = (frequency >= 500.0) & (frequency <= 2_000.0)
    smoothed -= float(np.median(smoothed[anchor]))
    correction = -smoothed

    # Keep the experiment small.  A single listening position is insufficient
    # evidence for aggressive or narrow correction.
    limit = np.where(frequency < 500.0, 1.0, 0.9)
    correction = np.clip(correction, -limit, limit)

    # No change below 120 Hz.  Fade in over 120-180 Hz and fade out above 8 kHz
    # so the strong-bass-control safety characteristic remains identical.
    fade = np.ones_like(frequency)
    fade[frequency <= 120.0] = 0.0
    transition = (frequency > 120.0) & (frequency < 180.0)
    x = (frequency[transition] - 120.0) / 60.0
    fade[transition] = 0.5 - 0.5 * np.cos(np.pi * x)
    transition = (frequency > 8_000.0) & (frequency < 12_000.0)
    x = (frequency[transition] - 8_000.0) / 4_000.0
    fade[transition] = 0.5 + 0.5 * np.cos(np.pi * x)
    fade[frequency >= 12_000.0] = 0.0
    return correction * fade


def impulse_metrics(samples: np.ndarray) -> dict[str, object]:
    energy = np.square(samples.astype(np.float64))
    cumulative = np.cumsum(energy)
    total = float(cumulative[-1])
    peak = int(np.argmax(np.abs(samples)))
    return {
        "peak_tap": peak,
        "peak_time_ms": round(1_000.0 * peak / 48_000.0, 4),
        "energy_50_tap": int(np.searchsorted(cumulative, total * 0.5)),
        "energy_90_tap": int(np.searchsorted(cumulative, total * 0.9)),
        "pre_peak_energy_percent": round(100.0 * float(cumulative[peak]) / total, 3),
    }


def svg_report(
    path: Path,
    frequency: np.ndarray,
    curves: dict[str, dict[str, np.ndarray]],
) -> None:
    width, height = 1040, 620
    left, right, top, bottom = 72, 24, 54, 54
    plot_w, plot_h = width - left - right, height - top - bottom

    def px(freq: np.ndarray) -> np.ndarray:
        return left + (np.log10(freq) - math.log10(20.0)) / 3.0 * plot_w

    def py(value: np.ndarray) -> np.ndarray:
        return top + (8.0 - np.clip(value, -12.0, 8.0)) / 20.0 * plot_h

    colors = {
        "left_current": "#78a9ff",
        "left_candidate": "#33d6a6",
        "right_current": "#ff8db3",
        "right_candidate": "#ffc857",
    }
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#111827"/>',
        '<text x="72" y="30" fill="#f3f4f6" font-family="sans-serif" font-size="18">AudioDSP tonal refinement — target residual (dB)</text>',
    ]
    for db in (-12, -8, -4, 0, 4, 8):
        y = float(py(np.asarray([db]))[0])
        lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#334155"/>')
        lines.append(f'<text x="{left-10}" y="{y+4:.1f}" fill="#94a3b8" font-family="sans-serif" font-size="12" text-anchor="end">{db}</text>')
    for freq in (20, 50, 100, 200, 500, 1_000, 2_000, 5_000, 10_000, 20_000):
        x = float(px(np.asarray([freq]))[0])
        lines.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height-bottom}" stroke="#273449"/>')
        label = f"{freq // 1000}k" if freq >= 1_000 else str(freq)
        lines.append(f'<text x="{x:.1f}" y="{height-25}" fill="#94a3b8" font-family="sans-serif" font-size="12" text-anchor="middle">{label}</text>')
    for side in ("left", "right"):
        for name in ("current", "candidate"):
            values = curves[side][name]
            points = " ".join(
                f"{x:.1f},{y:.1f}" for x, y in zip(px(frequency), py(values))
            )
            dash = ' stroke-dasharray="6 5"' if name == "current" else ""
            lines.append(
                f'<polyline points="{points}" fill="none" stroke="{colors[f"{side}_{name}"]}" stroke-width="2"{dash}/>'
            )
    legend = [
        ("L current", colors["left_current"]),
        ("L candidate", colors["left_candidate"]),
        ("R current", colors["right_current"]),
        ("R candidate", colors["right_candidate"]),
    ]
    x = 590
    for label, color in legend:
        lines.append(f'<line x1="{x}" y1="27" x2="{x+22}" y2="27" stroke="{color}" stroke-width="3"/>')
        lines.append(f'<text x="{x+27}" y="31" fill="#d1d5db" font-family="sans-serif" font-size="12">{label}</text>')
        x += 108
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--source-fir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--output-name",
        default="Harman_StrongBassControl_RefinedTone_Stereo_48k_NoPreamp.wav",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    sample_rate, source = wavfile.read(args.source_fir)
    if sample_rate != 48_000 or source.ndim != 2 or source.shape != (32_768, 2):
        raise ValueError("source FIR must be stereo, 48 kHz, and exactly 32768 taps")
    if source.dtype != np.float32:
        raise ValueError("source FIR must be IEEE float32 WAV")

    comparison = read_comparison(args.comparison)
    fft_frequency = np.fft.rfftfreq(source.shape[0], 1.0 / sample_rate)
    candidate = np.empty_like(source)
    report: dict[str, object] = {
        "method": {
            "bass_lock_hz": 120,
            "fade_in_hz": [120, 180],
            "fade_out_hz": [8_000, 12_000],
            "maximum_correction_db": {"120_500_hz": 1.0, "500_12000_hz": 0.9},
            "smoothing": "Gaussian sigma 0.25 octave (about 0.59-octave FWHM)",
            "phase": "source FIR phase preserved at every non-null FFT bin",
        },
        "source": {
            "path": str(args.source_fir),
            "sha256": sha256(args.source_fir),
            "sample_rate": sample_rate,
            "taps": int(source.shape[0]),
            "dtype": str(source.dtype),
        },
        "sides": {},
    }
    graph_curves: dict[str, dict[str, np.ndarray]] = {}
    csv_rows: list[dict[str, object]] = []
    for channel, side in enumerate(("left", "right")):
        data = comparison[side]
        frequency = data["frequency_hz"]
        correction_grid = smooth_correction(
            frequency, data["predicted_fir_db"], data["target_relative_db"]
        )
        correction_fft = np.interp(
            fft_frequency,
            frequency,
            correction_grid,
            left=0.0,
            right=0.0,
        )
        correction_fft[fft_frequency <= 120.0] = 0.0
        correction_fft[fft_frequency >= 12_000.0] = 0.0

        source_spectrum = np.fft.rfft(source[:, channel].astype(np.float64))
        source_db = 20.0 * np.log10(np.maximum(np.abs(source_spectrum), 1e-12))
        # Never create a response peak higher than the source channel's existing
        # maximum; this preserves the current FIR's digital headroom.
        correction_fft = np.minimum(
            correction_fft, float(np.max(source_db)) - source_db
        )
        candidate_spectrum = source_spectrum * np.power(10.0, correction_fft / 20.0)
        candidate[:, channel] = np.fft.irfft(
            candidate_spectrum, n=source.shape[0]
        ).astype(np.float32)

        actual_candidate_spectrum = np.fft.rfft(candidate[:, channel].astype(np.float64))
        actual_delta_fft = 20.0 * np.log10(
            np.maximum(np.abs(actual_candidate_spectrum), 1e-12)
            / np.maximum(np.abs(source_spectrum), 1e-12)
        )
        delta_grid = np.interp(frequency, fft_frequency, actual_delta_fft)
        predicted_candidate = data["predicted_fir_db"] + delta_grid
        current_residual, _ = aligned_residual(
            frequency, data["predicted_fir_db"], data["target_relative_db"]
        )
        candidate_residual, _ = aligned_residual(
            frequency, predicted_candidate, data["target_relative_db"]
        )
        graph_curves[side] = {
            "current": current_residual,
            "candidate": candidate_residual,
        }

        valid_phase = np.abs(source_spectrum) > 1e-8
        phase_error = np.angle(
            actual_candidate_spectrum[valid_phase] / source_spectrum[valid_phase]
        )
        low_bass = fft_frequency <= 120.0
        low_bass_change = actual_delta_fft[low_bass]
        side_report = {
            "current_target_fit": band_metrics(
                frequency, data["predicted_fir_db"], data["target_relative_db"]
            ),
            "candidate_target_fit": band_metrics(
                frequency, predicted_candidate, data["target_relative_db"]
            ),
            "correction_db": {
                "minimum": round(float(np.min(actual_delta_fft)), 4),
                "maximum": round(float(np.max(actual_delta_fft)), 4),
                "max_abs_change_at_or_below_120_hz": round(
                    float(np.max(np.abs(low_bass_change))), 7
                ),
            },
            "phase_max_abs_error_degrees": round(
                float(np.max(np.abs(np.degrees(phase_error)))), 7
            ),
            "source_impulse": impulse_metrics(source[:, channel]),
            "candidate_impulse": impulse_metrics(candidate[:, channel]),
        }
        report["sides"][side] = side_report
        for index, freq in enumerate(frequency):
            csv_rows.append(
                {
                    "side": side,
                    "frequency_hz": f"{freq:.3f}",
                    "current_predicted_db": f"{data['predicted_fir_db'][index]:.4f}",
                    "candidate_predicted_db": f"{predicted_candidate[index]:.4f}",
                    "target_relative_db": f"{data['target_relative_db'][index]:.4f}",
                    "candidate_delta_db": f"{delta_grid[index]:.4f}",
                    "current_target_residual_db": f"{current_residual[index]:.4f}",
                    "candidate_target_residual_db": f"{candidate_residual[index]:.4f}",
                }
            )

    output_wav = args.output_dir / args.output_name
    wavfile.write(output_wav, sample_rate, candidate)
    report["candidate"] = {
        "path": str(output_wav),
        "sha256": sha256(output_wav),
        "sample_rate": sample_rate,
        "taps": int(candidate.shape[0]),
        "dtype": str(candidate.dtype),
    }
    report_path = args.output_dir / "tonal_refinement_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    csv_path = args.output_dir / "tonal_refinement_comparison.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    svg_report(
        args.output_dir / "tonal_refinement_comparison.svg",
        comparison["left"]["frequency_hz"],
        graph_curves,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
