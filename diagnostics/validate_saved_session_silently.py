#!/usr/bin/env python3
"""Recalculate a saved AudioDSP response session without opening audio devices."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
from typing import Any


def load_engine(path: Path):
    if os.name == "nt" and "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.flock = lambda _handle, _operation: None
        sys.modules["fcntl"] = stub
    spec = importlib.util.spec_from_file_location("audiodsp_saved_session_validation", path)
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


def scenarios() -> list[tuple[str, dict[str, Any]]]:
    baseline = {
        "target": "flat", "preset": "none", "woofer_trim_db": 0,
        "phase_mode": "magnitude", "phase_cutoff": 200, "spatial_mode": "equal",
        "bass_tilt_db": 0, "treble_tilt_db": 0,
        "correction_low_hz": 20, "correction_high_hz": 20_000,
        "max_boost_db": 6, "max_cut_db": 18,
        "crossover_enabled": True, "crossover_frequency_hz": 100,
    }
    requests = [
        ("flat-baseline", {}),
        ("harman-baseline", {"target": "harman"}),
        ("bass-phase", {"phase_mode": "bass"}),
        ("crossover-off", {"crossover_enabled": False}),
        ("crossover-60", {"crossover_frequency_hz": 60}),
        ("crossover-80", {"crossover_frequency_hz": 80}),
        ("crossover-120", {"crossover_frequency_hz": 120}),
        ("trim-minus-4", {"woofer_trim_db": -4}),
        ("trim-minus-9", {"woofer_trim_db": -9}),
        ("primus360", {"preset": "primus360"}),
        ("strong-control", {"preset": "strong"}),
        ("night-voicing", {"bass_tilt_db": -2, "woofer_trim_db": -3}),
    ]
    result = []
    for name, update in requests:
        options = dict(baseline)
        options.update(update)
        result.append((name, options))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="print the gate and one compact row per scenario instead of full diagnostics",
    )
    args = parser.parse_args()
    source_session = args.session.resolve()
    source_state = json.loads((source_session / "session.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="audiodsp-saved-session-") as temporary:
        root = Path(temporary)
        session = root / "session"
        session.mkdir()
        os.environ["AUDIODSP_MEASUREMENT_DIR"] = str(root)
        os.environ["AUDIODSP_TARGET_DIR"] = str(args.target_dir.resolve())
        os.environ["AUDIODSP_PREFERENCES_PATH"] = str(root / "correction-preferences.json")
        os.environ["AUDIODSP_MEASUREMENT_LOCK"] = str(root / "measurement.lock")
        os.environ["AUDIODSP_AUDIO_LOCK"] = str(root / "audio.lock")
        engine = load_engine(args.engine.resolve())
        positions = int(source_state.get("positions_total", 3))
        sources = list(source_state.get("sources") or ())
        if not sources:
            raise RuntimeError("saved session has no source routing metadata")
        for position in range(1, positions + 1):
            for source in sources:
                name = f"p{position}_{source}_response.json"
                source_path = source_session / name
                if not source_path.is_file():
                    raise RuntimeError(f"missing response: {source_path}")
                shutil.copy2(source_path, session / name)
        template = copy.deepcopy(source_state)
        template.update({
            "session_dir": str(session), "state": "measured", "stage": "silent saved-session validation",
            "result": None, "post_filter_validation": None, "worker_pid": None,
            "active_pids": [], "preview_profile": None, "applied_profile": None,
        })
        reports = []
        for name, options in scenarios():
            engine.save_current(copy.deepcopy(template))
            engine.build_worker(
                options["target"], options["preset"], options["woofer_trim_db"],
                options["phase_mode"], options["phase_cutoff"], options["spatial_mode"],
                options["bass_tilt_db"], options["treble_tilt_db"],
                options["correction_low_hz"], options["correction_high_hz"],
                options["max_boost_db"], options["max_cut_db"],
                crossover_enabled=options["crossover_enabled"],
                crossover_frequency_hz=options["crossover_frequency_hz"],
            )
            built = engine.load_current()["result"]
            validation = built["self_validation"]
            target_fit = validation.get("target_fit") or {}
            reports.append({
                "name": name, "options": options,
                "overall_pass": validation.get("overall_pass"),
                "core_pass": all((validation.get("core_checks") or {}).values()),
                "independent_positions": validation.get("independent_positions"),
                "target_fit": {key: target_fit.get(key) for key in ("left", "right", "woofer")},
                "crossover_sum": validation.get("crossover_sum"),
                "crossover_phase_reliable": built.get("crossover", {}).get("phase_alignment_reliable"),
                "front_sha256": built.get("front_sha256"),
                "rear_sha256": built.get("rear_sha256"),
            })
        baseline = reports[0]
        baseline_front_fit = baseline["target_fit"]
        structural_pass = all(item["core_pass"] for item in reports)
        target_baseline_pass = all(bool((baseline_front_fit.get(key) or {}).get("pass")) for key in ("left", "right"))
        output = {
            "result": "PASS" if structural_pass and target_baseline_pass else "FAIL",
            "audio_playback": False,
            "source_session": source_session.name,
            "algorithm_revision": engine.RESULT_ALGORITHM_REVISION,
            "scenario_count": len(reports),
            "baseline_apply_gate": {
                "overall_pass": baseline["overall_pass"],
                "crossover_status": (baseline.get("crossover_sum") or {}).get("status"),
                "independent_positions_pass": (baseline.get("independent_positions") or {}).get("pass"),
                "phase_reliable": baseline["crossover_phase_reliable"],
            },
            "scenarios": reports,
        }
        if args.summary_only:
            compact = {
                "result": output["result"],
                "audio_playback": output["audio_playback"],
                "source_session": output["source_session"],
                "algorithm_revision": output["algorithm_revision"],
                "scenario_count": output["scenario_count"],
                "baseline_apply_gate": output["baseline_apply_gate"],
                "scenarios": [
                    {
                        "name": report["name"],
                        "overall_pass": report["overall_pass"],
                        "core_pass": report["core_pass"],
                        "independent_positions_pass": (
                            report.get("independent_positions") or {}
                        ).get("pass"),
                        "crossover_status": (
                            report.get("crossover_sum") or {}
                        ).get("status"),
                        "left_target_pass": (
                            report.get("target_fit", {}).get("left") or {}
                        ).get("pass"),
                        "right_target_pass": (
                            report.get("target_fit", {}).get("right") or {}
                        ).get("pass"),
                        "woofer_target_pass": (
                            report.get("target_fit", {}).get("woofer") or {}
                        ).get("pass"),
                    }
                    for report in reports
                ],
            }
            print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        if output["result"] != "PASS":
            raise AssertionError("saved-session FIR structural/Flat baseline target validation failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
