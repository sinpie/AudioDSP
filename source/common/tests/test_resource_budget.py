#!/usr/bin/env python3
"""Silent memory-model validation for current and projected AudioDSP matrices."""

from __future__ import annotations

import argparse
import gc
import importlib.util
import json
from pathlib import Path
import tracemalloc


def load(path: Path):
    spec = importlib.util.spec_from_file_location("audiodsp_mimo_budget_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("MIMO engine import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def allocation_probe(paths: int, taps: int) -> dict[str, float | int]:
    """Mirror the generator's three largest live-bank phases with unique objects."""
    fft_length = 1 << (max(taps * 2, 2) - 1).bit_length()
    bins = fft_length // 2 + 1
    tracemalloc.start()
    spectra = [
        [complex((path + 1) * 1.0e-7, (index + 1) * 1.0e-12) for index in range(bins)]
        for path in range(paths)
    ]
    impulses = [
        [float((path + 1) * 1.0e-7 + (index + 1) * 1.0e-12) for index in range(fft_length)]
        for path in range(paths)
    ]
    del spectra
    gc.collect()
    causal = [[value for value in path[:taps]] for path in impulses]
    del impulses
    gc.collect()
    actual_spectra = [
        [complex((path + 1) * 1.0e-7, (index + 1) * 1.0e-12) for index in range(bins)]
        for path in range(paths)
    ]
    current, peak = tracemalloc.get_traced_memory()
    del actual_spectra, causal
    tracemalloc.stop()
    return {
        "paths": paths,
        "fft_length": fft_length,
        "python_live_mib": round(current / (1024 * 1024), 2),
        "python_peak_mib": round(peak / (1024 * 1024), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mimo", type=Path, required=True)
    parser.add_argument("--allocate", action="store_true")
    args = parser.parse_args()
    mimo = load(args.mimo)
    scenarios = {
        "stereo_2x4": mimo.resource_budget(2, 4),
        "five_one_diagonal": mimo.resource_budget(1, 6),
        "five_one_dual_sub_dense_6x7": mimo.resource_budget(6, 7),
    }
    worst = scenarios["five_one_dual_sub_dense_6x7"]
    if worst["filter_generation_planning_mib"] >= 1536:
        raise AssertionError("2 GB generation planning margin failed")
    if worst["runtime_dsp_planning_mib"] >= 512:
        raise AssertionError("2 GB runtime planning margin failed")
    result = {"result": "PASS", "scenarios": scenarios}
    if args.allocate:
        result["allocation_probe_64bit_cpython"] = allocation_probe(42, 32768)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
