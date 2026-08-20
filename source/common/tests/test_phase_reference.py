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
        require(all(10 <= count <= 24 for count in tone_counts.values()), f"tone density is not sparse: {tone_counts}")
        all_bins = [item["bin"] for source in metadata["sources"].values() for item in source["bins"]]
        require(len(all_bins) == len(set(all_bins)), "phase source bins overlap")

        length = metadata["block_samples"]
        delays = {"left": 120.0, "right": 132.0, "woofer": 165.0}
        gains = {"left": 1.0, "right": 0.9, "woofer": 1.6}
        captured_spectrum = [0j] * (length // 2 + 1)
        for source, source_metadata in metadata["sources"].items():
            for item in source_metadata["bins"]:
                bin_index = int(item["bin"])
                reference = complex(float(item["real"]), float(item["imag"]))
                rotation = complex(
                    math.cos(-2.0 * math.pi * bin_index * delays[source] / length),
                    math.sin(-2.0 * math.pi * bin_index * delays[source] / length),
                )
                captured_spectrum[bin_index] = reference * gains[source] * rotation
        captured_block = NumpyFFT().irfft(captured_spectrum, length)
        samples = [0.0] * engine.RATE + captured_block * (engine.PHASE_REFERENCE_ANALYSIS_PERIODS + 2)
        calibration = {"frequencies": [10.0, 24_000.0], "corrections": [0.0, 0.0]}
        result = engine.analyze_phase_reference_samples(samples, metadata, calibration, NumpyFFT())
        require(result["reliable"], json.dumps(result, indent=2))
        expected = {"left_right": 12.0, "left_woofer": 45.0, "right_woofer": 33.0}
        for pair, expected_samples in expected.items():
            actual = float(result["pairs"][pair]["second_minus_first_delay_samples"])
            require(abs(actual - expected_samples) <= 2.5, f"{pair} delay {actual} != {expected_samples}")

    print(json.dumps({
        "status": "PASS",
        "tone_counts": tone_counts,
        "pair_delay_samples": {
            key: value["second_minus_first_delay_samples"] for key, value in result["pairs"].items()
        },
        "period_correlation": result["period_correlation"],
        "minimum_median_snr_db": result["minimum_median_snr_db"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
