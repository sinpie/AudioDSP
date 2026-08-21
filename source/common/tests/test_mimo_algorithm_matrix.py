#!/usr/bin/env python3
"""Silent full-value matrix for the experimental Pi4/5 2x4 MIMO designer."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import types


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mimo", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    os.environ["AUDIODSP_TARGET_DIR"] = str(args.target_dir.resolve())
    if os.name == "nt":
        fcntl_stub = types.ModuleType("fcntl")
        fcntl_stub.LOCK_EX = 2
        fcntl_stub.flock = lambda _handle, _operation: None
        sys.modules.setdefault("fcntl", fcntl_stub)
    mimo = load(args.mimo.resolve(), "audiodsp_mimo_algorithm_matrix")
    if os.name == "nt":
        import numpy as np

        class NumpyFFTBackend:
            kind = "numpy-offline-test"

            def close(self) -> None:
                return None

            def rfft(self, values, length: int) -> list[complex]:
                return np.fft.rfft(np.asarray(list(values), dtype=np.float32), n=length).tolist()

            def irfft(self, values, length: int) -> list[float]:
                return np.fft.irfft(np.asarray(values, dtype=np.complex64), n=length).astype(np.float32).tolist()

        native_loader = mimo.load_measurement_engine

        def numpy_loader(path=None):
            engine = native_loader(path)
            engine.FFTBackend = NumpyFFTBackend
            return engine

        mimo.load_measurement_engine = numpy_loader
    report = mimo.self_test(args.measurement.resolve())
    option_matrix = report.get("mimo_option_matrix", [])
    summary = {
        "result": report.get("result"),
        "spatial_weight_consistency": report.get("spatial_weight_consistency"),
        "topologies": [
            {
                "mode": item.get("mode"),
                "application_allowed": item.get("application_allowed"),
                "safe_rejection": item.get("safe_rejection"),
                "model_pass": item.get("model_pass"),
                "core_checks": item.get("core_checks"),
                "independent_positions": item.get("independent_positions"),
                "headroom": item.get("headroom"),
                "causality": item.get("causality"),
                "target_level_normalization": item.get("target_level_normalization"),
                "spatial_weighting": item.get("spatial_weighting"),
                "regularized_condition_number_1": item.get("regularized_condition_number_1"),
                "robust_uncertainty": item.get("robust_uncertainty"),
                "graph_coverage_hz": item.get("graph_coverage_hz"),
            }
            for item in report.get("topologies", [])
        ],
        "option_scenarios": len(option_matrix),
        "structural_failures": [item for item in option_matrix if not item.get("pass")],
        "model_limited_scenarios": sum(not bool(item.get("model_pass")) for item in option_matrix),
        "model_limited_details": [
            {
                "family": item.get("family"),
                "field": item.get("field"),
                "value": item.get("value"),
                "failed_checks": [key for key, value in (item.get("core_checks") or {}).items() if not value],
                "application_status": item.get("application_status"),
            }
            for item in option_matrix if not bool(item.get("model_pass"))
        ],
        "application_statuses": sorted({str(item.get("application_status")) for item in option_matrix}),
        "topology_predictions": {
            str(item.get("mode")): item.get("prediction")
            for item in report.get("topologies", [])
        },
        "first_option_prediction": option_matrix[0].get("prediction") if option_matrix else None,
        "failure_guidance_reset_executed": report.get("remediation_baseline"),
        "paths": report.get("paths"),
        "taps": report.get("taps"),
        "rate": report.get("rate"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if report.get("result") != "PASS" or not summary["spatial_weight_consistency"] or summary["structural_failures"]:
        raise AssertionError("MIMO algorithm matrix failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
