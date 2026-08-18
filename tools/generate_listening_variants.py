#!/usr/bin/env python3
"""Generate non-applied listening-test FIR pairs from one completed position."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil


VARIANTS = (
    ("01_Harman_Strong_Magnitude_Trim-12", "harman", "strong", -12, "magnitude"),
    ("02_Harman_Strong_Magnitude_Trim-9", "harman", "strong", -9, "magnitude"),
    ("03_Harman_Strong_Magnitude_Trim-6", "harman", "strong", -6, "magnitude"),
    ("04_Harman_Primus360_Magnitude_Trim-9", "harman", "primus360", -9, "magnitude"),
    ("05_Harman_NoExtraBassPreset_Magnitude_Trim-12", "harman", "none", -12, "magnitude"),
    ("06_Flat_Strong_Magnitude_Trim-9", "flat", "strong", -9, "magnitude"),
    ("07_Toole_Strong_Magnitude_Trim-9", "toole", "strong", -9, "magnitude"),
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_engine(path: Path):
    specification = importlib.util.spec_from_file_location("audiodsp_listening_engine", path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--source-session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", type=int, choices=range(1, len(VARIANTS) + 1))
    args = parser.parse_args()

    work = args.output.parent / f".{args.output.name}-work"
    measurements = work / "measurements"
    session = measurements / "single-position-listening-test"
    session.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "AUDIODSP_MEASUREMENT_DIR": str(measurements),
        "AUDIODSP_CAL_DIR": "/var/lib/audiodsp/calibration",
        "AUDIODSP_TARGET_DIR": "/usr/local/share/audiodsp/targets",
        "AUDIODSP_MEASUREMENT_LOCK": str(work / "measurement.lock"),
        "AUDIODSP_AUDIO_LOCK": str(work / "audio.lock"),
        "AUDIODSP_PREFERENCES_PATH": str(work / "preferences.json"),
    })
    engine = load_engine(args.engine)

    measurements_index = []
    for position in range(1, 4):
        for source in ("left", "right", "woofer"):
            source_path = args.source_session / f"p1_{source}_response.json"
            destination = session / f"p{position}_{source}_response.json"
            shutil.copyfile(source_path, destination)
            measurements_index.append({"position": position, "source": source, "response": destination.name})

    base_state = {
        "version": 1,
        "state": "measured",
        "stage": "single-position listening test",
        "progress": 100.0,
        "eta_seconds": None,
        "session_id": "single-position-listening-test",
        "session_dir": str(session),
        "mode": "lrw",
        "sources": ["left", "right", "woofer"],
        "positions_completed": 3,
        "positions_total": 3,
        "orientation": "90",
        "level_dbfs": 0,
        "noise_level_dbfs": -6,
        "woofer_measurement_attenuation_db": -9,
        "sweep_seconds": 4,
        "measurements": measurements_index,
        "validation": None,
        "result": None,
    }
    manifest = {
        "purpose": "listening_test_only",
        "measurement_basis": "position 1 duplicated to satisfy the 3-position build path",
        "warning": "Not a final spatial room calibration. Download/inspect only; nothing was applied.",
        "variants": [],
    }
    selected_variants = list(enumerate(VARIANTS, start=1))
    if args.only:
        selected_variants = [item for item in selected_variants if item[0] == args.only]
    for index, (name, target, preset, trim, phase_mode) in selected_variants:
        state = dict(base_state)
        state["result"] = None
        engine.save_current(state)
        print(f"[{index}/{len(VARIANTS)}] {name}", flush=True)
        engine.build_worker(target, preset, trim, phase_mode, 200)
        result = engine.load_current()["result"]
        variant_dir = args.output / name
        variant_dir.mkdir(parents=True, exist_ok=True)
        front = variant_dir / "Front_LR_32768.wav"
        rear = variant_dir / "Woofer_RL_RR_32768.wav"
        shutil.copyfile(session / result["front"], front)
        shutil.copyfile(session / result["rear"], rear)
        settings = {
            "name": name,
            "target": target,
            "bass_control": preset,
            "woofer_trim_db": trim,
            "phase_mode": phase_mode,
            "phase_cutoff_hz": 200 if phase_mode == "bass" else None,
            "front_sha256": digest(front),
            "rear_sha256": digest(rear),
            "self_validation": result.get("self_validation"),
            "diagnostics": result.get("diagnostics"),
            "woofer_level_control": result.get("woofer_level_control"),
            "phase": {
                "left": result.get("graphs", {}).get("left", {}).get("phase"),
                "right": result.get("graphs", {}).get("right", {}).get("phase"),
                "woofer": result.get("graphs", {}).get("woofer", {}).get("phase"),
            },
        }
        (variant_dir / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest["variants"].append(settings)

    approved = Path("/etc/camilladsp/Harman_StrongBassControl_Stereo_48k_NoPreamp.wav")
    if approved.is_file():
        reference = args.output / "00_Approved_Current_Reference_Stereo.wav"
        shutil.copyfile(approved, reference)
        manifest["approved_reference_sha256"] = digest(reference)
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "README.txt").write_text(
        "AudioDSP listening-test FIR set\n\n"
        "This set uses only the valid center-position L/R/W capture and duplicates it for the build path.\n"
        "It is intended for tonal/phase A-B listening, not final three-position room calibration.\n"
        "Each folder contains a Front stereo FIR and a duplicated RL/RR Woofer stereo FIR.\n"
        "Lower Woofer trim numbers are quieter: -12 dB < -9 dB < -6 dB.\n"
        "No variant was applied to the Raspberry Pi.\n",
        encoding="utf-8",
    )
    archive = shutil.make_archive(str(args.output), "zip", root_dir=args.output)
    print(json.dumps({"output": str(args.output), "archive": archive, "variants_generated": len(selected_variants)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
