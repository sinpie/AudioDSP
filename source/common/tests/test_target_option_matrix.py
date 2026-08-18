#!/usr/bin/env python3
"""Run every selectable SISO FIR option value without audio playback."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types


def load_engine(path: Path):
    if os.name == "nt" and "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.flock = lambda _handle, _operation: None
        sys.modules["fcntl"] = stub
    spec = importlib.util.spec_from_file_location("audiodsp_target_option_matrix", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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

        module.FFTBackend = NumpyFFTBackend
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="audiodsp-target-matrix-") as temporary:
        os.environ["AUDIODSP_MEASUREMENT_DIR"] = temporary
        os.environ["AUDIODSP_TARGET_DIR"] = str(args.target_dir.resolve())
        engine = load_engine(args.engine.resolve())
        report = engine.target_matrix_self_test()
        failures = [
            {
                "family": item.get("family"),
                "option": item.get("option"),
                "value": item.get("value"),
                "target_status": item.get("target_status"),
                "failed_semantics": [key for key, value in item.get("semantic_checks", {}).items() if not value],
            }
            for item in report.get("option_value_matrix", {}).get("matrix", [])
            if not item.get("pass")
        ]
        summary = {
            "result": report.get("result"),
            "target_preset_combinations": report.get("combinations"),
            "option_scenarios": report.get("option_value_matrix", {}).get("scenarios"),
            "option_result": report.get("option_value_matrix", {}).get("result"),
            "failure_guidance_reset_executed": report.get("failure_guidance_reset"),
            "failures": failures,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        if report.get("result") != "PASS" or failures:
            raise AssertionError("SISO target/option matrix failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
