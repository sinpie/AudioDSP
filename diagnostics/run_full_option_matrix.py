#!/usr/bin/env python3
"""Generate and audit every single-option AudioDSP FIR value on a live Pi.

This is an engineering E2E utility, not a replacement for the Web wizard.  It
starts from one baseline configuration and changes exactly one selectable SISO
correction value at a time.  This covers every value exposed by the UI without
attempting the meaningless multi-million-member Cartesian product.

Run as root after a measurement session has three usable response slots.  For
a fixed-microphone functional test, slots two and three may explicitly reuse
the physical slot-one response, provided the session records that protocol.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any


BASELINE: dict[str, Any] = {
    "target": "flat",
    "preset": "none",
    "woofer_trim_db": 0,
    "phase_mode": "bass",
    "phase_cutoff": 200,
    "spatial_mode": "equal",
    "bass_tilt_db": 0,
    "treble_tilt_db": 0,
    "correction_low_hz": 20,
    "correction_high_hz": 20_000,
    "max_boost_db": 10,
    "max_cut_db": 18,
    "mimo_high_hz": 150,
    "mimo_strength": "balanced",
    "mimo_support_penalty_db": 6,
}

DIMENSIONS: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("target", ("flat", "harman", "rtings", "acoustix", "toole", "bk")),
    ("preset", ("none", "primus360", "strong")),
    ("woofer_trim_db", tuple(range(-18, 1))),
    # Magnitude is one value; bass-phase cutoffs are five distinct values.
    ("phase_choice", ("magnitude", "bass_80", "bass_120", "bass_160", "bass_200", "bass_250")),
    ("spatial_mode", ("equal", "center")),
    ("bass_tilt_db", tuple(range(-6, 7))),
    ("treble_tilt_db", tuple(range(-6, 3))),
    ("correction_low_hz", (20, 30, 40, 60, 80)),
    ("correction_high_hz", (300, 500, 1_000, 5_000, 20_000)),
    ("max_boost_db", (0, 3, 6, 9, 10)),
    ("max_cut_db", (6, 9, 12, 18, 24)),
)


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slug(value: Any) -> str:
    return str(value).replace("-", "m").replace(".", "p")


def phase_choice(options: dict[str, Any]) -> str:
    if options["phase_mode"] == "magnitude":
        return "magnitude"
    return f"bass_{options['phase_cutoff']}"


def variants() -> list[dict[str, Any]]:
    result = [{"id": "baseline", "dimension": "baseline", "value": None, "options": copy.deepcopy(BASELINE)}]
    for dimension, values in DIMENSIONS:
        baseline_value: Any
        if dimension == "phase_choice":
            baseline_value = phase_choice(BASELINE)
        else:
            baseline_value = BASELINE[dimension]
        for value in values:
            if value == baseline_value:
                continue
            options = copy.deepcopy(BASELINE)
            if dimension == "phase_choice":
                if value == "magnitude":
                    options["phase_mode"] = "magnitude"
                else:
                    options["phase_mode"] = "bass"
                    options["phase_cutoff"] = int(str(value).split("_", 1)[1])
            else:
                options[dimension] = value
            result.append({
                "id": f"{dimension}-{slug(value)}",
                "dimension": dimension,
                "value": value,
                "options": options,
            })
    if len(result) != 68:
        raise RuntimeError(f"option coverage changed: expected 68 variants, found {len(result)}")
    if len({item["id"] for item in result}) != len(result):
        raise RuntimeError("variant identifiers are not unique")
    return result


def engine_arguments(engine: Path, options: dict[str, Any]) -> list[str]:
    return [
        "python3", str(engine), "_worker-build",
        str(options["target"]), str(options["preset"]), str(options["woofer_trim_db"]),
        str(options["phase_mode"]), str(options["phase_cutoff"]), str(options["spatial_mode"]),
        str(options["bass_tilt_db"]), str(options["treble_tilt_db"]),
        str(options["correction_low_hz"]), str(options["correction_high_hz"]),
        str(options["max_boost_db"]), str(options["max_cut_db"]),
        str(options["mimo_high_hz"]), str(options["mimo_strength"]),
        str(options["mimo_support_penalty_db"]),
    ]


def copy_result(session: Path, destination: Path, state: dict[str, Any]) -> dict[str, Any]:
    result = state.get("result") or {}
    destination.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    names = {
        "front": result.get("front"),
        "rear": result.get("rear"),
        "report_json": result.get("report_json"),
        "report_md": result.get("report_md"),
    }
    for key, name in names.items():
        if not name:
            continue
        source = session / str(name)
        if not source.is_file():
            raise RuntimeError(f"result file is missing: {source}")
        target_name = {
            "front": "Front_LR_32768.wav",
            "rear": "Woofer_LR_32768.wav",
            "report_json": "Room_Tuning_Report.json",
            "report_md": "Room_Tuning_Report.md",
        }[key]
        target = destination / target_name
        shutil.copy2(source, target)
        copied[key] = target.name
    atomic_json(destination / "result-state.json", state)
    return copied


def check_wav(manager: Path, path: Path) -> dict[str, Any]:
    process = subprocess.run(
        ["python3", str(manager), "validate-wav", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stdout.strip() or f"WAV validation failed: {path}")
    value = json.loads(process.stdout)
    if value.get("frames") != 32_768 or value.get("sample_rate") != 48_000:
        raise RuntimeError(f"unexpected FIR format: {value}")
    if value.get("format") != "float" or value.get("bits") != 32 or value.get("channels") != 2:
        raise RuntimeError(f"unexpected FIR encoding: {value}")
    return value


def restore_baseline(session: Path, current: Path, output: Path, initial: dict[str, Any], variants_count: int) -> None:
    baseline = output / "filters" / "baseline"
    state = json.loads((baseline / "result-state.json").read_text(encoding="utf-8"))
    result = state["result"]
    shutil.copy2(baseline / "Front_LR_32768.wav", session / result["front"])
    shutil.copy2(baseline / "Woofer_LR_32768.wav", session / result["rear"])
    shutil.copy2(baseline / "Room_Tuning_Report.json", session / result["report_json"])
    shutil.copy2(baseline / "Room_Tuning_Report.md", session / result["report_md"])
    state["session_dir"] = str(session)
    state["session_id"] = initial.get("session_id")
    state["measurements"] = initial.get("measurements", [])
    state["measurement_protocol"] = initial.get("measurement_protocol")
    state["level_check"] = initial.get("level_check")
    state["stage"] = f"옵션 FIR {variants_count}개 생성 완료 · 기준 Flat/추가 억제 없음/trim 0 dB/상대 보상 10 dB 결과 선택"
    state["option_matrix"] = {
        "output_dir": str(output),
        "variants": variants_count,
        "baseline": "baseline",
        "completed_unix": time.time(),
    }
    state["updated_unix"] = time.time()
    atomic_json(current, state)
    atomic_json(session / "session.json", state)


def run_variant(
    variant: dict[str, Any],
    *,
    initial: dict[str, Any],
    source_session: Path,
    output: Path,
    engine: Path,
    manager: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    entry: dict[str, Any] = {key: variant[key] for key in ("id", "dimension", "value", "options")}
    destination = output / "filters" / variant["id"]
    work_base = output / "work" / variant["id"]
    work_session = work_base / "session"
    work_session.mkdir(parents=True, exist_ok=True)
    state = copy.deepcopy(initial)
    state.update({
        "session_dir": str(work_session),
        "state": "measured",
        "result": None,
        "worker_pid": None,
        "active_pids": [],
    })
    for position in range(1, 4):
        for source in state["sources"]:
            name = f"p{position}_{source}_response.json"
            source_path = source_session / name
            if not source_path.is_file():
                raise RuntimeError(f"source response is missing: {source_path}")
            os.link(source_path, work_session / name)
    atomic_json(work_base / "current.json", state)
    atomic_json(work_session / "session.json", state)
    environment = dict(os.environ)
    environment.update({
        "AUDIODSP_MEASUREMENT_DIR": str(work_base),
        "AUDIODSP_MEASUREMENT_LOCK": str(work_base / "measurement.lock"),
        "AUDIODSP_AUDIO_LOCK": str(work_base / "audio.lock"),
        "AUDIODSP_PREFERENCES_PATH": str(work_base / "preferences.json"),
    })
    process = subprocess.Popen(
        engine_arguments(engine, variant["options"]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    # _worker-build uses the same launch-race protection as Web-started jobs:
    # the parent must persist the exact child PID before work is allowed.
    state.update({"state": "processing", "worker_pid": process.pid})
    atomic_json(work_base / "current.json", state)
    process_output, _ = process.communicate()
    entry["engine_output"] = process_output.strip()[-4_000:]
    try:
        state = json.loads((work_base / "current.json").read_text(encoding="utf-8"))
        if process.returncode:
            raise RuntimeError(process_output.strip() or "measurement engine returned an error")
        result = state.get("result") or {}
        if state.get("state") != "built" or not result:
            raise RuntimeError(f"engine did not produce a built result: {state.get('state')}")
        if not result.get("self_validation", {}).get("overall_pass"):
            raise RuntimeError("generated FIR failed engine self-validation")
        copied = copy_result(work_session, destination, state)
        front_path = destination / copied["front"]
        rear_path = destination / copied["rear"]
        front_meta = check_wav(manager, front_path)
        rear_meta = check_wav(manager, rear_path)
        if sha256(front_path) != result.get("front_sha256"):
            raise RuntimeError("archived Front FIR SHA does not match engine result")
        if sha256(rear_path) != result.get("rear_sha256"):
            raise RuntimeError("archived Woofer FIR SHA does not match engine result")
        entry.update({
            "status": "PASS",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "files": copied,
            "front": front_meta,
            "rear": rear_meta,
            "front_sha256": result["front_sha256"],
            "rear_sha256": result["rear_sha256"],
            "self_validation": result["self_validation"],
            "target_fit": result["self_validation"].get("target_fit"),
            "time_alignment": result.get("time_alignment"),
            "diagnostics": result.get("diagnostics"),
        })
    except Exception as exc:
        entry.update({
            "status": "FAIL",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": str(exc),
        })
    finally:
        shutil.rmtree(work_base, ignore_errors=True)
    return entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--current", type=Path, default=Path("/var/lib/audiodsp/measurements/current.json"))
    parser.add_argument("--engine", type=Path, default=Path("/usr/local/bin/audiodsp-measurement.py"))
    parser.add_argument("--manager", type=Path, default=Path("/usr/local/bin/audiodsp-profile-manager.py"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--limit", type=int, choices=range(1, 69), default=None, help="preflight only: run the first N variants")
    parser.add_argument("--variant-id", action="append", default=[], help="run only this variant ID; may be repeated for a selective regression")
    args = parser.parse_args()

    if args.limit is not None and args.variant_id:
        parser.error("--limit and --variant-id cannot be combined")

    args.output.mkdir(parents=True, exist_ok=True)
    filters = args.output / "filters"
    filters.mkdir(exist_ok=True)
    initial = json.loads(args.current.read_text(encoding="utf-8"))
    if Path(initial.get("session_dir", "")) != args.session:
        raise RuntimeError("current state does not point to the requested session")
    if int(initial.get("positions_completed", 0)) != 3:
        raise RuntimeError("session does not contain three algorithm response slots")
    if initial.get("measurement_protocol", {}).get("kind") == "fixed_microphone_single_position_e2e":
        if initial["measurement_protocol"].get("physical_generation_sweeps") != {"left": 1, "right": 1, "woofer": 1}:
            raise RuntimeError("fixed-position physical sweep audit is invalid")
    atomic_json(args.output / "current.before-matrix.json", initial)

    full_matrix = variants()
    if args.variant_id:
        by_id = {item["id"]: item for item in full_matrix}
        unknown = sorted(set(args.variant_id) - set(by_id))
        if unknown:
            parser.error(f"unknown --variant-id: {', '.join(unknown)}")
        matrix = [by_id[variant_id] for variant_id in dict.fromkeys(args.variant_id)]
    else:
        matrix = full_matrix[:args.limit] if args.limit is not None else full_matrix
    manifest: dict[str, Any] = {
        "schema": 1,
        "started_unix": time.time(),
        "session": str(args.session),
        "baseline": BASELINE,
        "coverage_method": "baseline plus every single UI SISO option value",
        "cartesian_product_deliberately_not_used": True,
        "parallel_jobs": args.jobs,
        "total": len(matrix),
        "full_matrix_total": len(full_matrix),
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "variants": [],
    }
    progress_path = args.output / "progress.json"
    atomic_json(progress_path, manifest)

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {
            executor.submit(
                run_variant,
                variant,
                initial=initial,
                source_session=args.session,
                output=args.output,
                engine=args.engine,
                manager=args.manager,
            ): variant
            for variant in matrix
        }
        for future in as_completed(pending):
            variant = pending[future]
            try:
                entry = future.result()
            except Exception as exc:
                entry = {key: variant[key] for key in ("id", "dimension", "value", "options")}
                entry.update({"status": "FAIL", "elapsed_seconds": 0.0, "error": str(exc)})
            manifest["variants"].append(entry)
            manifest["completed"] = len(manifest["variants"])
            manifest["passed"] = sum(item["status"] == "PASS" for item in manifest["variants"])
            manifest["failed"] = sum(item["status"] == "FAIL" for item in manifest["variants"])
            manifest["current"] = entry["id"]
            manifest["updated_unix"] = time.time()
            atomic_json(progress_path, manifest)

    if (filters / "baseline" / "result-state.json").is_file():
        restore_baseline(args.session, args.current, args.output, initial, len(matrix))
    manifest["finished_unix"] = time.time()
    manifest["elapsed_seconds"] = round(manifest["finished_unix"] - manifest["started_unix"], 3)
    manifest["unique_front_sha256"] = len({
        item.get("front_sha256") for item in manifest["variants"] if item["status"] == "PASS"
    })
    manifest["unique_rear_sha256"] = len({
        item.get("rear_sha256") for item in manifest["variants"] if item["status"] == "PASS"
    })
    atomic_json(args.output / "manifest.json", manifest)
    atomic_json(progress_path, manifest)
    return 0 if manifest["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
