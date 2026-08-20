#!/usr/bin/env python3
"""Silent regression test for simultaneous L/R/W relative-phase acquisition."""

from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import types

import numpy as np


class NumpyFFT:
    kind = "numpy-silent-phase-test"

    def close(self) -> None:
        return None

    def rfft(self, values, length: int) -> list[complex]:
        return np.fft.rfft(np.asarray(list(values), dtype=np.float64), n=length).tolist()

    def irfft(self, values, length: int) -> list[float]:
        return np.fft.irfft(np.asarray(values, dtype=np.complex128), n=length).tolist()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    if os.name == "nt" and "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.flock = lambda _handle, _operation: None
        sys.modules["fcntl"] = stub
    engine_path = Path(__file__).resolve().parents[1] / "payload" / "audiodsp-measurement.py"
    specification = importlib.util.spec_from_file_location("audiodsp_phase_reference_test", engine_path)
    require(specification is not None and specification.loader is not None, "cannot load measurement engine")
    engine = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(engine)
    engine.FFTBackend = NumpyFFT

    require(engine.sweep_band_for_source("left") == (30.0, 22_000.0), "Front sweep band mismatch")
    require(engine.sweep_band_for_source("woofer") == (15.0, 320.0), "Woofer sweep band mismatch")
    require(engine.sweep_band_for_source("left_woofer") == (15.0, 22_000.0), "sum sweep band mismatch")

    with tempfile.TemporaryDirectory(prefix="audiodsp-phase-reference-") as temporary:
        output = Path(temporary) / "phase.wav"
        metadata = engine.write_phase_reference(output, -30, -9)
        require(output.is_file() and output.stat().st_size > 1_000_000, "phase signal WAV was not created")
        tone_counts = {name: len(value["bins"]) for name, value in metadata["sources"].items()}
        require(all(15 <= count <= 32 for count in tone_counts.values()), f"tone density is not sparse: {tone_counts}")
        source_bins = {
            source: {int(item["bin"]) for item in value["bins"]}
            for source, value in metadata["sources"].items()
        }
        common_bins = set.intersection(*source_bins.values())
        require(len(common_bins) >= 12, "Walsh phase sources do not share enough exact crossover bins")
        codes = {
            source: [float(state["codes"][source]) for state in metadata["walsh_states"]]
            for source in ("left", "right", "woofer")
        }
        for first, second in (("left", "right"), ("left", "woofer"), ("right", "woofer")):
            require(abs(sum(a * b for a, b in zip(codes[first], codes[second]))) < 1.0e-9,
                    f"Walsh columns are not orthogonal: {first}/{second}")

        length = metadata["block_samples"]
        delays = {"left": 120.0, "right": 132.0, "woofer": 165.0}
        gains = {"left": 1.0, "right": 0.9, "woofer": 1.6}
        captured_blocks = []
        for state in metadata["walsh_states"]:
            captured_spectrum = [0j] * (length // 2 + 1)
            for source, source_metadata in metadata["sources"].items():
                code = float(state["codes"][source])
                for item in source_metadata["bins"]:
                    bin_index = int(item["bin"])
                    reference = complex(float(item["real"]), float(item["imag"]))
                    rotation = complex(
                        math.cos(-2.0 * math.pi * bin_index * delays[source] / length),
                        math.sin(-2.0 * math.pi * bin_index * delays[source] / length),
                    )
                    captured_spectrum[bin_index] += code * reference * gains[source] * rotation
            captured_block = NumpyFFT().irfft(captured_spectrum, length)
            captured_blocks.extend([captured_block] * int(metadata["state_periods"]))
        samples = [0.0] * engine.RATE + [value for block in captured_blocks for value in block] + [0.0] * engine.RATE
        calibration = {"frequencies": [10.0, 24_000.0], "corrections": [0.0, 0.0]}
        result = engine.analyze_phase_reference_samples(samples, metadata, calibration, NumpyFFT())
        require(result["reliable"], json.dumps(result, indent=2))
        expected = {"left_right": 12.0, "left_woofer": 45.0, "right_woofer": 33.0}
        for pair, expected_samples in expected.items():
            actual = float(result["pairs"][pair]["second_minus_first_delay_samples"])
            require(abs(actual - expected_samples) <= 2.5, f"{pair} delay {actual} != {expected_samples}")
            require(result["pairs"][pair]["same_frequency_bins"], f"{pair} did not use common-frequency bins")

        # Six-capture fusion: individual magnitudes + simultaneous phase +
        # physical L+W magnitude must reduce closure error without averaging W
        # into the Front branch or normalizing either response.
        frequencies = [50.0, 200.0]
        front_response = {"frequencies": frequencies, "db": [0.0, 0.0], "phase_rad": [0.0, 0.0], "bulk_delay_samples": 0}
        woofer_response = {"frequencies": frequencies, "db": [0.0, 0.0], "phase_rad": [0.0, 0.0], "bulk_delay_samples": 0}
        reference_phase = -0.45
        desired_phase = -1.0
        phase_reference = {
            "sources": {
                "left": {"frequencies": frequencies, "phase_rad": [0.0, 0.0]},
                "right": {"frequencies": frequencies, "phase_rad": [0.0, 0.0]},
                "woofer": {"frequencies": frequencies, "phase_rad": [reference_phase, reference_phase]},
            }
        }
        measurement_scale = 0.5
        measured_sum = math.sqrt(1.0 + measurement_scale ** 2 + 2.0 * measurement_scale * math.cos(desired_phase))
        combined_response = {
            "frequencies": frequencies,
            "db": [20.0 * math.log10(measured_sum)] * 2,
            "phase_rad": [0.0, 0.0],
            "bulk_delay_samples": 0,
            "measurement_quality": {"snr_db": 30.0},
        }
        constrained_front, constrained_woofer, closure = engine.closure_constrained_acoustic_pair(
            front_response,
            woofer_response,
            combined_response,
            phase_reference,
            "left",
            100.0,
            measurement_scale,
        )
        raw_woofer = complex(math.cos(reference_phase), math.sin(reference_phase))
        raw_error = abs(abs(1.0 + measurement_scale * raw_woofer) - measured_sum)
        constrained_error = abs(abs(constrained_front + measurement_scale * constrained_woofer) - measured_sum)
        require(closure["used"], json.dumps(closure, indent=2))
        require(constrained_error < raw_error, f"sum constraint did not improve closure: {constrained_error} >= {raw_error}")
        require(abs(abs(constrained_front) - 1.0) < 1.0e-9, "Front magnitude was mutated")
        require(abs(abs(constrained_woofer) - 1.0) < 1.0e-9, "Woofer magnitude was mutated")

    print(json.dumps({
        "status": "PASS",
        "tone_counts": tone_counts,
        "common_tone_count": len(common_bins),
        "pair_delay_samples": {
            key: value["second_minus_first_delay_samples"] for key, value in result["pairs"].items()
        },
        "period_correlation": result["period_correlation"],
        "minimum_median_snr_db": result["minimum_median_snr_db"],
        "six_capture_closure": {
            "physical_weight": closure["physical_weight"],
            "raw_error": round(raw_error, 8),
            "constrained_error": round(constrained_error, 8),
        },
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
