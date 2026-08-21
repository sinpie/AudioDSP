#!/usr/bin/env python3
"""Silent regression for explicit completed-session output-path correction."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def load_engine(path: Path):
    if os.name == "nt" and "fcntl" not in sys.modules:
        fcntl_stub = types.ModuleType("fcntl")
        fcntl_stub.LOCK_EX = 2
        fcntl_stub.flock = lambda _handle, _operation: None
        sys.modules["fcntl"] = fcntl_stub
    spec = importlib.util.spec_from_file_location("audiodsp_output_correction_test", path)
    require(spec is not None and spec.loader is not None, "measurement engine import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="audiodsp-output-correction-") as temporary:
        root = Path(temporary)
        base = root / "measurements"
        session = base / "test-session"
        session.mkdir(parents=True)
        boot_id = root / "boot-id"
        selector = root / "selector.json"
        boot_id.write_text("test-boot\n", encoding="ascii")
        selector.write_text(json.dumps({
            "profile": "speaker",
            "state_byte": "0xe0",
            "source": "offline-test",
            "boot_id": "test-boot",
        }), encoding="utf-8")
        os.environ.update({
            "AUDIODSP_MEASUREMENT_DIR": str(base),
            "AUDIODSP_MEASUREMENT_LOCK": str(root / "measurement.lock"),
            "AUDIODSP_CAL_DIR": str(root / "calibration"),
            "AUDIODSP_TARGET_DIR": str(root / "targets"),
            "AUDIODSP_PREFERENCES_PATH": str(root / "preferences.json"),
            "AUDIODSP_SELECTOR_STATE_PATH": str(selector),
            "AUDIODSP_BOOT_ID_PATH": str(boot_id),
            "AUDIODSP_PLATFORM_CLASS": "test",
        })
        engine = load_engine(arguments.engine.resolve())
        state = {
            "version": 2,
            "state": "built",
            "session_id": "test-session",
            "session_dir": str(session.resolve()),
            "measurement_profile": "headphone",
            "measurement_output": {
                "profile": "headphone",
                "label": engine.OUTPUT_PROFILE_LABELS["headphone"],
                "state_byte": "0x30",
                "source": "hidio_get_input",
            },
            "level_check": {
                "snr_db": 20.0,
                "measurement_profile": "headphone",
                "measurement_output_label": engine.OUTPUT_PROFILE_LABELS["headphone"],
            },
            "result": {
                "measurement_output": {
                    "physical_profile": "headphone",
                    "physical_label": engine.OUTPUT_PROFILE_LABELS["headphone"],
                },
            },
        }
        engine.atomic_json(base / "current.json", state)
        engine.atomic_json(session / "session.json", state)
        engine.atomic_json(session / "Room_Tuning_Report.json", state["result"])

        changed = engine.correct_measurement_output_profile("speaker", "실제 측정은 스피커 출력에서 수행")
        require(changed["changed"] is True, "correction did not report a change")
        require(len(changed["backups"]) == 3, "all persisted JSON files were not backed up")
        require(all(Path(path).is_file() for path in changed["backups"]), "a correction backup is missing")
        current = json.loads((base / "current.json").read_text(encoding="utf-8"))
        saved = json.loads((session / "session.json").read_text(encoding="utf-8"))
        report = json.loads((session / "Room_Tuning_Report.json").read_text(encoding="utf-8"))
        for payload in (current, saved):
            require(payload["measurement_profile"] == "speaker", "top-level measurement path was not corrected")
            require(payload["measurement_output"]["profile"] == "speaker", "bound output metadata was not corrected")
            require(payload["level_check"]["measurement_profile"] == "speaker", "level-check metadata was not corrected")
            require(payload["measurement_output"]["state_byte"] == "0x30", "raw selector evidence was overwritten")
            require(payload["measurement_output_match"] is True, "current physical speaker path did not match")
        require(report["measurement_output"]["physical_profile"] == "speaker", "download report path stayed stale")
        require(current["measurement_output_correction"]["previous_profile"] == "headphone", "audit trail lost the old path")
        require(current["measurement_output_correction"]["preserved_selector_state_byte"] == "0x30", "audit trail lost the raw selector byte")
        unchanged = engine.correct_measurement_output_profile("speaker", "repeat")
        require(unchanged["changed"] is False, "idempotent correction unexpectedly rewrote the session")
    print("PASS: completed-session output profile correction is atomic, backed up, and auditable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
