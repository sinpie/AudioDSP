#!/usr/bin/env python3
"""AudioDSP UMIK-1 measurement, spatial averaging, and 32768-tap FIR builder."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


RATE = 48_000
TAPS = 32_768
POSITIONS = 3
ALLOWED_POSITION_COUNTS = (1, 3)
SESSION_NOTE_MAX = 500


def environment(suffix: str, default: str) -> str:
    """Read an AudioDSP runtime override."""
    return os.environ.get(f"AUDIODSP_{suffix}", default)


BASE = Path(environment("MEASUREMENT_DIR", "/var/lib/audiodsp/measurements"))
CURRENT = BASE / "current.json"
CAL_DIR = Path(environment("CAL_DIR", "/var/lib/audiodsp/calibration"))
TARGET_DIR = Path(environment("TARGET_DIR", "/usr/local/share/audiodsp/targets"))
LOCK = Path(environment("MEASUREMENT_LOCK", "/run/audiodsp-measurement.lock"))
AUDIO_LOCK = Path(environment("AUDIO_LOCK", "/run/audiodsp-audio-exclusive.lock"))
MANAGER = environment("PROFILE_MANAGER", "/usr/local/bin/audiodsp-profile-manager.py")
MIMO_ENGINE = environment("MIMO_ENGINE", "/usr/local/bin/audiodsp-mimo.py")
PREFERENCES = Path(environment("PREFERENCES_PATH", str(BASE.parent / "correction-preferences.json")))
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
APLAY = environment("APLAY", "/usr/bin/aplay")
ARECORD = environment("ARECORD", "/usr/bin/arecord")
SYSTEMCTL = environment("SYSTEMCTL", "/usr/bin/systemctl")
AMIXER = environment("AMIXER", "/usr/bin/amixer")
U7_MIXER = environment("U7_MIXER", "hw:U7")
CAMILLA = environment("CAMILLA", "/usr/local/bin/camilladsp")
CAMILLA_CONFIG = Path(environment("CAMILLA_CONFIG", "/etc/camilladsp/camilladsp.yml"))
CAPTURE_DEVICE = environment("UMIK_DEVICE", "hw:CARD=UMIK1,DEV=0")
PLAYBACK_DEVICE = environment("U7_DEVICE", "audiodsp_announce")
SELECTOR_STATE_PATH = Path(environment("SELECTOR_STATE_PATH", "/var/lib/audiodsp/u7-selector-state.json"))
BOOT_ID_PATH = Path(environment("BOOT_ID_PATH", "/proc/sys/kernel/random/boot_id"))
OUTPUT_PROFILE_LABELS = {
    "speaker": "U7 Speaker output · speaker chain",
    "headphone": "U7 Headphone jack · speaker chain",
}
MEASUREMENT_OUTPUT_GAIN_DB = 0.0
DEFAULT_NOISE_LEVEL_DBFS = -42
DEFAULT_SWEEP_LEVEL_DBFS = -42
DEFAULT_WOOFER_MEASUREMENT_ATTENUATION_DB = -9
MINIMUM_USABLE_SNR_DB = 6.0
RECOMMENDED_SNR_DB = 15.0
PREFLIGHT_TARGET_SNR_DB = MINIMUM_USABLE_SNR_DB
POST_VALIDATION_SWEEP_SECONDS = 28
POST_VALIDATION_SILENT_LEAD_SECONDS = 2.0
FIR_IMPLEMENTATION_MAE_LIMIT_DB = 0.25
FIR_IMPLEMENTATION_P95_LIMIT_DB = 0.80
RESULT_ALGORITHM_REVISION = "2026-08-21-post-validation-settle-v23"
RESPONSE_ALGORITHM_REVISION = "2026-08-21-power-domain-smoothing-v1"
SMOOTHING_NAME = "power-domain variable 1/12 octave <200 Hz; 1/6 octave 200-2000 Hz; 1/3 octave >2 kHz"
LEGACY_SMOOTHING_NAMES = frozenset((
    "variable 1/12 octave <200 Hz; 1/6 octave 200-2000 Hz; 1/3 octave >2 kHz",
))
PHASE_CLOCK_SHARED = environment("PHASE_CLOCK_SHARED", "0") == "1"
WOOFER_MEASUREMENT_ATTENUATION_DB = float(environment(
    "WOOFER_MEASUREMENT_ATTENUATION_DB",
    str(DEFAULT_WOOFER_MEASUREMENT_ATTENUATION_DB),
))
if not -18.0 <= WOOFER_MEASUREMENT_ATTENUATION_DB <= 0.0:
    raise RuntimeError("WOOFER_MEASUREMENT_ATTENUATION_DB must be between -18 and 0 dB")
WOOFER_MEASUREMENT_SCALE = 10.0 ** (WOOFER_MEASUREMENT_ATTENUATION_DB / 20.0)
PHASE_REFERENCE_BLOCK = 16_384
# Legacy disjoint-bin recordings carry their own 32768/6/4 values in JSON;
# these defaults describe new Walsh-coded captures only.
PHASE_REFERENCE_PERIODS = 6
PHASE_REFERENCE_ANALYSIS_PERIODS = 4
PHASE_REFERENCE_WALSH_CODES = (
    (1.0, 1.0, 1.0),
    (1.0, -1.0, -1.0),
    (-1.0, 1.0, -1.0),
    (-1.0, -1.0, 1.0),
)
# Keep one settling/guard period at both ends and average four complete
# interior periods.  Compared with the previous two-period estimator this
# improves random-noise rejection without changing the signal level or tone
# set; the extra playback time is about 2.7 seconds at 48 kHz.
PHASE_REFERENCE_STATE_PERIODS = 6
PHASE_REFERENCE_STATE_ANALYSIS_INDICES = (1, 2, 3, 4)
# Three same-frequency acoustic branches can add by 9.54 dB.  Ten dB of
# per-branch headroom therefore keeps the coded reference no louder than the
# configured ESS even in the all-positive Walsh state.
PHASE_REFERENCE_HEADROOM_DB = 10.0
PHASE_REFERENCE_MIN_CORRELATION = 0.65
PHASE_REFERENCE_MAX_PHASE_P90_DEG = 45.0
ALLOWED_NOISE_LEVELS = tuple(range(-54, -5))
ALLOWED_SWEEP_LEVELS = tuple(range(-54, 1))
ALLOWED_WOOFER_MEASUREMENT_ATTENUATIONS = tuple(range(-18, 1))
# Compatibility name for older tests and API clients.
ALLOWED_LEVELS = ALLOWED_SWEEP_LEVELS
ALLOWED_DURATIONS = (2, 4, 6, 8, 10, 12, 14)
SOURCES = {
    # Shared-filter SISO measures the system exactly as it is heard: each Front
    # channel and its matching Rear/woofer channel play together.
    "lr": ("left_woofer", "right_woofer"),
    "lrw": ("left", "right", "woofer"),
    # Precision SISO captures the three independent transfer paths and both
    # physical sums at every microphone position.  The sum captures are used
    # only to verify the linear complex model; they are never corrected again
    # or averaged into L/R/W, which avoids double counting.
    "lrw_sum": ("left", "right", "woofer", "left_woofer", "right_woofer"),
    "mimo_stereo": ("front_left", "front_right"),
    "mimo_one_sub": ("front_left", "front_right", "sub_pair"),
    "mimo_dual_sub": ("front_left", "front_right", "sub_left", "sub_right"),
}
SOURCE_LABELS = {
    "left": "프런트 L (우퍼 음소거)",
    "right": "프런트 R (우퍼 음소거)",
    "woofer": "우퍼 단독",
    "left_woofer": "L + 우퍼",
    "right_woofer": "R + 우퍼",
    "phase_reference": "L/R/우퍼 동시 위상 기준",
    "front_left": "Front L actuator",
    "front_right": "Front R actuator",
    "sub_pair": "T5S single-sub actuator",
    "sub_left": "Independent Sub 1",
    "sub_right": "Independent Sub 2",
}
SUBWOOFER_ONLY_SOURCES = frozenset(("woofer", "sub_pair", "sub_left", "sub_right"))
MIMO_MODES = tuple(mode for mode in SOURCES if mode.startswith("mimo_"))
SEPARATE_WOOFER_MODES = frozenset(("lrw", "lrw_sum"))
PREMEASURED_SUM_MODES = frozenset(("lrw_sum",))
TARGET_FILES = {
    "flat": None,
    "harman": "target_Harman_Kardon.txt",
    "rtings": "target_RTings.txt",
    "acoustix": "target_AcoustiX.txt",
    "toole": "target_Not_Dr_Toole.txt",
    "bk": "target_Bruel_Kjaer.txt",
}
PHASE_CUTOFFS = (80, 120, 160, 200, 250)
CROSSOVER_FREQUENCIES = (60, 70, 80, 90, 100, 120)
MAX_PHASE_SHIFT = 2048
MAX_PLAUSIBLE_BULK_DELAY_SAMPLES = RATE // 4
DEFAULT_CORRECTION_PREFERENCES = {
    "target": "flat",
    # Baseline must mean exactly the selected acoustic target. Optional bass
    # suppression and trim are explicit deltas applied only after that baseline.
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
    "crossover_enabled": True,
    "crossover_frequency_hz": 100,
    "mimo_high_hz": 150,
    "mimo_strength": "balanced",
    "mimo_support_penalty_db": 6,
}


class MeasurementError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def measurement_worker_alive(value: dict[str, Any]) -> bool:
    """Verify that a stored PID still belongs to this engine's worker."""
    pid = value.get("worker_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return time.time() < float(value.get("worker_launch_pending_until", 0.0))
    try:
        os.kill(pid, 0)
    except PermissionError:
        # A non-root maintenance/status command cannot signal a root-owned
        # web worker even with signal 0. EPERM proves that the PID exists; the
        # /proc command-line check below still verifies that it is ours.
        pass
    except (OSError, ValueError):
        return False
    command_line = Path(f"/proc/{pid}/cmdline")
    if command_line.is_file():
        try:
            command = command_line.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except OSError:
            return False
        return Path(__file__).name in command and "_worker-" in command
    # Non-Linux unit tests have no procfs. A live PID is the best available
    # evidence there; production Raspberry Pi always takes the branch above.
    return True


def recover_interrupted_worker(value: dict[str, Any]) -> dict[str, Any]:
    """Return a non-mutating error view when an async worker disappeared."""
    if value.get("state") not in ("running", "processing", "cancelling"):
        return value
    if measurement_worker_alive(value):
        return value
    recovered = dict(value)
    previous_state = str(value.get("state"))
    recovered.update({
        "state": "error",
        "stage": "이전 측정 작업이 비정상 종료되었습니다. 저장된 녹음은 유지됩니다.",
        "error": "측정 worker가 실행 중 상태를 남긴 채 종료되었습니다. 다시 실행하거나 저장 녹음을 무음 재처리하세요.",
        "eta_seconds": None,
        "worker_pid": None,
        "active_pids": [],
        "interrupted_worker": {"previous_state": previous_state, "detected_unix": time.time()},
    })
    recovered.pop("worker_launch_pending_until", None)
    return recovered


def normalize_level_check(
    level_check: dict[str, Any],
    configured_sweep_level_dbfs: int | None = None,
    expected_sources: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Apply the current quick-sweep contract to new and saved sessions.

    New level checks measure every selected output using the same routing,
    band and estimator as the full measurement.  Therefore PASS uses the
    same 6 dB usable floor; 15 dB remains a quality recommendation, not a
    second blocking threshold.
    """
    result = dict(level_check)
    measured_snr = float(result.get("snr_db", -300.0))
    measured_peak = float(result.get("peak_dbfs", -300.0))
    checked_level = int(result.get(
        "requested_level_dbfs",
        configured_sweep_level_dbfs if configured_sweep_level_dbfs is not None else DEFAULT_SWEEP_LEVEL_DBFS,
    ))
    configured_level = checked_level if configured_sweep_level_dbfs is None else int(configured_sweep_level_dbfs)

    # Schema-1 level checks could use the white-noise slider for the quick
    # sweep. Project that capture to the configured full-sweep level so a
    # stale PASS cannot unlock measurement at a quieter level.
    level_delta = configured_level - checked_level
    assessment_snr = measured_snr + level_delta
    assessment_peak = measured_peak + level_delta
    channels = result.get("channels") if isinstance(result.get("channels"), list) else []
    measured_sources = {str(item.get("source")) for item in channels if isinstance(item, dict)}
    expected = {str(source) for source in (expected_sources or ())}
    coverage_ok = not expected or measured_sources == expected
    if not coverage_ok:
        ok = False
        verdict = "FAIL · 이전 빠른 검사는 현재 측정 구성의 모든 출력을 확인하지 않았습니다. 빠른 검사를 다시 실행하세요."
    elif assessment_peak >= -1.0:
        ok = False
        verdict = "FAIL · 입력 클리핑 위험 · 스윕 출력을 낮추고 다시 검사하세요."
    elif assessment_snr < PREFLIGHT_TARGET_SNR_DB:
        ok = False
        verdict = (
            f"FAIL · 빠른 스윕 최저 SNR {assessment_snr:.1f} dB · "
            f"본 측정과 같은 사용 가능 하한 {PREFLIGHT_TARGET_SNR_DB:.0f} dB에 미달합니다."
        )
    else:
        ok = True
        verdict = (
            f"PASS · 빠른 스윕 최저 {assessment_snr:.1f} dB · 권장 {RECOMMENDED_SNR_DB:.0f} dB 이상"
            if assessment_snr >= RECOMMENDED_SNR_DB else
            f"PASS · 빠른 스윕 최저 {assessment_snr:.1f} dB · 사용 가능, 권장 {RECOMMENDED_SNR_DB:.0f} dB 미만"
        )

    result.update({
        "required_snr_db": PREFLIGHT_TARGET_SNR_DB,
        "minimum_measurement_snr_db": MINIMUM_USABLE_SNR_DB,
        "recommended_measurement_snr_db": RECOMMENDED_SNR_DB,
        "preflight_target_snr_db": PREFLIGHT_TARGET_SNR_DB,
        "preflight_safety_margin_db": 0.0,
        "assessment_snr_db": round(assessment_snr, 2),
        "assessment_peak_dbfs": round(assessment_peak, 2),
        "coverage_ok": coverage_ok,
        "measured_sources": sorted(measured_sources),
        "expected_sources": sorted(expected),
        "quality_recommended": coverage_ok and assessment_peak < -1.0 and assessment_snr >= RECOMMENDED_SNR_DB,
        "ok": ok,
        "verdict": verdict,
    })

    if configured_sweep_level_dbfs is not None:
        required_raise = max(0, int(math.ceil(PREFLIGHT_TARGET_SNR_DB - assessment_snr)))
        quality_raise = max(0, int(math.ceil(RECOMMENDED_SNR_DB - assessment_snr)))
        safe_raise = max(0, int(math.floor(-6.0 - assessment_peak)))
        applied_raise = min(required_raise, safe_raise)
        if not coverage_ok:
            recommended_level = configured_level
            action = "2 · 출력 설정과 빠른 검사에서 현재 측정 구성의 모든 출력 조합을 다시 검사하세요."
        elif assessment_peak >= -1.0:
            recommended_level = max(-54, min(0, configured_level + int(math.floor(-6.0 - assessment_peak))))
            action = f"2 · 레벨 확인에서 스윕 출력을 {configured_level} → {recommended_level} dBFS로 낮추고 다시 검사하세요."
        elif required_raise:
            recommended_level = max(-54, min(0, configured_level + applied_raise))
            if applied_raise >= required_raise:
                action = (
                    f"2 · 레벨 확인에서 스윕 출력을 {configured_level} → {recommended_level} dBFS "
                    f"(+{applied_raise} dB)로 올리고 빠른 스윕을 다시 실행하세요."
                )
            else:
                action = (
                    f"입력 여유를 고려한 스윕 안전 상한은 {recommended_level} dBFS(+{applied_raise} dB)입니다. "
                    f"그래도 {PREFLIGHT_TARGET_SNR_DB:.0f} dB가 안 되면 주변 소음을 줄이거나 마이크/기기 레벨을 확인하세요."
                )
        elif quality_raise:
            optional_raise = min(quality_raise, safe_raise)
            optional_level = max(-54, min(0, configured_level + optional_raise))
            recommended_level = optional_level
            action = (
                f"PASS · 현재 {configured_level} dBFS로 본 측정 가능. 권장 {RECOMMENDED_SNR_DB:.0f} dB 품질이 필요하면 "
                f"{optional_level} dBFS(+{optional_raise} dB)까지 단계적으로 올릴 수 있습니다."
            )
        else:
            recommended_level = configured_level
            action = f"현재 스윕 출력 {configured_level} dBFS를 유지하세요."
        result.update({
            "recommended_sweep_level_dbfs": recommended_level,
            "recommended_raise_db": max(0, recommended_level - configured_level),
            "required_raise_db": required_raise,
            "quality_raise_db": quality_raise,
            "level_action": action,
        })
    return result


def load_current() -> dict[str, Any]:
    if not CURRENT.is_file():
        selector = output_selector_status()
        return {
            "state": "idle",
            "stage": "새 측정을 시작하세요.",
            "progress": 0.0,
            "eta_seconds": None,
            "umik_connected": umik_connected(),
            "installed_calibrations": calibration_inventory(),
            "correction_preferences": load_correction_preferences(),
            "capabilities": platform_capabilities(),
            "output_selector": selector,
            "measurement_profile": None,
            "measurement_acquisition_revision": None,
            "measurement_output_match": None,
            "measurement_sweep_output": {"active": False, "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB},
            "session_note": "",
        }
    try:
        value = json.loads(CURRENT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasurementError(f"측정 상태 파일 오류: {exc}") from exc
    # Schema-1 sessions used one level for both signals and a process-wide
    # Woofer attenuation. Keep them readable without rewriting their files.
    value.setdefault("noise_level_dbfs", value.get("level_dbfs", DEFAULT_NOISE_LEVEL_DBFS))
    value.setdefault("woofer_measurement_attenuation_db", int(WOOFER_MEASUREMENT_ATTENUATION_DB))
    value.setdefault("measurement_profile", None)
    value.setdefault("measurement_output", None)
    value.setdefault("measurement_acquisition_revision", None)
    value.setdefault("measurement_sweep_output", {"active": False, "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB})
    level_check = value.get("level_check")
    if isinstance(level_check, dict) and isinstance(level_check.get("snr_db"), (int, float)):
        value["level_check"] = normalize_level_check(
            level_check,
            int(value.get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS)),
            list(value.get("sources", ())),
        )
    session_directory = Path(str(value.get("session_dir", "")))
    note_path = session_directory / "session-note.txt"
    if value.get("session_dir") and note_path.is_file():
        try:
            value["session_note"] = note_path.read_text(encoding="utf-8")[:SESSION_NOTE_MAX].strip()
        except OSError:
            value.setdefault("session_note", "")
    else:
        value.setdefault("session_note", "")
    value = recover_interrupted_worker(value)
    value["umik_connected"] = umik_connected()
    value["installed_calibrations"] = calibration_inventory()
    value["correction_preferences"] = load_correction_preferences()
    value["capabilities"] = platform_capabilities()
    selector = output_selector_status()
    value["output_selector"] = selector
    measured_profile = value.get("measurement_profile")
    value["measurement_output_match"] = (
        None if measured_profile not in OUTPUT_PROFILE_LABELS
        else bool(not selector.get("stale") and selector.get("profile") == measured_profile)
    )
    if session_directory.is_dir():
        level_sources = list(value.get("sources", []))
        level_files = [
            {
                "source": source,
                "sweep": f"level_check_{source}_sweep.wav",
                "recorded": f"level_check_{source}_recorded.wav",
            }
            for source in level_sources
            if (session_directory / f"level_check_{source}_sweep.wav").is_file()
            and (session_directory / f"level_check_{source}_recorded.wav").is_file()
        ]
        value["level_recording_inventory"] = {
            "expected": len(level_sources),
            "complete_count": len(level_files),
            "files": level_files,
            "can_reprocess_all": bool(level_sources) and len(level_files) == len(level_sources),
        }
        expected = [
            (position, source)
            for position in range(1, session_position_count(value) + 1)
            for source in value.get("sources", [])
        ]
        raw = [
            {"position": position, "source": source, "file": f"p{position}_{source}_recorded.wav"}
            for position, source in expected
            if (session_directory / f"p{position}_{source}_recorded.wav").is_file()
        ]
        responses = [
            {"position": position, "source": source, "file": f"p{position}_{source}_response.json"}
            for position, source in expected
            if (session_directory / f"p{position}_{source}_response.json").is_file()
        ]
        response_revisions = []
        for item in responses:
            try:
                payload = json.loads((session_directory / item["file"]).read_text(encoding="utf-8"))
                response_revisions.append(str(payload.get("response_algorithm_revision") or "legacy-db-domain-smoothing"))
            except (OSError, ValueError, TypeError):
                response_revisions.append("unreadable-response")
        current_response_count = sum(revision == RESPONSE_ALGORITHM_REVISION for revision in response_revisions)
        value["capture_inventory"] = {
            "expected": len(expected),
            "raw_count": len(raw),
            "response_count": len(responses),
            "raw": raw,
            "responses": responses,
            "can_reprocess_all": bool(expected) and len(raw) == len(expected),
            "expected_response_revision": RESPONSE_ALGORITHM_REVISION,
            "current_response_count": current_response_count,
            "legacy_response_count": len(responses) - current_response_count,
            "needs_algorithm_reprocess": bool(responses) and current_response_count < len(responses),
        }
        phase_expected = (
            list(range(1, session_position_count(value) + 1))
            if value.get("mode") in SEPARATE_WOOFER_MODES else []
        )
        phase_raw = [
            {
                "position": position,
                "recording": f"p{position}_phase_reference_recorded.wav",
                "signal": f"p{position}_phase_reference_signal.json",
            }
            for position in phase_expected
            if (session_directory / f"p{position}_phase_reference_recorded.wav").is_file()
            and (session_directory / f"p{position}_phase_reference_signal.json").is_file()
        ]
        phase_results = [
            item for item in value.get("phase_references", [])
            if isinstance(item, dict)
            and (session_directory / str(item.get("result", ""))).is_file()
        ]
        value["phase_capture_inventory"] = {
            "expected": len(phase_expected),
            "raw_count": len(phase_raw),
            "result_count": len(phase_results),
            "reliable_count": sum(bool(item.get("reliable")) for item in phase_results),
            "raw": phase_raw,
            "results": phase_results,
            "can_reprocess_all": len(phase_raw) == len(phase_expected),
        }
    return value


def normalize_correction_preferences(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MeasurementError("보정 기본 설정은 JSON object여야 합니다.")
    result = dict(DEFAULT_CORRECTION_PREFERENCES)
    checks = {
        "target": tuple(TARGET_FILES), "preset": ("none", "primus360", "strong"),
        "woofer_trim_db": tuple(range(-18, 1)), "phase_mode": ("magnitude", "bass"),
        "phase_cutoff": PHASE_CUTOFFS, "spatial_mode": ("equal", "center"),
        "bass_tilt_db": tuple(range(-6, 7)), "treble_tilt_db": tuple(range(-6, 3)),
        "correction_low_hz": (20, 30, 40, 60, 80),
        "correction_high_hz": (300, 500, 1000, 5000, 20_000),
        "max_boost_db": (0, 3, 6, 9, 10), "max_cut_db": (6, 9, 12, 18, 24),
        "crossover_enabled": (False, True),
        "crossover_frequency_hz": CROSSOVER_FREQUENCIES,
        "mimo_high_hz": (80, 120, 150),
        "mimo_strength": ("safe", "balanced", "maximum"),
        "mimo_support_penalty_db": (3, 6, 9, 12),
    }
    for key, allowed in checks.items():
        if key in value:
            candidate = value[key]
            if candidate not in allowed or (isinstance(candidate, bool) and key != "crossover_enabled"):
                raise MeasurementError(f"보정 기본 설정이 잘못되었습니다: {key}")
            result[key] = candidate
    if result["correction_low_hz"] >= result["correction_high_hz"]:
        raise MeasurementError("보정 기본 주파수 범위가 잘못되었습니다.")
    return result


def load_correction_preferences() -> dict[str, Any]:
    if not PREFERENCES.is_file():
        return dict(DEFAULT_CORRECTION_PREFERENCES)
    try:
        value = json.loads(PREFERENCES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasurementError(f"보정 기본 설정 파일 오류: {exc}") from exc
    return normalize_correction_preferences(value)


def save_correction_preferences(value: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_correction_preferences(value)
    atomic_json(PREFERENCES, normalized)
    return normalized


def save_current(value: dict[str, Any]) -> None:
    value["updated_unix"] = time.time()
    atomic_json(CURRENT, value)


def session_directory(session_id: str) -> Path:
    """Resolve an exact saved session directory without accepting traversal."""
    candidate_name = str(session_id).strip()
    if not candidate_name or Path(candidate_name).name != candidate_name:
        raise MeasurementError("Session ID가 잘못되었습니다.")
    base = BASE.resolve()
    candidate = (BASE / candidate_name).resolve()
    if candidate.parent != base or not candidate.is_dir():
        raise MeasurementError("저장된 Session을 찾을 수 없습니다.")
    return candidate


def session_position_count(state: dict[str, Any]) -> int:
    """Return the explicit acoustic coverage selected for this session."""
    count = int(state.get("positions_total", POSITIONS))
    if count not in ALLOWED_POSITION_COUNTS:
        raise MeasurementError("측정 위치 수는 빠른 측정 1위치 또는 표준 측정 3위치여야 합니다.")
    return count


def read_session_note(directory: Path) -> str:
    note_path = directory / "session-note.txt"
    if not note_path.is_file():
        return ""
    try:
        return note_path.read_text(encoding="utf-8")[:SESSION_NOTE_MAX].strip()
    except OSError:
        return ""


def set_session_note(note: str) -> dict[str, Any]:
    normalized = str(note).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > SESSION_NOTE_MAX:
        raise MeasurementError(f"Session 메모는 {SESSION_NOTE_MAX}자까지 입력할 수 있습니다.")
    with LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = load_current()
        if state.get("state") == "idle" or not state.get("session_dir"):
            raise MeasurementError("메모를 저장할 측정 Session이 없습니다.")
        directory = session_directory(str(state.get("session_id", "")))
        expected = Path(str(state["session_dir"])).resolve()
        if directory != expected:
            raise MeasurementError("현재 Session 경로가 저장 목록과 일치하지 않습니다.")
        atomic_text(directory / "session-note.txt", normalized + ("\n" if normalized else ""))
        state["session_note"] = normalized
        save_current(state)
        return {"session_id": state["session_id"], "session_note": normalized, "max_length": SESSION_NOTE_MAX}


def session_integrity(state: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Verify artifacts that justify completed wizard steps before loading."""
    missing: list[str] = []
    measurements = state.get("measurements", [])
    if not isinstance(measurements, list):
        raise MeasurementError("저장된 Session의 측정 목록이 손상되었습니다.")
    for item in measurements:
        if not isinstance(item, dict):
            raise MeasurementError("저장된 Session의 측정 항목이 손상되었습니다.")
        response = str(item.get("response", ""))
        if not response or Path(response).name != response or not (directory / response).is_file():
            missing.append(response or "response.json")
    positions_total = session_position_count(state)
    positions = int(state.get("positions_completed", 0))
    if positions < 0 or positions > positions_total:
        raise MeasurementError("저장된 Session의 완료 위치 수가 잘못되었습니다.")
    expected_sources = tuple(state.get("sources", ()))
    if positions:
        completed = {(int(item.get("position", 0)), str(item.get("source", ""))) for item in measurements}
        for position in range(1, positions + 1):
            for source in expected_sources:
                if (position, str(source)) not in completed:
                    missing.append(f"p{position}_{source}_response.json")
        if state.get("phase_reference_acquisition_revision") in (
            "simultaneous-multisine-v1", "simultaneous-walsh-v2",
        ):
            references = {
                int(item.get("position", 0)): item
                for item in state.get("phase_references", [])
                if isinstance(item, dict)
            }
            for position in range(1, positions + 1):
                item = references.get(position, {})
                result_name = str(item.get("result", ""))
                if not result_name or Path(result_name).name != result_name or not (directory / result_name).is_file():
                    missing.append(f"p{position}_phase_reference.json")
    result = state.get("result")
    if result:
        if not isinstance(result, dict):
            raise MeasurementError("저장된 Session의 FIR 결과가 손상되었습니다.")
        for key in ("front", "rear"):
            filename = result.get(key)
            if filename and (Path(str(filename)).name != str(filename) or not (directory / str(filename)).is_file()):
                missing.append(str(filename))
    if missing:
        shown = ", ".join(sorted(set(missing))[:6])
        raise MeasurementError(f"Session 완료 상태를 뒷받침하는 파일이 없습니다: {shown}")
    return {
        "positions_completed": positions,
        "positions_total": positions_total,
        "measurement_count": len(measurements),
        "phase_reference_count": len(state.get("phase_references", [])),
        "has_result": bool(result),
        "has_level_check": bool(state.get("level_check")),
    }


def list_sessions() -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    active_id = None
    try:
        active_id = load_current().get("session_id")
    except MeasurementError:
        pass
    if BASE.is_dir():
        for directory in BASE.iterdir():
            session_path = directory / "session.json"
            if not directory.is_dir() or not session_path.is_file():
                continue
            try:
                state = json.loads(session_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    continue
                session_id = str(state.get("session_id") or directory.name)
                if session_id != directory.name:
                    continue
                result = state.get("result") if isinstance(state.get("result"), dict) else None
                sessions.append({
                    "session_id": session_id,
                    "active": session_id == active_id,
                    "created_unix": float(state.get("created_unix", session_path.stat().st_mtime)),
                    "updated_unix": float(state.get("updated_unix", session_path.stat().st_mtime)),
                    "state": str(state.get("state", "ready")),
                    "stage": str(state.get("stage", "")),
                    "mode": str(state.get("mode", "lrw")),
                    "positions_completed": int(state.get("positions_completed", 0)),
                    "positions_total": int(state.get("positions_total", POSITIONS)),
                    "level_ok": bool((state.get("level_check") or {}).get("ok")),
                    "has_result": bool(result),
                    "applied_profile": state.get("applied_profile"),
                    "target": result.get("target") if result else None,
                    "preset": result.get("preset") if result else None,
                    "note": read_session_note(directory),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    sessions.sort(key=lambda item: (item["created_unix"], item["session_id"]), reverse=True)
    return {"active_session_id": active_id, "sessions": sessions}


def load_session(session_id: str) -> dict[str, Any]:
    current = load_current()
    if current.get("state") in ("running", "processing", "cancelling"):
        raise MeasurementError("측정 또는 FIR 계산 중에는 다른 Session을 불러올 수 없습니다.")
    current = restore_preview_if_needed(current)
    with LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        current = load_current()
        if current.get("state") in ("running", "processing", "cancelling"):
            raise MeasurementError("측정 또는 FIR 계산 중에는 다른 Session을 불러올 수 없습니다.")
        if current.get("session_dir"):
            active_directory = Path(str(current["session_dir"]))
            if active_directory.is_dir():
                atomic_json(active_directory / "session.json", current)
        directory = session_directory(session_id)
        try:
            loaded = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MeasurementError(f"저장된 Session 상태 파일 오류: {exc}") from exc
        if not isinstance(loaded, dict) or str(loaded.get("session_id", directory.name)) != directory.name:
            raise MeasurementError("저장된 Session 식별자가 손상되었습니다.")
        integrity = session_integrity(loaded, directory)
        loaded["session_id"] = directory.name
        loaded["session_dir"] = str(directory)
        loaded["session_note"] = read_session_note(directory)
        loaded["preview_active"] = False
        loaded["preview_profile"] = None
        loaded["worker_pid"] = None
        loaded["active_pids"] = []
        loaded["cancel_requested"] = False
        loaded.pop("worker_launch_pending_until", None)
        if loaded.get("state") in ("running", "processing", "cancelling"):
            loaded["state"] = "built" if loaded.get("result") else ("measured" if int(loaded.get("positions_completed", 0)) == session_position_count(loaded) else "ready")
            loaded["stage"] = "저장된 완료 지점에서 Session을 불러왔습니다."
        save_current(loaded)
        return {"session_id": directory.name, "integrity": integrity, "state": loaded}


def delete_session(session_id: str) -> dict[str, Any]:
    """Delete one exact saved session without touching installed profile FIRs."""
    current = load_current()
    if current.get("state") in ("running", "processing", "cancelling"):
        raise MeasurementError("측정 또는 FIR 계산 중에는 Session을 삭제할 수 없습니다.")
    deleting_active = str(current.get("session_id", "")) == str(session_id)
    if deleting_active:
        current = restore_preview_if_needed(current)
    with LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        current = load_current()
        if current.get("state") in ("running", "processing", "cancelling"):
            raise MeasurementError("측정 또는 FIR 계산 중에는 Session을 삭제할 수 없습니다.")
        directory = session_directory(session_id)
        raw_directory = BASE / directory.name
        if raw_directory.is_symlink() or directory.is_symlink():
            raise MeasurementError("symbolic link인 Session은 안전을 위해 삭제하지 않습니다.")
        session_path = directory / "session.json"
        try:
            saved = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MeasurementError(f"Session 상태 파일을 검증할 수 없어 삭제하지 않았습니다: {exc}") from exc
        if not isinstance(saved, dict) or str(saved.get("session_id", "")) != directory.name:
            raise MeasurementError("Session 식별자가 디렉터리와 일치하지 않아 삭제하지 않았습니다.")
        entries = list(directory.rglob("*"))
        if any(entry.is_symlink() for entry in entries):
            raise MeasurementError("Session 안에 symbolic link가 있어 안전을 위해 삭제하지 않았습니다.")
        file_count = sum(entry.is_file() for entry in entries)
        byte_count = sum(entry.stat().st_size for entry in entries if entry.is_file())
        deleting_active = str(current.get("session_id", "")) == directory.name
        shutil.rmtree(directory)
        if deleting_active:
            selector = output_selector_status()
            idle = {
                "state": "idle",
                "stage": "Session 삭제 완료 · 새 측정을 시작하세요.",
                "progress": 0.0,
                "eta_seconds": None,
                "measurement_profile": None,
                "measurement_output_match": None,
                "preview_active": False,
                "preview_profile": None,
                "session_note": "",
                "output_selector": selector,
            }
            save_current(idle)
        return {
            "session_id": directory.name,
            "deleted": True,
            "was_active": deleting_active,
            "files_deleted": file_count,
            "bytes_deleted": byte_count,
            "installed_profiles_changed": False,
        }


def update_current(**changes: Any) -> dict[str, Any]:
    with LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = load_current()
        state.update(changes)
        save_current(state)
        return state


def umik_connected() -> bool:
    cards = Path("/proc/asound/cards")
    return cards.is_file() and "UMIK-1" in cards.read_text(errors="ignore")


def u7_connected() -> bool:
    cards = Path("/proc/asound/cards")
    return cards.is_file() and "Xonar U7" in cards.read_text(errors="ignore")


def output_selector_status() -> dict[str, Any]:
    """Read the physical U7 selector without changing either hardware or DSP."""
    result: dict[str, Any] = {
        "profile": None,
        "label": "U7 physical output not detected",
        "state_byte": None,
        "source": "not_detected",
        "stale": True,
    }
    if not SELECTOR_STATE_PATH.is_file():
        return result
    try:
        saved = json.loads(SELECTOR_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["source"] = "invalid_state_file"
        return result
    profile = saved.get("profile")
    if profile not in OUTPUT_PROFILE_LABELS:
        result["source"] = "invalid_profile"
        return result
    try:
        boot_id = BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        boot_id = ""
    result.update(saved)
    result["label"] = OUTPUT_PROFILE_LABELS[profile]
    result["stale"] = not boot_id or saved.get("boot_id") != boot_id
    return result


def bind_measurement_output(state: dict[str, Any]) -> dict[str, Any]:
    """Bind a session to the physical output used by its level check."""
    selector = output_selector_status()
    profile = selector.get("profile")
    if selector.get("stale") or profile not in OUTPUT_PROFILE_LABELS:
        raise MeasurementError("U7 물리 출력 상태를 확인할 수 없습니다. U7 상단 버튼을 한 번 누르고 다시 시도하세요.")
    if state.get("mode") in MIMO_MODES and profile != "speaker":
        raise MeasurementError("MIMO 4채널 측정은 U7 Speaker output에서만 시작할 수 있습니다.")
    state["measurement_profile"] = profile
    state["measurement_acquisition_revision"] = "u7-pcm-unity-v1"
    state["measurement_output"] = {
        "profile": profile,
        "label": OUTPUT_PROFILE_LABELS[profile],
        "state_byte": selector.get("state_byte"),
        "source": selector.get("source"),
        "boot_id": selector.get("boot_id"),
        "bound_unix": time.time(),
        "sweep_hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
        "listening_volume_ignored_during_sweep": True,
        "safety_order": ["input_off", "u7_pcm_unity", "sweep", "restore_volume", "input_on"],
    }
    state["output_selector"] = selector
    state["measurement_output_match"] = True
    return state


def ensure_measurement_output_path(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fail closed if the U7 path differs from the path that produced the data."""
    state = load_current() if state is None else state
    expected = state.get("measurement_profile")
    if expected not in OUTPUT_PROFILE_LABELS:
        raise MeasurementError("측정 출력 경로가 아직 고정되지 않았습니다. 먼저 레벨 검사를 실행하세요.")
    selector = output_selector_status()
    if selector.get("stale") or selector.get("profile") not in OUTPUT_PROFILE_LABELS:
        raise MeasurementError("U7 물리 출력 상태가 유효하지 않아 측정을 중단했습니다. 저장된 측정값은 유지됩니다.")
    if selector.get("profile") != expected:
        raise MeasurementError(
            f"U7 출력이 측정 경로와 다릅니다: 필요 {OUTPUT_PROFILE_LABELS[expected]}, "
            f"현재 {OUTPUT_PROFILE_LABELS[selector['profile']]}. U7 상단 버튼으로 원래 경로를 선택하세요."
        )
    return selector


def ensure_post_preview_output_path(state: dict[str, Any]) -> dict[str, Any]:
    """Use the bound measurement path, or explicitly match a legacy Preview path."""
    if state.get("measurement_profile") in OUTPUT_PROFILE_LABELS:
        return ensure_measurement_output_path(state)
    selector = output_selector_status()
    expected = state.get("preview_profile")
    if selector.get("stale") or expected not in OUTPUT_PROFILE_LABELS or selector.get("profile") != expected:
        raise MeasurementError("이전 Session은 측정 경로 기록이 없습니다. U7 물리 출력을 Preview 프로필과 같게 선택한 뒤 다시 실행하세요.")
    return selector


def read_u7_pcm_output_volume() -> dict[str, Any]:
    """Read the exact U7 PCM attenuation used after generated sweep samples."""
    try:
        process = subprocess.run(
            [AMIXER, "-D", U7_MIXER, "cget", "numid=6"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError(f"U7 출력 볼륨을 읽을 수 없습니다: {exc}") from exc
    if process.returncode:
        raise MeasurementError(process.stdout.strip() or "U7 출력 볼륨 읽기에 실패했습니다.")
    metadata = re.search(r"values=(\d+),min=(-?\d+),max=(-?\d+)", process.stdout)
    values_line = re.search(r"(?m)^\s*:\s*values=([0-9, -]+)\s*$", process.stdout)
    if metadata is None or values_line is None:
        raise MeasurementError("U7 PCM 출력 볼륨 응답 형식을 확인할 수 없습니다.")
    channel_count, raw_min, raw_max = (int(value) for value in metadata.groups())
    raw_values = [int(value.strip()) for value in values_line.group(1).split(",") if value.strip()]
    if channel_count <= 0 or len(raw_values) != channel_count or raw_max <= raw_min:
        raise MeasurementError("U7 PCM 출력 채널 수 또는 볼륨 범위가 잘못되었습니다.")
    if any(value < raw_min or value > raw_max for value in raw_values):
        raise MeasurementError("U7 PCM 출력 볼륨이 하드웨어 범위를 벗어났습니다.")
    db_metadata = re.search(r"dBminmax-min=(-?\d+),max=(-?\d+)", process.stdout)
    if db_metadata:
        hardware_min_db, hardware_max_db = (int(value) / 100.0 for value in db_metadata.groups())
    else:
        hardware_min_db, hardware_max_db = float(raw_min - raw_max), 0.0
    scale = (hardware_max_db - hardware_min_db) / (raw_max - raw_min)
    channel_db = [hardware_min_db + (value - raw_min) * scale for value in raw_values]
    return {
        "mixer": U7_MIXER,
        "control": "PCM,0 / numid=6",
        "channels": channel_count,
        "raw_min": raw_min,
        "raw_max": raw_max,
        "raw_values": raw_values,
        "uniform": len(set(raw_values)) == 1,
        "channel_db": [round(value, 3) for value in channel_db],
        "actual_db": round(sum(channel_db) / len(channel_db), 3),
        "hardware_min_db": hardware_min_db,
        "hardware_max_db": hardware_max_db,
    }


def set_u7_pcm_output_raw(raw: int) -> dict[str, Any]:
    """Set every U7 playback channel to one validated raw hardware value."""
    current = read_u7_pcm_output_volume()
    if raw < int(current["raw_min"]) or raw > int(current["raw_max"]):
        raise MeasurementError("복원할 U7 출력 볼륨이 하드웨어 범위를 벗어났습니다.")
    try:
        process = subprocess.run(
            [AMIXER, "-D", U7_MIXER, "set", "PCM,0", str(raw)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError(f"U7 출력 볼륨을 적용할 수 없습니다: {exc}") from exc
    if process.returncode:
        raise MeasurementError(process.stdout.strip() or "U7 출력 볼륨 적용에 실패했습니다.")
    applied = read_u7_pcm_output_volume()
    if not applied["uniform"] or any(int(value) != raw for value in applied["raw_values"]):
        raise MeasurementError("U7 전체 출력 채널에 같은 볼륨이 적용되지 않았습니다.")
    return applied


def _set_u7_capture(name: str, enabled: bool) -> None:
    """Apply the U7 capture switch and fail instead of advancing out of order."""
    action = "cap" if enabled else "nocap"
    try:
        process = subprocess.run(
            [AMIXER, "-D", U7_MIXER, "set", name, action],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasurementError(f"U7 {name} 입력 {'복구' if enabled else '차단'} 실패: {exc}") from exc
    if process.returncode:
        raise MeasurementError(
            process.stdout.strip() or f"U7 {name} 입력 {'복구' if enabled else '차단'}에 실패했습니다."
        )


def _camilla_service_active() -> bool:
    return subprocess.run(
        [SYSTEMCTL, "is-active", "--quiet", "camilladsp.service"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _start_camilla_service() -> None:
    subprocess.run([SYSTEMCTL, "start", "camilladsp.service"], check=True, timeout=25)
    if not _camilla_service_active():
        raise MeasurementError("CamillaDSP 입력 경로를 다시 시작하지 못했습니다. U7 볼륨은 복원된 상태입니다.")


def begin_measurement_audio_window(*, require_camilla: bool = False) -> dict[str, Any]:
    """Disconnect normal input first, then force sweep playback to hardware 0 dB.

    The order is deliberately transactional.  A sweep dBFS value is therefore
    the level presented to the U7 DAC, independent of the saved listening
    volume.  Normal input is never reconnected until the original PCM volume
    has been restored and read back successfully.
    """
    window: dict[str, Any] = {
        "camilla_was_active": _camilla_service_active(),
        "input_disconnected": False,
        "volume_snapshot": None,
        "unity_applied": False,
    }
    if require_camilla and not window["camilla_was_active"]:
        raise MeasurementError("CamillaDSP가 실행 중이 아닙니다. Preview를 다시 적용하세요.")
    try:
        if window["camilla_was_active"]:
            subprocess.run([SYSTEMCTL, "stop", "camilladsp.service"], check=True, timeout=20)
            if _camilla_service_active():
                raise MeasurementError("CamillaDSP 입력 경로가 완전히 정지하지 않아 측정음을 재생하지 않습니다.")
            time.sleep(0.75)
        _set_u7_capture("Mic", False)
        _set_u7_capture("Line", False)
        window["input_disconnected"] = True
        snapshot = read_u7_pcm_output_volume()
        if not snapshot["uniform"]:
            raise MeasurementError("U7 출력 채널 볼륨이 서로 다릅니다. 현황의 출력 볼륨을 한 번 적용한 뒤 다시 시도하세요.")
        window["volume_snapshot"] = snapshot
        unity = set_u7_pcm_output_raw(int(snapshot["raw_max"]))
        if abs(float(unity["actual_db"]) - MEASUREMENT_OUTPUT_GAIN_DB) > 0.05:
            raise MeasurementError(f"U7 측정 출력 기준이 0 dB가 아닙니다: {unity['actual_db']:.2f} dB")
        window["unity_applied"] = True
        window["unity"] = unity
        return window
    except Exception:
        snapshot = window.get("volume_snapshot")
        volume_safe = snapshot is None
        if isinstance(snapshot, dict) and snapshot.get("raw_values"):
            try:
                set_u7_pcm_output_raw(int(snapshot["raw_values"][0]))
                volume_safe = True
            except Exception:
                volume_safe = False
        if volume_safe:
            try:
                if window["camilla_was_active"]:
                    _start_camilla_service()
                elif window["input_disconnected"]:
                    _set_u7_capture("Line", True)
            except Exception:
                pass
        raise


def ensure_measurement_audio_window(window: dict[str, Any]) -> dict[str, Any]:
    """Continuously enforce input-off and hardware-unity during sweep playback."""
    ensure_measurement_output_path()
    if _camilla_service_active():
        raise MeasurementError("측정 중 CamillaDSP 입력 경로가 다시 켜져 안전을 위해 재생을 중단했습니다.")
    current = read_u7_pcm_output_volume()
    if not current["uniform"] or any(int(value) != int(current["raw_max"]) for value in current["raw_values"]):
        raise MeasurementError("측정 중 U7 출력 볼륨이 0 dB에서 변경되어 재생을 중단했습니다. 원래 볼륨은 자동 복원됩니다.")
    if not window.get("input_disconnected") or not window.get("unity_applied"):
        raise MeasurementError("측정 오디오 안전 순서가 성립하지 않아 재생을 중단했습니다.")
    return current


def restore_measurement_audio_window(window: dict[str, Any] | None) -> None:
    """Restore PCM first and reconnect normal input only after read-back PASS."""
    if window is None:
        return
    snapshot = window.get("volume_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("raw_values"):
        raise MeasurementError("원래 U7 볼륨 기록이 없어 입력을 다시 연결하지 않습니다.")
    original_raw = int(snapshot["raw_values"][0])
    restored = set_u7_pcm_output_raw(original_raw)
    if not restored["uniform"] or any(int(value) != original_raw for value in restored["raw_values"]):
        raise MeasurementError("원래 U7 볼륨 복원 확인에 실패하여 입력을 다시 연결하지 않습니다.")
    window["volume_restored"] = True
    window["restored"] = restored
    if window.get("camilla_was_active"):
        _start_camilla_service()
    else:
        _set_u7_capture("Line", True)
    window["input_restored"] = True


def platform_capabilities() -> dict[str, Any]:
    override = environment("PLATFORM_CLASS", "").strip().lower()
    if override:
        kind = override
    else:
        machine = platform.machine().lower()
        model_path = Path("/proc/device-tree/model")
        model = model_path.read_text(errors="ignore").strip("\x00") if model_path.is_file() else ""
        if "raspberry pi 2" in model or machine in ("armv6l", "armv7l"):
            kind = "pi2"
        elif "raspberry pi" in model or machine in ("aarch64", "arm64"):
            kind = "pi4plus"
        else:
            kind = "development"
    mimo_compute_supported = kind in ("pi4plus", "development", "test")
    mimo_supported = mimo_compute_supported and PHASE_CLOCK_SHARED
    if not mimo_compute_supported:
        mimo_reason = "Raspberry Pi 4/5 64-bit 전용입니다."
    elif not PHASE_CLOCK_SHARED:
        mimo_reason = "U7 출력과 UMIK-1 입력의 공통 timing reference가 없어 복소 MIMO 생성을 안전하게 차단했습니다."
    else:
        mimo_reason = "사용 가능"
    return {
        "platform_class": kind,
        "mimo_compute_supported": mimo_compute_supported,
        "phase_clock_shared": PHASE_CLOCK_SHARED,
        "mimo_supported": mimo_supported,
        "reason": mimo_reason,
        "mimo_runtime_paths": 8,
        "mimo_minimum": "Raspberry Pi 4 / 64-bit AudioDSP",
        "offline_estimates_seconds": {
            "response_per_channel": 70 if kind == "pi2" else 20,
            "fir_magnitude": 55 if kind == "pi2" else 20,
            "fir_bass_phase": 85 if kind == "pi2" else 40,
            "mimo_bank": None if kind == "pi2" else 240,
        },
    }


def parse_calibration(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise MeasurementError("보정 파일이 없거나 너무 큽니다.")
    frequencies: list[float] = []
    corrections: list[float] = []
    serial = ""
    sensitivity = None
    for raw in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
        line = raw.strip().strip('"')
        if not line:
            continue
        if "SERNO:" in line.upper():
            serial = line.upper().split("SERNO:", 1)[1].strip()
            if "SENS FACTOR" in line.upper():
                try:
                    sensitivity = float(line.split("=", 1)[1].lower().split("db", 1)[0])
                except (ValueError, IndexError):
                    sensitivity = None
            continue
        if line.lower().startswith("auto-generated"):
            continue
        fields = line.replace(",", ".").split()
        if len(fields) < 2:
            continue
        try:
            frequency, correction = float(fields[0]), float(fields[1])
        except ValueError as exc:
            raise MeasurementError(f"보정 파일 행을 읽을 수 없습니다: {raw}") from exc
        if not (1.0 <= frequency <= 96_000.0 and -40.0 <= correction <= 40.0):
            raise MeasurementError("보정 파일 값이 허용 범위를 벗어났습니다.")
        if frequencies and frequency <= frequencies[-1]:
            raise MeasurementError("보정 주파수는 오름차순이어야 합니다.")
        frequencies.append(frequency)
        corrections.append(correction)
    if len(frequencies) < 20:
        raise MeasurementError("유효한 보정 지점이 20개 미만입니다.")
    if serial and serial != "7200660":
        raise MeasurementError(f"UMIK-1 일련번호가 다릅니다: {serial}")
    return {
        "path": str(path),
        "serial": serial or "unknown",
        "sensitivity_db": sensitivity,
        "points": len(frequencies),
        "frequencies": frequencies,
        "corrections": corrections,
    }


def install_calibration(source: Path, orientation: str) -> dict[str, Any]:
    if orientation not in ("0", "90"):
        raise MeasurementError("보정 방향은 0 또는 90이어야 합니다.")
    metadata = parse_calibration(source)
    state = load_current()
    affects_session = state.get("state") != "idle" and state.get("orientation") == orientation
    if affects_session and state.get("state") in ("running", "processing", "cancelling"):
        raise MeasurementError("측정 작업 중에는 현재 세션이 사용하는 마이크 보정 파일을 바꿀 수 없습니다.")
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    target = CAL_DIR / ("7200660_90deg.txt" if orientation == "90" else "7200660.txt")
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
    metadata = parse_calibration(target)
    metadata["orientation"] = orientation
    if affects_session:
        state = invalidate_from_step(load_current(), 1, f"{orientation}° calibration 변경")
        state["calibration"] = {
            key: value for key, value in metadata.items()
            if key not in ("frequencies", "corrections")
        }
        save_current(state)
    return metadata


def calibration_for(orientation: str) -> dict[str, Any]:
    name = "7200660_90deg.txt" if orientation == "90" else "7200660.txt"
    result = parse_calibration(CAL_DIR / name)
    result["orientation"] = orientation
    return result


def calibration_inventory() -> dict[str, Any]:
    inventory: dict[str, Any] = {}
    for orientation in ("0", "90"):
        try:
            metadata = calibration_for(orientation)
            inventory[orientation] = {
                key: value for key, value in metadata.items()
                if key not in ("frequencies", "corrections")
            }
        except Exception as exc:
            inventory[orientation] = {"orientation": orientation, "available": False, "error": str(exc)}
        else:
            inventory[orientation]["available"] = True
    return inventory


def calibration_changed(orientation: str) -> dict[str, Any]:
    if orientation not in ("0", "90"):
        raise MeasurementError("보정 방향은 0 또는 90이어야 합니다.")
    metadata = calibration_for(orientation)
    state = load_current()
    if state.get("state") != "idle" and state.get("orientation") == orientation:
        state = invalidate_from_step(state, 1, f"{orientation}° calibration 복원")
        state["calibration"] = {
            key: value for key, value in metadata.items()
            if key not in ("frequencies", "corrections")
        }
        save_current(state)
        atomic_json(Path(state["session_dir"]) / "session.json", state)
    return {key: value for key, value in metadata.items() if key not in ("frequencies", "corrections")}


def interpolate_log(frequencies: list[float], values: list[float], frequency: float) -> float:
    if frequency <= frequencies[0]:
        return values[0]
    if frequency >= frequencies[-1]:
        return values[-1]
    lo, hi = 0, len(frequencies) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if frequencies[mid] <= frequency:
            lo = mid
        else:
            hi = mid
    x0, x1, x = math.log(frequencies[lo]), math.log(frequencies[hi]), math.log(frequency)
    fraction = (x - x0) / (x1 - x0)
    return values[lo] + fraction * (values[hi] - values[lo])


class FFTBackend:
    def __init__(self) -> None:
        self.kind = "fftw3f"
        name = ctypes.util.find_library("fftw3f") or "libfftw3f.so.3"
        try:
            self.lib = ctypes.CDLL(name)
        except OSError as exc:
            raise MeasurementError("FFTW3 single-precision library is required.") from exc
        self.lib.fftwf_malloc.argtypes = [ctypes.c_size_t]
        self.lib.fftwf_malloc.restype = ctypes.c_void_p
        self.lib.fftwf_free.argtypes = [ctypes.c_void_p]
        self.lib.fftwf_plan_dft_r2c_1d.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self.lib.fftwf_plan_dft_r2c_1d.restype = ctypes.c_void_p
        self.lib.fftwf_plan_dft_c2r_1d.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self.lib.fftwf_plan_dft_c2r_1d.restype = ctypes.c_void_p
        self.lib.fftwf_execute.argtypes = [ctypes.c_void_p]
        self.lib.fftwf_destroy_plan.argtypes = [ctypes.c_void_p]
        self._forward_workspaces: dict[int, tuple[int, int, int]] = {}
        self._inverse_workspaces: dict[int, tuple[int, int, int]] = {}

    def close(self) -> None:
        """Release cached FFTW plans and aligned buffers."""
        for workspaces in (self._forward_workspaces, self._inverse_workspaces):
            for in_pointer, out_pointer, plan in workspaces.values():
                if plan:
                    self.lib.fftwf_destroy_plan(plan)
                if in_pointer:
                    self.lib.fftwf_free(in_pointer)
                if out_pointer:
                    self.lib.fftwf_free(out_pointer)
            workspaces.clear()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _forward_workspace(self, length: int) -> tuple[int, int, int]:
        cached = self._forward_workspaces.get(length)
        if cached is not None:
            return cached
        in_pointer = self.lib.fftwf_malloc(length * 4)
        out_pointer = self.lib.fftwf_malloc((length // 2 + 1) * 8)
        if not in_pointer or not out_pointer:
            if in_pointer:
                self.lib.fftwf_free(in_pointer)
            if out_pointer:
                self.lib.fftwf_free(out_pointer)
            raise MemoryError("FFTW allocation failed")
        plan = self.lib.fftwf_plan_dft_r2c_1d(length, in_pointer, out_pointer, 64)
        if not plan:
            self.lib.fftwf_free(in_pointer)
            self.lib.fftwf_free(out_pointer)
            raise MeasurementError("FFTW forward plan failed")
        cached = (in_pointer, out_pointer, plan)
        self._forward_workspaces[length] = cached
        return cached

    def _inverse_workspace(self, length: int) -> tuple[int, int, int]:
        cached = self._inverse_workspaces.get(length)
        if cached is not None:
            return cached
        in_pointer = self.lib.fftwf_malloc((length // 2 + 1) * 8)
        out_pointer = self.lib.fftwf_malloc(length * 4)
        if not in_pointer or not out_pointer:
            if in_pointer:
                self.lib.fftwf_free(in_pointer)
            if out_pointer:
                self.lib.fftwf_free(out_pointer)
            raise MemoryError("FFTW allocation failed")
        plan = self.lib.fftwf_plan_dft_c2r_1d(length, in_pointer, out_pointer, 64)
        if not plan:
            self.lib.fftwf_free(in_pointer)
            self.lib.fftwf_free(out_pointer)
            raise MeasurementError("FFTW inverse plan failed")
        cached = (in_pointer, out_pointer, plan)
        self._inverse_workspaces[length] = cached
        return cached

    def rfft(self, values: Iterable[float], length: int) -> list[complex]:
        in_pointer, out_pointer, plan = self._forward_workspace(length)
        out_length = length // 2 + 1
        # fftwf_malloc does not zero memory. Inputs shorter than the FFT length
        # (notably a 32768-tap FIR transformed at 65536 points) require exact zero padding.
        ctypes.memset(in_pointer, 0, length * 4)
        ctypes.memset(out_pointer, 0, out_length * 8)
        input_data = (ctypes.c_float * length).from_address(in_pointer)
        for index, value in enumerate(values):
            if index >= length:
                break
            numeric = float(value)
            if not math.isfinite(numeric):
                raise MeasurementError("Non-finite FFT input sample.")
            input_data[index] = numeric
        output_data = (ctypes.c_float * (out_length * 2)).from_address(out_pointer)
        self.lib.fftwf_execute(plan)
        result = [complex(output_data[2 * i], output_data[2 * i + 1]) for i in range(out_length)]
        if not all(math.isfinite(value.real) and math.isfinite(value.imag) for value in result):
            raise MeasurementError("FFTW produced a non-finite forward transform.")
        return result

    def irfft(self, values: list[complex], length: int) -> list[float]:
        in_length = length // 2 + 1
        in_pointer, out_pointer, plan = self._inverse_workspace(length)
        ctypes.memset(in_pointer, 0, in_length * 8)
        ctypes.memset(out_pointer, 0, length * 4)
        input_data = (ctypes.c_float * (in_length * 2)).from_address(in_pointer)
        for index, value in enumerate(values[:in_length]):
            if not math.isfinite(value.real) or not math.isfinite(value.imag):
                raise MeasurementError("Non-finite inverse FFT input bin.")
            input_data[2 * index] = float(value.real)
            input_data[2 * index + 1] = float(value.imag)
        output_data = (ctypes.c_float * length).from_address(out_pointer)
        self.lib.fftwf_execute(plan)
        scale = 1.0 / length
        result = [output_data[i] * scale for i in range(length)]
        if not all(math.isfinite(value) for value in result):
            raise MeasurementError("FFTW produced a non-finite inverse transform.")
        return result


def next_power_of_two(value: int) -> int:
    return 1 << max(1, value - 1).bit_length()


def pack_pcm24(value: float) -> bytes:
    integer = round(max(-1.0, min(0.99999988, value)) * 8_388_608.0)
    if integer < 0:
        integer += 1 << 24
    return bytes((integer & 255, (integer >> 8) & 255, (integer >> 16) & 255))


def generate_sweep_mono(
    level_dbfs: int,
    seconds: int,
    *,
    level_check: bool = False,
    low_hz: float | None = None,
    high_hz: float | None = None,
    lead_seconds: float = 0.35,
) -> tuple[list[float], int]:
    """Generate deterministic mono logarithmic sine sweep samples."""
    lead_seconds = float(lead_seconds)
    if not 0.0 <= lead_seconds <= 5.0:
        raise MeasurementError("Sweep 무음 준비 시간은 0~5초여야 합니다.")
    tail_seconds = 1.0 if level_check else 2.0
    sweep_seconds = float(seconds)
    frames = round((lead_seconds + sweep_seconds + tail_seconds) * RATE)
    amplitude = 10.0 ** (level_dbfs / 20.0)
    default_band = (40.0, 2_000.0) if level_check else (15.0, 22_000.0)
    f1 = default_band[0] if low_hz is None else float(low_hz)
    f2 = default_band[1] if high_hz is None else float(high_hz)
    if not 5.0 <= f1 < f2 <= RATE * 0.48:
        raise MeasurementError(f"Sweep 대역이 잘못되었습니다: {f1:g}~{f2:g} Hz")
    ratio_log = math.log(f2 / f1)
    scale = 2.0 * math.pi * f1 * sweep_seconds / ratio_log
    fade = max(1, round(0.03 * RATE))
    mono = [0.0] * frames
    lead = round(lead_seconds * RATE)
    sweep_frames = round(sweep_seconds * RATE)
    for index in range(sweep_frames):
        t = index / RATE
        envelope = 1.0
        if index < fade:
            envelope *= 0.5 - 0.5 * math.cos(math.pi * index / fade)
        if index >= sweep_frames - fade:
            envelope *= 0.5 - 0.5 * math.cos(math.pi * (sweep_frames - 1 - index) / fade)
        mono[lead + index] = amplitude * envelope * math.sin(scale * (math.exp(t * ratio_log / sweep_seconds) - 1.0))
    return mono, frames


def reference_sweep_for_source(
    source: str,
    level_dbfs: int,
    seconds: int,
    *,
    woofer_attenuation_db: float | None = None,
    level_check: bool = False,
) -> list[float]:
    """Build the deconvolution reference without touching a saved WAV."""
    low_hz, high_hz = sweep_band_for_source(source)
    mono, _ = generate_sweep_mono(
        level_dbfs,
        seconds,
        level_check=level_check,
        low_hz=low_hz,
        high_hz=high_hz,
    )
    if source not in SUBWOOFER_ONLY_SOURCES:
        return mono
    woofer_db = WOOFER_MEASUREMENT_ATTENUATION_DB if woofer_attenuation_db is None else float(woofer_attenuation_db)
    if not -18.0 <= woofer_db <= 0.0:
        raise MeasurementError("우퍼 측정 감쇄는 -18~0 dB여야 합니다.")
    scale = 10.0 ** (woofer_db / 20.0)
    return [value * scale for value in mono]


def sweep_band_for_source(source: str) -> tuple[float, float]:
    """Avoid spending sweep energy where a routed actuator cannot reproduce it."""
    if source in SUBWOOFER_ONLY_SOURCES:
        return 15.0, 320.0
    if source in ("left", "right", "front_left", "front_right", "front_both"):
        return 30.0, 22_000.0
    # Physical L+Woofer/R+Woofer sums must cover both branches.
    return 15.0, 22_000.0


def write_sweep(
    path: Path,
    source: str,
    level_dbfs: int,
    seconds: int,
    *,
    level_check: bool = False,
    woofer_attenuation_db: float | None = None,
) -> list[float]:
    """Write a deterministic logarithmic sweep to one physical channel in 4ch S24_3LE."""
    low_hz, high_hz = sweep_band_for_source(source)
    mono, frames = generate_sweep_mono(
        level_dbfs,
        seconds,
        level_check=level_check,
        low_hz=low_hz,
        high_hz=high_hz,
    )
    woofer_db = WOOFER_MEASUREMENT_ATTENUATION_DB if woofer_attenuation_db is None else float(woofer_attenuation_db)
    if not -18.0 <= woofer_db <= 0.0:
        raise MeasurementError("우퍼 측정 상대레벨은 -18~0 dB여야 합니다.")
    woofer_scale = 10.0 ** (woofer_db / 20.0)
    data_bytes = frames * 4 * 3
    with path.open("wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE")
        handle.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 4, RATE, RATE * 12, 12, 24))
        handle.write(b"data" + struct.pack("<I", data_bytes))
        zero = b"\x00\x00\x00"
        buffer = bytearray()
        for value in mono:
            if source in ("left", "front_left"):
                frame = pack_pcm24(value) + zero + zero + zero
            elif source in ("right", "front_right"):
                frame = zero + pack_pcm24(value) + zero + zero
            elif source in ("woofer", "sub_pair"):
                rear = pack_pcm24(value * 0.5 * woofer_scale)
                frame = zero + zero + rear + rear
            elif source == "sub_left":
                frame = zero + zero + pack_pcm24(value * woofer_scale) + zero
            elif source == "sub_right":
                frame = zero + zero + zero + pack_pcm24(value * woofer_scale)
            elif source == "front_both":
                front = pack_pcm24(value * 0.5)
                frame = front + front + zero + zero
            elif source == "left_woofer":
                frame = pack_pcm24(value) + zero + pack_pcm24(value * woofer_scale) + zero
            elif source == "right_woofer":
                frame = zero + pack_pcm24(value) + zero + pack_pcm24(value * woofer_scale)
            else:
                raise MeasurementError(f"Unknown source: {source}")
            buffer.extend(frame)
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)
    if source in ("woofer", "sub_pair", "sub_left", "sub_right"):
        return [value * woofer_scale for value in mono]
    return mono


def phase_reference_bins() -> dict[str, list[int]]:
    """Build common-bin sets that a four-state Walsh capture can separate."""
    used: set[int] = set()

    def grid(low_hz: float, high_hz: float, divisions_per_octave: int) -> list[int]:
        result: list[int] = []
        index = 0
        frequency = low_hz
        while frequency <= high_hz * 1.000001:
            ideal = frequency * PHASE_REFERENCE_BLOCK / RATE
            candidates = sorted(
                range(max(2, round(ideal) - 5), min(PHASE_REFERENCE_BLOCK // 2 - 1, round(ideal) + 5) + 1),
                key=lambda item: (abs(item - ideal), item),
            )
            chosen = next((item for item in candidates if all(abs(item - other) >= 2 for other in used)), None)
            if chosen is not None:
                result.append(chosen)
                used.add(chosen)
            index += 1
            frequency = low_hz * (2.0 ** (index / divisions_per_octave))
        return result

    # L/R/W share the exact same crossover bins; the Walsh states separate
    # their transfer functions.  Extra Front-only bins extend L/R timing high
    # enough to stabilize the delay fit, while sub-only bins describe the
    # bottom octave without pretending that the Front is observable there.
    woofer_low = grid(20.0, 42.0, 4)
    crossover_common = grid(45.0, 220.0, 6)
    front_high = grid(240.0, 800.0, 6)
    return {
        "left": sorted(crossover_common + front_high),
        "right": sorted(crossover_common + front_high),
        "woofer": sorted(woofer_low + crossover_common),
    }


def synthesize_low_crest_multisine(
    bins: list[int],
    peak_dbfs: float,
    seed: int,
    fft: FFTBackend,
) -> tuple[list[float], list[complex], float]:
    """Choose deterministic random phases with the lowest peak/RMS ratio."""
    if not bins:
        raise MeasurementError("위상 기준 tone bin이 비어 있습니다.")
    best: tuple[float, list[float], list[complex]] | None = None
    for trial in range(12):
        generator = random.Random(seed + trial * 1_000_003)
        spectrum = [0j] * (PHASE_REFERENCE_BLOCK // 2 + 1)
        for bin_index in bins:
            phase = generator.uniform(-math.pi, math.pi)
            spectrum[bin_index] = 0.5 * PHASE_REFERENCE_BLOCK * complex(math.cos(phase), math.sin(phase))
        samples = fft.irfft(spectrum, PHASE_REFERENCE_BLOCK)
        peak = max(abs(value) for value in samples)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        crest = peak / max(rms, 1.0e-15)
        if best is None or crest < best[0]:
            best = (crest, samples, spectrum)
    assert best is not None
    target_peak = 10.0 ** (float(peak_dbfs) / 20.0)
    scale = target_peak / max(max(abs(value) for value in best[1]), 1.0e-15)
    scaled_samples = [value * scale for value in best[1]]
    scaled_spectrum = [value * scale for value in best[2]]
    return scaled_samples, scaled_spectrum, best[0]


def write_phase_reference(
    path: Path,
    level_dbfs: int,
    woofer_attenuation_db: float,
) -> dict[str, Any]:
    """Write one quiet four-state Walsh signal that identifies L/R/W together."""
    if not -18.0 <= float(woofer_attenuation_db) <= 0.0:
        raise MeasurementError("우퍼 측정 감쇄는 -18~0 dB여야 합니다.")
    bins = phase_reference_bins()
    fft = FFTBackend()
    try:
        source_levels = {
            "left": float(level_dbfs) - PHASE_REFERENCE_HEADROOM_DB,
            "right": float(level_dbfs) - PHASE_REFERENCE_HEADROOM_DB,
            "woofer": float(level_dbfs) - PHASE_REFERENCE_HEADROOM_DB + float(woofer_attenuation_db),
        }
        generated = {}
        for source, seed in (("left", 0x4C4654), ("right", 0x524748), ("woofer", 0x574652)):
            samples, spectrum, crest = synthesize_low_crest_multisine(
                bins[source], source_levels[source], seed, fft,
            )
            generated[source] = {"samples": samples, "spectrum": spectrum, "crest": crest}
    finally:
        fft.close()

    lead_frames = round(0.35 * RATE)
    tail_frames = round(0.75 * RATE)
    source_order = ("left", "right", "woofer")
    active_frames = (
        PHASE_REFERENCE_BLOCK
        * PHASE_REFERENCE_STATE_PERIODS
        * len(PHASE_REFERENCE_WALSH_CODES)
    )
    frames = lead_frames + active_frames + tail_frames
    data_bytes = frames * 4 * 3
    zero = b"\x00\x00\x00"
    fade_frames = round(0.08 * RATE)
    with path.open("wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE")
        handle.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 4, RATE, RATE * 12, 12, 24))
        handle.write(b"data" + struct.pack("<I", data_bytes))
        buffer = bytearray()
        for frame_index in range(frames):
            active_index = frame_index - lead_frames
            if not 0 <= active_index < active_frames:
                frame = zero * 4
            else:
                period = active_index // PHASE_REFERENCE_BLOCK
                state_index = period // PHASE_REFERENCE_STATE_PERIODS
                index = active_index % PHASE_REFERENCE_BLOCK
                envelope = 1.0
                if period == 0 and index < fade_frames:
                    envelope = 0.5 - 0.5 * math.cos(math.pi * index / max(1, fade_frames))
                elif (
                    period == PHASE_REFERENCE_STATE_PERIODS * len(PHASE_REFERENCE_WALSH_CODES) - 1
                    and index >= PHASE_REFERENCE_BLOCK - fade_frames
                ):
                    remaining = PHASE_REFERENCE_BLOCK - 1 - index
                    envelope = 0.5 - 0.5 * math.cos(math.pi * remaining / max(1, fade_frames))
                codes = PHASE_REFERENCE_WALSH_CODES[state_index]
                left = generated["left"]["samples"][index] * codes[0] * envelope
                right = generated["right"]["samples"][index] * codes[1] * envelope
                woofer_half = generated["woofer"]["samples"][index] * codes[2] * 0.5 * envelope
                frame = pack_pcm24(left) + pack_pcm24(right) + pack_pcm24(woofer_half) + pack_pcm24(woofer_half)
            buffer.extend(frame)
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)

    return {
        "version": 2,
        "method": "simultaneous common-bin four-state Walsh periodic multisine",
        "sample_rate": RATE,
        "block_samples": PHASE_REFERENCE_BLOCK,
        "periods": PHASE_REFERENCE_STATE_PERIODS * len(PHASE_REFERENCE_WALSH_CODES),
        "state_periods": PHASE_REFERENCE_STATE_PERIODS,
        "analysis_period_indices": list(PHASE_REFERENCE_STATE_ANALYSIS_INDICES),
        "walsh_states": [
            {"name": f"W{index + 1}", "codes": dict(zip(source_order, codes))}
            for index, codes in enumerate(PHASE_REFERENCE_WALSH_CODES)
        ],
        "lead_frames": lead_frames,
        "tail_frames": tail_frames,
        "configured_sweep_level_dbfs": int(level_dbfs),
        "phase_reference_headroom_db": PHASE_REFERENCE_HEADROOM_DB,
        "woofer_measurement_attenuation_db": float(woofer_attenuation_db),
        "source_levels_dbfs": source_levels,
        "tone_policy": "same-frequency 1/6-octave crossover coverage separated by orthogonal Walsh codes; exact octave/harmonic stacking avoided",
        "sources": {
            source: {
                "crest_factor": round(float(generated[source]["crest"]), 5),
                "bins": [
                    {
                        "bin": bin_index,
                        "frequency_hz": round(bin_index * RATE / PHASE_REFERENCE_BLOCK, 6),
                        "real": float(generated[source]["spectrum"][bin_index].real),
                        "imag": float(generated[source]["spectrum"][bin_index].imag),
                    }
                    for bin_index in bins[source]
                ],
            }
            for source in ("left", "right", "woofer")
        },
    }


def linear_resample_period(values: list[float], output_length: int) -> list[float]:
    if len(values) == output_length:
        return list(values)
    if len(values) < 2 or output_length < 2:
        raise MeasurementError("위상 기준 반복 구간이 너무 짧습니다.")
    scale = (len(values) - 1) / (output_length - 1)
    result = []
    for index in range(output_length):
        position = index * scale
        lower = min(len(values) - 2, int(position))
        fraction = position - lower
        result.append(values[lower] * (1.0 - fraction) + values[lower + 1] * fraction)
    return result


def interpolate_linear(frequencies: list[float], values: list[float], frequency: float) -> float:
    if frequency <= frequencies[0]:
        return values[0]
    if frequency >= frequencies[-1]:
        return values[-1]
    lower = 0
    upper = len(frequencies) - 1
    while upper - lower > 1:
        middle = (lower + upper) // 2
        if frequencies[middle] <= frequency:
            lower = middle
        else:
            upper = middle
    fraction = (frequency - frequencies[lower]) / max(1.0e-15, frequencies[upper] - frequencies[lower])
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _analyze_disjoint_phase_reference_samples(
    samples: list[float],
    metadata: dict[str, Any],
    cal: dict[str, Any],
    fft: FFTBackend,
) -> dict[str, Any]:
    """Recover relative L/R/W phase from one continuous UMIK recording."""
    block = int(metadata.get("block_samples", PHASE_REFERENCE_BLOCK))
    analysis_periods = int(metadata.get("analysis_periods", PHASE_REFERENCE_ANALYSIS_PERIODS))
    start = round(1.0 * RATE)
    if len(samples) < start + (analysis_periods + 1) * (block + 8):
        raise MeasurementError("L/R/우퍼 위상 기준 녹음 길이가 부족합니다.")
    # The U7 playback and UMIK capture clocks are independent. Estimate the
    # recorded period, then resample each period to the nominal FFT size.
    correlations: list[tuple[float, int]] = []
    compare_length = min(block * 2, len(samples) - start - block - 8)
    stride = 8
    for lag in range(block - 8, block + 9):
        first = samples[start:start + compare_length:stride]
        second = samples[start + lag:start + lag + compare_length:stride]
        count = min(len(first), len(second))
        if count < 100:
            continue
        first = first[:count]
        second = second[:count]
        first_mean = sum(first) / count
        second_mean = sum(second) / count
        numerator = sum((a - first_mean) * (b - second_mean) for a, b in zip(first, second))
        first_power = sum((value - first_mean) ** 2 for value in first)
        second_power = sum((value - second_mean) ** 2 for value in second)
        correlation = numerator / max(math.sqrt(first_power * second_power), 1.0e-30)
        correlations.append((correlation, lag))
    if not correlations:
        raise MeasurementError("위상 기준 반복 주기를 찾을 수 없습니다.")
    period_correlation, recorded_period = max(correlations)
    spectra = []
    for period_index in range(analysis_periods):
        period_start = start + period_index * recorded_period
        values = samples[period_start:period_start + recorded_period]
        if len(values) != recorded_period:
            raise MeasurementError("위상 기준 반복 구간이 중간에 끊겼습니다.")
        spectra.append(fft.rfft(linear_resample_period(values, block), block))
    average_spectrum = [sum(values) / len(values) for values in zip(*spectra)]
    used_bins = {
        int(item["bin"])
        for source in metadata["sources"].values()
        for item in source["bins"]
    }
    cal_f, cal_db = cal["frequencies"], cal["corrections"]
    source_results: dict[str, Any] = {}
    all_phase_errors = []
    for source in ("left", "right", "woofer"):
        frequencies, levels, phases, snrs, phase_errors = [], [], [], [], []
        for item in metadata["sources"][source]["bins"]:
            bin_index = int(item["bin"])
            reference = complex(float(item["real"]), float(item["imag"]))
            transfers = [spectrum[bin_index] / reference for spectrum in spectra]
            transfer = sum(transfers) / len(transfers)
            frequency = bin_index * RATE / block
            frequencies.append(frequency)
            levels.append(
                20.0 * math.log10(max(abs(transfer), 1.0e-15))
                + interpolate_log(cal_f, cal_db, frequency)
            )
            phases.append(math.atan2(transfer.imag, transfer.real))
            local_noise = [
                abs(average_spectrum[candidate])
                for offset in (-6, -5, -4, 4, 5, 6)
                for candidate in (bin_index + offset,)
                if 1 <= candidate < len(average_spectrum) and candidate not in used_bins
            ]
            noise = statistics.median(local_noise) if local_noise else 1.0e-15
            snrs.append(20.0 * math.log10(max(abs(average_spectrum[bin_index]), 1.0e-15) / max(noise, 1.0e-15)))
            for value in transfers:
                difference = math.atan2(value.imag, value.real) - math.atan2(transfer.imag, transfer.real)
                phase_errors.append(abs(math.degrees(math.atan2(math.sin(difference), math.cos(difference)))))
        phases = unwrap(phases)
        source_snr = statistics.median(snrs) if snrs else float("-inf")
        phase_p90 = percentile(phase_errors, 0.90)
        all_phase_errors.extend(phase_errors)
        source_results[source] = {
            "frequencies": [round(value, 6) for value in frequencies],
            "db": [round(value, 5) for value in levels],
            "phase_rad": [round(value, 9) for value in phases],
            "median_snr_db": round(source_snr, 3),
            "phase_repeatability_p90_deg": round(phase_p90, 3),
            "tone_count": len(frequencies),
        }

    pair_results = {}
    for key, first_name, second_name, low_hz, high_hz in (
        ("left_right", "left", "right", 70.0, 650.0),
        ("left_woofer", "left", "woofer", 50.0, 220.0),
        ("right_woofer", "right", "woofer", 50.0, 220.0),
    ):
        first = source_results[first_name]
        second = source_results[second_name]
        low = max(low_hz, first["frequencies"][0], second["frequencies"][0])
        high = min(high_hz, first["frequencies"][-1], second["frequencies"][-1])
        count = 28
        frequencies = [low * ((high / low) ** (index / (count - 1))) for index in range(count)]
        phase_difference = [
            interpolate_linear(second["frequencies"], second["phase_rad"], frequency)
            - interpolate_linear(first["frequencies"], first["phase_rad"], frequency)
            for frequency in frequencies
        ]
        phase_difference = unwrap(phase_difference)
        slopes = []
        for left_index in range(len(frequencies)):
            for right_index in range(left_index + 1, len(frequencies)):
                if frequencies[right_index] - frequencies[left_index] >= max(20.0, 0.15 * low):
                    slopes.append(
                        (phase_difference[right_index] - phase_difference[left_index])
                        / (frequencies[right_index] - frequencies[left_index])
                    )
        slope = statistics.median(slopes) if slopes else 0.0
        delay_samples = -slope * RATE / (2.0 * math.pi)
        intercepts = [phase + 2.0 * math.pi * frequency * delay_samples / RATE for frequency, phase in zip(frequencies, phase_difference)]
        center = statistics.median(intercepts)
        residuals = [
            abs(math.degrees(math.atan2(math.sin(value - center), math.cos(value - center))))
            for value in intercepts
        ]
        pair_results[key] = {
            "first": first_name,
            "second": second_name,
            "frequency_hz": [round(value, 4) for value in frequencies],
            "relative_phase_deg": [round(math.degrees(value), 4) for value in phase_difference],
            "second_minus_first_delay_samples": round(delay_samples, 4),
            "second_minus_first_delay_ms": round(delay_samples * 1000.0 / RATE, 5),
            "delay_fit_residual_p90_deg": round(percentile(residuals, 0.90), 3),
            "polarity_hint": "inverted" if math.cos(center) < -0.5 else "normal" if math.cos(center) > 0.5 else "ambiguous",
        }

    minimum_snr = min(float(value["median_snr_db"]) for value in source_results.values())
    phase_p90 = percentile(all_phase_errors, 0.90)
    reliable = (
        period_correlation >= PHASE_REFERENCE_MIN_CORRELATION
        and minimum_snr >= MINIMUM_USABLE_SNR_DB
        and phase_p90 <= PHASE_REFERENCE_MAX_PHASE_P90_DEG
    )
    return {
        "version": 1,
        "method": metadata["method"],
        "reliable": reliable,
        "recommended": reliable and minimum_snr >= RECOMMENDED_SNR_DB and phase_p90 <= 20.0,
        "recorded_period_samples": recorded_period,
        "sample_clock_offset_ppm": round((recorded_period / block - 1.0) * 1_000_000.0, 3),
        "period_correlation": round(period_correlation, 6),
        "minimum_median_snr_db": round(minimum_snr, 3),
        "phase_repeatability_p90_deg": round(phase_p90, 3),
        "thresholds": {
            "minimum_period_correlation": PHASE_REFERENCE_MIN_CORRELATION,
            "minimum_snr_db": MINIMUM_USABLE_SNR_DB,
            "maximum_phase_repeatability_p90_deg": PHASE_REFERENCE_MAX_PHASE_P90_DEG,
        },
        "sources": source_results,
        "pairs": pair_results,
        "timing_scope": "relative L/R/W timing inside this recording; not a shared absolute U7/UMIK clock",
        "normalization_applied": False,
    }


def _phase_pair_results(source_results: dict[str, Any]) -> dict[str, Any]:
    """Fit useful relative delays while retaining the measured phase curve."""
    pair_results: dict[str, Any] = {}
    for key, first_name, second_name, low_hz, high_hz in (
        ("left_right", "left", "right", 70.0, 650.0),
        ("left_woofer", "left", "woofer", 50.0, 220.0),
        ("right_woofer", "right", "woofer", 50.0, 220.0),
    ):
        first = source_results[first_name]
        second = source_results[second_name]
        common = sorted(set(round(float(value), 6) for value in first["frequencies"]).intersection(
            round(float(value), 6) for value in second["frequencies"]
        ))
        frequencies = [value for value in common if low_hz <= value <= high_hz]
        if len(frequencies) < 3:
            low = max(low_hz, float(first["frequencies"][0]), float(second["frequencies"][0]))
            high = min(high_hz, float(first["frequencies"][-1]), float(second["frequencies"][-1]))
            count = 28
            frequencies = [low * ((high / low) ** (index / (count - 1))) for index in range(count)]
        phase_difference = unwrap([
            interpolate_linear(second["frequencies"], second["phase_rad"], frequency)
            - interpolate_linear(first["frequencies"], first["phase_rad"], frequency)
            for frequency in frequencies
        ])
        slopes = []
        for left_index in range(len(frequencies)):
            for right_index in range(left_index + 1, len(frequencies)):
                if frequencies[right_index] - frequencies[left_index] >= max(20.0, 0.15 * frequencies[0]):
                    slopes.append(
                        (phase_difference[right_index] - phase_difference[left_index])
                        / (frequencies[right_index] - frequencies[left_index])
                    )
        slope = statistics.median(slopes) if slopes else 0.0
        delay_samples = -slope * RATE / (2.0 * math.pi)
        intercepts = [
            phase + 2.0 * math.pi * frequency * delay_samples / RATE
            for frequency, phase in zip(frequencies, phase_difference)
        ]
        center = statistics.median(intercepts)
        residuals = [
            abs(math.degrees(math.atan2(math.sin(value - center), math.cos(value - center))))
            for value in intercepts
        ]
        pair_results[key] = {
            "first": first_name,
            "second": second_name,
            "frequency_hz": [round(value, 4) for value in frequencies],
            "relative_phase_deg": [round(math.degrees(value), 4) for value in phase_difference],
            "second_minus_first_delay_samples": round(delay_samples, 4),
            "second_minus_first_delay_ms": round(delay_samples * 1000.0 / RATE, 5),
            "delay_fit_residual_p90_deg": round(percentile(residuals, 0.90), 3),
            "polarity_hint": (
                "inverted" if math.cos(center) < -0.5 else
                "normal" if math.cos(center) > 0.5 else "ambiguous"
            ),
            "same_frequency_bins": len(common) >= 3,
        }
    return pair_results


def _phase_signal_onset(samples: list[float]) -> int:
    """Find coded-reference onset coarsely; guard periods absorb the offset."""
    window = max(256, round(0.012 * RATE))
    stride = max(128, window // 2)
    # Input switching can create one loud pulse at the start of arecord.  A
    # whole-prefix RMS therefore overestimates the noise floor and hides a
    # deliberately quiet reference.  Use the lower stable prefix blocks, as
    # the ESS quality estimator does, and require a sustained rise.
    noise_window = round(0.05 * RATE)
    noise_powers = []
    for start in range(round(0.12 * RATE), min(len(samples) - noise_window, round(0.70 * RATE)), noise_window):
        values = samples[start:start + noise_window]
        mean = sum(values) / len(values)
        noise_powers.append(sum((value - mean) ** 2 for value in values) / len(values))
    if not noise_powers:
        raise MeasurementError("Walsh 위상 기준의 재생 전 배경 구간이 부족합니다.")
    noise_rms = math.sqrt(percentile(noise_powers, 0.35))
    threshold = max(noise_rms * 1.6, 1.0e-10)
    search_end = min(len(samples) - window, round(2.5 * RATE))
    consecutive = 0
    first = 0
    for start in range(round(0.20 * RATE), max(round(0.20 * RATE), search_end), stride):
        values = samples[start:start + window]
        mean = sum(values) / len(values)
        rms = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
        if rms >= threshold:
            if consecutive == 0:
                first = start
            consecutive += 1
            if consecutive >= 3:
                return first
        else:
            consecutive = 0
    raise MeasurementError("Walsh 위상 기준 신호 시작점을 찾지 못했습니다. 측정 출력을 확인하세요.")


def _pearson_correlation(first: list[float], second: list[float]) -> float:
    count = min(len(first), len(second))
    if count < 100:
        return 0.0
    first, second = first[:count], second[:count]
    first_mean = sum(first) / count
    second_mean = sum(second) / count
    numerator = sum((a - first_mean) * (b - second_mean) for a, b in zip(first, second))
    first_power = sum((value - first_mean) ** 2 for value in first)
    second_power = sum((value - second_mean) ** 2 for value in second)
    return numerator / max(math.sqrt(first_power * second_power), 1.0e-30)


def _analyze_walsh_phase_reference_samples(
    samples: list[float],
    metadata: dict[str, Any],
    cal: dict[str, Any],
    fft: FFTBackend,
) -> dict[str, Any]:
    """Solve same-frequency L/R/W transfer functions from four Walsh states."""
    block = int(metadata.get("block_samples", PHASE_REFERENCE_BLOCK))
    state_periods = int(metadata.get("state_periods", PHASE_REFERENCE_STATE_PERIODS))
    analysis_indices = [int(value) for value in metadata.get(
        "analysis_period_indices", PHASE_REFERENCE_STATE_ANALYSIS_INDICES,
    )]
    states = metadata.get("walsh_states", [])
    if len(states) != 4 or len(analysis_indices) < 2 or max(analysis_indices) >= state_periods - 1:
        raise MeasurementError("Walsh 위상 기준 metadata의 상태/guard 구성이 잘못되었습니다.")
    onset = _phase_signal_onset(samples)

    # Estimate independent U7/UMIK clock drift inside the first Walsh state.
    probe_start = onset + block // 4
    probe_length = min(block + block // 2, len(samples) - probe_start - block - 8)
    correlations: list[tuple[float, int]] = []
    stride = 4
    for lag in range(block - 8, block + 9):
        first = samples[probe_start:probe_start + probe_length:stride]
        second = samples[probe_start + lag:probe_start + lag + probe_length:stride]
        correlations.append((_pearson_correlation(first, second), lag))
    if not correlations:
        raise MeasurementError("Walsh 위상 기준 반복 주기를 찾을 수 없습니다.")
    _, recorded_period = max(correlations)

    state_spectra: list[list[list[complex]]] = []
    repeat_correlations: list[float] = []
    for state_index in range(len(states)):
        spectra: list[list[complex]] = []
        time_blocks: list[list[float]] = []
        state_start = onset + state_index * state_periods * recorded_period
        for analysis_index in analysis_indices:
            period_start = state_start + analysis_index * recorded_period
            values = samples[period_start:period_start + recorded_period]
            if len(values) != recorded_period:
                raise MeasurementError("Walsh 위상 기준 녹음이 상태 중간에 끊겼습니다.")
            resampled = linear_resample_period(values, block)
            time_blocks.append(resampled)
            spectra.append(fft.rfft(resampled, block))
        for index in range(1, len(time_blocks)):
            repeat_correlations.append(_pearson_correlation(time_blocks[0][::4], time_blocks[index][::4]))
        state_spectra.append(spectra)

    source_order = ("left", "right", "woofer")
    code_columns = {
        source: [float(state["codes"][source]) for state in states]
        for source in source_order
    }
    for first_index, first_source in enumerate(source_order):
        for second_source in source_order[first_index + 1:]:
            dot = sum(a * b for a, b in zip(code_columns[first_source], code_columns[second_source]))
            if abs(dot) > 1.0e-9:
                raise MeasurementError("Walsh 위상 기준 code가 직교하지 않습니다.")

    used_bins = {
        int(item["bin"])
        for source in metadata["sources"].values()
        for item in source["bins"]
    }
    cal_f, cal_db = cal["frequencies"], cal["corrections"]
    source_results: dict[str, Any] = {}
    all_phase_errors: list[float] = []
    repeats = len(analysis_indices)
    for source in source_order:
        frequencies: list[float] = []
        levels: list[float] = []
        phases: list[float] = []
        snrs: list[float] = []
        phase_errors: list[float] = []
        codes = code_columns[source]
        code_power = sum(value * value for value in codes)
        for item in metadata["sources"][source]["bins"]:
            bin_index = int(item["bin"])
            reference = complex(float(item["real"]), float(item["imag"]))
            numerators = [
                sum(codes[state_index] * state_spectra[state_index][repeat_index][bin_index]
                    for state_index in range(len(states))) / code_power
                for repeat_index in range(repeats)
            ]
            transfers = [value / reference for value in numerators]
            transfer = sum(transfers) / len(transfers)
            frequency = bin_index * RATE / block
            frequencies.append(frequency)
            levels.append(
                20.0 * math.log10(max(abs(transfer), 1.0e-15))
                + interpolate_log(cal_f, cal_db, frequency)
            )
            phases.append(math.atan2(transfer.imag, transfer.real))
            local_noise = []
            for offset in (-6, -5, -4, 4, 5, 6):
                candidate = bin_index + offset
                if not 1 <= candidate < len(state_spectra[0][0]) or candidate in used_bins:
                    continue
                for repeat_index in range(repeats):
                    local_noise.append(abs(sum(
                        codes[state_index] * state_spectra[state_index][repeat_index][candidate]
                        for state_index in range(len(states))
                    ) / code_power))
            noise = statistics.median(local_noise) if local_noise else 1.0e-15
            signal = abs(sum(numerators) / len(numerators))
            snrs.append(20.0 * math.log10(max(signal, 1.0e-15) / max(noise, 1.0e-15)))
            for value in transfers:
                difference = math.atan2(value.imag, value.real) - math.atan2(transfer.imag, transfer.real)
                phase_errors.append(abs(math.degrees(math.atan2(math.sin(difference), math.cos(difference)))))
        phases = unwrap(phases)
        source_snr = statistics.median(snrs) if snrs else float("-inf")
        phase_p90 = percentile(phase_errors, 0.90)
        all_phase_errors.extend(phase_errors)
        source_results[source] = {
            "frequencies": [round(value, 6) for value in frequencies],
            "db": [round(value, 5) for value in levels],
            "phase_rad": [round(value, 9) for value in phases],
            "median_snr_db": round(source_snr, 3),
            "phase_repeatability_p90_deg": round(phase_p90, 3),
            "tone_count": len(frequencies),
            "same_frequency_walsh_separation": True,
        }

    minimum_snr = min(float(value["median_snr_db"]) for value in source_results.values())
    phase_p90 = percentile(all_phase_errors, 0.90)
    period_correlation = min(repeat_correlations) if repeat_correlations else 0.0
    reliable = (
        period_correlation >= PHASE_REFERENCE_MIN_CORRELATION
        and minimum_snr >= MINIMUM_USABLE_SNR_DB
        and phase_p90 <= PHASE_REFERENCE_MAX_PHASE_P90_DEG
    )
    return {
        "version": 2,
        "method": metadata["method"],
        "reliable": reliable,
        "recommended": reliable and minimum_snr >= RECOMMENDED_SNR_DB and phase_p90 <= 20.0,
        "recorded_period_samples": recorded_period,
        "sample_clock_offset_ppm": round((recorded_period / block - 1.0) * 1_000_000.0, 3),
        "signal_onset_sample": onset,
        "period_correlation": round(period_correlation, 6),
        "minimum_median_snr_db": round(minimum_snr, 3),
        "phase_repeatability_p90_deg": round(phase_p90, 3),
        "thresholds": {
            "minimum_period_correlation": PHASE_REFERENCE_MIN_CORRELATION,
            "minimum_snr_db": MINIMUM_USABLE_SNR_DB,
            "maximum_phase_repeatability_p90_deg": PHASE_REFERENCE_MAX_PHASE_P90_DEG,
        },
        "sources": source_results,
        "pairs": _phase_pair_results(source_results),
        "timing_scope": "same-frequency relative L/R/W timing from one Walsh-coded recording; not an absolute U7/UMIK clock",
        "separation": "four orthogonal sign states; four analyzed repeats with guard periods per state",
        "normalization_applied": False,
    }


def analyze_phase_reference_samples(
    samples: list[float],
    metadata: dict[str, Any],
    cal: dict[str, Any],
    fft: FFTBackend,
) -> dict[str, Any]:
    if int(metadata.get("version", 1)) >= 2 and metadata.get("walsh_states"):
        return _analyze_walsh_phase_reference_samples(samples, metadata, cal, fft)
    return _analyze_disjoint_phase_reference_samples(samples, metadata, cal, fft)


def phase_reference_from_recording(recorded: Path, metadata: dict[str, Any], cal: dict[str, Any]) -> dict[str, Any]:
    _, bits, samples = read_pcm_wav(recorded)
    peak = max(abs(value) for value in samples)
    if peak >= 0.988:
        raise MeasurementError("L/R/우퍼 위상 기준 녹음이 클리핑되었습니다. 측정 출력을 낮추세요.")
    fft = FFTBackend()
    try:
        result = analyze_phase_reference_samples(samples, metadata, cal, fft)
    finally:
        fft.close()
    result["capture_bits"] = bits
    result["peak_dbfs"] = round(20.0 * math.log10(max(peak, 1.0e-15)), 3)
    return result


def write_filtered_stereo_sweep(path: Path, side: str, level_dbfs: int, seconds: int) -> list[float]:
    """Write a stereo input sweep that must travel through the active FIR graph directly in 2ch."""
    if side not in ("left", "right"):
        raise MeasurementError("사후 합산 검증 채널이 잘못되었습니다.")
    # A standalone CamillaDSP file graph can switch the U7 stream on startup.
    # Keep that transition inside silence so it cannot overlap low-frequency
    # ESS content; this does not add another audible sweep.
    mono, _ = generate_sweep_mono(
        level_dbfs,
        seconds,
        lead_seconds=POST_VALIDATION_SILENT_LEAD_SECONDS,
    )
    data_bytes = len(mono) * 2 * 3
    zero = b"\x00\x00\x00"
    with path.open("wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE")
        handle.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 6, 6, 24))
        handle.write(b"data" + struct.pack("<I", data_bytes))
        buffer = bytearray()
        for value in mono:
            payload = pack_pcm24(value)
            buffer.extend(payload + zero if side == "left" else zero + payload)
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)
    return mono

def filtered_file_config(input_path: Path) -> str:
    """Derive a finite-file capture config from the exact current Preview graph."""
    try:
        config = CAMILLA_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        raise MeasurementError(f"현재 CamillaDSP 설정을 읽을 수 없습니다: {exc}") from exc
    capture_marker = "  capture:\n"
    playback_marker = "  playback:\n"
    capture_start = config.find(capture_marker)
    playback_start = config.find(playback_marker, capture_start + len(capture_marker))
    if capture_start < 0 or playback_start < 0:
        raise MeasurementError("CamillaDSP capture/playback 설정 구조를 확인할 수 없습니다.")
    capture = (
        "  capture:\n"
        "    type: WavFile\n"
        f"    filename: {json.dumps(str(input_path))}\n"
        f"    extra_samples: {RATE * 2}\n"
    )
    config = config[:capture_start] + capture + config[playback_start:]
    return config.replace("__PLAYBACK_DEVICE__", "audiodsp_dsp").replace("__CAPTURE_DEVICE__", "unused_file_capture")


def write_quick_sweep(path: Path, level_dbfs: int, seconds: float = 2.0, low_hz: float = 40.0, high_hz: float = 12000.0) -> None:
    """Write a comfortable 2.0 s log sine sweep (40 Hz ~ 12 kHz) to Front L/R in 4ch S24_3LE."""
    frames = round(seconds * RATE)
    amplitude = 10.0 ** (level_dbfs / 20.0)
    fade = round(0.04 * RATE)
    log_ratio = math.log(high_hz / low_hz)
    data_bytes = frames * 4 * 3
    with path.open("wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE")
        handle.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 4, RATE, RATE * 12, 12, 24))
        handle.write(b"data" + struct.pack("<I", data_bytes))
        buffer = bytearray()
        zero = b"\x00\x00\x00"
        for index in range(frames):
            t = index / RATE
            phase = 2.0 * math.pi * low_hz * seconds / log_ratio * (math.exp(t / seconds * log_ratio) - 1.0)
            sample = amplitude * math.sin(phase)
            if index < fade:
                sample *= 0.5 - 0.5 * math.cos(math.pi * index / fade)
            elif index >= frames - fade:
                sample *= 0.5 - 0.5 * math.cos(math.pi * (frames - 1 - index) / fade)
            payload = pack_pcm24(sample)
            buffer.extend(payload + payload + zero + zero)
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)


def write_white_noise(path: Path, level_dbfs: int, seconds: int = 5) -> None:
    """Compatibility alias: redirects to write_quick_sweep for comfortable level checks."""
    write_quick_sweep(path, level_dbfs, seconds=2.0)


def read_pcm_wav(path: Path) -> tuple[int, int, list[float]]:
    raw = path.read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise MeasurementError("녹음 파일이 WAV가 아닙니다.")
    offset, fmt, data = 12, None, None
    while offset + 8 <= len(raw):
        chunk_id, size = raw[offset:offset + 4], struct.unpack_from("<I", raw, offset + 4)[0]
        payload = raw[offset + 8:offset + 8 + size]
        if chunk_id == b"fmt ":
            fmt = payload
        elif chunk_id == b"data":
            data = payload
        offset += 8 + size + (size & 1)
    if fmt is None or data is None or len(fmt) < 16:
        raise MeasurementError("녹음 WAV 청크가 손상되었습니다.")
    code, channels, rate, _, block, bits = struct.unpack("<HHIIHH", fmt[:16])
    if code != 1 or channels != 1 or rate != RATE or bits not in (16, 24, 32):
        raise MeasurementError(f"UMIK 녹음 형식 오류: code={code}, ch={channels}, rate={rate}, bits={bits}")
    samples: list[float] = []
    width = bits // 8
    for index in range(0, len(data) - width + 1, block):
        if bits == 16:
            value = struct.unpack_from("<h", data, index)[0] / 32768.0
        elif bits == 24:
            integer = int.from_bytes(data[index:index + 3], "little")
            if integer & 0x800000:
                integer -= 1 << 24
            value = integer / 8_388_608.0
        else:
            value = struct.unpack_from("<i", data, index)[0] / 2_147_483_648.0
        samples.append(value)
    return rate, bits, samples


def run_direct_capture_batch(
    captures: list[tuple[Path, Path, str]],
    progress_base: float,
    progress_span: float,
) -> None:
    """Capture several sweeps inside one DSP-bypass window, then restore audio."""
    if not captures:
        return
    ensure_measurement_output_path()
    with AUDIO_LOCK.open("w") as audio_handle:
        fcntl.flock(audio_handle, fcntl.LOCK_EX)
        active_processes: list[subprocess.Popen] = []
        audio_window: dict[str, Any] | None = None
        try:
            audio_window = begin_measurement_audio_window()
            update_current(
                dsp_mode="direct_bypass",
                u7_input="off",
                measurement_sweep_output={
                    "active": True,
                    "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
                    "listening_volume_ignored": True,
                    "restore_before_input": True,
                },
                stage="DSP bypass · U7 입력 OFF · 출력 0 dB 기준 · UMIK 녹음 준비",
                progress=progress_base,
            )
            item_span = progress_span / len(captures)
            for index, (output, recorded, label) in enumerate(captures):
                if load_current().get("cancel_requested"):
                    raise MeasurementError("사용자가 측정을 취소했습니다.")
                ensure_measurement_audio_window(audio_window)
                item_base = progress_base + item_span * index
                capture_seconds = max(2, math.ceil(output.stat().st_size / (RATE * 12) + 1.0))
                capture = subprocess.Popen([
                    ARECORD, "-q", "--fatal-errors", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
                    "-r", str(RATE), "-c", "1", "-d", str(capture_seconds), str(recorded),
                ])
                active_processes = [capture]
                update_current(active_pids=[capture.pid], stage=f"DSP bypass · {label} 녹음 준비")
                time.sleep(0.4)
                playback = subprocess.Popen([APLAY, "-q", "-D", PLAYBACK_DEVICE, str(output)])
                active_processes.append(playback)
                update_current(active_pids=[capture.pid, playback.pid], stage=f"DSP bypass · {label} 측정음 재생 중")
                start = time.monotonic()
                expected = max(1.0, output.stat().st_size / (RATE * 12))
                while playback.poll() is None:
                    ensure_measurement_audio_window(audio_window)
                    elapsed = time.monotonic() - start
                    fraction = min(0.98, elapsed / expected)
                    update_current(progress=item_base + item_span * fraction, eta_seconds=max(0, round(expected - elapsed)))
                    time.sleep(0.5)
                if playback.returncode != 0:
                    raise MeasurementError(f"U7 측정음 재생 실패: {playback.returncode}")
                ensure_measurement_audio_window(audio_window)
                capture.wait(timeout=capture_seconds + 3)
                if capture.returncode != 0:
                    raise MeasurementError(f"UMIK 녹음 실패: {capture.returncode}")
                active_processes = []
                update_current(progress=item_base + item_span)
        finally:
            for process in active_processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            restore_measurement_audio_window(audio_window)
            update_current(
                dsp_mode="restored",
                u7_input="restored",
                active_pids=[],
                measurement_sweep_output={
                    "active": False,
                    "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
                    "listening_volume_ignored": True,
                    "volume_restored_before_input": True,
                },
            )


def run_direct_capture(output: Path, recorded: Path, duration: int, progress_base: float, progress_span: float) -> None:
    # Kept as a compatibility wrapper for one-off validation and tests.
    del duration
    run_direct_capture_batch([(output, recorded, "측정")], progress_base, progress_span)


def run_filtered_capture_batch(
    captures: list[tuple[Path, Path, str]],
    progress_base: float,
    progress_span: float,
) -> None:
    """Play finite stereo inputs through the exact active CamillaDSP Preview."""
    if not captures:
        return
    state = load_current()
    if not state.get("preview_active") or not state.get("preview_profile"):
        raise MeasurementError("먼저 이번 튜닝을 Preview로 적용하세요.")
    manager_process = subprocess.run(
        [PYTHON, MANAGER, "status"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=30,
    )
    try:
        manager_status = json.loads(manager_process.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(manager_process.stdout.strip() or "Preview 상태 확인 실패") from exc
    preview = manager_status.get("preview", {})
    if manager_process.returncode or not preview.get("active") or preview.get("stale") or preview.get("profile") != state.get("preview_profile"):
        raise MeasurementError("실제 CamillaDSP Preview가 현재 결과와 일치하지 않습니다. 이번 튜닝 Preview를 다시 적용하세요.")
    ensure_post_preview_output_path(state)
    if not _camilla_service_active():
        raise MeasurementError("CamillaDSP가 실행 중이 아닙니다. Preview를 다시 적용하세요.")
    with AUDIO_LOCK.open("w") as audio_handle:
        fcntl.flock(audio_handle, fcntl.LOCK_EX)
        active_processes: list[subprocess.Popen] = []
        audio_window: dict[str, Any] | None = None
        try:
            audio_window = begin_measurement_audio_window(require_camilla=True)
            update_current(
                dsp_mode="filtered_file_preview",
                u7_input="off",
                measurement_sweep_output={
                    "active": True,
                    "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
                    "listening_volume_ignored": True,
                    "restore_before_input": True,
                },
                stage="Preview FIR 유지 · U7 입력 OFF · 출력 0 dB 기준 · 사후 합산 검증 준비",
                progress=progress_base,
            )
            item_span = progress_span / len(captures)
            for index, (input_path, recorded, label) in enumerate(captures):
                if load_current().get("cancel_requested"):
                    raise MeasurementError("사용자가 측정을 취소했습니다.")
                ensure_measurement_audio_window(audio_window)
                item_base = progress_base + item_span * index
                config_path = input_path.with_suffix(".camilladsp.yml")
                atomic_text(config_path, filtered_file_config(input_path))
                checked = subprocess.run(
                    [CAMILLA, "--check", str(config_path)], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=30,
                )
                if checked.returncode:
                    raise MeasurementError(f"사후 검증용 CamillaDSP 설정 오류: {checked.stdout.strip()}")
                input_seconds = max(2.0, max(0, input_path.stat().st_size - 44) / (RATE * 6.0))
                capture_seconds = math.ceil(input_seconds + 3.0)
                capture = subprocess.Popen([
                    ARECORD, "-q", "--fatal-errors", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
                    "-r", str(RATE), "-c", "1", "-d", str(capture_seconds), str(recorded),
                ])
                active_processes = [capture]
                update_current(active_pids=[capture.pid], stage=f"Preview FIR · {label} UMIK 녹음 준비")
                time.sleep(0.4)
                playback = subprocess.Popen([CAMILLA, str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                active_processes.append(playback)
                update_current(active_pids=[capture.pid, playback.pid], stage=f"Preview FIR · {label} 검증 sweep 재생 중")
                started = time.monotonic()
                while playback.poll() is None:
                    ensure_measurement_audio_window(audio_window)
                    elapsed = time.monotonic() - started
                    update_current(
                        progress=item_base + item_span * min(0.98, elapsed / max(input_seconds, 1.0)),
                        eta_seconds=max(0, round(input_seconds - elapsed)),
                    )
                    time.sleep(0.5)
                if playback.returncode:
                    raise MeasurementError(f"Preview FIR 검증 재생 실패: {playback.returncode}")
                ensure_measurement_audio_window(audio_window)
                capture.wait(timeout=capture_seconds + 3)
                if capture.returncode:
                    raise MeasurementError(f"UMIK 사후 검증 녹음 실패: {capture.returncode}")
                active_processes = []
                update_current(progress=item_base + item_span)
        finally:
            for process in active_processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            restore_measurement_audio_window(audio_window)
            update_current(
                dsp_mode="restored_preview",
                u7_input="restored",
                active_pids=[],
                measurement_sweep_output={
                    "active": False,
                    "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
                    "listening_volume_ignored": True,
                    "volume_restored_before_input": True,
                },
            )


def run_level_sequence(noise: Path, silence_recorded: Path, noise_recorded: Path) -> None:
    """Capture 2 s background and 2 s quick sweep under one exclusive bypass window."""
    ensure_measurement_output_path()
    with AUDIO_LOCK.open("w") as audio_handle:
        fcntl.flock(audio_handle, fcntl.LOCK_EX)
        processes: list[subprocess.Popen] = []
        audio_window: dict[str, Any] | None = None
        try:
            audio_window = begin_measurement_audio_window()
            update_current(
                dsp_mode="direct_bypass",
                u7_input="off",
                measurement_sweep_output={
                    "active": True,
                    "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
                    "listening_volume_ignored": True,
                    "restore_before_input": True,
                },
                stage="1/2 · 입력 OFF · 출력 0 dB 기준 · 무음 2초 배경소음 측정 중",
                progress=10.0,
                eta_seconds=4,
            )
            silence = subprocess.Popen([
                ARECORD, "-q", "--fatal-errors", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
                "-r", str(RATE), "-c", "1", "-d", "2", str(silence_recorded),
            ])
            processes = [silence]
            update_current(active_pids=[silence.pid])
            silence.wait(timeout=5)
            if silence.returncode != 0:
                raise MeasurementError(f"UMIK 무음 녹음 실패: {silence.returncode}")

            update_current(stage="2/2 · 2초 퀵 스윕 · 신호 레벨 측정 중", progress=50.0, eta_seconds=2)
            capture = subprocess.Popen([
                ARECORD, "-q", "--fatal-errors", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
                "-r", str(RATE), "-c", "1", "-d", "3", str(noise_recorded),
            ])
            processes = [capture]
            time.sleep(0.3)
            ensure_measurement_audio_window(audio_window)
            playback = subprocess.Popen([APLAY, "-q", "-D", PLAYBACK_DEVICE, str(noise)])
            processes.append(playback)
            update_current(active_pids=[capture.pid, playback.pid])
            playback_deadline = time.monotonic() + 5.0
            while playback.poll() is None:
                ensure_measurement_audio_window(audio_window)
                if time.monotonic() >= playback_deadline:
                    raise MeasurementError("U7 퀵 스윕 재생 시간이 초과되었습니다.")
                time.sleep(0.15)
            if playback.returncode != 0:
                raise MeasurementError(f"U7 퀵 스윕 재생 실패: {playback.returncode}")
            ensure_measurement_audio_window(audio_window)
            capture.wait(timeout=5)
            if capture.returncode != 0:
                raise MeasurementError(f"UMIK 퀵 스윕 녹음 실패: {capture.returncode}")
            update_current(progress=95.0, eta_seconds=1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            restore_measurement_audio_window(audio_window)
            update_current(
                dsp_mode="restored",
                u7_input="restored",
                active_pids=[],
                measurement_sweep_output={
                    "active": False,
                    "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
                    "listening_volume_ignored": True,
                    "volume_restored_before_input": True,
                },
            )


def detect_subwoofer_passband(
    samples: list[float],
    reference: list[float],
    capture_lead: int,
    reference_start: int,
    reference_end: int,
    background_rms: float,
) -> dict[str, Any]:
    """Detect the sustained -3 dB acoustic passband from chirp-time energy."""
    ratio_log = math.log(22_000.0 / 15.0)
    sweep_samples = reference_end - reference_start

    def ac_rms(values: list[float]) -> float:
        if not values:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    frequencies: list[float] = []
    levels: list[float] = []
    center = 20.0
    while center <= 500.0:
        # Overlapping 1/3-octave windows suppress individual cycles and narrow
        # room notches while preserving the low-pass transition.
        low = max(15.0, center / (2.0 ** (1.0 / 6.0)))
        high = min(22_000.0, center * (2.0 ** (1.0 / 6.0)))
        low_fraction = math.log(low / 15.0) / ratio_log
        high_fraction = math.log(high / 15.0) / ratio_log
        ref_lo = reference_start + round(sweep_samples * low_fraction)
        ref_hi = reference_start + round(sweep_samples * high_fraction)
        # Include modest acoustic/device delay without allowing unrelated
        # portions of the sweep into the energy estimate.
        padding = round(0.015 * RATE)
        capture_lo = max(0, capture_lead + ref_lo - padding)
        capture_hi = min(len(samples), capture_lead + ref_hi + padding)
        reference_values = reference[max(0, ref_lo - padding):min(len(reference), ref_hi + padding)]
        captured_rms = ac_rms(samples[capture_lo:capture_hi])
        reference_rms = ac_rms(reference_values)
        signal_power = max(0.0, captured_rms * captured_rms - background_rms * background_rms)
        transfer_db = 10.0 * math.log10(max(signal_power, 1.0e-30) / max(reference_rms * reference_rms, 1.0e-30))
        frequencies.append(center)
        levels.append(transfer_db)
        center *= 2.0 ** (1.0 / 6.0)

    smoothed = [statistics.median(levels[max(0, index - 1):min(len(levels), index + 2)]) for index in range(len(levels))]
    reference_levels = [level for frequency, level in zip(frequencies, smoothed) if 30.0 <= frequency <= 120.0 and math.isfinite(level)]
    if len(reference_levels) < 4:
        return {"detected": False, "reason": "insufficient 30-120 Hz passband bins"}
    ordered = sorted(reference_levels)
    upper_half = ordered[len(ordered) // 2:]
    passband_db = statistics.median(upper_half)
    threshold_db = passband_db - 3.0
    mask = [18.0 <= frequency <= 400.0 and level >= threshold_db for frequency, level in zip(frequencies, smoothed)]
    # Fill a single-bin room notch; a crossover roll-off must remain below the
    # threshold for at least two adjacent 1/6-octave centers.
    for index in range(1, len(mask) - 1):
        if not mask[index] and mask[index - 1] and mask[index + 1]:
            mask[index] = True
    groups: list[list[int]] = []
    current: list[int] = []
    for index, active in enumerate(mask):
        if active:
            current.append(index)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    groups = [
        group for group in groups
        if any(30.0 <= frequencies[index] <= 120.0 for index in group)
    ]
    if not groups:
        return {"detected": False, "reason": "no sustained -3 dB passband"}
    group = max(groups, key=lambda item: (len(item), max(smoothed[index] for index in item)))
    if len(group) < 2:
        return {"detected": False, "reason": "detected passband is too narrow"}
    lower_hz = max(15.0, frequencies[group[0]] / (2.0 ** (1.0 / 12.0)))
    upper_hz = min(500.0, frequencies[group[-1]] * (2.0 ** (1.0 / 12.0)))
    return {
        "detected": True,
        "lower_hz": round(lower_hz, 2),
        "upper_cutoff_hz": round(upper_hz, 2),
        "passband_reference_db": round(passband_db, 2),
        "threshold_db": round(threshold_db, 2),
        "method": "chirp-time normalized sustained passband; robust 30-120 Hz reference; -3 dB; one-bin notch tolerance",
    }


def coherent_sweep_integration_gain_db(active_seconds: float) -> float:
    """Matched-filter processing gain relative to the 2 s preflight ESS."""
    return max(0.0, 10.0 * math.log10(max(active_seconds, 1.0e-9) / 2.0))


def sweep_capture_quality(
    samples: list[float],
    reference: list[float],
    source: str | None = None,
) -> dict[str, Any]:
    """Estimate sweep SNR after locating the actual recorded active interval.

    ALSA process startup is not deterministic, especially on the first UMIK
    capture after boot.  A fixed 400 ms arm delay can therefore put part of the
    sweep in the nominal pre-roll and make a valid capture look quieter than its
    background.  Locate the highest-energy sweep-length window on a compact
    50 ms envelope, then use only noise blocks outside that window.
    """
    active_indices = [index for index, value in enumerate(reference) if abs(value) > 1.0e-12]
    if not active_indices:
        raise MeasurementError("측정 sweep 기준 신호가 비어 있습니다.")
    reference_start = active_indices[0]
    reference_end = active_indices[-1] + 1
    analysis_band_hz = [15.0, 22_000.0]
    full_reference_end = reference_end

    def ac_rms(values: list[float]) -> float:
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    envelope_block = round(0.05 * RATE)
    envelope_power = []
    for start in range(0, len(samples), envelope_block):
        values = samples[start:min(len(samples), start + envelope_block)]
        if len(values) >= envelope_block // 2:
            value = ac_rms(values)
            envelope_power.append(value * value)
    active_length = reference_end - reference_start
    active_blocks = max(1, math.ceil(active_length / envelope_block))
    if len(envelope_power) < active_blocks:
        raise MeasurementError("측정 sweep의 신호 구간을 평가할 수 없습니다.")
    rolling = sum(envelope_power[:active_blocks])
    window_powers = [rolling]
    for index in range(active_blocks, len(envelope_power)):
        rolling += envelope_power[index] - envelope_power[index - active_blocks]
        window_powers.append(rolling)
    best_power = max(window_powers)
    # A bandwidth-limited actuator can make a later sweep-length window look
    # stronger simply because its silent natural roll-off was shifted out of
    # the window. Search only around the known arecord -> 400 ms -> aplay
    # sequence; otherwise Front roll-off can masquerade as seconds of delay.
    nominal_block = round((round(0.4 * RATE) + reference_start) / envelope_block)
    timing_radius_blocks = round(0.75 * RATE / envelope_block)
    plausible_start = max(0, nominal_block - timing_radius_blocks)
    plausible_end = min(len(window_powers), nominal_block + timing_radius_blocks + 1)
    plausible_powers = window_powers[plausible_start:plausible_end]
    if not plausible_powers:
        raise MeasurementError("측정 sweep의 재생 시점을 평가할 수 없습니다.")
    plausible_best = max(plausible_powers)
    near_best = [
        index for index in range(plausible_start, plausible_end)
        if window_powers[index] >= plausible_best * 0.995
    ]
    best_block = min(near_best, key=lambda index: abs(index - nominal_block))
    active_start = min(len(samples), best_block * envelope_block)
    timing_method = "nominal arecord/aplay anchor with bounded +/-750 ms maximum-energy refinement"

    # Correlate a one-second signature from the middle of the ESS instead of
    # trusting total energy alone. Music or a U7 stream-switch transient in the
    # pre-roll can otherwise look like the strongest sweep-length window (the
    # real Pi2 capture reproduced this as a false -350 ms start). Restricting
    # both operands to the known ALSA start-latency range keeps this at a
    # 262144-point FFT for both 2 s and 14 s sweeps, instead of allocating a
    # multi-million-point full-sweep correlation on a 1 GB Pi2. A subwoofer is
    # deliberately excluded because its narrow low-pass output does not contain
    # the middle-band signature.
    if source not in SUBWOOFER_ONLY_SOURCES:
        signature_length = min(RATE, max(1, active_length // 2))
        signature_start = reference_start + (active_length - signature_length) // 2
        signature_end = signature_start + signature_length
        signature = reference[signature_start:signature_end]
        search_start = max(0, signature_start - round(0.35 * RATE))
        search_end = min(len(samples), signature_end + round(1.25 * RATE))
        search = samples[search_start:search_end]
        fft_length = next_power_of_two(len(search) + len(signature) - 1)
        fft = FFTBackend()
        try:
            sample_mean = sum(search) / len(search)
            reference_mean = sum(signature) / len(signature)
            sample_spectrum = fft.rfft((value - sample_mean for value in search), fft_length)
            reference_spectrum = fft.rfft(
                (value - reference_mean for value in signature), fft_length,
            )
            for index in range(len(sample_spectrum)):
                sample_spectrum[index] *= reference_spectrum[index].conjugate()
            correlation = fft.irfft(sample_spectrum, fft_length)
            maximum_lag = max(0, len(search) - len(signature))
            if maximum_lag:
                best_lag = max(
                    range(maximum_lag + 1),
                    key=lambda index: abs(correlation[index]),
                )
                capture_lead = search_start + best_lag - signature_start
                active_start = max(0, min(len(samples), capture_lead + reference_start))
                timing_method = "bounded FFT cross-correlation of a one-second middle-band ESS signature"
        finally:
            fft.close()
    active_end = min(len(samples), active_start + active_length)
    capture_lead = active_start - reference_start

    # Estimate stationary room noise from the quieter 35th percentile of all
    # 200 ms pre/post blocks. A U7/Camilla stream transition can contaminate
    # one side for several blocks; choosing max(pre, post) turned that click
    # into a false negative SNR. Persistent noise remains present on both
    # sides, while side imbalance is reported separately as contamination.
    noise_guard = round(0.10 * RATE)
    noise_block = round(0.20 * RATE)
    noise_segments = [
        samples[:max(0, active_start - noise_guard)],
        samples[min(len(samples), active_end + noise_guard):],
    ]
    noise_estimates = []
    noise_blocks_by_side: list[list[float]] = []
    for segment in noise_segments:
        blocks = [
            ac_rms(segment[start:start + noise_block])
            for start in range(0, len(segment) - noise_block + 1, noise_block)
        ]
        if blocks:
            noise_blocks_by_side.append(blocks)
            noise_estimates.append(percentile(blocks, 0.35))
    if not noise_estimates:
        raise MeasurementError("측정 sweep의 무음 구간을 평가할 수 없습니다.")
    all_noise_blocks = [value for blocks in noise_blocks_by_side for value in blocks]
    background_rms = percentile(all_noise_blocks, 0.35)
    noise_side_dbfs = [20.0 * math.log10(max(value, 1.0e-15)) for value in noise_estimates]
    noise_side_spread_db = max(noise_side_dbfs) - min(noise_side_dbfs) if len(noise_side_dbfs) > 1 else 0.0
    passband: dict[str, Any] | None = None
    if source in SUBWOOFER_ONLY_SOURCES:
        passband = detect_subwoofer_passband(
            samples, reference, capture_lead, reference_start, full_reference_end, background_rms,
        )
        if passband.get("detected"):
            lower_hz = float(passband["lower_hz"])
            upper_hz = float(passband["upper_cutoff_hz"])
            ratio_log = math.log(22_000.0 / 15.0)
            sweep_samples = full_reference_end - reference_start
            low_fraction = math.log(lower_hz / 15.0) / ratio_log
            high_fraction = math.log(upper_hz / 15.0) / ratio_log
            active_start = min(len(samples), capture_lead + reference_start + round(sweep_samples * low_fraction))
            active_end = min(len(samples), capture_lead + reference_start + round(sweep_samples * high_fraction))
            analysis_band_hz = [round(lower_hz, 2), round(upper_hz, 2)]
        else:
            fraction_300_hz = math.log(300.0 / 15.0) / math.log(22_000.0 / 15.0)
            active_end = min(len(samples), capture_lead + reference_start + round((full_reference_end - reference_start) * fraction_300_hz))
            analysis_band_hz = [15.0, 300.0]
    active = samples[active_start:active_end]
    # A 2 s full-band quick sweep maps a legitimate narrow subwoofer passband
    # (for example the measured T5S 53-95 Hz band) to about 160 ms. Requiring a
    # fixed 200 ms rejected that valid recording before SNR could be reported.
    # Fifty milliseconds still provides multiple low-frequency cycles and the
    # sustained -3 dB detector already requires at least two adjacent bins.
    minimum_active_samples = RATE // 20 if source in SUBWOOFER_ONLY_SOURCES else RATE
    if len(active) < minimum_active_samples:
        raise MeasurementError("측정 sweep의 무음/신호 구간을 평가할 수 없습니다.")
    active_rms = ac_rms(active)
    signal_power = max(0.0, active_rms * active_rms - background_rms * background_rms)
    raw_snr_db = 10.0 * math.log10(max(signal_power, 1.0e-30) / max(background_rms * background_rms, 1.0e-30))
    # ESS deconvolution is a coherent matched-filter operation: at the same
    # playback level, longer frequency dwell integrates more signal energy
    # while uncorrelated room noise grows only in power. The former scalar RMS
    # gate ignored that processing gain, so a 14 s sweep could fail after its
    # otherwise identical 2 s preflight passed. Keep the 2 s quick check as the
    # zero-gain safety reference and expose both numbers for auditability.
    reference_active_seconds = active_length / RATE
    coherent_integration_gain_db = coherent_sweep_integration_gain_db(reference_active_seconds)
    snr_db = raw_snr_db + coherent_integration_gain_db
    return {
        "background_rms_dbfs": round(20.0 * math.log10(max(background_rms, 1.0e-15)), 2),
        "active_rms_dbfs": round(20.0 * math.log10(max(active_rms, 1.0e-15)), 2),
        "estimated_signal_rms_dbfs": round(10.0 * math.log10(max(signal_power, 1.0e-30)), 2),
        "snr_db": round(snr_db, 2),
        "raw_instantaneous_snr_db": round(raw_snr_db, 2),
        "coherent_integration_gain_db": round(coherent_integration_gain_db, 2),
        "reference_active_seconds": round(reference_active_seconds, 3),
        "minimum_usable_snr_db": MINIMUM_USABLE_SNR_DB,
        "recommended_snr_db": RECOMMENDED_SNR_DB,
        "usable": snr_db >= MINIMUM_USABLE_SNR_DB,
        "recommended": snr_db >= RECOMMENDED_SNR_DB,
        "analysis_band_hz": analysis_band_hz,
        "subwoofer_passband": passband,
        "source": source,
        "active_interval_samples": [active_start, active_end],
        "capture_delay_samples": capture_lead,
        "capture_delay_ms": round(capture_lead * 1000.0 / RATE, 3),
        "timing_method": timing_method,
        "noise_segments_used": len(noise_estimates),
        "noise_blocks_used": len(all_noise_blocks),
        "noise_side_estimates_dbfs": [round(value, 2) for value in noise_side_dbfs],
        "noise_side_spread_db": round(noise_side_spread_db, 2),
        "switching_transient_suspected": noise_side_spread_db >= 4.0,
        "noise_method": "35th percentile of 200 ms pre/post AC-RMS blocks; side imbalance reported separately",
    }


def measurement_level_guidance(
    quality: dict[str, Any],
    configured_level_dbfs: int,
    peak_dbfs: float,
) -> dict[str, Any]:
    """Return exact minimum/recommended output changes with peak headroom."""
    snr_db = float(quality.get("snr_db", -120.0))
    safe_raise_db = max(0, int(math.floor(-3.0 - float(peak_dbfs))))
    minimum_raise_db = max(0, int(math.ceil(MINIMUM_USABLE_SNR_DB - snr_db)))
    recommended_raise_db = max(0, int(math.ceil(RECOMMENDED_SNR_DB - snr_db)))
    minimum_raise_db = min(minimum_raise_db, safe_raise_db)
    recommended_raise_db = min(recommended_raise_db, safe_raise_db)
    minimum_level = min(0, int(configured_level_dbfs) + minimum_raise_db)
    recommended_level = min(0, int(configured_level_dbfs) + recommended_raise_db)
    return {
        "configured_level_dbfs": int(configured_level_dbfs),
        "minimum_raise_db": minimum_raise_db,
        "minimum_level_dbfs": minimum_level,
        "recommended_raise_db": recommended_raise_db,
        "recommended_level_dbfs": recommended_level,
        "safe_raise_limit_db": safe_raise_db,
        "minimum_usable_snr_db": MINIMUM_USABLE_SNR_DB,
        "recommended_snr_db": RECOMMENDED_SNR_DB,
        "headroom_limited": recommended_raise_db < max(0, int(math.ceil(RECOMMENDED_SNR_DB - snr_db))),
    }


def measurement_quality_message(
    quality: dict[str, Any],
    source: str | None,
    configured_level_dbfs: int,
    peak_dbfs: float,
) -> tuple[str, dict[str, Any]]:
    guidance = measurement_level_guidance(quality, configured_level_dbfs, peak_dbfs)
    source_label = SOURCE_LABELS.get(str(source), str(source or "측정 채널"))
    band = quality.get("analysis_band_hz") or [15.0, 22_000.0]
    snr_db = float(quality.get("snr_db", -120.0))
    integration_gain = float(quality.get("coherent_integration_gain_db", 0.0))
    raw_snr = float(quality.get("raw_instantaneous_snr_db", snr_db))
    integration_note = (
        f" · 원신호 {raw_snr:.1f} + ESS 적분 {integration_gain:.1f} dB"
        if integration_gain >= 0.05 else ""
    )
    if guidance["headroom_limited"]:
        action = (
            f"디지털 출력을 최대 +{guidance['safe_raise_limit_db']} dB까지만 올릴 수 있습니다. "
            "기기 볼륨·마이크 위치를 확인하거나 Sweep 길이를 늘리세요."
        )
    else:
        action = (
            f"Sweep 출력을 {configured_level_dbfs} → {guidance['recommended_level_dbfs']} dBFS"
            f" (+{guidance['recommended_raise_db']} dB)로 올리면 권장 품질을 예상할 수 있습니다."
        )
    message = (
        f"{source_label} 유효 SNR {snr_db:.1f} dB{integration_note} "
        f"(평가 {float(band[0]):g}–{float(band[1]):g} Hz · 최소 {MINIMUM_USABLE_SNR_DB:g} / 권장 {RECOMMENDED_SNR_DB:g} dB). "
        f"{action}"
    )
    return message, guidance


def decay_fit(values: list[float]) -> dict[str, Any]:
    """Noise-compensated Schroeder EDT/T20 estimate for one impulse band."""
    if len(values) < RATE // 2:
        return {"reliable": False, "reason": "impulse window too short"}
    squares = [value * value for value in values]
    tail = squares[int(len(squares) * 0.85):]
    noise_power = statistics.median(tail) if tail else 0.0
    cumulative = [0.0] * len(squares)
    running = 0.0
    for index in range(len(squares) - 1, -1, -1):
        running += squares[index]
        compensated = running - noise_power * (len(squares) - index)
        cumulative[index] = max(compensated, 1.0e-30)
    reference = max(cumulative[0], 1.0e-30)
    decay_db = [10.0 * math.log10(max(value, 1.0e-30) / reference) for value in cumulative]

    def regression(upper_db: float, lower_db: float) -> tuple[float | None, float | None, int]:
        points = [(index / RATE, level) for index, level in enumerate(decay_db) if lower_db <= level <= upper_db]
        if len(points) < RATE // 20:
            return None, None, len(points)
        count = len(points)
        mean_x = sum(item[0] for item in points) / count
        mean_y = sum(item[1] for item in points) / count
        denominator = sum((item[0] - mean_x) ** 2 for item in points)
        if denominator <= 0.0:
            return None, None, count
        slope = sum((item[0] - mean_x) * (item[1] - mean_y) for item in points) / denominator
        if slope >= -1.0e-9:
            return None, None, count
        intercept = mean_y - slope * mean_x
        residual = sum((level - (intercept + slope * seconds)) ** 2 for seconds, level in points)
        total = sum((level - mean_y) ** 2 for _, level in points)
        r_squared = 1.0 - residual / total if total > 1.0e-12 else 1.0
        return -60.0 / slope, r_squared, count

    edt, edt_r2, _ = regression(0.0, -10.0)
    t20, t20_r2, points = regression(-5.0, -25.0)
    reliable = bool(t20 is not None and t20_r2 is not None and t20_r2 >= 0.80 and 0.05 <= t20 <= 5.0)
    return {
        "edt_s": round(edt, 3) if edt is not None else None,
        "edt_r_squared": round(edt_r2, 4) if edt_r2 is not None else None,
        "t20_rt60_s": round(t20, 3) if t20 is not None else None,
        "t20_r_squared": round(t20_r2, 4) if t20_r2 is not None else None,
        "fit_points": points,
        "noise_floor_db": round(10.0 * math.log10(max(noise_power, 1.0e-30) / max(reference, 1.0e-30)), 2),
        "reliable": reliable,
    }


def room_decay_metrics(impulse: list[float], peak_index: int, fft: FFTBackend) -> dict[str, Any]:
    """Estimate octave-band room decay without attempting unstable late-reverb inversion."""
    pre_roll = round(0.10 * RATE)
    analysis_seconds = 2.5
    segment_frames = round(analysis_seconds * RATE)
    fft_length = next_power_of_two(segment_frames)
    start = peak_index - pre_roll
    segment = [impulse[(start + index) % len(impulse)] for index in range(segment_frames)]
    fade_start = int(segment_frames * 0.90)
    for index in range(fade_start, segment_frames):
        fraction = (index - fade_start) / max(1, segment_frames - fade_start - 1)
        segment[index] *= 0.5 + 0.5 * math.cos(math.pi * fraction)
    spectrum = fft.rfft(segment, fft_length)
    bands = []
    for center in (63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0):
        inner_low, inner_high = center / math.sqrt(2.0), center * math.sqrt(2.0)
        outer_low, outer_high = center / 2.0, center * 2.0
        filtered = []
        for index, value in enumerate(spectrum):
            frequency = index * RATE / fft_length
            if frequency <= outer_low or frequency >= outer_high:
                weight = 0.0
            elif frequency < inner_low:
                position = math.log(frequency / outer_low) / math.log(inner_low / outer_low)
                weight = 0.5 - 0.5 * math.cos(math.pi * position)
            elif frequency <= inner_high:
                weight = 1.0
            else:
                position = math.log(frequency / inner_high) / math.log(outer_high / inner_high)
                weight = 0.5 + 0.5 * math.cos(math.pi * position)
            filtered.append(value * weight)
        band_impulse = fft.irfft(filtered, fft_length)
        search_end = min(len(band_impulse), pre_roll + round(0.40 * RATE))
        band_peak = max(range(search_end), key=lambda index: abs(band_impulse[index]))
        decay = decay_fit(band_impulse[band_peak:min(len(band_impulse), band_peak + round(2.0 * RATE))])
        bands.append({"center_hz": int(center), **decay})
    reliable_values = [item["t20_rt60_s"] for item in bands if item.get("reliable")]
    return {
        "method": "noise-compensated Schroeder T20/EDT after cosine-tapered octave-band FFT filtering",
        "analysis_window_s": analysis_seconds,
        "bands": bands,
        "reliable_band_count": len(reliable_values),
        "median_t20_rt60_s": round(statistics.median(reliable_values), 3) if reliable_values else None,
        "correction_policy": "late reverb is never inverted; reliable long-decay bass bands may receive up to 3 dB additional cut",
    }


def temporal_room_metrics(impulse: list[float], peak_index: int) -> dict[str, Any]:
    """ISO-3382-inspired energy ratios plus reflection diagnostics.

    These are engineering diagnostics from one loudspeaker/receiver transfer
    path, not a certified diffuse-field room-acoustics measurement.
    """
    length = min(len(impulse), round(1.2 * RATE))
    aligned = [impulse[(peak_index + index) % len(impulse)] for index in range(length)]
    energy = [value * value for value in aligned]
    total = max(sum(energy), 1.0e-30)

    def energy_until(milliseconds: float) -> float:
        return sum(energy[:min(length, round(milliseconds * RATE / 1000.0))])

    early_50 = energy_until(50.0)
    early_80 = energy_until(80.0)
    late_50 = max(total - early_50, 1.0e-30)
    late_80 = max(total - early_80, 1.0e-30)
    direct = energy_until(5.0)
    after_direct = max(total - direct, 1.0e-30)
    center_seconds = sum((index / RATE) * value for index, value in enumerate(energy)) / total
    peak = max(abs(aligned[0]), 1.0e-15)

    reflection_windows = []
    for start_ms, end_ms in ((1, 5), (5, 20), (20, 80)):
        start = min(length, round(start_ms * RATE / 1000.0))
        end = min(length, round(end_ms * RATE / 1000.0))
        relative = max((abs(value) for value in aligned[start:end]), default=0.0) / peak
        reflection_windows.append({
            "window_ms": [start_ms, end_ms],
            "strongest_relative_db": round(20.0 * math.log10(max(relative, 1.0e-15)), 2),
        })
    return {
        "method": "single-path energy ratios aligned to the direct impulse peak",
        "c50_db": round(10.0 * math.log10(max(early_50, 1.0e-30) / late_50), 3),
        "c80_db": round(10.0 * math.log10(max(early_80, 1.0e-30) / late_80), 3),
        "d50_percent": round(100.0 * early_50 / total, 3),
        "center_time_ms": round(center_seconds * 1000.0, 3),
        "direct_to_remainder_db": round(10.0 * math.log10(max(direct, 1.0e-30) / after_direct), 3),
        "reflection_windows": reflection_windows,
        "classification": "diagnostic_only_above_bass",
    }


def group_delay_metrics(frequencies: list[float], phases: list[float]) -> dict[str, Any]:
    values = []
    for index in range(1, len(frequencies) - 1):
        frequency = frequencies[index]
        if not 20.0 <= frequency <= 300.0:
            continue
        delta_frequency = frequencies[index + 1] - frequencies[index - 1]
        if delta_frequency <= 0.0:
            continue
        seconds = -(phases[index + 1] - phases[index - 1]) / (2.0 * math.pi * delta_frequency)
        if math.isfinite(seconds):
            values.append(abs(seconds) * 1000.0)
    return {
        "method": "absolute residual group delay after bulk-delay removal",
        "frequency_range_hz": [20, 300],
        "bass_median_ms": round(statistics.median(values), 3) if values else None,
        "bass_p90_ms": round(percentile(values, 0.90), 3) if values else None,
        "classification": "limited_low_frequency_correction",
    }


def frequency_noise_metrics(
    samples: list[float],
    spectrum: list[complex],
    fft_length: int,
    frequencies: list[float],
    fft: FFTBackend,
    evaluation_band_hz: list[float] | None = None,
    active_interval_samples: list[int] | None = None,
) -> dict[str, Any]:
    """Estimate per-band confidence from pre/post-roll noise and sweep spectrum."""
    if active_interval_samples and len(active_interval_samples) == 2:
        active_start = max(0, min(len(samples), int(active_interval_samples[0])))
        active_end = max(active_start, min(len(samples), int(active_interval_samples[1])))
        guard = round(0.10 * RATE)
        pre_end = max(0, active_start - guard)
        post_start = min(len(samples), active_end + guard)
        pre = samples[max(0, pre_end - round(0.60 * RATE)):pre_end]
        post = samples[post_start:min(len(samples), post_start + round(0.75 * RATE))]
        if len(pre) < round(0.20 * RATE):
            pre = post
        if len(post) < round(0.20 * RATE):
            post = pre
    else:
        pre = samples[round(0.05 * RATE):round(0.65 * RATE)]
        post = samples[max(0, len(samples) - round(0.75 * RATE)):]

    def centered(values: list[float]) -> list[float]:
        mean = sum(values) / max(1, len(values))
        return [value - mean for value in values]

    def stable_noise_window(values: list[float]) -> list[float]:
        block = round(0.20 * RATE)
        candidates = [
            values[start:start + block]
            for start in range(0, len(values) - block + 1, block)
        ]
        if not candidates:
            return values
        def power(candidate: list[float]) -> float:
            mean = sum(candidate) / len(candidate)
            return sum((value - mean) ** 2 for value in candidate) / len(candidate)
        # Lowest stable block rejects one-off selector/stream transitions. A
        # persistent fan, HVAC or household floor remains in every candidate.
        return min(candidates, key=power)

    pre = stable_noise_window(pre)
    post = stable_noise_window(post)

    noise_fft_length = next_power_of_two(max(len(pre), len(post)))
    pre_spectrum = fft.rfft(centered(pre), noise_fft_length)
    post_spectrum = fft.rfft(centered(post), noise_fft_length)
    pre_scale = len(samples) / max(1, len(pre))
    post_scale = len(samples) / max(1, len(post))
    snr_values: list[float] = []
    confidence: list[float] = []
    for frequency in frequencies:
        center_bin = frequency * fft_length / RATE
        low_bin = max(1, int(center_bin / (2.0 ** (1.0 / 24.0))))
        high_bin = min(len(spectrum) - 1, max(low_bin + 2, math.ceil(center_bin * (2.0 ** (1.0 / 24.0)))))
        signal_bins = [
            spectrum[index].real * spectrum[index].real + spectrum[index].imag * spectrum[index].imag
            for index in range(low_bin, high_bin + 1)
        ]
        noise_center_bin = frequency * noise_fft_length / RATE
        noise_low_bin = max(1, int(noise_center_bin / (2.0 ** (1.0 / 24.0))))
        noise_high_bin = min(
            len(pre_spectrum) - 1,
            max(noise_low_bin + 2, math.ceil(noise_center_bin * (2.0 ** (1.0 / 24.0)))),
        )
        noise_bins = [
            max(
                (pre_spectrum[index].real ** 2 + pre_spectrum[index].imag ** 2) * pre_scale,
                (post_spectrum[index].real ** 2 + post_spectrum[index].imag ** 2) * post_scale,
            )
            for index in range(noise_low_bin, noise_high_bin + 1)
        ]
        total = statistics.median(signal_bins) if signal_bins else 0.0
        noise = statistics.median(noise_bins) if noise_bins else 0.0
        coherent = max(0.0, total - noise)
        snr = 10.0 * math.log10(max(coherent, 1.0e-30) / max(noise, 1.0e-30))
        snr_values.append(max(-30.0, min(80.0, snr)))
        confidence.append(max(0.0, min(1.0, (snr - 6.0) / 9.0)))

    # Sudden positive energy spikes are not a normal smooth exponential-sweep
    # envelope. This catches speech onsets, impacts, and doors without treating
    # the normal low/high roll-off as contamination.
    block = round(0.10 * RATE)
    block_levels = []
    for start in range(round(0.70 * RATE), max(round(0.70 * RATE), len(samples) - round(1.0 * RATE)), block):
        values = samples[start:min(len(samples), start + block)]
        if len(values) < block // 2:
            continue
        mean = sum(values) / len(values)
        rms = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
        block_levels.append(20.0 * math.log10(max(rms, 1.0e-15)))
    positive_outliers = []
    for index, level in enumerate(block_levels):
        neighborhood = block_levels[max(0, index - 3):index] + block_levels[index + 1:min(len(block_levels), index + 4)]
        if len(neighborhood) >= 3:
            positive_outliers.append(level - statistics.median(neighborhood))
    maximum_outlier_db = max(positive_outliers, default=0.0)
    transient_detected = maximum_outlier_db >= 15.0
    evaluation_low, evaluation_high = evaluation_band_hz or [20.0, 20_000.0]
    usable_values = [value for frequency, value in zip(frequencies, snr_values) if evaluation_low <= frequency <= evaluation_high]
    return {
        "frequencies": [round(value, 3) for value in frequencies],
        "snr_db": [round(value, 2) for value in snr_values],
        "confidence": [round(value, 4) for value in confidence],
        "minimum_snr_db": round(min(usable_values), 2) if usable_values else None,
        "median_snr_db": round(statistics.median(usable_values), 2) if usable_values else None,
        "maximum_sweep_envelope_outlier_db": round(maximum_outlier_db, 2),
        "transient_contamination_detected": transient_detected,
        "evaluation_band_hz": [round(evaluation_low, 2), round(evaluation_high, 2)],
        "noise_fft_size": noise_fft_length,
        "method": "max of stable 200 ms pre/post noise PSD windows; local 1/12-octave FFT SNR; 6-15 dB confidence ramp; 100 ms positive-transient detector",
    }


def assess_bulk_delay(raw_peak_index: int, fft_length: int) -> tuple[int, dict[str, Any]]:
    """Reject non-causal/implausibly late ESS peaks before phase correction."""
    signed_peak_index = raw_peak_index - fft_length if raw_peak_index > fft_length // 2 else raw_peak_index
    reliable = 0 <= signed_peak_index <= MAX_PLAUSIBLE_BULK_DELAY_SAMPLES
    details = {
        "reliable": reliable,
        "raw_peak_index": raw_peak_index,
        "signed_peak_samples": signed_peak_index,
        "plausible_range_samples": [0, MAX_PLAUSIBLE_BULK_DELAY_SAMPLES],
        "plausible_range_ms": [0.0, round(MAX_PLAUSIBLE_BULK_DELAY_SAMPLES * 1000.0 / RATE, 3)],
        "method": "global deconvolved-impulse peak with a causal 250 ms device/acoustic plausibility gate",
        "reason": None if reliable else "global impulse peak is outside the causal 0-250 ms window; phase, decay and Front/Woofer delay correction are disabled",
    }
    return (signed_peak_index if reliable else 0), details


def regularized_transfer_spectrum(
    samples: list[float],
    reference: list[float],
    fft: FFTBackend,
) -> tuple[list[complex], int, float]:
    """Recover Y/X with scale-relative regularization.

    Because the regularization follows the reference power, changing the
    playback dBFS or the temporary Woofer measurement attenuation changes only
    SNR/headroom, not the recovered transfer magnitude.
    """
    length = next_power_of_two(max(len(samples), len(reference)))
    y = fft.rfft(samples, length)
    x = fft.rfft(reference, length)
    maximum_power = max((value.real * value.real + value.imag * value.imag) for value in x)
    regularization = maximum_power * 1.0e-9
    transfer: list[complex] = []
    for output_value, input_value in zip(y, x):
        power = input_value.real * input_value.real + input_value.imag * input_value.imag
        transfer.append(output_value * input_value.conjugate() / (power + regularization))
    return transfer, length, regularization


def response_from_recording(
    recorded: Path,
    reference: list[float],
    cal: dict[str, Any],
    source: str | None = None,
    *,
    configured_level_dbfs: int | None = None,
) -> dict[str, Any]:
    _, bits, samples = read_pcm_wav(recorded)
    if len(samples) < RATE:
        raise MeasurementError("녹음이 너무 짧습니다.")
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if peak >= 0.988:
        raise MeasurementError("UMIK 입력이 클리핑되었습니다. 볼륨을 낮추세요.")
    quality = sweep_capture_quality(samples, reference, source)
    if configured_level_dbfs is None:
        try:
            configured_level_dbfs = int(load_current().get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS))
        except Exception:
            configured_level_dbfs = DEFAULT_SWEEP_LEVEL_DBFS
    else:
        configured_level_dbfs = int(configured_level_dbfs)
    quality_message, level_guidance = measurement_quality_message(
        quality,
        source,
        configured_level_dbfs,
        20.0 * math.log10(max(peak, 1.0e-15)),
    )
    quality["level_guidance"] = level_guidance
    if not quality["usable"]:
        if CURRENT.is_file():
            update_current(last_measurement_quality={
                "source": source,
                "message": quality_message,
                "quality": quality,
                "recording": recorded.name,
            })
        raise MeasurementError(quality_message)
    capture_delay = int(quality.get("capture_delay_samples", round(0.4 * RATE)))
    if capture_delay >= 0:
        delayed_reference = [0.0] * capture_delay + reference
    else:
        delayed_reference = reference[min(len(reference), -capture_delay):]
    fft = FFTBackend()
    h, length, regularization = regularized_transfer_spectrum(samples, delayed_reference, fft)
    y = fft.rfft(samples, length)
    impulse = fft.irfft(h, length)
    raw_peak_index = max(range(length), key=lambda index: abs(impulse[index]))
    peak_index, bulk_delay = assess_bulk_delay(raw_peak_index, length)
    bulk_delay_reliable = bool(bulk_delay["reliable"])
    # ESS harmonic products and low-frequency noise can dominate the global
    # deconvolution peak of a bandwidth-limited subwoofer many seconds after
    # the direct response.  Never turn that artifact into a physical delay or
    # a room-decay correction.  Magnitude remains usable through its separate
    # passband/SNR confidence gate.
    if bulk_delay_reliable:
        decay = room_decay_metrics(impulse, raw_peak_index, fft)
        temporal = temporal_room_metrics(impulse, raw_peak_index)
    else:
        decay = {
            "analysis_window_s": None,
            "bands": [],
            "correction_policy": "disabled because the direct impulse peak is not reliable",
            "median_t20_rt60_s": None,
            "method": "not evaluated",
            "reliable_band_count": 0,
            "reason": bulk_delay["reason"],
        }
        temporal = {
            "reliable": False,
            "classification": "insufficient_data",
            "method": "not evaluated",
            "reason": bulk_delay["reason"],
        }
    frequencies = [20.0 * (1000.0 ** (index / 511.0)) for index in range(512)]
    frequency_noise = frequency_noise_metrics(
        samples,
        y,
        length,
        frequencies,
        fft,
        quality.get("analysis_band_hz"),
        quality.get("active_interval_samples"),
    )
    quality["frequency_noise"] = {
        key: value for key, value in frequency_noise.items()
        if key not in ("frequencies", "snr_db", "confidence")
    }
    if frequency_noise["transient_contamination_detected"]:
        quality["recommended"] = False
    levels: list[float] = []
    phases: list[float] = []
    cal_f = cal["frequencies"]
    cal_db = cal["corrections"]
    for frequency in frequencies:
        bin_value = frequency * length / RATE
        lo = min(len(h) - 2, max(0, int(bin_value)))
        fraction = bin_value - lo
        magnitude = abs(h[lo]) * (1.0 - fraction) + abs(h[lo + 1]) * fraction
        levels.append(20.0 * math.log10(max(magnitude, 1.0e-15)) + interpolate_log(cal_f, cal_db, frequency))
        value = h[lo] * (1.0 - fraction) + h[lo + 1] * fraction
        # Remove only the bulk acoustic/device delay. The remaining phase is
        # used by the optional low-frequency excess-phase correction.
        phase = math.atan2(value.imag, value.real) + 2.0 * math.pi * frequency * peak_index / RATE
        phases.append(phase)
    for index in range(1, len(phases)):
        while phases[index] - phases[index - 1] > math.pi:
            phases[index] -= 2.0 * math.pi
        while phases[index] - phases[index - 1] < -math.pi:
            phases[index] += 2.0 * math.pi
    smoothed = variable_power_smooth(frequencies, levels)
    group_delay = group_delay_metrics(frequencies, phases) if bulk_delay_reliable else {
        "reliable": False,
        "classification": "insufficient_data",
        "frequency_range_hz": [20, 300],
        "method": "not evaluated",
        "reason": bulk_delay["reason"],
    }
    return {
        "frequencies": [round(value, 3) for value in frequencies],
        "db": [round(value, 4) for value in smoothed],
        "phase_rad": [round(value, 7) for value in phases],
        "bulk_delay_samples": peak_index,
        "bulk_delay_ms": round(peak_index * 1000.0 / RATE, 3),
        "bulk_delay_reliable": bulk_delay_reliable,
        "bulk_delay": bulk_delay,
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1.0e-15)), 2),
        "rms_dbfs": round(20.0 * math.log10(max(rms, 1.0e-15)), 2),
        "capture_bits": bits,
        "fft_backend": fft.kind,
        "fft_size": length,
        "response_algorithm_revision": RESPONSE_ALGORITHM_REVISION,
        "smoothing": SMOOTHING_NAME,
        "measurement_quality": quality,
        "room_decay": decay,
        "temporal": temporal,
        "group_delay": group_delay,
        "frequency_quality": frequency_noise,
    }


def inspect_saved_recording(
    position: int,
    source: str,
    *,
    reprocess: bool = False,
    batch_reprocess: bool = False,
) -> dict[str, Any]:
    """Quality-check or rebuild one saved raw capture without playing sound."""
    state = load_current()
    positions_total = session_position_count(state)
    if position not in range(1, positions_total + 1) or source not in state.get("sources", []):
        raise MeasurementError("현재 세션의 측정 위치/채널이 아닙니다.")
    directory = Path(state["session_dir"])
    recorded = directory / f"p{position}_{source}_recorded.wav"
    if not recorded.is_file():
        raise MeasurementError(f"저장된 원본 녹음이 없습니다: {recorded.name}")
    reference = reference_sweep_for_source(
        source,
        int(state["level_dbfs"]),
        int(state["sweep_seconds"]),
        woofer_attenuation_db=float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
    )
    _, bits, samples = read_pcm_wav(recorded)
    quality = sweep_capture_quality(samples, reference, source)
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    result: dict[str, Any] = {
        "position": position,
        "source": source,
        "recording": recorded.name,
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1.0e-15)), 2),
        "rms_dbfs": round(20.0 * math.log10(max(rms, 1.0e-15)), 2),
        "capture_bits": bits,
        "measurement_quality": quality,
        "sound_played": False,
        "reprocessed": False,
    }
    if not reprocess:
        return result
    calibration = calibration_for(state["orientation"])
    response = response_from_recording(recorded, reference, calibration, source)
    response_path = directory / f"p{position}_{source}_response.json"
    atomic_json(response_path, response)
    measurements = [
        item for item in state.get("measurements", [])
        if (int(item.get("position", 0)), str(item.get("source", ""))) != (position, source)
    ]
    measurements.append({
        "position": position,
        "source": source,
        "recording": recorded.name,
        "response": response_path.name,
        "peak_dbfs": response["peak_dbfs"],
        "rms_dbfs": response["rms_dbfs"],
        "snr_db": response["measurement_quality"]["snr_db"],
        "quality_recommended": response["measurement_quality"]["recommended"],
    })
    completed_keys = {
        (int(item.get("position", 0)), str(item.get("source", "")))
        for item in measurements
        if (directory / str(item.get("response", ""))).is_file()
    }
    completed_positions = 0
    for candidate in range(1, positions_total + 1):
        if all((candidate, candidate_source) in completed_keys for candidate_source in state["sources"]):
            completed_positions = candidate
        else:
            break
    job_state = "measured" if completed_positions == positions_total else "ready"
    completion_fields: dict[str, Any] = {}
    if completed_positions == positions_total and state.get("mode") in PREMEASURED_SUM_MODES:
        preferences = load_correction_preferences()
        sum_model = evaluate_premeasured_sum_model(
            directory,
            positions_total,
            float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
            int(preferences.get("crossover_frequency_hz", 100)),
        )
        completion_fields["premeasured_sum_validation"] = sum_model
        stage = (
            f"저장 원본 {positions_total}위치 재계산 완료 · 합산 일치 검증 PASS · FIR 계산 가능"
            if sum_model["pass"] else
            f"저장 원본 {positions_total}위치 재계산 완료 · 합산 모델 FAIL · 3단계 조치 확인"
        )
    elif completed_positions == positions_total:
        stage = f"저장 원본 {positions_total}위치 재계산 완료 · FIR 계산 가능"
    else:
        stage = f"저장 원본 무음 재계산 완료 · 위치 {completed_positions + 1} 준비"
    updated = update_current(
        state="processing" if batch_reprocess and completed_positions < positions_total else job_state,
        stage=stage,
        error=None,
        worker_pid=state.get("worker_pid") if batch_reprocess else None,
        measurements=measurements,
        positions_completed=completed_positions,
        progress=100.0 * completed_positions / positions_total,
        last_measurement_quality=None,
        **completion_fields,
    )
    atomic_json(directory / "session.json", updated)
    result["reprocessed"] = True
    result["response"] = response_path.name
    result["positions_completed"] = completed_positions
    result["measurement_quality"] = response["measurement_quality"]
    return result


def reprocess_saved_recordings_worker() -> None:
    """Rebuild every ESS and simultaneous phase result without playback."""
    state = load_current()
    if state.get("state") == "idle" or not state.get("session_dir"):
        raise MeasurementError("재계산할 측정 Session이 없습니다.")
    directory = Path(state["session_dir"])
    expected = [
        (position, source)
        for position in range(1, session_position_count(state) + 1)
        for source in state.get("sources", [])
    ]
    available = [
        item for item in expected
        if (directory / f"p{item[0]}_{item[1]}_recorded.wav").is_file()
    ]
    if not available:
        raise MeasurementError("저장된 원본 녹음이 없습니다.")
    missing = [item for item in expected if item not in available]
    if missing:
        names = ", ".join(f"P{position} {SOURCE_LABELS.get(source, source)}" for position, source in missing)
        raise MeasurementError(f"저장 원본이 부족합니다: {names}. 3 · 위치 측정에서 해당 위치를 측정하세요.")
    phase_positions = (
        list(range(1, session_position_count(state) + 1))
        if state.get("mode") in SEPARATE_WOOFER_MODES else []
    )
    missing_phase = [
        position for position in phase_positions
        if not (directory / f"p{position}_phase_reference_recorded.wav").is_file()
        or not (directory / f"p{position}_phase_reference_signal.json").is_file()
    ]
    if missing_phase:
        names = ", ".join(f"P{position} L/R/우퍼 동시 위상 기준" for position in missing_phase)
        raise MeasurementError(f"저장된 위상 원본이 부족합니다: {names}. 3 · 위치 측정에서 해당 위치를 다시 측정하세요.")
    total_items = len(available) + len(phase_positions)
    for index, (position, source) in enumerate(available):
        update_current(
            state="processing",
            stage=f"저장 원본 재계산 {index + 1}/{total_items} · P{position} {SOURCE_LABELS.get(source, source)}",
            progress=100.0 * index / total_items,
            eta_seconds=round((total_items - index) * platform_capabilities()["offline_estimates_seconds"]["response_per_channel"]),
            last_measurement_quality=None,
        )
        inspect_saved_recording(position, source, reprocess=True, batch_reprocess=True)
    calibration = calibration_for(state["orientation"])
    phase_references: list[dict[str, Any]] = []
    for offset, position in enumerate(phase_positions):
        item_index = len(available) + offset
        update_current(
            state="processing",
            stage=f"저장 원본 재계산 {item_index + 1}/{total_items} · P{position} L/R/우퍼 상대 위상",
            progress=100.0 * item_index / total_items,
            eta_seconds=round((total_items - item_index) * platform_capabilities()["offline_estimates_seconds"]["response_per_channel"] * 0.4),
        )
        metadata_path = directory / f"p{position}_phase_reference_signal.json"
        recorded_path = directory / f"p{position}_phase_reference_recorded.wav"
        result_path = directory / f"p{position}_phase_reference.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        phase_result = phase_reference_from_recording(recorded_path, metadata, calibration)
        atomic_json(result_path, phase_result)
        phase_references.append({
            "position": position,
            "recording": recorded_path.name,
            "signal": metadata_path.name,
            "result": result_path.name,
            "reliable": bool(phase_result["reliable"]),
            "minimum_median_snr_db": phase_result["minimum_median_snr_db"],
            "phase_repeatability_p90_deg": phase_result["phase_repeatability_p90_deg"],
        })
    final_state = load_current()
    completion_fields: dict[str, Any] = {"phase_references": phase_references}
    sum_model = None
    if final_state.get("mode") in PREMEASURED_SUM_MODES:
        preferences = load_correction_preferences()
        sum_model = evaluate_premeasured_sum_model(
            directory,
            session_position_count(final_state),
            float(final_state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
            int(preferences.get("crossover_frequency_hz", 100)),
        )
        completion_fields["premeasured_sum_validation"] = sum_model
    phase_pass = not phase_positions or (
        len(phase_references) == len(phase_positions)
        and all(bool(item.get("reliable")) for item in phase_references)
    )
    sum_pass = sum_model is None or bool(sum_model.get("pass"))
    stage = (
        "저장된 ESS·동시 위상 원본 무음 재계산 완료 · 합산·위상 PASS · FIR 계산 가능"
        if phase_pass and sum_pass else
        "저장된 ESS·동시 위상 원본 무음 재계산 완료 · 3단계 진단을 확인하세요"
    )
    final_state = update_current(
        state="measured",
        worker_pid=None,
        active_pids=[],
        error=None,
        eta_seconds=None,
        progress=100.0,
        stage=stage,
        **completion_fields,
    )
    atomic_json(directory / "session.json", final_state)


def validate_measurement_output_levels(
    sweep_level_dbfs: int,
    noise_level_dbfs: int,
    woofer_attenuation_db: int,
) -> None:
    if sweep_level_dbfs not in ALLOWED_SWEEP_LEVELS:
        raise MeasurementError("Sweep 출력은 -54~0 dBFS 범위여야 합니다.")
    if noise_level_dbfs not in ALLOWED_NOISE_LEVELS:
        raise MeasurementError("레벨 검사 출력은 -54~-6 dBFS 범위여야 합니다.")
    if woofer_attenuation_db not in ALLOWED_WOOFER_MEASUREMENT_ATTENUATIONS:
        raise MeasurementError("우퍼 측정 상대레벨은 -18~0 dB 범위여야 합니다.")


def new_session(
    mode: str,
    orientation: str,
    level_dbfs: int,
    sweep_seconds: int,
    noise_level_dbfs: int | None = None,
    woofer_measurement_attenuation_db: int | None = None,
    position_count: int = POSITIONS,
) -> dict[str, Any]:
    if mode not in SOURCES:
        raise MeasurementError("측정 모드가 잘못되었습니다.")
    capability = platform_capabilities()
    if mode in MIMO_MODES and not capability["mimo_supported"]:
        raise MeasurementError("MIMO 측정/보정은 Raspberry Pi 4/5 전용입니다.")
    if position_count not in ALLOWED_POSITION_COUNTS:
        raise MeasurementError("측정 위치 수는 빠른 측정 1위치 또는 표준 측정 3위치여야 합니다.")
    if mode in MIMO_MODES and position_count != POSITIONS:
        raise MeasurementError("MIMO 공동제어는 독립 3위치 측정이 필요합니다.")
    if orientation != "90":
        raise MeasurementError("최종 FIR 측정은 UMIK 90° 방향만 허용됩니다.")
    noise_level_dbfs = level_dbfs if noise_level_dbfs is None else int(noise_level_dbfs)
    woofer_measurement_attenuation_db = (
        int(WOOFER_MEASUREMENT_ATTENUATION_DB)
        if woofer_measurement_attenuation_db is None
        else int(woofer_measurement_attenuation_db)
    )
    validate_measurement_output_levels(level_dbfs, noise_level_dbfs, woofer_measurement_attenuation_db)
    if sweep_seconds not in ALLOWED_DURATIONS:
        raise MeasurementError("측정 레벨 또는 sweep 시간이 허용 범위를 벗어났습니다.")
    if not umik_connected() or not u7_connected():
        raise MeasurementError("UMIK-1과 Xonar U7을 모두 연결하세요.")
    cal = calibration_for(orientation)
    base_id = time.strftime("%Y%m%d_%H%M%S")
    session_id = base_id
    directory = BASE / session_id
    suffix = 2
    while directory.exists():
        session_id = f"{base_id}-{suffix:02d}"
        directory = BASE / session_id
        suffix += 1
    directory.mkdir(parents=True, exist_ok=False)
    state = {
        "version": 2,
        "session_id": session_id,
        "session_dir": str(directory),
        "state": "ready",
        "stage": "레벨 검사를 실행하면 현재 U7 물리 출력 경로가 이 측정에 고정됩니다.",
        "progress": 0.0,
        "eta_seconds": None,
        "mode": mode,
        "sources": list(SOURCES[mode]),
        "positions_total": position_count,
        "positions_completed": 0,
        "level_dbfs": level_dbfs,
        "noise_level_dbfs": noise_level_dbfs,
        "woofer_measurement_attenuation_db": woofer_measurement_attenuation_db,
        "sweep_seconds": sweep_seconds,
        "orientation": orientation,
        "calibration": {key: value for key, value in cal.items() if key not in ("frequencies", "corrections")},
        "measurements": [],
        "phase_references": [],
        "phase_reference_acquisition_revision": (
            "simultaneous-walsh-v2" if mode in SEPARATE_WOOFER_MODES else None
        ),
        "result": None,
        "validation": None,
        "premeasured_sum_validation": None,
        "measurement_profile": None,
        "measurement_acquisition_revision": None,
        "measurement_output": None,
        "measurement_output_match": None,
        "measurement_sweep_output": {"active": False, "hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB},
        "dsp_mode": "normal",
        "active_pids": [],
        "created_unix": time.time(),
        "capabilities": capability,
    }
    save_current(state)
    atomic_json(directory / "session.json", state)
    return state


def restore_preview_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    if not state.get("preview_active"):
        return state
    process = subprocess.run(
        [PYTHON, MANAGER, "restore-profile"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if process.returncode:
        raise MeasurementError(process.stdout.strip() or "임시 튜닝 복귀 실패")
    return load_current()


def invalidate_from_step(state: dict[str, Any], step: int, reason: str) -> dict[str, Any]:
    """Invalidate exactly the artifacts that depend on a changed wizard step."""
    if step not in range(1, 7):
        raise MeasurementError("측정 단계는 1~6이어야 합니다.")
    if state.get("state") in ("running", "processing", "cancelling"):
        raise MeasurementError("작업 중에는 이전 단계로 돌아갈 수 없습니다.")
    if step <= 4:
        state = restore_preview_if_needed(state)
        state.update({
            "result": None,
            "post_filter_validation": None,
            "applied_profile": None,
            "preview_active": False,
            "preview_profile": None,
        })
    if step <= 3:
        state.update({
            "measurements": [],
            "phase_references": [],
            "positions_completed": 0,
            "validation": None,
            "premeasured_sum_validation": None,
            "last_measurement_quality": None,
        })
    if step <= 2:
        state.update({
            "level_check": None,
            "measurement_profile": None,
            "measurement_acquisition_revision": None,
            "measurement_output": None,
            "measurement_output_match": None,
        })
    if step <= 3:
        state.update(state="ready", progress=0.0, eta_seconds=None)
    elif step == 4:
        state.update(state="measured", progress=100.0, eta_seconds=None)
    state.update(
        stage=f"{step}단계로 돌아감 · {reason} · 이후 결과 초기화 완료",
        error=None,
        worker_pid=None,
        active_pids=[],
        invalidation={"from_step": step, "reason": reason, "unix": time.time()},
    )
    return state


def reconfigure_session(
    mode: str,
    orientation: str,
    level_dbfs: int,
    sweep_seconds: int,
    noise_level_dbfs: int | None = None,
    woofer_measurement_attenuation_db: int | None = None,
    position_count: int | None = None,
) -> dict[str, Any]:
    if mode not in SOURCES or orientation != "90":
        raise MeasurementError("측정 모드 또는 UMIK 방향이 잘못되었습니다.")
    if mode in MIMO_MODES and not platform_capabilities()["mimo_supported"]:
        raise MeasurementError("MIMO 측정/보정은 Raspberry Pi 4/5 전용입니다.")
    state = load_current()
    if state.get("state") == "idle":
        raise MeasurementError("먼저 새 측정 세션을 만드세요.")
    # Retain the schema field for old sessions/API clients, but the UI and
    # measurement engine now have one authoritative sweep level.
    noise_level_dbfs = int(level_dbfs)
    woofer_measurement_attenuation_db = int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)) if woofer_measurement_attenuation_db is None else int(woofer_measurement_attenuation_db)
    position_count = session_position_count(state) if position_count is None else int(position_count)
    if position_count not in ALLOWED_POSITION_COUNTS:
        raise MeasurementError("측정 위치 수는 빠른 측정 1위치 또는 표준 측정 3위치여야 합니다.")
    if mode in MIMO_MODES and position_count != POSITIONS:
        raise MeasurementError("MIMO 공동제어는 독립 3위치 측정이 필요합니다.")
    validate_measurement_output_levels(level_dbfs, noise_level_dbfs, woofer_measurement_attenuation_db)
    if sweep_seconds not in ALLOWED_DURATIONS:
        raise MeasurementError("Sweep 시간이 허용 범위를 벗어났습니다.")
    changes = []
    earliest = 7
    if mode != state.get("mode"):
        changes.append("측정 구성")
        earliest = min(earliest, 2)
    if position_count != session_position_count(state):
        changes.append("측정 위치 수")
        earliest = min(earliest, 3)
    if level_dbfs != state.get("level_dbfs"):
        changes.append("스윕 출력")
        earliest = min(earliest, 2)
    if woofer_measurement_attenuation_db != int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)):
        changes.append("우퍼 측정 감쇄")
        earliest = min(earliest, 2)
    if sweep_seconds != state.get("sweep_seconds"):
        changes.append("sweep 길이")
        earliest = min(earliest, 3)
    if orientation != state.get("orientation"):
        changes.append("UMIK 방향")
        earliest = min(earliest, 1)
    preserve_front_measurements = changes == ["우퍼 측정 감쇄"] and state.get("mode") in SEPARATE_WOOFER_MODES
    retained_front = [
        item for item in state.get("measurements", [])
        if item.get("source") in ("left", "right")
        and (Path(state["session_dir"]) / str(item.get("response", ""))).is_file()
    ] if preserve_front_measurements else []
    if earliest <= 6:
        state = invalidate_from_step(state, earliest, ", ".join(changes) + " 변경")
    if preserve_front_measurements:
        state["measurements"] = retained_front
        state["stage"] = f"우퍼 측정 감쇄 변경 · 프런트 L/R {len(retained_front)}개 보존 · 빠른 검사 후 우퍼 관련 출력만 재측정"
    state.update(
        mode=mode,
        sources=list(SOURCES[mode]),
        phase_reference_acquisition_revision=(
            "simultaneous-walsh-v2" if mode in SEPARATE_WOOFER_MODES else None
        ),
        positions_total=position_count,
        level_dbfs=level_dbfs,
        noise_level_dbfs=noise_level_dbfs,
        woofer_measurement_attenuation_db=woofer_measurement_attenuation_db,
        sweep_seconds=sweep_seconds,
        orientation=orientation,
    )
    if orientation != state.get("calibration", {}).get("orientation"):
        cal = calibration_for(orientation)
        state["calibration"] = {key: value for key, value in cal.items() if key not in ("frequencies", "corrections")}
    if not changes:
        state["stage"] = "측정 설정 변경 없음 · 기존 측정값 유지"
    save_current(state)
    atomic_json(Path(state["session_dir"]) / "session.json", state)
    return state


def prepare_level_check() -> dict[str, Any]:
    state = load_current()
    if state.get("state") == "idle" or not state.get("session_dir"):
        raise MeasurementError("먼저 새 측정 세션을 만드세요.")
    state = invalidate_from_step(state, 2, "레벨 검사 다시 실행")
    state = bind_measurement_output(state)
    state["stage"] = f"{state['measurement_output']['label']} 경로 고정 · 레벨 검사 준비"
    save_current(state)
    return state


def prepare_position_restart() -> dict[str, Any]:
    state = load_current()
    if not (state.get("level_check") or {}).get("ok"):
        raise MeasurementError(
            f"2 · 레벨 확인에서 빠른 스윕 SNR {MINIMUM_USABLE_SNR_DB:g} dB 이상을 PASS한 뒤 재측정하세요."
        )
    ensure_measurement_output_path(state)
    count = session_position_count(state)
    state = invalidate_from_step(state, 3, f"{count}위치 재측정 실행")
    save_current(state)
    return state


def prepare_phase_reference_remeasurement() -> dict[str, Any]:
    """Invalidate FIR results but preserve all five ESS measurements."""
    state = load_current()
    positions_total = session_position_count(state)
    if state.get("mode") not in SEPARATE_WOOFER_MODES:
        raise MeasurementError("L/R/우퍼 동시 위상 기준은 분리 우퍼 측정 구성에서만 사용합니다.")
    if int(state.get("positions_completed", 0)) != positions_total:
        raise MeasurementError(f"3 · 위치 측정에서 {positions_total}위치 ESS 측정을 먼저 완료하세요.")
    state = invalidate_from_step(state, 4, "L/R/우퍼 동시 위상 기준만 재측정")
    state.update(
        stage="3 · 위치 측정 · 기존 L/R/W/L+W/R+W 보존 · L+R+W 위상 기준 재측정 준비",
        last_measurement_quality=None,
    )
    save_current(state)
    return state


def prepare_build() -> dict[str, Any]:
    state = load_current()
    positions_total = session_position_count(state)
    if int(state.get("positions_completed", 0)) != positions_total:
        raise MeasurementError(f"선택한 {positions_total}위치 측정을 먼저 완료하세요.")
    state = invalidate_from_step(state, 4, "보정 설정 적용")
    save_current(state)
    return state


def prepare_saved_reprocess() -> dict[str, Any]:
    """Invalidate every response-dependent artifact when raw reprocessing starts."""
    state = load_current()
    if state.get("state") in ("running", "processing", "cancelling"):
        raise MeasurementError("다른 측정 작업이 진행 중입니다.")
    capture_inventory = state.get("capture_inventory") or {}
    phase_inventory = state.get("phase_capture_inventory") or {}
    if not bool(capture_inventory.get("can_reprocess_all")):
        raise MeasurementError("모든 위치의 저장 ESS 원본이 있어야 원본 재계산을 시작할 수 있습니다.")
    if int(phase_inventory.get("expected", 0)) and not bool(phase_inventory.get("can_reprocess_all")):
        raise MeasurementError("모든 위치의 L/R/우퍼 동시 위상 원본이 있어야 원본 재계산을 시작할 수 있습니다.")
    state = restore_preview_if_needed(state)
    state.update({
        "state": "ready",
        "stage": "3 · 위치 측정 · 저장 원본 재계산 준비 · 이전 FIR 결과 폐기",
        "progress": 0.0,
        "eta_seconds": None,
        "measurements": [],
        "phase_references": [],
        "positions_completed": 0,
        "validation": None,
        "premeasured_sum_validation": None,
        "last_measurement_quality": None,
        "result": None,
        "post_filter_validation": None,
        "applied_profile": None,
        "preview_active": False,
        "preview_profile": None,
        "error": None,
    })
    save_current(state)
    return state


def spawn_worker(action: str, *arguments: str) -> dict[str, Any]:
    with LOCK.open("w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        state = load_current()
        if state.get("state") in ("running", "processing", "cancelling"):
            raise MeasurementError("다른 측정 작업이 진행 중입니다.")
        log = Path(state.get("session_dir", str(BASE))) / "worker.log"
        state.update({
            "state": "running",
            "worker_pid": None,
            "worker_launch_pending_until": time.time() + 5.0,
            "cancel_requested": False,
            "error": None,
            "interrupted_worker": None,
        })
        save_current(state)
        handle = log.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [PYTHON, str(Path(__file__).resolve()), action, *arguments],
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:
            state.update({
                "state": "error",
                "stage": "측정 worker 시작 실패",
                "error": str(exc),
                "worker_pid": None,
                "active_pids": [],
            })
            state.pop("worker_launch_pending_until", None)
            save_current(state)
            raise
        finally:
            handle.close()
        state["worker_pid"] = process.pid
        state.pop("worker_launch_pending_until", None)
        save_current(state)
        return state


def position_measurement_source_order(state: dict[str, Any]) -> list[str]:
    """Return the stable audible ESS order used at every mic position."""
    return list(state.get("sources", ()))


def measure_position_worker() -> None:
    state = load_current()
    if not (state.get("level_check") or {}).get("ok"):
        raise MeasurementError(
            f"2 · 레벨 확인에서 빠른 스윕 SNR {MINIMUM_USABLE_SNR_DB:g} dB 이상을 PASS한 뒤 "
            "3 · 위치 측정을 실행하세요."
        )
    ensure_measurement_output_path(state)
    position = int(state["positions_completed"])
    positions_total = session_position_count(state)
    if position >= positions_total:
        raise MeasurementError(f"선택한 {positions_total}위치 측정이 이미 완료되었습니다.")
    directory = Path(state["session_dir"])
    cal = calibration_for(state["orientation"])
    sources = position_measurement_source_order(state)
    # Main measurements use one predictable order at every position.  The
    # short level preflight remains free to prioritize Front L and Woofer, but
    # carrying that special order into position 1 made the Standard workflow
    # disagree with positions 2 and 3 without improving acquisition quality.
    phase_reference_required = state.get("mode") in SEPARATE_WOOFER_MODES
    acquisitions_per_position = len(sources) + (1 if phase_reference_required else 0)
    total_items = positions_total * acquisitions_per_position
    completed_items = position * acquisitions_per_position
    new_items = list(state.get("measurements", []))
    completed_keys = {
        (int(item.get("position", 0)), str(item.get("source", "")))
        for item in new_items
        if (directory / str(item.get("response", ""))).is_file()
    }
    pending: list[dict[str, Any]] = []
    phase_pending: dict[str, Any] | None = None
    response_eta = int(platform_capabilities()["offline_estimates_seconds"]["response_per_channel"])
    for source_index, source in enumerate(sources):
        if (position + 1, source) in completed_keys:
            update_current(
                stage=f"위치 {position + 1}/{positions_total} · {SOURCE_LABELS.get(source, source)} 기존 측정 보존 · 건너뜀",
                progress=100.0 * (completed_items + source_index + 1) / total_items,
            )
            continue
        if load_current().get("cancel_requested"):
            raise MeasurementError("사용자가 측정을 취소했습니다.")
        item_index = completed_items + source_index
        base = 100.0 * item_index / total_items
        span = 100.0 / total_items
        source_label = SOURCE_LABELS.get(source, source)
        update_current(state="running", stage=f"위치 {position + 1}/{positions_total} · {source_label} sweep 준비", progress=base, eta_seconds=round((len(sources) - source_index) * (state["sweep_seconds"] + 5)))
        sweep_path = directory / f"p{position + 1}_{source}_sweep.wav"
        record_path = directory / f"p{position + 1}_{source}_recorded.wav"
        reference = write_sweep(
            sweep_path, source, int(state["level_dbfs"]), int(state["sweep_seconds"]),
            woofer_attenuation_db=float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
        )
        pending.append({
            "source": source,
            "source_label": source_label,
            "sweep_path": sweep_path,
            "record_path": record_path,
            "reference": reference,
        })

    existing_phase_positions = {
        int(item.get("position", 0))
        for item in state.get("phase_references", [])
        if isinstance(item, dict)
        and (directory / str(item.get("result", ""))).is_file()
    }
    if phase_reference_required and position + 1 not in existing_phase_positions:
        phase_sweep_path = directory / f"p{position + 1}_phase_reference_sweep.wav"
        phase_record_path = directory / f"p{position + 1}_phase_reference_recorded.wav"
        phase_metadata_path = directory / f"p{position + 1}_phase_reference_signal.json"
        phase_result_path = directory / f"p{position + 1}_phase_reference.json"
        phase_metadata = write_phase_reference(
            phase_sweep_path,
            int(state["level_dbfs"]),
            float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
        )
        atomic_json(phase_metadata_path, phase_metadata)
        phase_pending = {
            "source_label": SOURCE_LABELS["phase_reference"],
            "sweep_path": phase_sweep_path,
            "record_path": phase_record_path,
            "metadata": phase_metadata,
            "metadata_path": phase_metadata_path,
            "result_path": phase_result_path,
        }

    # Sound first, processing second: CamillaDSP is stopped/restored once for
    # the whole position and no FFT delays occur between audible sweeps.
    position_base = 100.0 * position / positions_total
    position_span = 100.0 / positions_total
    captures = [
        (item["sweep_path"], item["record_path"], item["source_label"])
        for item in pending
    ]
    if phase_pending is not None:
        captures.append((
            phase_pending["sweep_path"],
            phase_pending["record_path"],
            phase_pending["source_label"],
        ))
    run_direct_capture_batch(captures, position_base, position_span * 0.65)

    for pending_index, item in enumerate(pending):
        if load_current().get("cancel_requested"):
            raise MeasurementError("사용자가 측정을 취소했습니다.")
        source = item["source"]
        source_label = item["source_label"]
        process_fraction = pending_index / max(1, len(pending))
        progress = position_base + position_span * (0.65 + 0.35 * process_fraction)
        update_current(
            state="processing",
            stage=f"위치 {position + 1}/{positions_total} · 모든 녹음 완료 · {source_label} 응답 일괄 계산",
            progress=progress,
            eta_seconds=round((len(pending) - pending_index) * response_eta),
        )
        response = response_from_recording(item["record_path"], item["reference"], cal, source)
        response_path = directory / f"p{position + 1}_{source}_response.json"
        atomic_json(response_path, response)
        new_items = [
            existing for existing in new_items
            if (int(existing.get("position", 0)), str(existing.get("source", ""))) != (position + 1, source)
        ]
        new_items.append({
            "position": position + 1,
            "source": source,
            "recording": item["record_path"].name,
            "response": response_path.name,
            "peak_dbfs": response["peak_dbfs"],
            "rms_dbfs": response["rms_dbfs"],
            "snr_db": response["measurement_quality"]["snr_db"],
            "quality_recommended": response["measurement_quality"]["recommended"],
        })
        update_current(measurements=new_items, progress=position_base + position_span * (0.65 + 0.35 * (pending_index + 1) / max(1, len(pending))), eta_seconds=0)
    phase_references = list(state.get("phase_references", []))
    if phase_pending is not None:
        update_current(
            state="processing",
            stage=f"위치 {position + 1}/{positions_total} · L/R/우퍼 상대 위상·지연 계산",
            progress=position_base + position_span * 0.96,
            eta_seconds=round(response_eta * 0.35),
        )
        phase_result = phase_reference_from_recording(
            phase_pending["record_path"], phase_pending["metadata"], cal,
        )
        atomic_json(phase_pending["result_path"], phase_result)
        phase_references = [
            item for item in phase_references
            if int(item.get("position", 0)) != position + 1
        ]
        phase_references.append({
            "position": position + 1,
            "recording": phase_pending["record_path"].name,
            "signal": phase_pending["metadata_path"].name,
            "result": phase_pending["result_path"].name,
            "reliable": bool(phase_result["reliable"]),
            "minimum_median_snr_db": phase_result["minimum_median_snr_db"],
            "phase_repeatability_p90_deg": phase_result["phase_repeatability_p90_deg"],
        })
        update_current(phase_references=phase_references, progress=position_base + position_span)
    positions_completed = position + 1
    completion_fields: dict[str, Any] = {}
    if positions_completed < positions_total:
        stage = f"위치 {positions_completed + 1}: 마이크를 조금 옮기고 천장 방향을 유지하세요."
        job_state = "ready"
    else:
        if state.get("mode") in PREMEASURED_SUM_MODES:
            preferences = load_correction_preferences()
            sum_model = evaluate_premeasured_sum_model(
                directory,
                positions_total,
                float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
                int(preferences.get("crossover_frequency_hz", 100)),
            )
            completion_fields["premeasured_sum_validation"] = sum_model
            phase_pass = (
                len(phase_references) == positions_total
                and all(bool(item.get("reliable")) for item in phase_references)
            )
            if sum_model["pass"] and phase_pass:
                stage = f"{positions_total}위치 여섯 측정 완료 · 합산·상대 위상 PASS · FIR 계산 가능"
            elif not phase_pass:
                stage = f"{positions_total}위치 측정 완료 · 동시 위상 기준 FAIL · 3단계에서 해당 위치 다시 측정"
            else:
                stage = f"{positions_total}위치 정밀 측정 완료 · L+우퍼/R+우퍼 모델 FAIL · 3단계 조치 확인"
        else:
            stage = f"{positions_total}위치 측정 완료 · 32768탭 FIR을 생성할 수 있습니다."
        job_state = "measured"
    state = update_current(
        state=job_state,
        positions_completed=positions_completed,
        stage=stage,
        progress=100.0 * positions_completed / positions_total,
        eta_seconds=None,
        worker_pid=None,
        phase_references=phase_references,
        **completion_fields,
    )
    atomic_json(directory / "session.json", state)


def _install_phase_reference_capture(
    state: dict[str, Any],
    pending_sweep: Path,
    pending_recording: Path,
    pending_signal: Path,
    pending_result: Path,
    phase_result: dict[str, Any],
) -> dict[str, Any]:
    """Back up the previous phase capture and atomically install a new one."""
    directory = Path(state["session_dir"])
    positions_total = session_position_count(state)
    canonical = {
        pending_sweep: directory / "p1_phase_reference_sweep.wav",
        pending_recording: directory / "p1_phase_reference_recorded.wav",
        pending_signal: directory / "p1_phase_reference_signal.json",
        pending_result: directory / "p1_phase_reference.json",
    }
    backup_token = time.strftime("%Y%m%d_%H%M%S")
    backups = []
    for destination in canonical.values():
        if destination.is_file():
            backup = destination.with_name(f"{destination.name}.backup-{backup_token}")
            shutil.copy2(destination, backup)
            backups.append(backup.name)
    for source, destination in canonical.items():
        os.replace(source, destination)

    phase_references = [{
        "position": 1,
        "recording": "p1_phase_reference_recorded.wav",
        "signal": "p1_phase_reference_signal.json",
        "result": "p1_phase_reference.json",
        "reliable": bool(phase_result["reliable"]),
        "minimum_median_snr_db": phase_result["minimum_median_snr_db"],
        "phase_repeatability_p90_deg": phase_result["phase_repeatability_p90_deg"],
        "method": phase_result["method"],
    }]
    preferences = load_correction_preferences()
    sum_model = (
        evaluate_premeasured_sum_model(
            directory,
            positions_total,
            float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
            int(preferences.get("crossover_frequency_hz", 100)),
        )
        if state.get("mode") in PREMEASURED_SUM_MODES else None
    )
    phase_pass = bool(phase_result["reliable"])
    sum_pass = sum_model is None or bool(sum_model.get("pass"))
    stage = (
        "Walsh L+R+W 재측정 완료 · 동일 주파수 위상 및 물리 합산 PASS · FIR 계산 가능"
        if phase_pass and sum_pass else
        "Walsh L+R+W 재측정 완료 · 진단 항목을 확인하세요"
    )
    updated = update_current(
        state="measured",
        stage=stage,
        progress=100.0,
        eta_seconds=None,
        worker_pid=None,
        active_pids=[],
        error=None,
        phase_references=phase_references,
        phase_reference_acquisition_revision="simultaneous-walsh-v2",
        premeasured_sum_validation=sum_model,
        last_measurement_quality=None,
        phase_reference_backup_files=backups,
    )
    atomic_json(directory / "session.json", updated)
    return updated


def measure_phase_reference_worker() -> None:
    """Remeasure only the simultaneous phase reference; preserve ESS WAVs."""
    state = load_current()
    if state.get("mode") not in SEPARATE_WOOFER_MODES:
        raise MeasurementError("현재 측정 구성에는 L/R/우퍼 동시 위상 기준이 없습니다.")
    positions_total = session_position_count(state)
    if positions_total != 1:
        raise MeasurementError(
            "개별 위상 기준 재측정은 현재 1위치 세션에서만 지원합니다. "
            "3위치 세션은 각 위치의 마이크 좌표가 필요하므로 해당 위치를 다시 측정하세요."
        )
    directory = Path(state["session_dir"])
    calibration = calibration_for(state["orientation"])
    token = f"walsh-v2-pending-{os.getpid()}"
    pending_sweep = directory / f".{token}-sweep.wav"
    pending_recording = directory / f".{token}-recorded.wav"
    pending_signal = directory / f".{token}-signal.json"
    pending_result = directory / f".{token}-result.json"
    metadata = write_phase_reference(
        pending_sweep,
        int(state["level_dbfs"]),
        float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
    )
    atomic_json(pending_signal, metadata)
    update_current(
        state="running",
        stage="3 · 위치 측정 · 기존 5개 ESS 보존 · Walsh L+R+W 1회 재생 준비",
        progress=0.0,
        last_measurement_quality=None,
    )
    run_direct_capture_batch(
        [(pending_sweep, pending_recording, "L/R/우퍼 Walsh 동시 위상 기준")],
        0.0,
        70.0,
    )
    update_current(
        state="processing",
        stage="Walsh L+R+W 녹음 완료 · 동일 주파수 L/R/W 직교 분리 계산",
        progress=75.0,
    )
    phase_result = phase_reference_from_recording(pending_recording, metadata, calibration)
    atomic_json(pending_result, phase_result)
    _install_phase_reference_capture(
        state, pending_sweep, pending_recording, pending_signal, pending_result, phase_result,
    )


def recover_pending_phase_reference() -> dict[str, Any]:
    """Analyze and install the newest saved Walsh capture without playback."""
    state = load_current()
    directory = Path(state.get("session_dir", ""))
    if not directory.is_dir():
        raise MeasurementError("복구할 측정 Session이 없습니다.")
    candidates = sorted(
        directory.glob(".walsh-v2-pending-*-signal.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for pending_signal in candidates:
        prefix = pending_signal.name[:-len("-signal.json")]
        pending_sweep = directory / f"{prefix}-sweep.wav"
        pending_recording = directory / f"{prefix}-recorded.wav"
        pending_result = directory / f"{prefix}-result.json"
        if not pending_sweep.is_file() or not pending_recording.is_file():
            continue
        metadata = json.loads(pending_signal.read_text(encoding="utf-8"))
        calibration = calibration_for(state["orientation"])
        update_current(
            state="processing",
            stage="저장된 Walsh L+R+W 원본 무음 재계산",
            progress=75.0,
            worker_pid=None,
            active_pids=[],
            error=None,
        )
        phase_result = phase_reference_from_recording(pending_recording, metadata, calibration)
        atomic_json(pending_result, phase_result)
        return _install_phase_reference_capture(
            state, pending_sweep, pending_recording, pending_signal, pending_result, phase_result,
        )
    raise MeasurementError("무음 복구할 Walsh L+R+W 원본 녹음을 찾지 못했습니다.")


def evaluate_level_samples(silence_samples: list[float], active_samples: list[float], bits: int) -> dict[str, Any]:
    if len(silence_samples) < round(1.5 * RATE) or len(active_samples) < round(1.5 * RATE):
        raise MeasurementError("레벨 검사 녹음 길이가 부족합니다.")

    def robust_ac_rms(samples: list[float]) -> float:
        trim = min(round(0.15 * RATE), len(samples) // 8)
        stable = samples[trim:len(samples) - trim] if trim else samples
        block = round(0.15 * RATE)
        powers = []
        for start in range(0, len(stable) - block + 1, block):
            values = stable[start:start + block]
            mean = sum(values) / len(values)
            powers.append(sum((value - mean) ** 2 for value in values) / len(values))
        if not powers:
            raise MeasurementError("레벨 검사 안정 구간이 부족합니다.")
        return math.sqrt(statistics.median(powers))

    background_rms = robust_ac_rms(silence_samples)
    total_rms = robust_ac_rms(active_samples)
    signal_power = max(0.0, total_rms * total_rms - background_rms * background_rms)
    signal_rms = math.sqrt(signal_power)
    peak = max(abs(value) for value in active_samples)
    background_dbfs = 20.0 * math.log10(max(background_rms, 1e-15))
    signal_dbfs = 20.0 * math.log10(max(signal_rms, 1e-15))
    peak_dbfs = 20.0 * math.log10(max(peak, 1e-15))
    snr_db = 10.0 * math.log10(max(signal_power, 1e-30) / max(background_rms * background_rms, 1e-30))
    return normalize_level_check({
        "bits": bits,
        "silence_seconds": 2,
        "quick_sweep_seconds": 2,
        "background_rms_dbfs": round(background_dbfs, 2),
        "estimated_signal_rms_dbfs": round(signal_dbfs, 2),
        "snr_db": round(snr_db, 2),
        "peak_dbfs": round(peak_dbfs, 2),
    })


def level_check_source_order(state: dict[str, Any]) -> list[str]:
    sources = list(state.get("sources", ()))
    if all(source in sources for source in ("left", "right", "woofer")):
        sources = ["left", "woofer", "right"] + [
            source for source in sources if source not in ("left", "right", "woofer")
        ]
    if not sources:
        raise MeasurementError("빠른 검사에 사용할 출력 조합이 없습니다.")
    return sources


def analyze_level_check_recordings(state: dict[str, Any], sources: list[str]) -> dict[str, Any]:
    """Re-evaluate saved quick sweeps without replaying any audio."""
    selector = ensure_measurement_output_path(state)
    directory = Path(state["session_dir"])
    level = int(state["level_dbfs"])
    woofer_attenuation = float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB))
    channel_results: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        reference = reference_sweep_for_source(
            source,
            level,
            2,
            woofer_attenuation_db=woofer_attenuation,
            level_check=True,
        )
        record_path = directory / f"level_check_{source}_recorded.wav"
        sweep_path = directory / f"level_check_{source}_sweep.wav"
        if not record_path.is_file() or not sweep_path.is_file():
            raise MeasurementError(f"빠른 검사 저장 파일이 없습니다: {SOURCE_LABELS.get(source, source)}")
        _, bits, samples = read_pcm_wav(record_path)
        quality = sweep_capture_quality(samples, reference, source)
        peak_dbfs = 20.0 * math.log10(max(max(abs(value) for value in samples), 1.0e-15))
        channel = normalize_level_check({
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "bits": bits,
            "snr_db": quality["snr_db"],
            "peak_dbfs": round(peak_dbfs, 2),
            "background_rms_dbfs": quality["background_rms_dbfs"],
            "estimated_signal_rms_dbfs": quality["estimated_signal_rms_dbfs"],
            "analysis_band_hz": quality["analysis_band_hz"],
            "subwoofer_passband": quality.get("subwoofer_passband"),
            "requested_level_dbfs": level,
        }, level)
        channel_results.append(channel)
        update_current(
            stage=f"빠른 검사 저장 원본 · {SOURCE_LABELS.get(source, source)} SNR 계산",
            progress=80.0 + 15.0 * (index + 1) / len(sources),
            eta_seconds=0,
        )

    worst = min(channel_results, key=lambda item: float(item.get("assessment_snr_db", -300.0)))
    peak = max(float(item.get("assessment_peak_dbfs", -300.0)) for item in channel_results)
    result = normalize_level_check({
        "bits": min(int(item.get("bits", 0)) for item in channel_results),
        "silence_seconds": 0,
        "quick_sweep_seconds": 2,
        "quick_sweep_count": len(channel_results),
        "quick_sweep_method": "same ESS generator, routing, per-source passband and SNR estimator as full measurement",
        "snr_db": float(worst["assessment_snr_db"]),
        "peak_dbfs": peak,
        "background_rms_dbfs": worst.get("background_rms_dbfs"),
        "estimated_signal_rms_dbfs": worst.get("estimated_signal_rms_dbfs"),
        "analysis_band_hz": worst.get("analysis_band_hz"),
        "worst_source": worst.get("source"),
        "worst_source_label": worst.get("source_label"),
        "requested_white_noise_level_dbfs": int(state.get("noise_level_dbfs", DEFAULT_NOISE_LEVEL_DBFS)),
        "requested_level_dbfs": level,
        "sweep_level_dbfs": level,
        "woofer_measurement_attenuation_db": int(woofer_attenuation),
        "measurement_profile": state["measurement_profile"],
        "measurement_output_label": OUTPUT_PROFILE_LABELS[state["measurement_profile"]],
        "sweep_hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB,
        "listening_volume_ignored_during_sweep": True,
        "selector_state_byte": selector.get("state_byte"),
        "channels": channel_results,
    }, level, sources)
    ok = result["ok"]
    snr_db = result["snr_db"]
    update_current(
        state="ready",
        stage=(
            f"빠른 검사 {'PASS' if ok else 'FAIL'} · 최저 {result.get('worst_source_label', '출력')} "
            f"SNR {snr_db:.1f} dB"
        ),
        progress=100.0,
        eta_seconds=None,
        worker_pid=None,
        level_check=result,
    )
    return result


def level_check_worker() -> None:
    state = load_current()
    ensure_measurement_output_path(state)
    directory = Path(state["session_dir"])
    level = int(state["level_dbfs"])
    woofer_attenuation = float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB))
    sources = level_check_source_order(state)

    pending: list[dict[str, Any]] = []
    for source in sources:
        sweep_path = directory / f"level_check_{source}_sweep.wav"
        record_path = directory / f"level_check_{source}_recorded.wav"
        # Use the same 15 Hz–22 kHz ESS generator and physical routing as the
        # full measurement. Only the sweep duration is shortened to 2 s.
        write_sweep(
            sweep_path,
            source,
            level,
            2,
            woofer_attenuation_db=woofer_attenuation,
        )
        pending.append({
            "source": source,
            "label": SOURCE_LABELS.get(source, source),
            "sweep": sweep_path,
            "recorded": record_path,
        })
    update_current(
        stage=f"빠른 검사 · {len(pending)}개 출력 조합을 각 2초 측정",
        progress=5.0,
        eta_seconds=round(len(pending) * 6),
    )
    run_direct_capture_batch(
        [(item["sweep"], item["recorded"], f"빠른 검사 · {item['label']}") for item in pending],
        5.0,
        75.0,
    )
    analyze_level_check_recordings(load_current(), sources)


def level_check_reprocess_worker() -> None:
    state = load_current()
    inventory = state.get("level_recording_inventory") or {}
    if not inventory.get("can_reprocess_all"):
        raise MeasurementError("빠른 검사 저장 원본이 모두 있어야 무음 재계산할 수 있습니다.")
    ensure_measurement_output_path(state)
    update_current(stage="빠른 검사 저장 원본 무음 재계산", progress=80.0, eta_seconds=0)
    analyze_level_check_recordings(state, level_check_source_order(state))


def validation_worker() -> None:
    state = load_current()
    positions_total = session_position_count(state)
    if state.get("mode") != "lrw" or int(state.get("positions_completed", 0)) != positions_total:
        raise MeasurementError(f"L/R/W {positions_total}위치 측정을 먼저 완료하세요.")
    ensure_measurement_output_path(state)
    directory = Path(state["session_dir"])
    cal = calibration_for("90")
    results = {}
    pending = []
    response_eta = int(platform_capabilities()["offline_estimates_seconds"]["response_per_channel"])
    for index, source in enumerate(("left_woofer", "right_woofer")):
        base = index * 50.0
        update_current(state="running", stage=f"중앙 위치 합산 검증 · {source}", progress=base, eta_seconds=round((2 - index) * (state["sweep_seconds"] + 5)))
        sweep = directory / f"center_{source}_sweep.wav"
        recorded = directory / f"center_{source}_recorded.wav"
        reference = write_sweep(
            sweep, source, int(state["level_dbfs"]), int(state["sweep_seconds"]),
            woofer_attenuation_db=float(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)),
        )
        pending.append((source, sweep, recorded, reference))
    run_direct_capture_batch(
        [(sweep, recorded, SOURCE_LABELS[source]) for source, sweep, recorded, _ in pending],
        0.0,
        70.0,
    )
    for index, (source, _, recorded, reference) in enumerate(pending):
        update_current(state="processing", stage=f"합산 검증 녹음 완료 · {SOURCE_LABELS[source]} 응답 계산", progress=70.0 + index * 15.0, eta_seconds=round((2 - index) * response_eta))
        response = response_from_recording(recorded, reference, cal, source)
        response_path = directory / f"center_{source}_response.json"
        atomic_json(response_path, response)
        results[source] = response_path.name
    state = update_current(state="measured", stage="L+우퍼 / R+우퍼 중앙 합산 검증 완료", progress=100.0, eta_seconds=None, worker_pid=None, validation=results)
    atomic_json(directory / "session.json", state)


def integration_summary(
    directory: Path,
    validation: dict[str, str] | None,
    woofer_attenuation_db: float,
) -> dict[str, Any] | None:
    if not validation:
        return None
    woofer = json.loads((directory / "p1_woofer_response.json").read_text(encoding="utf-8"))
    woofer_scale = 10.0 ** (float(woofer_attenuation_db) / 20.0)
    result = {}
    for side, source in (("left", "left_woofer"), ("right", "right_woofer")):
        main = json.loads((directory / f"p1_{side}_response.json").read_text(encoding="utf-8"))
        combined = json.loads((directory / validation[source]).read_text(encoding="utf-8"))
        deficits = []
        graph = []
        for frequency, main_db, woofer_db, combined_db in zip(main["frequencies"], main["db"], woofer["db"], combined["db"]):
            coherent_max = 20.0 * math.log10(
                10.0 ** (main_db / 20.0) + woofer_scale * 10.0 ** (woofer_db / 20.0)
            )
            # Both values use the same unscaled Front reference.  Adding 3 dB
            # here used to make a perfectly linear physical sum look 3 dB too
            # high; no RMS/power conversion is involved in this magnitude
            # comparison.
            deficit = combined_db - coherent_max
            if 50.0 <= frequency <= 180.0:
                deficits.append(deficit)
            if 30.0 <= frequency <= 250.0 and (not graph or frequency / graph[-1][0] >= 1.06):
                graph.append((frequency, deficit))
        median_deficit = statistics.median(deficits) if deficits else 0.0
        result[side] = {
            "median_crossover_sum_deficit_db": round(median_deficit, 2),
            "verdict": "양호" if median_deficit >= -3.0 else "상쇄 있음 · 저역 phase 정렬 권장",
            "frequency": [round(item[0], 2) for item in graph],
            "sum_deficit_db": [round(item[1], 3) for item in graph],
            "woofer_measurement_attenuation_db": float(woofer_attenuation_db),
        }
    return result


def wrapped_phase_error_degrees(first: complex, second: complex) -> float:
    if abs(first) <= 1.0e-15 or abs(second) <= 1.0e-15:
        return 180.0
    difference = math.atan2(first.imag, first.real) - math.atan2(second.imag, second.real)
    return abs(math.degrees(math.atan2(math.sin(difference), math.cos(difference))))


def evaluate_premeasured_sum_model(
    directory: Path,
    positions_total: int,
    woofer_attenuation_db: float,
    crossover_frequency_hz: int,
) -> dict[str, Any]:
    """Verify H(L+W)=H(L)+a*H(W) before any FIR is calculated.

    The combined captures retain their absolute playback/reference scale.  No
    per-channel normalization is permitted here because it would hide routing,
    polarity, level or timing errors.  L/R/W remain the branch magnitude
    inputs; after this closure gate passes, L+W/R+W provide dense cross-term
    constraints for the joint FIR synthesis rather than being averaged in.
    """
    woofer_scale = 10.0 ** (float(woofer_attenuation_db) / 20.0)
    low_hz = max(30.0, float(crossover_frequency_hz) * 0.45)
    high_hz = min(300.0, float(crossover_frequency_hz) * 2.5)
    thresholds = {
        "magnitude_mae_db": 2.0,
        "magnitude_p90_db": 4.0,
        "phase_repeatability_p90_deg": PHASE_REFERENCE_MAX_PHASE_P90_DEG,
        # Keep this acquisition gate identical to the quick sweep and normal
        # response path.  15 dB is a recommendation, not a hidden later gate.
        "minimum_snr_db": MINIMUM_USABLE_SNR_DB,
        "recommended_snr_db": RECOMMENDED_SNR_DB,
    }
    channels: dict[str, Any] = {}
    phase_references = load_phase_reference_results(directory, positions_total)
    simultaneous_phase_reliable = len(phase_references) == positions_total
    all_pass = True
    all_phase_reliable = True
    all_snrs: list[float] = []
    for side, combined_source in (("left", "left_woofer"), ("right", "right_woofer")):
        magnitude_errors: list[float] = []
        phase_linearity_residuals: list[float] = []
        phase_repeatability_values: list[float] = []
        graph_frequency: list[float] = []
        graph_measured: list[float] = []
        graph_predicted: list[float] = []
        side_phase_reliable = simultaneous_phase_reliable or PHASE_CLOCK_SHARED
        side_snrs: list[float] = []
        for position in range(1, positions_total + 1):
            responses = {
                source: json.loads((directory / f"p{position}_{source}_response.json").read_text(encoding="utf-8"))
                for source in (side, "woofer", combined_source)
            }
            if not simultaneous_phase_reliable:
                side_phase_reliable = side_phase_reliable and all(
                    bool(response.get("bulk_delay_reliable", response.get("bulk_delay", {}).get("reliable", True)))
                    for response in responses.values()
                )
            for response in responses.values():
                snr = response.get("measurement_quality", {}).get("snr_db")
                if isinstance(snr, (int, float)):
                    side_snrs.append(float(snr))
                    all_snrs.append(float(snr))
            frequencies = [float(value) for value in responses[side]["frequencies"]]
            for frequency in frequencies:
                if not low_hz <= frequency <= high_hz:
                    continue
                phase_reference = phase_references[position - 1] if simultaneous_phase_reliable else None
                main_value = phase_referenced_response_complex(
                    responses[side], phase_reference, side, frequency,
                )
                woofer_value = phase_referenced_response_complex(
                    responses["woofer"], phase_reference, "woofer", frequency,
                ) * woofer_scale
                measured = response_complex(responses[combined_source], frequency)
                measured_db = 20.0 * math.log10(max(abs(measured), 1.0e-15))
                if side_phase_reliable:
                    predicted = main_value + woofer_value
                    predicted_db = 20.0 * math.log10(max(abs(predicted), 1.0e-15))
                    magnitude_errors.append(abs(predicted_db - measured_db))
                    if simultaneous_phase_reliable:
                        pair_key = f"{side}_woofer"
                        residual = phase_reference.get("pairs", {}).get(pair_key, {}).get("delay_fit_residual_p90_deg")
                        if isinstance(residual, (int, float)):
                            phase_linearity_residuals.append(float(residual))
                        for source_name in (side, "woofer"):
                            repeatability = phase_reference.get("sources", {}).get(source_name, {}).get(
                                "phase_repeatability_p90_deg"
                            )
                            if isinstance(repeatability, (int, float)):
                                phase_repeatability_values.append(float(repeatability))
                    else:
                        # Only a shared hardware clock makes the absolute phase
                        # origins of separate ESS recordings comparable.
                        phase_linearity_residuals.append(wrapped_phase_error_degrees(predicted, measured))
                else:
                    # Separate USB playback/capture clocks cannot preserve an
                    # absolute phase origin between independent sweeps. Check
                    # the physical sum against the triangle-inequality bounds
                    # instead of manufacturing a complex notch from start-time
                    # jitter. This still catches routing and relative-level
                    # errors without claiming exact phase closure.
                    lower = abs(abs(main_value) - abs(woofer_value))
                    upper = abs(main_value) + abs(woofer_value)
                    lower_db = 20.0 * math.log10(max(lower, 1.0e-15))
                    upper_db = 20.0 * math.log10(max(upper, 1.0e-15))
                    magnitude_errors.append(max(0.0, lower_db - measured_db, measured_db - upper_db))
                    predicted_db = upper_db
                if position == 1 and (not graph_frequency or frequency / graph_frequency[-1] >= 1.06):
                    graph_frequency.append(round(frequency, 3))
                    graph_measured.append(round(measured_db, 3))
                    graph_predicted.append(round(predicted_db, 3))
        magnitude_mae = sum(magnitude_errors) / len(magnitude_errors) if magnitude_errors else float("inf")
        magnitude_p90 = percentile(magnitude_errors, 0.90) if magnitude_errors else float("inf")
        phase_repeatability_p90 = (
            percentile(phase_repeatability_values, 0.90)
            if phase_repeatability_values else None
        )
        delay_fit_residual_p90 = (
            percentile(phase_linearity_residuals, 0.90)
            if phase_linearity_residuals else None
        )
        minimum_snr = min(side_snrs) if side_snrs else None
        magnitude_pass = bool(magnitude_errors) and magnitude_mae <= thresholds["magnitude_mae_db"] and magnitude_p90 <= thresholds["magnitude_p90_db"]
        phase_pass = bool(
            side_phase_reliable
            and phase_repeatability_p90 is not None
            and phase_repeatability_p90 <= thresholds["phase_repeatability_p90_deg"]
        )
        snr_pass = minimum_snr is not None and minimum_snr >= thresholds["minimum_snr_db"]
        # U7 playback and UMIK capture do not share a hardware clock.  Accept
        # magnitude closure with usable SNR, while reporting absolute phase as
        # limited evidence when its direct-delay reference is unreliable.
        channel_pass = magnitude_pass and snr_pass
        all_pass = all_pass and channel_pass
        all_phase_reliable = all_phase_reliable and side_phase_reliable
        channels[side] = {
            "pass": channel_pass,
            "magnitude_pass": magnitude_pass,
            "phase_pass": phase_pass,
            "phase_applicable": side_phase_reliable,
            "snr_pass": snr_pass,
            "magnitude_mae_db": round(magnitude_mae, 3),
            "magnitude_p90_abs_error_db": round(magnitude_p90, 3),
            # A large delay-fit residual is often real room/excess phase, not
            # measurement error.  Report it as phase linearity evidence while
            # judging acquisition quality by same-recording repeatability.
            "phase_median_abs_error_deg": None,
            "phase_p90_abs_error_deg": None,
            "phase_repeatability_p90_deg": (
                round(phase_repeatability_p90, 2)
                if phase_repeatability_p90 is not None else None
            ),
            "delay_fit_residual_p90_deg": (
                round(delay_fit_residual_p90, 2)
                if delay_fit_residual_p90 is not None else None
            ),
            "phase_evidence": (
                "same-frequency Walsh separation"
                if simultaneous_phase_reliable and all(
                    bool(value.get("same_frequency_walsh_separation"))
                    for value in phase_reference.get("sources", {}).values()
                ) else
                "same-recording sparse-frequency interpolation"
                if simultaneous_phase_reliable else
                "shared-clock absolute ESS phase"
                if PHASE_CLOCK_SHARED else "not available"
            ),
            "minimum_snr_db": round(minimum_snr, 2) if minimum_snr is not None else None,
            "frequency": graph_frequency,
            "measured_combined_db": graph_measured,
            "predicted_complex_sum_db": graph_predicted,
        }
    minimum_snr = min(all_snrs) if all_snrs else None
    if all_pass:
        status = (
            "pass_premeasured_magnitude_plus_simultaneous_relative_phase"
            if simultaneous_phase_reliable else
            "pass_premeasured_complex_sum" if all_phase_reliable else
            "pass_premeasured_magnitude_phase_limited"
        )
        action = "추가 조치 없음. 4 · FIR 계산에서 타깃과 디지털 크로스오버를 선택하고 ‘FIR 계산’을 누르세요."
    elif minimum_snr is None or minimum_snr < thresholds["minimum_snr_db"]:
        status = "fail_premeasured_sum_snr"
        try:
            configured_level = int(load_current().get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS))
        except Exception:
            configured_level = DEFAULT_SWEEP_LEVEL_DBFS
        raise_db = max(1, int(math.ceil(MINIMUM_USABLE_SNR_DB - float(minimum_snr or -120.0))))
        recommended_level = min(0, configured_level + raise_db)
        action = (
            f"2 · 레벨 확인에서 스윕 출력을 {configured_level} → {recommended_level} dBFS (+{raise_db} dB)로 바꾸고 ‘레벨 확인’을 누르세요. "
            f"그 다음 3 · 위치 측정에서 ‘{positions_total}곳 처음부터 재측정’을 실행하세요."
        )
    else:
        status = "fail_premeasured_complex_sum"
        action = f"T5S 극성·LPF 노브와 우퍼 L/R 케이블을 확인한 뒤 3 · 위치 측정에서 ‘{positions_total}곳 처음부터 재측정’을 실행하세요. 계속되면 1 · 출력 설정의 ‘측정 구성’을 ‘표준 분리 SISO’로 바꾸고 ‘구성 적용’을 누르세요."
    return {
        "required": True,
        "pass": all_pass,
        "status": status,
        "evaluation_band_hz": [round(low_hz, 1), round(high_hz, 1)],
        "woofer_measurement_attenuation_db": float(woofer_attenuation_db),
        "normalization_applied": False,
        "normalization_policy": "absolute transfer closure; never normalize L/R/W or measured sums independently",
        "phase_clock_shared": PHASE_CLOCK_SHARED,
        "simultaneous_phase_reference": simultaneous_phase_reliable,
        "limited_phase_method": None if all_phase_reliable else "measured physical sum must remain inside |Front|-|Woofer| .. |Front|+|Woofer| magnitude bounds",
        "design_usage": "L/R/W ESS magnitudes define the branches; simultaneous L/R/W supplies phase sign/timing; passing L+W/R+W supplies dense acoustic cross-term constraints. Combined captures are never normalized or averaged into a branch.",
        "phase_reference_reliable": all_phase_reliable,
        "phase_verification_status": "pass" if all_phase_reliable and all(
            bool(item.get("phase_pass")) for item in channels.values()
        ) else "limited" if not all_phase_reliable else "fail",
        "minimum_snr_db": round(minimum_snr, 2) if minimum_snr is not None else None,
        "thresholds": thresholds,
        "channels": channels,
        "action": action,
    }


def result_fingerprint(result: dict[str, Any]) -> str:
    identity = "|".join(str(result.get(key, "")) for key in (
        "algorithm_revision", "front_sha256", "rear_sha256", "target", "preset", "woofer_trim_db",
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def effective_combined_target(result: dict[str, Any], frequencies: list[float]) -> list[float]:
    """Return the requested full-system target, including explicit user deltas."""
    target_f, target_db = target_curve(str(result.get("target", "flat")))
    preference = result.get("preference", {})
    bass_tilt_db = int(preference.get("bass_db_at_20_hz", 0))
    treble_tilt_db = int(preference.get("treble_db_at_20_khz", 0))
    preset = str(result.get("preset", "none"))
    trim_db = float(result.get("woofer_trim_db", 0))
    crossover = result.get("crossover", {})
    crossover_hz = float(crossover.get("frequency_hz") or 100.0)
    trim_scale = 10.0 ** (trim_db / 20.0)
    values = []
    for frequency in frequencies:
        value = (
            interpolate_log(target_f, target_db, frequency)
            + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
            + (bass_modifier_db(frequency, preset) if frequency <= 350.0 else 0.0)
        )
        if crossover.get("enabled") and trim_db:
            lowpass = linkwitz_riley_4_magnitude(frequency, crossover_hz, "lowpass")
            highpass = linkwitz_riley_4_magnitude(frequency, crossover_hz, "highpass")
            value += 20.0 * math.log10(max(highpass + lowpass * trim_scale, 1.0e-12))
        values.append(value)
    reference_values = [value for frequency, value in zip(frequencies, values) if 500.0 <= frequency <= 2000.0]
    reference = statistics.median(reference_values) if reference_values else 0.0
    return [value - reference for value in values]


def predicted_combined_response(
    result: dict[str, Any],
    side: str,
    frequencies: list[float],
) -> tuple[list[float], str, bool]:
    """Compose the same full-band Front+Woofer prediction shown by the UI.

    The branch graph is full-band, while the crossover model replaces its
    low-frequency section with the predicted acoustic sum.  Keeping this
    composition in the engine prevents the browser graph and post-FIR grading
    from silently using different models.
    """
    graph = (result.get("graphs") or {}).get(side) or {}
    graph_frequencies = [float(value) for value in graph.get("frequency") or ()]
    graph_values = [float(value) for value in graph.get("predicted_db") or ()]
    if not graph_frequencies or len(graph_frequencies) != len(graph_values):
        raise MeasurementError(f"{side} 프런트 FIR 예상 응답이 없습니다.")
    predicted = [interpolate_log(graph_frequencies, graph_values, frequency) for frequency in frequencies]
    crossover_channel = ((result.get("crossover") or {}).get("channels") or {}).get(side) or {}
    sum_frequencies = [float(value) for value in crossover_channel.get("frequency") or ()]
    phase_reliable = bool(crossover_channel.get("complex_prediction_reliable"))
    if phase_reliable:
        sum_values = [float(value) for value in crossover_channel.get("predicted_complex_db") or ()]
        basis = "phase_reliable_complex_sum"
    else:
        # Energy sum is the least-biased phase-agnostic estimate.  The
        # coherent upper curve remains a safety guard, not an expected value.
        source = crossover_channel.get("phase_agnostic_energy_db") or crossover_channel.get("predicted_complex_db") or ()
        sum_values = [float(value) for value in source]
        basis = "phase_limited_energy_sum"
    if sum_frequencies and len(sum_frequencies) == len(sum_values):
        low, high = sum_frequencies[0], sum_frequencies[-1]
        predicted = [
            interpolate_log(sum_frequencies, sum_values, frequency)
            if low <= frequency <= high else value
            for frequency, value in zip(frequencies, predicted)
        ]
    return predicted, basis, phase_reliable


def evaluate_post_filter_sum(directory: Path, state: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    """Judge actual FIR output against both the target and its prediction.

    Microphone SPL has an arbitrary absolute origin, so measured and predicted
    curves each receive one shared L/R 500-2000 Hz reference.  Never normalize
    L and R independently: doing so can hide a real channel-level mismatch.
    """
    result = state["result"]
    positions_total = session_position_count(state)
    channels: dict[str, Any] = {}
    raw_averaged_by_side: dict[str, list[float]] = {}
    spatial_std_by_side: dict[str, list[float]] = {}
    predicted_raw_by_side: dict[str, list[float]] = {}
    prediction_basis_by_side: dict[str, str] = {}
    prediction_reliable_by_side: dict[str, bool] = {}
    all_snrs: list[float] = []
    switching_transient_captures: list[dict[str, Any]] = []
    active_transient_captures: list[dict[str, Any]] = []
    correction_limits = result.get("correction_limits", {})
    correction_low = float(correction_limits.get("low_hz", 20.0))
    correction_high = float(correction_limits.get("high_hz", 20_000.0))
    crossover_hz = float(result.get("crossover", {}).get("frequency_hz") or 100.0)
    frequencies: list[float] | None = None
    for side in ("left", "right"):
        responses = []
        for position in range(1, positions_total + 1):
            path = directory / f"post_p{position}_{side}_sum_response.json"
            if not path.is_file():
                raise MeasurementError(f"사후 합산 응답이 없습니다: {path.name}")
            response = json.loads(path.read_text(encoding="utf-8"))
            responses.append(response)
            quality = response.get("measurement_quality", {})
            snr = quality.get("snr_db")
            if isinstance(snr, (int, float)):
                all_snrs.append(float(snr))
            if quality.get("switching_transient_suspected"):
                switching_transient_captures.append({
                    "position": position,
                    "side": side,
                    "noise_side_spread_db": quality.get("noise_side_spread_db"),
                })
            if (quality.get("frequency_noise") or {}).get("transient_contamination_detected"):
                active_transient_captures.append({"position": position, "side": side})
        if frequencies is None:
            frequencies = [float(value) for value in responses[0]["frequencies"]]
        smoothed = [
            [float(value) for value in response["db"]]
            if response.get("smoothing") == SMOOTHING_NAME or response.get("smoothing") in LEGACY_SMOOTHING_NAMES
            else variable_power_smooth(frequencies, [float(value) for value in response["db"]])
            for response in responses
        ]
        averaged = []
        spatial_std = []
        spatial_mode = str(result.get("spatial_mode", state.get("spatial_mode", "equal")))
        for index, frequency in enumerate(frequencies):
            base_weights = spatial_position_weights(frequency, positions_total, spatial_mode)
            confidence = []
            for response in responses:
                values = response.get("frequency_quality", {}).get("confidence")
                confidence.append(float(values[index]) if isinstance(values, list) and index < len(values) else 1.0)
            effective_weights = [
                weight * max(0.05, min(1.0, value))
                for weight, value in zip(base_weights, confidence)
            ]
            position_levels = [float(values[index]) for values in smoothed]
            averaged.append(weighted_power_mean_db(position_levels, effective_weights))
            spatial_std.append(weighted_std_db(position_levels, effective_weights))
        raw_averaged_by_side[side] = averaged
        spatial_std_by_side[side] = spatial_std
    assert frequencies is not None
    target_values = effective_combined_target(result, frequencies)
    measured_reference_values = [
        value
        for side in ("left", "right")
        for frequency, value in zip(frequencies, raw_averaged_by_side[side])
        if 500.0 <= frequency <= 2000.0
    ]
    if not measured_reference_values:
        raise MeasurementError("사후 실측 L/R 공통 0 dB 기준 대역을 계산할 수 없습니다.")
    measured_reference = statistics.median(measured_reference_values)
    for side in ("left", "right"):
        predicted, basis, reliable = predicted_combined_response(result, side, frequencies)
        predicted_raw_by_side[side] = predicted
        prediction_basis_by_side[side] = basis
        prediction_reliable_by_side[side] = reliable
    predicted_reference_values = [
        value
        for side in ("left", "right")
        for frequency, value in zip(frequencies, predicted_raw_by_side[side])
        if 500.0 <= frequency <= 2000.0
    ]
    if not predicted_reference_values:
        raise MeasurementError("FIR 예상 L/R 공통 0 dB 기준 대역을 계산할 수 없습니다.")
    predicted_reference = statistics.median(predicted_reference_values)
    averaged_by_side = {
        side: [value - measured_reference for value in raw_averaged_by_side[side]]
        for side in ("left", "right")
    }
    predicted_by_side = {
        side: [value - predicted_reference for value in predicted_raw_by_side[side]]
        for side in ("left", "right")
    }
    for side in ("left", "right"):
        averaged = raw_averaged_by_side[side]
        normalized = averaged_by_side[side]
        predicted = predicted_by_side[side]
        natural_low, _ = natural_usable_band(frequencies, averaged, measured_reference)
        fit_low = max(correction_low, natural_low, 20.0)
        fit_high = min(correction_high, 20_000.0)
        errors = [
            abs(measured - target)
            for frequency, measured, target in zip(frequencies, normalized, target_values)
            if fit_low <= frequency <= fit_high
        ]
        crossover_errors = [
            abs(measured - target)
            for frequency, measured, target in zip(frequencies, normalized, target_values)
            if crossover_hz * 0.5 <= frequency <= crossover_hz * 2.0
        ]
        prediction_signed_errors = [
            measured - expected
            for frequency, measured, expected in zip(frequencies, normalized, predicted)
            if fit_low <= frequency <= fit_high
        ]
        prediction_errors = [abs(value) for value in prediction_signed_errors]
        crossover_prediction_errors = [
            abs(measured - expected)
            for frequency, measured, expected in zip(frequencies, normalized, predicted)
            if crossover_hz * 0.5 <= frequency <= crossover_hz * 2.0
        ]
        mae = sum(errors) / len(errors) if errors else 0.0
        p90 = percentile(errors, 0.90)
        crossover_mae = sum(crossover_errors) / len(crossover_errors) if crossover_errors else 0.0
        crossover_p90 = percentile(crossover_errors, 0.90)
        prediction_mae = sum(prediction_errors) / len(prediction_errors) if prediction_errors else 0.0
        prediction_p90 = percentile(prediction_errors, 0.90)
        crossover_prediction_mae = sum(crossover_prediction_errors) / len(crossover_prediction_errors) if crossover_prediction_errors else 0.0
        crossover_prediction_p90 = percentile(crossover_prediction_errors, 0.90)
        channels[side] = {
            "evaluation_band_hz": [round(fit_low, 1), round(fit_high, 1)],
            "physical_extension_limit_hz": round(natural_low, 1),
            "target_mae_db": round(mae, 3),
            "target_p90_abs_error_db": round(p90, 3),
            "target_pass": bool(errors) and mae <= 3.5 and p90 <= 7.0,
            "crossover_band_hz": [round(crossover_hz * 0.5, 1), round(crossover_hz * 2.0, 1)],
            "crossover_mae_db": round(crossover_mae, 3),
            "crossover_p90_abs_error_db": round(crossover_p90, 3),
            "crossover_pass": bool(crossover_errors) and crossover_mae <= 2.5 and crossover_p90 <= 5.0,
            "prediction_basis": prediction_basis_by_side[side],
            "prediction_model_reliable": prediction_reliable_by_side[side],
            "prediction_mae_db": round(prediction_mae, 3),
            "prediction_p90_abs_error_db": round(prediction_p90, 3),
            "prediction_median_signed_error_db": round(statistics.median(prediction_signed_errors), 3) if prediction_signed_errors else None,
            "prediction_pass": bool(prediction_errors) and prediction_mae <= 3.5 and prediction_p90 <= 7.0,
            "crossover_prediction_mae_db": round(crossover_prediction_mae, 3),
            "crossover_prediction_p90_abs_error_db": round(crossover_prediction_p90, 3),
            "crossover_prediction_pass": bool(crossover_prediction_errors) and crossover_prediction_mae <= 2.5 and crossover_prediction_p90 <= 5.0,
            "frequency": [round(value, 3) for value in frequencies],
            "measured_sum_db": [round(value, 3) for value in normalized],
            "predicted_sum_db": [round(value, 3) for value in predicted],
            "effective_target_db": [round(value, 3) for value in target_values],
            "spatial_std_db": [round(value, 3) for value in spatial_std_by_side[side]],
        }
    lr_differences = [
        abs(left - right)
        for frequency, left, right in zip(frequencies, averaged_by_side["left"], averaged_by_side["right"])
        if 20.0 <= frequency <= 20_000.0
    ]
    lr_median = statistics.median(lr_differences) if lr_differences else 0.0
    reference_difference_values = [
        left - right
        for frequency, left, right in zip(frequencies, raw_averaged_by_side["left"], raw_averaged_by_side["right"])
        if 500.0 <= frequency <= 2000.0
    ]
    reference_difference = abs(statistics.median(reference_difference_values)) if reference_difference_values else 0.0
    snr_minimum = min(all_snrs) if all_snrs else None
    snr_usable = snr_minimum is not None and snr_minimum >= 6.0
    target_pass = all(item["target_pass"] for item in channels.values())
    crossover_pass = all(item["crossover_pass"] for item in channels.values())
    prediction_gate_required = all(item["prediction_model_reliable"] for item in channels.values())
    prediction_pass = all(
        item["prediction_pass"] and item["crossover_prediction_pass"]
        for item in channels.values()
    )
    lr_pass = lr_median <= 3.0 and reference_difference <= 3.0
    switching_transient_suspected = bool(switching_transient_captures)
    active_transient_suspected = bool(active_transient_captures)
    metric_pass = (
        target_pass
        and crossover_pass
        and lr_pass
        and snr_usable
        and (prediction_pass if prediction_gate_required else True)
    )
    strict_pass = metric_pass and not active_transient_suspected
    # A barely usable recording must not be mislabeled as a conclusive DSP
    # failure when its miss is close to the strict limit.  It remains a retry
    # warning and cannot become PASS.  Gross errors still block immediately.
    hard_failure = any(
        item["target_mae_db"] > 5.0
        or item["target_p90_abs_error_db"] > 10.0
        or item["crossover_mae_db"] > 4.0
        or item["crossover_p90_abs_error_db"] > 8.0
        or (item["prediction_model_reliable"] and (
            item["prediction_mae_db"] > 5.0
            or item["prediction_p90_abs_error_db"] > 10.0
            or item["crossover_prediction_mae_db"] > 4.0
            or item["crossover_prediction_p90_abs_error_db"] > 8.0
        ))
        for item in channels.values()
    ) or not lr_pass
    snr_recommended = snr_minimum is not None and snr_minimum >= RECOMMENDED_SNR_DB
    # A different pre/post stationary floor is evidence of a possible stream
    # transition, but it does not invalidate a fully passing acoustic transfer.
    # It becomes verdict-affecting only when the response also misses a metric,
    # or when the active sweep itself contains a detected transient.
    inconclusive_switching_transient = bool(
        not strict_pass
        and (
            active_transient_suspected
            or (switching_transient_suspected and not metric_pass)
        )
    )
    inconclusive_low_snr = bool(
        not inconclusive_switching_transient
        and snr_usable
        and not snr_recommended
        and not strict_pass
        and not hard_failure
    )
    inconclusive = inconclusive_switching_transient or inconclusive_low_snr
    verification_status = (
        "pass" if strict_pass else
        "inconclusive_switching_transient" if inconclusive_switching_transient else
        "inconclusive_low_snr" if inconclusive_low_snr else
        "fail"
    )
    prediction_status = (
        "inconclusive_switching_transient" if inconclusive_switching_transient else
        "pass" if prediction_pass else
        "inconclusive_low_snr" if inconclusive_low_snr else
        "fail" if prediction_gate_required else
        "warning_phase_limited"
    )
    return {
        "method": "actual Preview FIR via CamillaDSP WavFile capture; UMIK measured L+Woofer and R+Woofer after acoustic summation",
        "positions": positions_total,
        "coverage": "fast_single_position" if positions_total == 1 else "standard_three_position",
        "level_dbfs": int(post["level_dbfs"]),
        "sweep_seconds": int(post.get("sweep_seconds", state.get("sweep_seconds", POST_VALIDATION_SWEEP_SECONDS))),
        "gain_recovery": "input sweep amplitude is included in the deconvolution reference; test dBFS affects SNR/headroom only",
        "output_reference": "U7 PCM hardware unity (0 dB); saved listening volume is restored before normal input reconnects",
        "spatial_aggregation": {
            "method": "noise-confidence weighted acoustic transfer power mean",
            "formula": "10*log10(sum(w_p*10^(L_p/10))/sum(w_p))",
            "spatial_mode": str(result.get("spatial_mode", state.get("spatial_mode", "equal"))),
        },
        "channels": channels,
        "common_level_reference": {
            "scope": "post-filter measured L/R sums and predicted L/R sums",
            "reference_band_hz": [500, 2000],
            "measured_reference_db": round(measured_reference, 4),
            "predicted_reference_db": round(predicted_reference, 4),
            "independent_channel_normalization": False,
        },
        "target_pass": target_pass,
        "crossover_pass": crossover_pass,
        "prediction_consistency": {
            "gate_required": prediction_gate_required,
            "pass": prediction_pass and not inconclusive_switching_transient,
            "status": prediction_status,
            "thresholds_db": {"mae": 3.5, "p90": 7.0, "crossover_mae": 2.5, "crossover_p90": 5.0},
            "note": "신뢰 가능한 복소 합산 예측은 정식 PASS에 포함합니다. 위상 제한 모델은 차이를 표시하되 실제 타깃 실측을 우선합니다.",
        },
        "lr_match": {
            "median_shape_difference_db": round(lr_median, 3),
            "reference_level_difference_db": round(reference_difference, 3),
            "pass": lr_pass,
        },
        "snr": {
            "minimum_db": round(snr_minimum, 2) if snr_minimum is not None else None,
            "usable_minimum_db": 6.0,
            "recommended_minimum_db": 15.0,
            "pass": snr_usable,
            "recommended": snr_recommended,
        },
        "verification_status": verification_status,
        "inconclusive_low_snr": inconclusive_low_snr,
        "inconclusive_switching_transient": inconclusive_switching_transient,
        "switching_transient": {
            "suspected": switching_transient_suspected,
            "captures": switching_transient_captures,
            "active_sweep_transient_suspected": active_transient_suspected,
            "active_sweep_captures": active_transient_captures,
            "affected_verdict": inconclusive_switching_transient,
            "silent_lead_seconds": POST_VALIDATION_SILENT_LEAD_SECONDS,
            "meaning": "U7/CamillaDSP stream startup changed the pre/post noise floor and may have overlapped the low-frequency sweep start.",
        },
        "application_blocking": not strict_pass and not inconclusive,
        "recommended_retry": (
            {
                "level_dbfs": int(post["level_dbfs"]),
                "sweep_seconds": POST_VALIDATION_SWEEP_SECONDS,
                "action": "5 · 적용 전 검토에서 ‘검증 초기화’를 누른 뒤 같은 검증 sweep 입력으로 다시 측정하세요. 재측정은 시작 전 2초 무음으로 U7/CamillaDSP 출력을 안정화합니다.",
            }
            if inconclusive_switching_transient else
            {
                "level_dbfs": -25,
                "sweep_seconds": POST_VALIDATION_SWEEP_SECONDS,
                "action": "5 · 적용 전 검토에서 ‘검증 초기화’ 후 ‘검증 sweep 입력 -25 dBFS’로 다시 측정하세요. 사후 검증은 자동으로 28초 ESS를 사용합니다.",
            }
            if inconclusive_low_snr else None
        ),
        "overall_pass": strict_pass,
        "completed_unix": time.time(),
    }


def finalize_post_filter_evaluation(
    directory: Path,
    state: dict[str, Any],
    post: dict[str, Any],
) -> dict[str, Any]:
    """Re-grade saved post-FIR responses and persist one authoritative verdict."""
    positions_total = session_position_count(state)
    evaluation = evaluate_post_filter_sum(directory, state, post)
    post["evaluation"] = evaluation
    result = state["result"]
    self_validation = result.setdefault("self_validation", {})
    self_validation["post_filter_sum"] = evaluation
    previous_crossover_sum = self_validation.get("crossover_sum") or {}
    advisory = bool(
        evaluation.get("inconclusive_low_snr")
        or evaluation.get("inconclusive_switching_transient")
    )
    transient_advisory = bool(evaluation.get("inconclusive_switching_transient"))
    measured_gate_pass = bool(evaluation["overall_pass"])
    self_validation["crossover_sum"] = {
        "required": True,
        "pass": bool(previous_crossover_sum.get("pass")) if advisory else measured_gate_pass,
        "status": (
            "pass_measured" if measured_gate_pass else
            "warning_post_measurement_switching_transient" if transient_advisory else
            "warning_post_measurement_low_snr" if advisory else
            "fail_measured"
        ),
        "method": evaluation["method"],
        "positions": positions_total,
        "post_measurement_strict_pass": measured_gate_pass,
        "post_measurement_application_blocking": bool(evaluation.get("application_blocking")),
        "premeasurement_status": previous_crossover_sum.get("premeasurement_status") or previous_crossover_sum.get("status"),
    }
    core_pass = all(bool(value) for value in (self_validation.get("core_checks") or {}).values())
    independent_pass = bool((self_validation.get("independent_positions") or {}).get("pass"))
    required_target_pass = all(
        item is None or item.get("applicable") is False or item.get("pass")
        for item in (self_validation.get("target_fit") or {}).values()
    )
    self_validation["overall_pass"] = core_pass and independent_pass and required_target_pass and bool(self_validation["crossover_sum"]["pass"])
    result["crossover"]["post_filter_measurement"] = evaluation
    result["crossover"]["status"] = (
        "pass_measured" if measured_gate_pass else
        "warning_post_measurement_switching_transient" if transient_advisory else
        "warning_post_measurement_low_snr" if advisory else
        "fail_measured"
    )
    result["crossover"]["overall_acoustic_prediction_pass"] = measured_gate_pass
    sync_mimo_manifest_validation(directory, result)
    atomic_json(directory / result["report_json"], result)
    write_room_tuning_report(directory / result["report_md"], state, result)
    state["stage"] = (
        f"Preview FIR {positions_total}위치 합산 실측 PASS" if measured_gate_pass else
        f"Preview FIR {positions_total}위치 합산 실측 재검증 권장 · 출력 전환 감지" if transient_advisory else
        f"Preview FIR {positions_total}위치 합산 실측 재검증 권장 · SNR 부족" if advisory else
        f"Preview FIR {positions_total}위치 합산 실측 FAIL"
    )
    state["result"] = result
    state["post_filter_validation"] = post
    state["state"] = "built"
    state["worker_pid"] = None
    state["progress"] = 100.0
    save_current(state)
    atomic_json(directory / "session.json", state)
    return state


def reprocess_post_filter_validation() -> dict[str, Any]:
    """Re-grade completed saved post-FIR responses without replaying sound."""
    with LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = load_current()
        if state.get("state") in ("running", "processing", "cancelling"):
            raise MeasurementError("작업 중에는 저장 사후 검증을 다시 판정할 수 없습니다.")
        result = state.get("result")
        if not isinstance(result, dict):
            raise MeasurementError("유지할 FIR 결과가 없습니다.")
        validate_result_revision(result)
        post = state.get("post_filter_validation")
        if not isinstance(post, dict):
            raise MeasurementError("다시 판정할 사후 합산 검증이 없습니다.")
        positions_total = session_position_count(state)
        if int(post.get("positions_completed", 0)) != positions_total:
            raise MeasurementError("사후 합산 검증이 모든 위치에서 완료되지 않았습니다.")
        if post.get("result_fingerprint") != result_fingerprint(result):
            raise MeasurementError("저장 사후 검증이 현재 FIR 결과와 다릅니다. 이번 튜닝 Preview로 다시 측정하세요.")
        state["stage"] = "저장 사후 합산 응답 다시 판정 중"
        state["progress"] = 95.0
        save_current(state)
        return finalize_post_filter_evaluation(Path(state["session_dir"]), state, post)


def post_filter_validation_worker(level_dbfs: int) -> None:
    state = load_current()
    if level_dbfs not in ALLOWED_SWEEP_LEVELS or level_dbfs > -12:
        raise MeasurementError("사후 검증 sweep은 -54~-12 dBFS 범위여야 합니다.")
    if state.get("state") not in ("built", "running") or not isinstance(state.get("result"), dict):
        raise MeasurementError("먼저 32768탭 FIR을 생성하세요.")
    result = state["result"]
    validate_result_revision(result)
    supported_mode = state.get("mode") in SEPARATE_WOOFER_MODES or (
        state.get("mode") in MIMO_MODES and result.get("kind") == "mimo_2x4"
    )
    if not supported_mode or not (
        result.get("crossover", {}).get("enabled")
        or result.get("crossover", {}).get("sum_guard_enabled")
    ):
        raise MeasurementError("필터 적용 후 합산 검증은 독립 프런트/우퍼 합산 보호 또는 MIMO 구성에서 사용합니다.")
    if not state.get("preview_active") or not state.get("preview_profile"):
        raise MeasurementError("먼저 이번 튜닝 Preview를 적용하세요.")
    ensure_post_preview_output_path(state)
    positions_total = session_position_count(state)
    post_sweep_seconds = max(int(state["sweep_seconds"]), POST_VALIDATION_SWEEP_SECONDS)
    fingerprint = result_fingerprint(result)
    post = state.get("post_filter_validation")
    if not isinstance(post, dict) or post.get("result_fingerprint") != fingerprint or post.get("profile") != state.get("preview_profile"):
        post = {
            "result_fingerprint": fingerprint,
            "profile": state.get("preview_profile"),
            "level_dbfs": level_dbfs,
            "sweep_seconds": post_sweep_seconds,
            "positions_total": positions_total,
            "positions_completed": 0,
            "measurements": [],
            "evaluation": None,
        }
    elif int(post.get("level_dbfs", level_dbfs)) != level_dbfs and int(post.get("positions_completed", 0)):
        raise MeasurementError("진행 중인 사후 검증과 출력 레벨이 다릅니다. 기존 레벨로 계속하거나 사후 검증만 초기화하세요.")
    position = int(post.get("positions_completed", 0)) + 1
    if position > positions_total:
        raise MeasurementError("선택한 위치의 사후 합산 검증이 이미 완료되었습니다.")
    directory = Path(state["session_dir"])
    cal = calibration_for("90")
    pending = []
    references: dict[str, list[float]] = {}
    for side in ("left", "right"):
        input_path = directory / f"post_p{position}_{side}_sum_input.wav"
        recorded = directory / f"post_p{position}_{side}_sum_recorded.wav"
        references[side] = write_filtered_stereo_sweep(input_path, side, level_dbfs, post_sweep_seconds)
        pending.append((input_path, recorded, f"위치 {position}/{positions_total} · {'L' if side == 'left' else 'R'}+우퍼"))
    run_filtered_capture_batch(pending, 0.0, 70.0)
    measurements = list(post.get("measurements", []))
    for index, side in enumerate(("left", "right")):
        update_current(state="processing", stage=f"사후 합산 검증 · 위치 {position}/{positions_total} · {side.title()} 응답 계산", progress=75.0 + index * 10.0)
        recorded = directory / f"post_p{position}_{side}_sum_recorded.wav"
        response = response_from_recording(
            recorded,
            references[side],
            cal,
            f"{side}_woofer",
            configured_level_dbfs=level_dbfs,
        )
        response["measurement"] = {
            "kind": "post_filter_acoustic_sum",
            "position": position,
            "side": side,
            "input_level_dbfs": level_dbfs,
            "gain_recovered_by_reference": True,
            "preview_profile": state.get("preview_profile"),
            "result_fingerprint": fingerprint,
        }
        response_path = directory / f"post_p{position}_{side}_sum_response.json"
        atomic_json(response_path, response)
        measurements = [
            item for item in measurements
            if (int(item.get("position", 0)), str(item.get("side", ""))) != (position, side)
        ]
        measurements.append({
            "position": position,
            "side": side,
            "recording": recorded.name,
            "response": response_path.name,
            "snr_db": response["measurement_quality"]["snr_db"],
        })
    post.update({
        "level_dbfs": level_dbfs,
        "positions_completed": position,
        "measurements": measurements,
        "updated_unix": time.time(),
    })
    state = load_current()
    state["post_filter_validation"] = post
    state["state"] = "built"
    state["worker_pid"] = None
    state["progress"] = 100.0 * position / positions_total
    if position == positions_total:
        finalize_post_filter_evaluation(directory, state, post)
        return
    state["stage"] = f"사후 합산 검증 위치 {position}/{positions_total} 완료 · 마이크를 다음 위치로 옮기세요. FIR 결과는 유지됩니다."
    state["result"] = result
    state["post_filter_validation"] = post
    save_current(state)
    atomic_json(directory / "session.json", state)


def sync_mimo_manifest_validation(directory: Path, result: dict[str, Any]) -> None:
    """Keep the install gate in a generated MIMO manifest aligned with post-FIR validation."""
    if result.get("kind") != "mimo_2x4":
        return
    filename = result.get("mimo_manifest")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise MeasurementError("MIMO manifest 파일명이 잘못되었습니다.")
    path = directory / filename
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasurementError(f"MIMO manifest 검증 상태 갱신 실패: {exc}") from exc
    manifest["self_validation"] = result.get("self_validation", {})
    manifest["post_filter_measurement_pass"] = (result.get("self_validation", {}).get("crossover_sum") or {}).get("pass")
    atomic_json(path, manifest)


def reset_post_filter_validation() -> dict[str, Any]:
    """Reset only post-FIR verification; original captures and generated FIR stay."""
    with LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = load_current()
        if state.get("state") in ("running", "processing", "cancelling"):
            raise MeasurementError("작업 중에는 사후 합산 검증을 초기화할 수 없습니다.")
        if not state.get("result"):
            raise MeasurementError("유지할 FIR 결과가 없습니다.")
        state["post_filter_validation"] = None
        self_validation = state["result"].setdefault("self_validation", {})
        self_validation.pop("post_filter_sum", None)
        crossover_required = bool(
            (
                state["result"].get("crossover", {}).get("enabled")
                or state["result"].get("crossover", {}).get("sum_guard_enabled")
            )
            and (
                state.get("mode") in SEPARATE_WOOFER_MODES
                or (state.get("mode") in MIMO_MODES and state["result"].get("kind") == "mimo_2x4")
            )
        )
        if crossover_required:
            crossover = state["result"].setdefault("crossover", {})
            # Post-FIR verification deliberately replaces the visible crossover
            # status with pass_measured/fail_measured.  Reconstruct the immutable
            # premeasurement model verdict from its component checks instead of
            # reading that overwritten status back during Reset.
            safe_deploy_value = crossover.get("safe_deploy_pass")
            if isinstance(safe_deploy_value, bool):
                prediction_pass = safe_deploy_value
            else:
                prediction_pass = bool(
                    crossover.get("model_prediction_pass")
                    or crossover.get("overall_acoustic_prediction_pass")
                )
            phase_status = str(crossover.get("phase_verification_status") or "limited")
            if prediction_pass:
                model_prediction_status = (
                    "pass" if phase_status == "pass" else "pass_safe_upper_phase_limited"
                )
            else:
                coherent_guard = crossover.get("coherent_upper_guard_pass")
                model_prediction_status = (
                    "fail_upper_guard" if coherent_guard is False else "fail_target"
                )
            if state.get("mode") in PREMEASURED_SUM_MODES:
                premeasured = self_validation.get("premeasured_sum_model") or {}
                restored_pass = bool(premeasured.get("pass") and prediction_pass)
                precise_phase_pass = bool(
                    restored_pass
                    and premeasured.get("phase_verification_status") == "pass"
                    and phase_status == "pass"
                )
                restored_status = (
                    "pass_premeasured_complex_model" if precise_phase_pass else
                    "pass_safe_sum_phase_limited" if restored_pass else
                    str(premeasured.get("status") or model_prediction_status)
                )
                self_validation["crossover_sum"] = {
                    "required": True,
                    "pass": restored_pass,
                    "status": restored_status,
                    "prediction_status": model_prediction_status,
                    "verification": "premeasured_complex_model",
                }
                core_pass = all(bool(value) for value in (self_validation.get("core_checks") or {}).values())
                independent_pass = bool((self_validation.get("independent_positions") or {}).get("pass"))
                required_target_pass = all(
                    item is None or item.get("applicable") is False or item.get("pass")
                    for item in (self_validation.get("target_fit") or {}).values()
                )
                self_validation["overall_pass"] = core_pass and independent_pass and required_target_pass and restored_pass
            else:
                restored_status = (
                    "pass_independent_complex_model" if prediction_pass and phase_status == "pass" else
                    "pass_safe_upper_phase_limited" if prediction_pass else
                    model_prediction_status
                )
                self_validation["crossover_sum"] = {
                    "required": True,
                    "pass": prediction_pass,
                    "status": restored_status,
                    "prediction_status": model_prediction_status,
                    "verification": "independent_same_clock_complex_model",
                }
                core_pass = all(bool(value) for value in (self_validation.get("core_checks") or {}).values())
                independent_pass = bool((self_validation.get("independent_positions") or {}).get("pass"))
                required_target_pass = all(
                    item is None or item.get("applicable") is False or item.get("pass")
                    for item in (self_validation.get("target_fit") or {}).values()
                )
                self_validation["overall_pass"] = core_pass and independent_pass and required_target_pass and prediction_pass
            crossover.pop("post_filter_measurement", None)
            crossover["status"] = model_prediction_status
            crossover["overall_acoustic_prediction_pass"] = bool(
                prediction_pass and phase_status == "pass"
            )
            sync_mimo_manifest_validation(Path(state["session_dir"]), state["result"])
        state["stage"] = "사후 합산 검증만 초기화했습니다. 원측정과 생성 FIR은 유지됩니다."
        directory = Path(state["session_dir"])
        report_json = state["result"].get("report_json")
        report_md = state["result"].get("report_md")
        if isinstance(report_json, str) and Path(report_json).name == report_json:
            atomic_json(directory / report_json, state["result"])
        if isinstance(report_md, str) and Path(report_md).name == report_md:
            write_room_tuning_report(directory / report_md, state, state["result"])
        save_current(state)
        atomic_json(directory / "session.json", state)
        return state


def build_room_tuning_audit(directory: Path, state: dict[str, Any], *, mimo: bool = False) -> list[dict[str, Any]]:
    """Persist an explicit corrected/limited/not-measured inventory; never imply FIR can fix everything."""
    responses = []
    positions_total = session_position_count(state)
    for position in range(1, positions_total + 1):
        for source in state.get("sources", []):
            path = directory / f"p{position}_{source}_response.json"
            if path.is_file():
                responses.append(json.loads(path.read_text(encoding="utf-8")))
    snr = [float(item.get("measurement_quality", {}).get("snr_db")) for item in responses if isinstance(item.get("measurement_quality", {}).get("snr_db"), (int, float))]
    clipping = [bool(item.get("measurement_quality", {}).get("clipped")) for item in responses]
    c50 = [float(item.get("temporal", {}).get("c50_db")) for item in responses if isinstance(item.get("temporal", {}).get("c50_db"), (int, float))]
    c80 = [float(item.get("temporal", {}).get("c80_db")) for item in responses if isinstance(item.get("temporal", {}).get("c80_db"), (int, float))]
    group_delay = [float(item.get("group_delay", {}).get("bass_p90_ms")) for item in responses if isinstance(item.get("group_delay", {}).get("bass_p90_ms"), (int, float))]
    decay = [
        float(band["t20_rt60_s"]) for item in responses
        for band in item.get("room_decay", {}).get("bands", [])
        if band.get("reliable") and isinstance(band.get("t20_rt60_s"), (int, float))
    ]
    capability = platform_capabilities()
    noise_status = "pass" if snr and min(snr) >= 15.0 and not any(clipping) else ("usable_with_warning" if snr and min(snr) >= 6.0 and not any(clipping) else "fail")
    spatial_class = "mimo_correctable" if mimo else "limited_fir"
    spatial_action = "복소 전달행렬을 공동 최적화해 측정한 세 위치의 저역 편차를 줄임" if mimo else "공간 평균 FIR은 공통 peak를 줄이지만 좌석마다 다른 null은 해결하지 못함; Pi4/5 MIMO 또는 배치 변경 검토"
    return [
        {"id": "noise_headroom", "label": "배경소음·SNR·클리핑", "classification": "measurement_gate", "status": noise_status, "evidence": {"minimum_snr_db": round(min(snr), 2) if snr else None, "recommended_snr_db": 15.0, "clipped_sweeps": sum(clipping)}, "action": "SNR 15 dB 이상 권장; clipping 또는 6 dB 미만이면 결과 적용 차단"},
        {"id": "magnitude_target", "label": "주파수 응답·선호 타깃", "classification": "fir_correctable", "status": "evaluated", "action": "신뢰도·공간편차 가중, 자연 roll-off 보호, boost/cut 제한으로 보정"},
        {"id": "bass_extension_headroom", "label": "저역 확장·출력 headroom", "classification": "limited_fir", "status": "evaluated", "action": "자연 저역 한계 아래 boost 금지; 드라이버 변위·앰프 여유·왜곡은 FIR이 늘릴 수 없음"},
        {"id": "spatial_variance", "label": "좌석 간 저역 편차", "classification": spatial_class, "status": "evaluated", "evidence": {"mimo_platform_supported": capability["mimo_supported"]}, "action": spatial_action},
        {"id": "arrival_polarity_phase", "label": "도착시간·극성·저역 위상", "classification": "limited_mimo" if mimo else "limited_fir", "status": "evaluated", "evidence": {"bass_group_delay_p90_ms_median": round(statistics.median(group_delay), 2) if group_delay else None}, "action": "공통 인과 지연과 저역 excess phase만 제한적으로 보정; 위치별 고역 phase 역보정 금지"},
        {"id": "modal_decay", "label": "룸 모드·저역 감쇠시간", "classification": "limited_mimo" if mimo else "limited_fir", "status": "evaluated" if decay else "insufficient_data", "evidence": {"reliable_t20_rt60_s_median": round(statistics.median(decay), 3) if decay else None}, "action": "공진 peak cut 및 MIMO 능동 제어로 초기 꼬리 감소 가능; 방의 물리 RT60 전체 제거 불가"},
        {"id": "sbir_early_reflections", "label": "SBIR·초기반사·명료도", "classification": "diagnostic_placement", "status": "evaluated" if c50 or c80 else "insufficient_data", "evidence": {"median_c50_db": round(statistics.median(c50), 2) if c50 else None, "median_c80_db": round(statistics.median(c80), 2) if c80 else None}, "action": "깊은 null은 boost하지 않고 스피커/청취 위치·벽 거리·1차 반사 흡음 조정"},
        {"id": "late_reverberation", "label": "중·고역 늦은 잔향·확산", "classification": "physical_treatment", "status": "diagnostic_only", "action": "late field의 시간역 보정은 공간적으로 불안정; 흡음·확산·가구·배치로 개선"},
        {"id": "speaker_matching", "label": "L/R 감도·음색 매칭", "classification": "fir_correctable", "status": "evaluated", "action": "독립 L/R magnitude를 맞추되 청취영역 공통 성분과 지향성 차이를 혼동하지 않음"},
        {"id": "crossover_integration", "label": "메인–우퍼 크로스오버 합산", "classification": "limited_mimo" if mimo else "limited_fir", "status": "evaluated" if state.get("mode") in ("lrw", "lrw_sum", "mimo_one_sub", "mimo_dual_sub") else "not_applicable", "action": "레벨·지연·극성·저역 phase를 공동 조정; 아날로그 crossover 자체와 비선형은 변경 불가"},
        {"id": "nonlinear_distortion", "label": "고조파 왜곡·압축·기계 잡음", "classification": "not_measured", "status": "not_available", "action": "향후 다중 레벨 Farina harmonic 분리 측정 필요; 선형 convolution으로 보정 불가"},
        {"id": "directivity", "label": "지향성·파워 응답·오프축", "classification": "not_measured", "status": "not_available", "action": "회전/근접 다각도 측정이 필요; 단일 청취영역 UMIK 측정으로 분리 불가"},
        {"id": "binaural_spatial", "label": "IACC·양이간 공간감·이미징", "classification": "not_measured", "status": "not_available", "action": "단일 omnidirectional UMIK-1로 직접 측정 불가; 더미헤드/2마이크와 별도 지표 필요"},
        {"id": "absolute_spl_neighbor", "label": "절대 SPL·청력·층간소음", "classification": "not_certified", "status": "not_available", "action": "UMIK sensitivity/전체 체인 검교정과 수음세대 측정 없이는 보장 불가; 야간 저역 shelf·volume cap은 위험 저감일 뿐"},
        {"id": "latency_clock", "label": "실시간 latency·clock drift·XRUN", "classification": "runtime_validation", "status": "requires_runtime_test", "action": "적용 후 CamillaDSP load, ALSA XRUN, rate drift, end-to-end latency를 무음 상태에서 모니터링"},
        {"id": "post_verification", "label": "적용 후 독립 재측정", "classification": "measurement_gate", "status": "required", "action": "동일 위치 재사용만으로 과적합을 판단하지 말고 별도 검증 위치에서 전/후 측정"},
    ]


def write_room_tuning_report(path: Path, state: dict[str, Any], result: dict[str, Any]) -> None:
    normalization = result.get("filter_bank_normalization") or {}
    common_reference = result.get("common_level_reference") or {}
    lines = [
        "# AudioDSP 룸 튜닝 보고서", "",
        f"- 모드: `{state.get('mode')}`", f"- 타깃: `{result.get('target')}`", f"- FIR: {result.get('sample_rate')} Hz / {result.get('taps')} taps",
        f"- 자체 검증: {'PASS' if result.get('self_validation', {}).get('overall_pass') else 'FAIL'}",
        f"- 공통 0 dB 기준: `{common_reference.get('scope', 'unknown')}` / 채널별 정규화 `{common_reference.get('independent_channel_normalization')}`",
        f"- FIR 묶음 공통 gain: {normalization.get('applied_common_gain_db', '?')} dB / 최대 상대 보상 {normalization.get('max_relative_compensation_db', result.get('correction_limits', {}).get('max_relative_compensation_db', '?'))} dB",
        f"- 상대레벨 보존: `{normalization.get('relative_branch_gain_preserved')}`", "",
        "## 보정 가능성 분류", "",
    ]
    for item in result.get("room_tuning_audit", []):
        lines.append(f"- **{item['label']}** — `{item['classification']}` / `{item['status']}`: {item['action']}")
    lines += ["", "## 해석 원칙", "", "L/R/우퍼는 서로 다른 0 dB로 재정렬하지 않는다. 하나의 공통 측정 기준과 FIR 묶음 공통 gain을 사용해 상대레벨과 crossover 합산을 보존한다. 좁고 깊은 null은 최대 상대 보상보다 우선하는 과보정 보호 대상이다.", "", "`fir_correctable`도 측정한 위치와 선형·시간불변 범위에서만 유효하다. `limited_*`는 부분 개선이며, `physical_treatment`, `not_measured`, `not_certified`는 FIR 성공으로 표시하지 않는다.", ""]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def target_curve(name: str) -> tuple[list[float], list[float]]:
    if name == "flat":
        return [3.0, 24_000.0], [0.0, 0.0]
    if name not in TARGET_FILES:
        raise MeasurementError("Unknown target curve.")
    path = TARGET_DIR / str(TARGET_FILES[name])
    frequencies: list[float] = []
    levels: list[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        fields = raw.strip().split()
        if len(fields) >= 2:
            try:
                frequencies.append(float(fields[0]))
                levels.append(float(fields[1]))
            except ValueError:
                pass
    if len(frequencies) < 20:
        raise MeasurementError("Harman target curve is missing or invalid.")
    return frequencies, levels


def target_catalog() -> dict[str, Any]:
    labels = {
        "flat": "Flat",
        "harman": "Harman Kardon",
        "rtings": "RTINGS",
        "acoustix": "AcoustiX Default",
        "toole": "Not Dr. Toole",
        "bk": "Brüel & Kjær",
    }
    result = {}
    graph_frequencies = [20.0 * (1000.0 ** (index / 159.0)) for index in range(160)]
    for name in TARGET_FILES:
        frequencies, levels = target_curve(name)
        reference = interpolate_log(frequencies, levels, 1000.0)
        result[name] = {
            "label": labels[name],
            "frequency": [round(value, 2) for value in graph_frequencies],
            "db": [round(interpolate_log(frequencies, levels, value) - reference, 3) for value in graph_frequencies],
        }
    return {"targets": result, "reference_hz": 1000, "default": "harman"}


def weighted_power_mean_db(values: list[float], weights: list[float]) -> float:
    """Return 10log10 of a weighted mean-square response without overflow.

    The stored response is a magnitude level ``20log10|H|``.  Converting with
    ``10**(L/10)`` therefore averages ``|H|**2`` (acoustic transfer power), not
    amplitudes or logarithms.  A max-level offset implements a log-sum-exp
    evaluation and keeps very quiet bins numerically stable.
    """
    if not values or len(values) != len(weights):
        raise MeasurementError("파워 평균 입력 길이가 일치하지 않습니다.")
    pairs = [(float(value), float(weight)) for value, weight in zip(values, weights)]
    if any(not math.isfinite(value) or not math.isfinite(weight) for value, weight in pairs):
        raise MeasurementError("파워 평균에 NaN/Inf가 포함되어 있습니다.")
    positive = [(value, max(0.0, weight)) for value, weight in pairs if weight > 0.0]
    if not positive:
        raise MeasurementError("파워 평균의 유효 가중치가 없습니다.")
    maximum = max(value for value, _weight in positive)
    weight_sum = sum(weight for _value, weight in positive)
    relative_power = sum(weight * 10.0 ** ((value - maximum) / 10.0) for value, weight in positive)
    return maximum + 10.0 * math.log10(max(relative_power / weight_sum, 1.0e-30))


def weighted_std_db(values: list[float], weights: list[float]) -> float:
    """Noise-aware population spread in dB for spatial regularization."""
    if not values or len(values) != len(weights):
        raise MeasurementError("공간 분산 입력 길이가 일치하지 않습니다.")
    pairs = [(float(value), float(weight)) for value, weight in zip(values, weights)]
    if any(not math.isfinite(value) or not math.isfinite(weight) for value, weight in pairs):
        raise MeasurementError("공간 분산에 NaN/Inf가 포함되어 있습니다.")
    weight_sum = sum(max(0.0, weight) for _value, weight in pairs)
    if weight_sum <= 0.0:
        return 0.0
    mean = sum(max(0.0, weight) * value for value, weight in pairs) / weight_sum
    variance = sum(
        max(0.0, weight) * (value - mean) ** 2
        for value, weight in pairs
    ) / weight_sum
    return math.sqrt(max(0.0, variance))


def spatial_position_weights(frequency: float, positions_total: int, spatial_mode: str) -> list[float]:
    """Return normalized geometric position weights before SNR confidence.

    Standard/equal treats the three nearby microphone positions equally.
    Center-priority only increases the reference-position weight above 200 Hz;
    bass remains a true seat-area average because its modal field is spatial.
    """
    if positions_total not in ALLOWED_POSITION_COUNTS:
        raise MeasurementError("공간 평균의 위치 수가 잘못되었습니다.")
    if spatial_mode not in ("equal", "center"):
        raise MeasurementError("공간 평균 방식이 잘못되었습니다.")
    if positions_total == 1 or spatial_mode == "equal":
        return [1.0 / positions_total] * positions_total
    if frequency <= 200.0:
        center_weight = 1.0 / 3.0
    elif frequency >= 2000.0:
        center_weight = 0.60
    else:
        blend = math.log(frequency / 200.0) / math.log(10.0)
        center_weight = 1.0 / 3.0 + blend * (0.60 - 1.0 / 3.0)
    return [center_weight, (1.0 - center_weight) / 2.0, (1.0 - center_weight) / 2.0]


def variable_smooth(frequencies: list[float], values: list[float]) -> list[float]:
    """Triangular fractional-octave arithmetic smoothing in dB.

    Keep this operation for *filter gain* curves such as the cut-only crossover
    guard.  Power averaging a negative gain curve biases it toward 0 dB and can
    silently weaken a safety cut.  Measured acoustic responses use the distinct
    :func:`variable_power_smooth` operation below.
    """
    if len(frequencies) != len(values) or not values:
        raise MeasurementError("Smoothing input length mismatch.")
    result: list[float] = []
    for center, frequency in enumerate(frequencies):
        width_octaves = 1.0 / 12.0 if frequency < 200.0 else (1.0 / 6.0 if frequency < 2000.0 else 1.0 / 3.0)
        half = width_octaves / 2.0
        weighted = 0.0
        weight_sum = 0.0
        for candidate, value in zip(frequencies, values):
            distance = abs(math.log2(max(candidate, 1.0e-9) / max(frequency, 1.0e-9)))
            if distance <= half:
                weight = 1.0 - distance / max(half, 1.0e-9)
                weighted += float(value) * weight
                weight_sum += weight
        result.append(weighted / weight_sum if weight_sum else float(values[center]))
    return result


def variable_power_smooth(frequencies: list[float], values: list[float]) -> list[float]:
    """Triangular fractional-octave smoothing of acoustic transfer power."""
    if len(frequencies) != len(values) or not values:
        raise MeasurementError("Smoothing input length mismatch.")
    result: list[float] = []
    for center, frequency in enumerate(frequencies):
        width_octaves = 1.0 / 12.0 if frequency < 200.0 else (1.0 / 6.0 if frequency < 2000.0 else 1.0 / 3.0)
        half = width_octaves / 2.0
        local_values: list[float] = []
        local_weights: list[float] = []
        for candidate, value in zip(frequencies, values):
            distance = abs(math.log2(max(candidate, 1.0e-9) / max(frequency, 1.0e-9)))
            if distance <= half:
                weight = 1.0 - distance / max(half, 1.0e-9)
                local_values.append(float(value))
                local_weights.append(weight)
        result.append(weighted_power_mean_db(local_values, local_weights) if local_weights else float(values[center]))
    return result


def preference_modifier_db(frequency: float, bass_db: int, treble_db: int) -> float:
    """Smooth optional house-curve adjustments anchored at 250 Hz and 1 kHz."""
    bass = 0.0
    if frequency <= 20.0:
        bass = float(bass_db)
    elif frequency < 250.0:
        position = math.log(frequency / 20.0) / math.log(250.0 / 20.0)
        bass = bass_db * (0.5 + 0.5 * math.cos(math.pi * position))
    treble = 0.0
    if frequency >= 20_000.0:
        treble = float(treble_db)
    elif frequency > 1000.0:
        position = math.log(frequency / 1000.0) / math.log(20_000.0 / 1000.0)
        treble = treble_db * (0.5 - 0.5 * math.cos(math.pi * position))
    return bass + treble


def natural_usable_band(frequencies: list[float], levels_db: list[float], reference_db: float) -> tuple[float, float]:
    """Estimate a branch-local -10 dB band using broad local medians.

    This is a physical boost guard, not a channel normalization.  It must work
    for a Woofer whose upper edge is far below 1 kHz as well as a full-range
    front speaker.
    """
    if len(frequencies) != len(levels_db) or not frequencies:
        raise MeasurementError("자연 재생대역 응답 길이가 일치하지 않습니다.")
    normalized = [value - reference_db for value in levels_db]
    half_width = 2.0 ** 0.25
    supported = []
    for frequency in frequencies:
        if not 20.0 <= frequency <= 20_000.0:
            continue
        window = [
            value for candidate, value in zip(frequencies, normalized)
            if frequency / half_width <= candidate <= frequency * half_width
        ]
        if window and statistics.median(window) >= -10.0:
            supported.append(float(frequency))
    if not supported:
        # No boost may be inferred when even a broad local window never reaches
        # the branch-local -10 dB threshold.
        return 20_000.0, 20.0
    return max(20.0, supported[0]), min(20_000.0, supported[-1])


def correction_window(frequency: float, low_hz: int, high_hz: int) -> float:
    # A user-selected 20 kHz upper edge means full audible-band correction,
    # not a fade that has already reached zero at 20 kHz.  Keep full weight to
    # 20 kHz and taper only in the inaudible guard band so the FIR remains
    # smooth near Nyquist.
    full_band = high_hz >= 20_000
    upper_stop = 22_000.0 if full_band else float(high_hz)
    if frequency < low_hz or frequency > upper_stop:
        return 0.0
    lower_end = min(high_hz, low_hz * math.sqrt(2.0))
    upper_start = 20_000.0 if full_band else max(low_hz, high_hz / math.sqrt(2.0))
    if frequency < lower_end and lower_end > low_hz:
        position = math.log(frequency / low_hz) / math.log(lower_end / low_hz)
        return 0.5 - 0.5 * math.cos(math.pi * position)
    if frequency > upper_start and upper_stop > upper_start:
        position = math.log(frequency / upper_start) / math.log(upper_stop / upper_start)
        return 0.5 + 0.5 * math.cos(math.pi * position)
    return 1.0


def narrow_notch_reliability(frequencies: list[float], levels_db: list[float]) -> list[float]:
    """Down-weight narrow dips without treating a broad edge roll-off as a null.

    Both one-sixth-octave neighbours must exist.  That intentionally leaves a
    monotonic response near either measurement edge alone, while a local dip
    between two supported shoulders gets progressively less boost authority.
    """
    if len(frequencies) != len(levels_db) or not frequencies:
        raise MeasurementError("notch 보호용 주파수 응답 길이가 일치하지 않습니다.")
    ratio = 2.0 ** (1.0 / 6.0)
    result: list[float] = []
    for frequency, level in zip(frequencies, levels_db):
        lower_frequency = frequency / ratio
        upper_frequency = frequency * ratio
        if lower_frequency < frequencies[0] or upper_frequency > frequencies[-1]:
            result.append(1.0)
            continue
        lower = interpolate_log(frequencies, levels_db, lower_frequency)
        upper = interpolate_log(frequencies, levels_db, upper_frequency)
        local_trend = 0.5 * (lower + upper)
        notch_depth = max(0.0, local_trend - level)
        result.append(1.0 / (1.0 + (notch_depth / 3.0) ** 2))
    return result


def stereo_broad_rolloff_confidence(
    left_f: list[float],
    left_db: list[float],
    left_confidence: list[float],
    right_f: list[float],
    right_db: list[float],
    right_confidence: list[float],
    target_f: list[float],
    target_db: list[float],
    shared_measure_reference_db: float,
    shared_target_reference_db: float,
) -> tuple[list[float], list[float], list[float], list[float], dict[str, Any]]:
    """Corroborate broad upper-band roll-off seen by both front channels.

    A single in-room high-frequency dip is not enough evidence for boost.  A
    smooth deficit present in both independently measured channels can,
    however, raise a low raw SNR confidence floor.  Narrow-notch protection is
    applied later and remains authoritative.
    """
    def enhanced(
        frequencies: list[float],
        confidence: list[float],
        other_f: list[float],
        own_db: list[float],
        other_db: list[float],
    ) -> tuple[list[float], list[float], int, float]:
        values: list[float] = []
        floors: list[float] = []
        raised = 0
        largest_floor = 0.0
        for frequency, raw_confidence, own_level in zip(frequencies, confidence, own_db):
            base = max(0.0, min(1.0, float(raw_confidence)))
            if not 2_000.0 <= frequency <= 20_000.0:
                values.append(base)
                floors.append(0.0)
                continue
            other_level = interpolate_log(other_f, other_db, frequency)
            target_level = interpolate_log(target_f, target_db, frequency) - shared_target_reference_db
            own_relative = own_level - shared_measure_reference_db
            other_relative = other_level - shared_measure_reference_db
            own_deficit = target_level - own_relative
            other_deficit = target_level - other_relative
            common_deficit = min(own_deficit, other_deficit)
            disagreement = abs(own_deficit - other_deficit)
            floor = 0.0
            if common_deficit >= 1.5 and disagreement <= 4.0:
                # Both independently measured channels show the same smooth
                # upper-band loss.  This is stronger evidence than the raw
                # per-bin SNR near a natural roll-off edge.  Preserve a
                # disagreement penalty, but do not apply a second severe
                # attenuation that makes a user-selected 10 dB relative limit
                # behave like only 4-5 dB.  Narrow-notch reliability remains a
                # separate, authoritative guard in design_channel().
                agreement_weight = max(0.50, 1.0 - disagreement / 8.0)
                floor = min(0.90, 0.35 + 0.055 * min(common_deficit, 10.0)) * agreement_weight
            value = max(base, floor)
            if value > base + 1.0e-9:
                raised += 1
                largest_floor = max(largest_floor, floor)
            values.append(value)
            floors.append(floor)
        return values, floors, raised, largest_floor

    left_values, left_floors, left_raised, left_floor = enhanced(
        left_f, left_confidence, right_f, left_db, right_db
    )
    right_values, right_floors, right_raised, right_floor = enhanced(
        right_f, right_confidence, left_f, right_db, left_db
    )
    return left_values, right_values, left_floors, right_floors, {
        "method": "independent L/R agreement floor for broad 2-20 kHz roll-off",
        "left_bins_raised": left_raised,
        "right_bins_raised": right_raised,
        "maximum_confidence_floor": round(max(left_floor, right_floor), 4),
        "narrow_null_guard_remains_enabled": True,
    }


def tone_preference_window(frequency: float) -> float:
    """Keep explicit broad tone anchors intact at 20 Hz and 20 kHz."""
    if frequency <= 10.0 or frequency >= 22_000.0:
        return 0.0
    if frequency < 20.0:
        position = math.log(frequency / 10.0) / math.log(2.0)
        return 0.5 - 0.5 * math.cos(math.pi * position)
    if frequency <= 20_000.0:
        return 1.0
    position = math.log(frequency / 20_000.0) / math.log(22_000.0 / 20_000.0)
    return 0.5 + 0.5 * math.cos(math.pi * position)


def biquad_magnitude(kind: str, frequency: float, center: float, gain_db: float, shape: float) -> float:
    amplitude = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * center / RATE
    cosine, sine = math.cos(omega), math.sin(omega)
    if kind == "peak":
        alpha = sine / (2.0 * shape)
        b = (1 + alpha * amplitude, -2 * cosine, 1 - alpha * amplitude)
        a = (1 + alpha / amplitude, -2 * cosine, 1 - alpha / amplitude)
    else:
        alpha = (sine / 2.0) * math.sqrt((amplitude + 1 / amplitude) * (1 / shape - 1) + 2)
        root = math.sqrt(amplitude)
        b = (
            amplitude * ((amplitude + 1) - (amplitude - 1) * cosine + 2 * root * alpha),
            2 * amplitude * ((amplitude - 1) - (amplitude + 1) * cosine),
            amplitude * ((amplitude + 1) - (amplitude - 1) * cosine - 2 * root * alpha),
        )
        a = (
            (amplitude + 1) + (amplitude - 1) * cosine + 2 * root * alpha,
            -2 * ((amplitude - 1) + (amplitude + 1) * cosine),
            (amplitude + 1) + (amplitude - 1) * cosine - 2 * root * alpha,
        )
    z1 = complex(math.cos(-2 * math.pi * frequency / RATE), math.sin(-2 * math.pi * frequency / RATE))
    numerator = b[0] + b[1] * z1 + b[2] * z1 * z1
    denominator = a[0] + a[1] * z1 + a[2] * z1 * z1
    return abs(numerator / denominator)


def bass_modifier_db(frequency: float, preset: str) -> float:
    if preset == "none":
        return 0.0
    sections = [("peak", 96.0, -7.0, 3.0)]
    if preset == "strong":
        sections += [("shelf", 140.0, -9.0, 1.0), ("peak", 63.0, -5.0, 1.1)]
    if preset not in ("primus360", "strong"):
        raise MeasurementError("저음 제어 preset이 잘못되었습니다.")
    magnitude = 1.0
    for kind, center, gain, shape in sections:
        magnitude *= biquad_magnitude(kind, frequency, center, gain, shape)
    return 20.0 * math.log10(max(magnitude, 1e-15))


def linkwitz_riley_4_magnitude(frequency: float, crossover_hz: float, role: str) -> float:
    """Ideal LR4 branch magnitude; LP+HP equals unity when acoustic phase is aligned."""
    if crossover_hz <= 0.0 or role not in ("highpass", "lowpass"):
        raise MeasurementError("디지털 crossover 설정이 잘못되었습니다.")
    ratio = max(0.0, float(frequency)) / float(crossover_hz)
    ratio4 = ratio ** 4
    lowpass = 1.0 / (1.0 + ratio4)
    return ratio4 * lowpass if role == "highpass" else lowpass


def crossover_transfer_db(frequency: float, crossover_hz: int, role: str | None) -> float:
    if role is None:
        return 0.0
    magnitude = linkwitz_riley_4_magnitude(frequency, crossover_hz, role)
    return 20.0 * math.log10(max(magnitude, 1.0e-6))


def minimum_phase_fir(gains_db: list[float], fft: FFTBackend, fft_length: int) -> list[float]:
    log_magnitude = [complex(value * math.log(10.0) / 20.0, 0.0) for value in gains_db]
    cepstrum = fft.irfft(log_magnitude, fft_length)
    minimum_cepstrum = [0.0] * fft_length
    minimum_cepstrum[0] = cepstrum[0]
    for index in range(1, fft_length // 2):
        minimum_cepstrum[index] = 2.0 * cepstrum[index]
    minimum_cepstrum[fft_length // 2] = cepstrum[fft_length // 2]
    log_spectrum = fft.rfft(minimum_cepstrum, fft_length)
    spectrum = [cmath_exp(value) for value in log_spectrum]
    impulse = fft.irfft(spectrum, fft_length)[:TAPS]
    fade_start = int(TAPS * 0.90)
    for index in range(fade_start, TAPS):
        fraction = (index - fade_start) / max(1, TAPS - fade_start - 1)
        impulse[index] *= 0.5 + 0.5 * math.cos(math.pi * fraction)
    # Do not normalize channels independently here. Front HPF and Woofer LPF
    # are a coherent filter bank; per-channel peak normalization changes their
    # relative level and breaks the intended LR4 sum. The completed bank is
    # normalized once, with one common gain, immediately before WAV output.
    return impulse


def unwrap(values: list[float]) -> list[float]:
    result = list(values)
    for index in range(1, len(result)):
        while result[index] - result[index - 1] > math.pi:
            result[index] -= 2.0 * math.pi
        while result[index] - result[index - 1] < -math.pi:
            result[index] += 2.0 * math.pi
    return result


def apply_low_frequency_phase(
    impulse: list[float],
    measure_f: list[float],
    measure_db: list[float],
    measured_phase: list[float] | None,
    cutoff_hz: int,
    fft: FFTBackend,
    maximum_strength: float = 1.0,
) -> tuple[list[float], dict[str, Any]]:
    """Correct measured excess phase only below cutoff and add minimal causality delay."""
    if not measured_phase or len(measured_phase) != len(measure_f):
        raise MeasurementError("중앙 위치 phase 데이터가 없습니다.")
    fft_length = TAPS * 2
    measured_gains = []
    for index in range(fft_length // 2 + 1):
        frequency = max(3.0, index * RATE / fft_length)
        measured_gains.append(interpolate_log(measure_f, measure_db, max(measure_f[0], min(measure_f[-1], frequency))))
    measured_minimum = minimum_phase_fir(measured_gains, fft, fft_length)
    measured_minimum_spectrum = fft.rfft(measured_minimum, fft_length)
    minimum_phase = unwrap([math.atan2(value.imag, value.real) for value in measured_minimum_spectrum])
    base_spectrum = fft.rfft(impulse, fft_length)
    rotations: list[float] = []
    for index, value in enumerate(base_spectrum):
        frequency = index * RATE / fft_length
        if frequency <= 10.0 or frequency >= cutoff_hz:
            weight = 0.0
        elif frequency <= cutoff_hz * 0.70:
            weight = 1.0
        else:
            fraction = (frequency - cutoff_hz * 0.70) / (cutoff_hz * 0.30)
            weight = 0.5 + 0.5 * math.cos(math.pi * fraction)
        if weight:
            # Unwrapped phase is approximately linear with frequency for a
            # delay.  Log-frequency interpolation is appropriate for dB
            # curves, but bends phase slopes and therefore invents delay.
            measured = interpolate_linear(
                measure_f,
                measured_phase,
                max(measure_f[0], min(measure_f[-1], frequency)),
            )
            excess = measured - minimum_phase[index]
            rotations.append(-excess * weight)
        else:
            rotations.append(0.0)

    def render(strength: float) -> tuple[list[float], int, float]:
        if strength <= 0.0:
            return list(impulse), 0, 0.0
        corrected = [
            value * complex(math.cos(rotation * strength), math.sin(rotation * strength))
            for value, rotation in zip(base_spectrum, rotations)
        ]
        circular = fft.irfft(corrected, fft_length)
        negative = circular[fft_length // 2:]
        negative_energy = sum(value * value for value in negative)
        total_energy = sum(value * value for value in circular)
        shift = 0
        if negative_energy > total_energy * 1.0e-10:
            accumulated = 0.0
            for samples_back, sample in enumerate(reversed(negative), start=1):
                accumulated += sample * sample
                if accumulated >= negative_energy * 0.995:
                    shift = samples_back + 8
                    break
        shift = min(MAX_PHASE_SHIFT, shift)
        if shift:
            circular = circular[-shift:] + circular[:-shift]
        result = circular[:TAPS]
        fade_start = int(TAPS * 0.90)
        for sample_index in range(fade_start, TAPS):
            fraction = (sample_index - fade_start) / max(1, TAPS - fade_start - 1)
            result[sample_index] *= 0.5 + 0.5 * math.cos(math.pi * fraction)
        response = fft.rfft(result, fft_length)
        magnitude_errors = []
        for bin_index, (actual, wanted) in enumerate(zip(response, base_spectrum)):
            frequency = bin_index * RATE / fft_length
            if 20.0 <= frequency <= 20_000.0 and abs(wanted) >= 1.0e-8:
                magnitude_errors.append(20.0 * math.log10(max(abs(actual), 1.0e-15) / abs(wanted)))
        offset = statistics.median(magnitude_errors) if magnitude_errors else 0.0
        residual = [abs(value - offset) for value in magnitude_errors]
        return result, shift, max(residual, default=0.0)

    maximum_strength = max(0.0, min(1.0, float(maximum_strength)))
    strength = maximum_strength
    result, shift, magnitude_residual_max = render(strength)
    if magnitude_residual_max > 0.75:
        low, high = 0.0, maximum_strength
        best = (list(impulse), 0, 0.0, 0.0)
        for _ in range(8):
            candidate_strength = (low + high) * 0.5
            candidate, candidate_shift, candidate_residual = render(candidate_strength)
            if candidate_residual <= 0.75:
                low = candidate_strength
                best = (candidate, candidate_shift, candidate_residual, candidate_strength)
            else:
                high = candidate_strength
        result, shift, magnitude_residual_max, strength = best
    disabled_reason = None
    if 0.0 < strength < 0.10:
        result = list(impulse)
        shift = 0
        magnitude_residual_max = 0.0
        strength = 0.0
        disabled_reason = "safe phase strength below 10%; latency cost exceeds useful correction"
    return result, {
        "phase_cutoff_hz": cutoff_hz,
        "causality_shift_samples": shift,
        "causality_shift_ms": round(shift * 1000.0 / RATE, 3),
        "causality_shift_limit_samples": MAX_PHASE_SHIFT,
        "requested_strength": 1.0,
        "strength_limit": round(maximum_strength, 4),
        "applied_strength": round(strength, 4),
        "magnitude_residual_max_db": round(magnitude_residual_max, 4),
        "magnitude_preservation_limit_db": 0.75,
        "disabled_reason": disabled_reason,
        "method": "center-position excess phase with cosine transition; causality/latency-constrained strength; magnitude-preserving projection guard",
    }


def apply_common_lr_low_frequency_phase(
    left_impulse: list[float],
    right_impulse: list[float],
    frequencies: list[float],
    left_db: list[float],
    right_db: list[float],
    left_phase: list[float] | None,
    right_phase: list[float] | None,
    cutoff_hz: int,
    fft: FFTBackend,
) -> tuple[list[float], list[float], dict[str, Any]]:
    """Apply one common low-frequency excess-phase law and delay to stereo L/R."""
    if not left_phase or not right_phase or len(left_phase) != len(frequencies) or len(right_phase) != len(frequencies):
        raise MeasurementError("L/R 공통 phase 계산에 필요한 중앙 위치 데이터가 없습니다.")
    differences = [
        right - left for frequency, left, right in zip(frequencies, left_phase, right_phase)
        if 40.0 <= frequency <= min(200.0, float(cutoff_hz))
    ]
    cycle_offset = round(statistics.median(differences) / (2.0 * math.pi)) if differences else 0
    aligned_right_phase = [value - cycle_offset * 2.0 * math.pi for value in right_phase]
    common_db = [(left + right) * 0.5 for left, right in zip(left_db, right_db)]
    common_phase = [(left + right) * 0.5 for left, right in zip(left_phase, aligned_right_phase)]

    left_candidate, left_details = apply_low_frequency_phase(
        left_impulse, frequencies, common_db, common_phase, cutoff_hz, fft,
    )
    right_candidate, right_details = apply_low_frequency_phase(
        right_impulse, frequencies, common_db, common_phase, cutoff_hz, fft,
    )
    common_strength = min(float(left_details["applied_strength"]), float(right_details["applied_strength"])) * 0.98
    common_strength = max(0.0, min(1.0, common_strength))
    if common_strength < 0.10:
        common_strength = 0.0
        left_candidate, left_details = list(left_impulse), {**left_details, "applied_strength": 0.0, "causality_shift_samples": 0, "magnitude_residual_max_db": 0.0}
        right_candidate, right_details = list(right_impulse), {**right_details, "applied_strength": 0.0, "causality_shift_samples": 0, "magnitude_residual_max_db": 0.0}
    else:
        left_candidate, left_details = apply_low_frequency_phase(
            left_impulse, frequencies, common_db, common_phase, cutoff_hz, fft, maximum_strength=common_strength,
        )
        right_candidate, right_details = apply_low_frequency_phase(
            right_impulse, frequencies, common_db, common_phase, cutoff_hz, fft, maximum_strength=common_strength,
        )
        common_strength = min(float(left_details["applied_strength"]), float(right_details["applied_strength"]))

    intrinsic_left_shift = int(left_details["causality_shift_samples"])
    intrinsic_right_shift = int(right_details["causality_shift_samples"])
    common_shift = max(intrinsic_left_shift, intrinsic_right_shift)

    def align_delay(values: list[float], intrinsic_shift: int) -> list[float]:
        extra = common_shift - intrinsic_shift
        if extra <= 0:
            return values
        return [0.0] * extra + values[:TAPS - extra]

    left_result = align_delay(left_candidate, intrinsic_left_shift)
    right_result = align_delay(right_candidate, intrinsic_right_shift)
    details = {
        "phase_cutoff_hz": cutoff_hz,
        "requested_strength": 1.0,
        "applied_strength": round(common_strength, 4),
        "common_lr_phase": True,
        "common_causality_shift_samples": common_shift,
        "common_causality_shift_ms": round(common_shift * 1000.0 / RATE, 3),
        "intrinsic_shift_samples": {"left": intrinsic_left_shift, "right": intrinsic_right_shift},
        "relative_added_delay_samples": {"left": common_shift - intrinsic_left_shift, "right": common_shift - intrinsic_right_shift},
        "relative_output_delay_samples": 0,
        "magnitude_residual_max_db": {
            "left": left_details.get("magnitude_residual_max_db"),
            "right": right_details.get("magnitude_residual_max_db"),
        },
        "magnitude_preservation_limit_db": 0.75,
        "disabled_reason": "safe common L/R phase strength below 10%; phase correction bypassed" if common_strength == 0.0 else None,
        "method": "common L/R center-position low-frequency excess phase; shared strength and shared final delay; magnitude-preservation guard",
    }
    return left_result, right_result, details


def cmath_exp(value: complex) -> complex:
    amplitude = math.exp(max(-60.0, min(20.0, value.real)))
    return complex(amplitude * math.cos(value.imag), amplitude * math.sin(value.imag))


def write_float_stereo(path: Path, left: list[float], right: list[float]) -> None:
    if len(left) != TAPS or len(right) != TAPS:
        raise MeasurementError("FIR 길이는 정확히 32768이어야 합니다.")
    if not all(math.isfinite(value) for channel in (left, right) for value in channel):
        raise MeasurementError("FIR에 NaN 또는 무한대 샘플이 있어 WAV 생성을 중단했습니다.")
    payload = bytearray()
    for l_value, r_value in zip(left, right):
        payload += struct.pack("<ff", float(l_value), float(r_value))
    fmt = struct.pack("<HHIIHHH", 3, 2, RATE, RATE * 8, 8, 32, 0)
    riff_size = 4 + 8 + len(fmt) + 8 + 4 + 8 + len(payload)
    with path.open("wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        handle.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        handle.write(b"fact" + struct.pack("<II", 4, TAPS))
        handle.write(b"data" + struct.pack("<I", len(payload)) + payload)


def load_average_response(directory: Path, source: str, spatial_mode: str = "equal", positions_total: int = POSITIONS) -> dict[str, Any]:
    if spatial_mode not in ("equal", "center"):
        raise MeasurementError("공간 평균 방식이 잘못되었습니다.")
    responses = []
    if positions_total not in ALLOWED_POSITION_COUNTS:
        raise MeasurementError("응답 평균의 위치 수가 잘못되었습니다.")
    for position in range(1, positions_total + 1):
        path = directory / f"p{position}_{source}_response.json"
        if not path.is_file():
            raise MeasurementError(f"측정 응답이 없습니다: {path.name}")
        responses.append(json.loads(path.read_text(encoding="utf-8")))
    raw_frequencies = responses[0].get("frequencies")
    if not isinstance(raw_frequencies, list) or len(raw_frequencies) < 2:
        raise MeasurementError(f"{source} 응답의 주파수 grid가 없습니다.")
    frequencies = [float(value) for value in raw_frequencies]
    if any(not math.isfinite(value) or value <= 0.0 for value in frequencies) or any(
        right <= left for left, right in zip(frequencies, frequencies[1:])
    ):
        raise MeasurementError(f"{source} 응답의 주파수 grid가 유효하지 않습니다.")
    for position, response in enumerate(responses, start=1):
        candidate_frequencies = response.get("frequencies")
        candidate_db = response.get("db")
        if not isinstance(candidate_frequencies, list) or not isinstance(candidate_db, list):
            raise MeasurementError(f"p{position}_{source} 응답 배열이 없습니다.")
        if len(candidate_frequencies) != len(frequencies) or len(candidate_db) != len(frequencies):
            raise MeasurementError(f"p{position}_{source} 응답 grid 길이가 다른 위치와 일치하지 않습니다.")
        if any(
            abs(float(candidate) - reference) > max(1.0e-6, abs(reference) * 1.0e-7)
            for candidate, reference in zip(candidate_frequencies, frequencies)
        ):
            raise MeasurementError(f"p{position}_{source} 응답 주파수 grid가 다른 위치와 일치하지 않습니다.")
        if any(not math.isfinite(float(value)) for value in candidate_db):
            raise MeasurementError(f"p{position}_{source} 응답에 NaN/Inf가 포함되어 있습니다.")
    # Never smooth an already smoothed legacy response a second time.  New raw
    # WAV reprocessing writes RESPONSE_ALGORITHM_REVISION and uses power-domain
    # smoothing; old response JSON stays usable but is identified in the result
    # so the UI can recommend a silent raw-WAV recalculation.
    smoothed_positions = []
    response_revisions = []
    for response in responses:
        revision = str(response.get("response_algorithm_revision") or "legacy-db-domain-smoothing")
        response_revisions.append(revision)
        # A *_response.json file is a derived response artifact and therefore
        # already smoothed, including the oldest files that predate explicit
        # revision/smoothing metadata.  Never infer that missing metadata means
        # raw data.  Only the explicit raw-WAV reprocess worker may replace it
        # with the current power-domain calculation.
        smoothed_positions.append([float(value) for value in response["db"]])
    averaged = []
    geometric_averaged = []
    spatial_std_db = []
    power_lift_db = []
    aggregate_confidence = []
    for index, frequency in enumerate(frequencies):
        weights = spatial_position_weights(float(frequency), positions_total, spatial_mode)
        noise_confidence = []
        for response in responses:
            values = response.get("frequency_quality", {}).get("confidence")
            confidence = float(values[index]) if isinstance(values, list) and index < len(values) else 1.0
            noise_confidence.append(max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else 0.0)
        aggregate_confidence.append(sum(weight * confidence for weight, confidence in zip(weights, noise_confidence)))
        effective_weights = [weight * max(0.05, confidence) for weight, confidence in zip(weights, noise_confidence)]
        weight_sum = sum(effective_weights)
        position_levels = [float(values[index]) for values in smoothed_positions]
        geometric = sum(weight * level for weight, level in zip(effective_weights, position_levels)) / max(weight_sum, 1.0e-12)
        power = weighted_power_mean_db(position_levels, effective_weights)
        averaged.append(power)
        geometric_averaged.append(geometric)
        spatial_std_db.append(weighted_std_db(position_levels, effective_weights))
        power_lift_db.append(max(0.0, power - geometric))
    decay_centers = []
    decay_rt60 = []
    for center in (63, 125, 250, 500, 1000, 2000, 4000):
        values = []
        for response in responses:
            for band in response.get("room_decay", {}).get("bands", []):
                if band.get("center_hz") == center and band.get("reliable") and isinstance(band.get("t20_rt60_s"), (int, float)):
                    values.append(float(band["t20_rt60_s"]))
        if values:
            decay_centers.append(float(center))
            decay_rt60.append(statistics.median(values))
    return {
        "frequencies": frequencies,
        "average_db": averaged,
        "spatial_std_db": spatial_std_db,
        "frequency_confidence": aggregate_confidence,
        "center_phase_rad": responses[0].get("phase_rad"),
        "center_bulk_delay_samples": responses[0].get("bulk_delay_samples", 0),
        "center_bulk_delay_reliable": bool(responses[0].get(
            "bulk_delay_reliable",
            responses[0].get("bulk_delay", {}).get("reliable", True),
        )),
        "center_bulk_delay": responses[0].get("bulk_delay"),
        "spatial_mode": spatial_mode,
        "position_weights": (
            "single reference position" if positions_total == 1
            else "equal 1/3 each" if spatial_mode == "equal"
            else "frequency-dependent: center 1/3 below 200 Hz to 0.60 above 2 kHz"
        ) + "; multiplied by per-position noise confidence; normalized weighted mean-square response",
        "smoothing": SMOOTHING_NAME if all(revision == RESPONSE_ALGORITHM_REVISION for revision in response_revisions) else "stored response smoothing preserved; spatial integration uses weighted power",
        "spatial_aggregation": {
            "method": "noise-confidence weighted acoustic transfer power mean",
            "formula": "10*log10(sum(w_p*10^(L_p/10))/sum(w_p))",
            "dispersion": "sqrt(sum(w_p*(L_p-mu_db)^2)/sum(w_p))",
            "positions": positions_total,
            "geometric_average_db": geometric_averaged,
            "power_mean_lift_db": power_lift_db,
            "median_power_mean_lift_db": round(statistics.median(power_lift_db), 4),
            "p90_power_mean_lift_db": round(percentile(power_lift_db, 0.90), 4),
            "response_revisions": sorted(set(response_revisions)),
            "legacy_response_count": sum(revision != RESPONSE_ALGORITHM_REVISION for revision in response_revisions),
            "raw_reprocess_recommended": any(revision != RESPONSE_ALGORITHM_REVISION for revision in response_revisions),
        },
        "decay_frequency_hz": decay_centers,
        "decay_t20_rt60_s": decay_rt60,
    }


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    blend = position - lower
    return ordered[lower] * (1.0 - blend) + ordered[upper] * blend


def finalize_graph_with_fir(graph: dict[str, Any], impulse: list[float], fft: FFTBackend) -> dict[str, Any]:
    """Replace design estimates with the response of the actual truncated FIR."""
    spectrum = fft.rfft(impulse, TAPS * 2)
    actual_correction = []
    for frequency in graph["frequency"]:
        bin_value = frequency * (TAPS * 2) / RATE
        lower = min(len(spectrum) - 2, max(0, int(bin_value)))
        blend = bin_value - lower
        magnitude = abs(spectrum[lower]) * (1.0 - blend) + abs(spectrum[lower + 1]) * blend
        actual_correction.append(20.0 * math.log10(max(magnitude, 1.0e-15)))

    requested = [float(value) for value in graph["correction_db"]]
    crossover = graph.get("crossover", {})
    implementation_low = 20.0 if crossover.get("enabled") else max(20.0, float(graph["correction_band_hz"][0]))
    implementation_high = 20_000.0 if crossover.get("enabled") else min(20_000.0, float(graph["correction_band_hz"][1]))
    implementation_offsets = [
        actual - wanted
        for frequency, actual, wanted in zip(graph["frequency"], actual_correction, requested)
        if implementation_low <= frequency <= implementation_high
    ]
    normalization_offset = statistics.median(implementation_offsets)
    implementation_residual = [abs(value - normalization_offset) for value in implementation_offsets]
    raw_predicted = [before + correction for before, correction in zip(graph["before_db"], actual_correction)]
    reference_low, reference_high = graph["reference_band_hz"]
    reference_values = [
        value for frequency, value in zip(graph["frequency"], raw_predicted)
        if reference_low <= frequency <= reference_high
    ]
    predicted_reference = statistics.median(reference_values) if reference_values else 0.0
    predicted = [value - predicted_reference for value in raw_predicted]

    raw_effective = [float(value) for value in graph["effective_target_db"]]
    effective = list(raw_effective)
    effective_reference_values = [
        value for frequency, value in zip(graph["frequency"], effective)
        if reference_low <= frequency <= reference_high
    ]
    effective_reference = statistics.median(effective_reference_values) if effective_reference_values else 0.0
    effective = [value - effective_reference for value in effective]
    low_hz, high_hz = graph["correction_band_hz"]
    natural_low, natural_high = graph["natural_usable_band_hz"]
    fit_low = max(float(low_hz), float(natural_low), 20.0)
    fit_high = min(float(high_hz), float(natural_high), 180.0 if graph["woofer"] else 20_000.0)
    if crossover.get("enabled"):
        crossover_hz = float(crossover["frequency_hz"])
        if crossover.get("role") == "highpass":
            fit_low = max(fit_low, crossover_hz * 2.0)
        else:
            fit_high = min(fit_high, crossover_hz * 0.5)
    errors = [
        abs(after - target)
        for frequency, after, target in zip(graph["frequency"], predicted, effective)
        if fit_low <= frequency <= fit_high
    ]
    mae = sum(errors) / len(errors) if errors else 0.0
    p90 = percentile(errors, 0.90)

    graph["requested_correction_db"] = [round(value, 3) for value in requested]
    graph["actual_correction_db"] = [round(value, 3) for value in actual_correction]
    graph["correction_db"] = graph["actual_correction_db"]
    graph["predicted_db"] = [round(value, 3) for value in predicted]
    graph["effective_target_db"] = [round(value, 3) for value in effective]
    # Keep the shared Front-referenced values as well as the locally normalized
    # plot.  A constant Woofer trim disappears if both curves are independently
    # normalized in the 50-120 Hz band, which previously made the displayed
    # Woofer error impossible to fix by changing trim.
    graph["system_relative_predicted_db"] = [round(value, 3) for value in raw_predicted]
    graph["system_relative_effective_target_db"] = [round(value, 3) for value in raw_effective]
    implementation_mae = sum(implementation_residual) / len(implementation_residual)
    implementation_p95 = percentile(implementation_residual, 0.95)
    graph["fir_implementation"] = {
        "evaluation_band_hz": [round(implementation_low, 1), round(implementation_high, 1)],
        "normalization_offset_db": round(normalization_offset, 3),
        "residual_mae_db": round(implementation_mae, 4),
        "residual_p95_db": round(implementation_p95, 4),
        "limits_db": {
            "mae": FIR_IMPLEMENTATION_MAE_LIMIT_DB,
            "p95": FIR_IMPLEMENTATION_P95_LIMIT_DB,
        },
        "pass": (
            implementation_mae <= FIR_IMPLEMENTATION_MAE_LIMIT_DB
            and implementation_p95 <= FIR_IMPLEMENTATION_P95_LIMIT_DB
        ),
    }
    if graph["woofer"]:
        crossover_hz = float(crossover.get("frequency_hz") or 100.0)
        branch_low = max(40.0, float(graph["correction_band_hz"][0]))
        branch_high = min(
            80.0 if crossover.get("enabled") else 120.0,
            crossover_hz * 0.8 if crossover.get("enabled") else 120.0,
            float(graph["correction_band_hz"][1]),
        )
        signed_errors = [
            after - target
            for frequency, after, target in zip(graph["frequency"], raw_predicted, raw_effective)
            if branch_low <= frequency <= branch_high
        ]
        median_error = statistics.median(signed_errors) if signed_errors else None
        graph["branch_level_diagnostic"] = {
            "evaluation_band_hz": [round(branch_low, 1), round(branch_high, 1)],
            "requested_woofer_trim_db": int(graph.get("woofer_trim_db", 0)),
            "median_error_db": round(median_error, 3) if median_error is not None else None,
            "absolute_median_error_db": round(abs(median_error), 3) if median_error is not None else None,
            "status": (
                "on_target" if median_error is not None and abs(median_error) <= 2.0
                else "too_high" if median_error is not None and median_error > 0.0
                else "too_low" if median_error is not None
                else "insufficient_data"
            ),
            "pass": median_error is not None and abs(median_error) <= 2.0,
            "note": "공통 프런트 기준의 우퍼 유효 저역 분기 레벨입니다. 측정 감쇄는 역컨볼루션으로 제거됩니다. 최종 타깃은 감쇄 전용 합산 상한으로 판정하고, 정밀 구성은 미리 측정한 L+우퍼/R+우퍼로 물리 합산 크기를 추가 검증합니다.",
        }
        graph["target_fit"] = {
            "applicable": False,
            "evaluation_band_hz": None,
            "mae_db": None,
            "p90_abs_error_db": None,
            "pass": None,
            "reason": "독립 우퍼 분기는 전체 L+우퍼/R+우퍼 시스템 타깃의 판정 대상이 아닙니다.",
            "note": "우퍼 단독은 유효 저역 상대레벨만 진단하고, 크로스오버 켜짐/꺼짐 모두 최종 합산 응답으로 타깃을 판정합니다.",
        }
    else:
        graph["target_fit"] = {
            "applicable": True,
            "evaluation_band_hz": [round(fit_low, 1), round(fit_high, 1)],
            "mae_db": round(mae, 3),
            "p90_abs_error_db": round(p90, 3),
            "pass": bool(errors) and mae <= 3.5 and p90 <= 7.0,
            "note": "개별 crossover 통과대역에서 안전한 boost/cut 한계와 자연 roll-off 보호를 적용한 달성도" if crossover.get("enabled") else "안전한 boost/cut 한계와 자연 roll-off 보호를 적용한 뒤의 달성도",
        }
    return graph


def apply_common_graph_reference(
    left_graph: dict[str, Any],
    right_graph: dict[str, Any],
    woofer_graph: dict[str, Any] | None,
) -> dict[str, Any]:
    """Grade and display every branch against one shared L/R/W level origin.

    ``finalize_graph_with_fir`` must be usable while intermediate phase and
    delay stages are still being built, so it records raw system-relative
    curves.  Once the complete bank has received its one common no-preamp
    gain, this function chooses exactly one front reference and applies it to
    L, R and Woofer together.  No channel gets an independent 0 dB reset.
    """
    front_graphs = (left_graph, right_graph)
    predicted_reference_values = [
        float(value)
        for graph in front_graphs
        for frequency, value in zip(graph["frequency"], graph["system_relative_predicted_db"])
        if 500.0 <= float(frequency) <= 2_000.0
    ]
    target_reference_values = [
        float(value)
        for graph in front_graphs
        for frequency, value in zip(graph["frequency"], graph["system_relative_effective_target_db"])
        if 500.0 <= float(frequency) <= 2_000.0
    ]
    if not predicted_reference_values or not target_reference_values:
        raise MeasurementError("L/R/우퍼 공통 0 dB 기준 대역을 계산할 수 없습니다.")
    predicted_reference = statistics.median(predicted_reference_values)
    target_reference = statistics.median(target_reference_values)

    def update(graph: dict[str, Any]) -> None:
        predicted = [
            float(value) - predicted_reference
            for value in graph["system_relative_predicted_db"]
        ]
        effective = [
            float(value) - target_reference
            for value in graph["system_relative_effective_target_db"]
        ]
        graph["predicted_db"] = [round(value, 3) for value in predicted]
        graph["effective_target_db"] = [round(value, 3) for value in effective]
        graph["common_reference"] = {
            "scope": "L/R/Woofer complete bank" if woofer_graph is not None else "L/R complete bank",
            "reference_band_hz": [500, 2_000],
            "predicted_reference_db": round(predicted_reference, 4),
            "target_reference_db": round(target_reference, 4),
            "independent_channel_normalization": False,
        }
        crossover = graph.get("crossover", {})
        if graph.get("woofer"):
            crossover_hz = float(crossover.get("frequency_hz") or 100.0)
            branch_low = max(40.0, float(graph["correction_band_hz"][0]))
            branch_high = min(
                80.0 if crossover.get("enabled") else 120.0,
                crossover_hz * 0.8 if crossover.get("enabled") else 120.0,
                float(graph["correction_band_hz"][1]),
            )
            signed_errors = [
                after - target
                for frequency, after, target in zip(graph["frequency"], predicted, effective)
                if branch_low <= float(frequency) <= branch_high
            ]
            median_error = statistics.median(signed_errors) if signed_errors else None
            graph["branch_level_diagnostic"] = {
                "evaluation_band_hz": [round(branch_low, 1), round(branch_high, 1)],
                "requested_woofer_trim_db": int(graph.get("woofer_trim_db", 0)),
                "median_error_db": round(median_error, 3) if median_error is not None else None,
                "absolute_median_error_db": round(abs(median_error), 3) if median_error is not None else None,
                "status": (
                    "on_target" if median_error is not None and abs(median_error) <= 2.0
                    else "too_high" if median_error is not None and median_error > 0.0
                    else "too_low" if median_error is not None
                    else "insufficient_data"
                ),
                "pass": median_error is not None and abs(median_error) <= 2.0,
                "note": "L/R과 동일한 하나의 500-2000 Hz 0 dB 기준을 유지한 우퍼 유효 저역 상대레벨입니다. 우퍼만 따로 정규화하지 않습니다.",
            }
            graph["target_fit"] = {
                "applicable": False,
                "evaluation_band_hz": None,
                "mae_db": None,
                "p90_abs_error_db": None,
                "pass": None,
                "reason": "독립 우퍼 분기는 전체 L+우퍼/R+우퍼 시스템 타깃의 판정 대상이 아닙니다.",
                "note": "우퍼 상대레벨은 공통 기준으로 진단하고 최종 타깃은 실제 L+우퍼/R+우퍼 합산으로 판정합니다.",
            }
            return

        low_hz, high_hz = graph["correction_band_hz"]
        natural_low, natural_high = graph["natural_usable_band_hz"]
        fit_low = max(float(low_hz), float(natural_low), 20.0)
        # Corroborated full-band roll-off is deliberately eligible to 20 kHz;
        # otherwise keep the natural-band protection in the score as before.
        rolloff = graph.get("stereo_rolloff_confidence") or []
        has_corroborated_edge = any(
            float(frequency) >= float(natural_high) and float(confidence) >= 0.25
            for frequency, confidence in zip(graph["frequency"], rolloff)
        )
        fit_high = min(
            float(high_hz),
            20_000.0 if has_corroborated_edge else float(natural_high),
        )
        if woofer_graph is not None:
            crossover = graph.get("crossover", {})
            crossover_hz = float(crossover.get("frequency_hz") or 100.0)
            # The acoustic target belongs to Front+Woofer.  Grade an
            # individual Front branch only above the shared bass region even
            # when crossover is explicitly OFF; low-frequency success/failure
            # is decided by the actual combined model below.
            fit_low = max(fit_low, crossover_hz * 2.0 if crossover.get("enabled") else 200.0)
        if crossover.get("enabled"):
            fit_low = max(fit_low, float(crossover["frequency_hz"]) * 2.0)
        errors = [
            abs(after - target)
            for frequency, after, target in zip(graph["frequency"], predicted, effective)
            if fit_low <= float(frequency) <= fit_high
        ]
        mae = sum(errors) / len(errors) if errors else 0.0
        p90 = percentile(errors, 0.90)
        graph["target_fit"] = {
            "applicable": True,
            "evaluation_band_hz": [round(fit_low, 1), round(fit_high, 1)],
            "mae_db": round(mae, 3),
            "p90_abs_error_db": round(p90, 3),
            "pass": bool(errors) and mae <= 3.5 and p90 <= 7.0,
            "reference_scope": "one common L/R/Woofer level reference",
            "note": "채널별 재정규화 없이 실제 공통 FIR gain 뒤의 응답을 하나의 L/R/우퍼 기준으로 평가합니다.",
        }

    update(left_graph)
    update(right_graph)
    if woofer_graph is not None:
        update(woofer_graph)
    return {
        "scope": "L/R/Woofer complete bank" if woofer_graph is not None else "L/R complete bank",
        "reference_band_hz": [500, 2_000],
        "predicted_reference_db": round(predicted_reference, 4),
        "target_reference_db": round(target_reference, 4),
        "independent_channel_normalization": False,
    }


def summarize_high_frequency_compensation(
    left_graph: dict[str, Any],
    right_graph: dict[str, Any],
    maximum_relative_compensation_db: float,
    common_attenuation_db: float,
) -> dict[str, Any]:
    """Expose the upper-band tradeoff instead of hiding it in a full graph.

    This is diagnostic evidence, not another normalization or correction pass.
    All values are sampled after the one common L/R/Woofer graph reference has
    been applied, so L and R remain directly comparable.
    """
    channels: dict[str, Any] = {}
    worst_abs_residual = 0.0
    ceiling_reached = False
    for channel, graph in (("left", left_graph), ("right", right_graph)):
        frequencies = list(graph.get("frequency") or ())
        samples: dict[str, Any] = {}
        if frequencies:
            for requested_hz in (10_000, 15_000, 20_000):
                index = min(range(len(frequencies)), key=lambda item: abs(float(frequencies[item]) - requested_hz))
                predicted = float((graph.get("predicted_db") or [])[index])
                target = float((graph.get("target_db") or [])[index])
                requested = float((graph.get("requested_correction_db") or [])[index])
                residual = predicted - target
                if requested_hz >= 15_000:
                    worst_abs_residual = max(worst_abs_residual, abs(residual))
                if maximum_relative_compensation_db > 0 and requested >= maximum_relative_compensation_db - 0.05:
                    ceiling_reached = True
                samples[str(requested_hz)] = {
                    "frequency_hz": round(float(frequencies[index]), 2),
                    "predicted_minus_target_db": round(residual, 3),
                    "requested_correction_db": round(requested, 3),
                    "measurement_confidence": round(float((graph.get("measurement_confidence") or [])[index]), 3),
                }
        channels[channel] = samples
    return {
        "reference": "one common L/R/Woofer 500-2000 Hz reference",
        "maximum_relative_compensation_db": maximum_relative_compensation_db,
        "common_attenuation_db": round(common_attenuation_db, 4),
        "ceiling_reached": ceiling_reached,
        "worst_abs_residual_db_15_20khz": round(worst_abs_residual, 3),
        "channels": channels,
        "interpretation": (
            "The remaining upper-band slope is limited by the selected relative compensation ceiling."
            if ceiling_reached and worst_abs_residual > 1.5 else
            "Upper-band compensation remains inside the selected ceiling."
        ),
    }


def design_channel(measure_f: list[float], measure_db: list[float], spatial_std_db: list[float], measured_phase: list[float] | None, target_name: str, preset: str, *, woofer: bool, woofer_trim_db: int, phase_mode: str, phase_cutoff: int, spatial_mode: str = "equal", bass_tilt_db: int = 0, treble_tilt_db: int = 0, correction_low_hz: int = 20, correction_high_hz: int = 20_000, max_boost_db: int = 10, max_cut_db: int = 18, crossover_role: str | None = None, crossover_frequency_hz: int = 100, decay_frequency_hz: list[float] | None = None, decay_t20_rt60_s: list[float] | None = None, shared_reference_measure_db: float | None = None, shared_reference_target_db: float | None = None, frequency_confidence: list[float] | None = None, corroborated_rolloff_confidence: list[float] | None = None, fft: FFTBackend) -> tuple[list[float], dict[str, Any]]:
    target_f, target_db = target_curve(target_name)
    reference_band = (50, 120) if woofer else (500, 2000)
    target_reference_band = (500, 2000) if shared_reference_target_db is not None else reference_band
    local_reference_measure = statistics.median(value for frequency, value in zip(measure_f, measure_db) if reference_band[0] <= frequency <= reference_band[1])
    local_reference_target = statistics.median(
        interpolate_log(target_f, target_db, frequency) + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
        for frequency in measure_f
        if reference_band[0] <= frequency <= reference_band[1]
    )
    if (shared_reference_measure_db is None) != (shared_reference_target_db is None):
        raise MeasurementError("공통 음압 기준은 측정값과 타겟값을 함께 제공해야 합니다.")
    reference_measure = local_reference_measure if shared_reference_measure_db is None else float(shared_reference_measure_db)
    reference_target = local_reference_target if shared_reference_target_db is None else float(shared_reference_target_db)
    reference_preference = statistics.median(
        preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
        for frequency in measure_f
        if target_reference_band[0] <= frequency <= target_reference_band[1]
    )
    reference_target_without_preference = reference_target - reference_preference
    # Natural driver bandwidth is relative to this branch's own broad level.
    # The target/error still uses the one shared L/R/W system reference below.
    natural_low, natural_high = natural_usable_band(measure_f, measure_db, local_reference_measure)
    fft_length = TAPS * 2
    gains: list[float] = []
    graph_frequency: list[float] = []
    graph_before: list[float] = []
    graph_after: list[float] = []
    graph_variation: list[float] = []
    graph_confidence: list[float] = []
    graph_rolloff_confidence: list[float] = []
    graph_notch_reliability: list[float] = []
    graph_target: list[float] = []
    graph_effective_target: list[float] = []
    graph_correction: list[float] = []
    graph_automatic_room_correction: list[float] = []
    graph_preference_correction: list[float] = []
    graph_crossover: list[float] = []
    graph_decay: list[float | None] = []
    graph_decay_cut: list[float] = []
    woofer_target_cut: list[float] = []
    guarded_boost_bins = 0
    narrow_notch_guarded_bins = 0
    maximum_narrow_notch_boost_db = 0.0
    notch_reliability_values = narrow_notch_reliability(measure_f, measure_db)
    for index in range(fft_length // 2 + 1):
        frequency = index * RATE / fft_length
        safe_frequency = max(3.0, frequency)
        measured = interpolate_log(measure_f, measure_db, max(measure_f[0], min(measure_f[-1], safe_frequency))) - reference_measure
        variation = interpolate_log(measure_f, spatial_std_db, max(measure_f[0], min(measure_f[-1], safe_frequency)))
        noise_confidence = interpolate_log(measure_f, frequency_confidence, max(measure_f[0], min(measure_f[-1], safe_frequency))) if frequency_confidence else 1.0
        noise_confidence = max(0.0, min(1.0, noise_confidence))
        rolloff_confidence = interpolate_log(measure_f, corroborated_rolloff_confidence, max(measure_f[0], min(measure_f[-1], safe_frequency))) if corroborated_rolloff_confidence else 0.0
        rolloff_confidence = max(0.0, min(1.0, rolloff_confidence))
        notch_reliability = interpolate_log(measure_f, notch_reliability_values, max(measure_f[0], min(measure_f[-1], safe_frequency)))
        notch_reliability = max(0.0, min(1.0, notch_reliability))
        target_without_preference = interpolate_log(target_f, target_db, max(target_f[0], min(target_f[-1], safe_frequency))) - reference_target_without_preference
        window = correction_window(safe_frequency, correction_low_hz, correction_high_hz)
        # Keep explicit user tone controls outside the automatic room-EQ
        # limiter.  Otherwise a large woofer excess that saturates max_cut_db
        # silently erases every bass-tilt choice.  The preference is still
        # smoothly windowed, reference-normalized and included in the same
        # peak-normalized FIR; it cannot create digital clipping.
        preference_correction = (
            preference_modifier_db(safe_frequency, bass_tilt_db, treble_tilt_db) - reference_preference
        ) * tone_preference_window(safe_frequency)
        target_value = target_without_preference + preference_correction
        if woofer:
            correction = max(-float(max_cut_db), min(0.0, target_without_preference - measured)) * window * noise_confidence if 20.0 <= frequency <= 180.0 else 0.0
            if 40.0 <= frequency <= 120.0:
                woofer_target_cut.append(correction)
        else:
            unweighted_correction = (target_without_preference - measured) * window
            raw_correction = unweighted_correction * noise_confidence
            spatial_reliability = 1.0 / (1.0 + (variation / 3.0) ** 2)
            if raw_correction > 0.0:
                # Deep, position-dependent nulls are not safely invertible. This is the
                # spatial regularization term: a 3 dB position spread halves the boost.
                # A second, local-shape term prevents one narrow dip from forcing
                # the complete no-preamp bank downward. Broad L/R-correlated
                # roll-off can retain authority even near a natural band edge.
                boost_limit = float(max_boost_db)
                corroborated_broad = rolloff_confidence >= 0.25 and notch_reliability >= 0.6
                if boost_limit and corroborated_broad:
                    # A broad L/R-corroborated roll-off already passed noise,
                    # channel-agreement and local-shape guards.  Do not apply
                    # the same low edge-SNR confidence again as an amplitude
                    # multiplier: doing so leaves a visible 10-20 kHz slope
                    # even when the user selected a 10 dB relative ceiling.
                    # Spatial and notch regularization remain active.
                    raw_correction = unweighted_correction * spatial_reliability * notch_reliability
                    correction = min(boost_limit, raw_correction)
                else:
                    raw_correction *= spatial_reliability * notch_reliability
                    correction = boost_limit * math.tanh(raw_correction / max(boost_limit, 1.0e-9)) if boost_limit else 0.0
                if notch_reliability < 0.5:
                    correction = min(correction, 3.0)
                    narrow_notch_guarded_bins += 1
                    maximum_narrow_notch_boost_db = max(maximum_narrow_notch_boost_db, correction)
                corroborated_edge = corroborated_broad
                if (frequency < natural_low or frequency > natural_high) and not corroborated_edge:
                    correction = 0.0
                    guarded_boost_bins += 1
            else:
                # Above the room-dominated bass region, small head movements can
                # turn narrow peaks into dips. Apply spatial confidence to cuts as
                # well as boosts and never carve deep high-frequency notches from
                # an in-room microphone response.
                if frequency >= 500.0:
                    raw_correction *= spatial_reliability
                if frequency >= 2_000.0:
                    cut_limit = min(float(max_cut_db), 3.0)
                elif frequency >= 500.0:
                    cut_limit = min(float(max_cut_db), 6.0)
                else:
                    cut_limit = float(max_cut_db)
                correction = max(-cut_limit, raw_correction)
            audible_guard_high = 22_000.0 if correction_high_hz >= 20_000 else 20_000.0
            if not 20.0 <= frequency <= audible_guard_high:
                correction = 0.0
        modifier = bass_modifier_db(max(1.0, frequency), preset)
        decay_value = None
        decay_cut = 0.0
        if decay_frequency_hz and decay_t20_rt60_s and frequency <= 300.0:
            decay_value = interpolate_log(decay_frequency_hz, decay_t20_rt60_s, max(decay_frequency_hz[0], min(decay_frequency_hz[-1], safe_frequency)))
            preferred_decay = 0.55 if frequency <= 63.0 else (0.35 if frequency >= 250.0 else 0.55 - 0.20 * math.log(frequency / 63.0) / math.log(250.0 / 63.0))
            if correction < 0.0 and decay_value > preferred_decay:
                decay_cut = min(3.0, (decay_value - preferred_decay) * 5.0) * window
                correction -= decay_cut
        automatic_room_correction = correction
        correction += preference_correction
        if woofer or frequency <= 350.0:
            correction += modifier
        if woofer:
            correction += woofer_trim_db
            correction = min(0.0, correction)
        # This is a total relative compensation ceiling, not permission to
        # fill every measured dip.  Because the completed L/R/W bank is later
        # attenuated by one common amount, this also bounds how far a positive
        # design peak can lower the rest of the system in no-preamp mode.
        correction = min(float(max_boost_db), correction)
        crossover_db = crossover_transfer_db(safe_frequency, crossover_frequency_hz, crossover_role)
        correction += crossover_db
        gains.append(correction)
        if index > 0 and (not graph_frequency or frequency / graph_frequency[-1] >= 1.025) and frequency <= 20_000:
            graph_frequency.append(round(frequency, 2))
            graph_before.append(round(measured, 3))
            graph_after.append(round(measured + correction, 3))
            graph_variation.append(round(variation, 3))
            graph_confidence.append(round(noise_confidence, 4))
            graph_rolloff_confidence.append(round(rolloff_confidence, 4))
            graph_notch_reliability.append(round(notch_reliability, 4))
            graph_target.append(round(target_value, 3))
            graph_effective_target.append(round(target_value + (modifier if woofer or frequency <= 350.0 else 0.0) - decay_cut + (woofer_trim_db if woofer else 0), 3))
            graph_decay.append(round(decay_value, 3) if decay_value is not None else None)
            graph_decay_cut.append(round(-decay_cut, 3))
            graph_automatic_room_correction.append(round(automatic_room_correction, 3))
            graph_preference_correction.append(round(preference_correction, 3))
            graph_crossover.append(round(crossover_db, 3))
            graph_correction.append(round(correction, 3))
    impulse = minimum_phase_fir(gains, fft, fft_length)
    phase_details = {"method": "minimum phase magnitude only", "causality_shift_samples": 0, "causality_shift_ms": 0.0}
    if phase_mode == "bass":
        impulse, phase_details = apply_low_frequency_phase(impulse, measure_f, measure_db, measured_phase, phase_cutoff, fft)
    graph = {
        "frequency": graph_frequency,
        "before_db": graph_before,
        "predicted_db": graph_after,
        "spatial_std_db": graph_variation,
        "measurement_confidence": graph_confidence,
        "stereo_rolloff_confidence": graph_rolloff_confidence,
        "narrow_notch_reliability": graph_notch_reliability,
        "target_db": graph_target,
        "effective_target_db": graph_effective_target,
        "automatic_room_correction_db": graph_automatic_room_correction,
        "preference_correction_db": graph_preference_correction,
        "crossover_transfer_db": graph_crossover,
        "correction_db": graph_correction,
        "decay_t20_rt60_s": graph_decay,
        "decay_control_db": graph_decay_cut,
        "phase": phase_details,
        "regularization": "noise-confidence weighted mean-square spatial prototype; power-domain variable perceptual smoothing; weighted spatial variance plus narrow-notch soft boost guard; L/R-corroborated broad roll-off confidence; explicit broad tone preference independent of the automatic room-EQ band",
        "reference_band_hz": list(reference_band),
        "level_reference": "shared Front L/R 500-2000 Hz" if shared_reference_measure_db is not None else f"local {reference_band[0]}-{reference_band[1]} Hz",
        "reference_measure_db": round(reference_measure, 3),
        "reference_target_db": round(reference_target, 3),
        "automatic_target_cut_median_db_40_120": round(statistics.median(woofer_target_cut), 3) if woofer_target_cut else None,
        "positive_woofer_gain_allowed": False if woofer else None,
        "spatial_mode": spatial_mode,
        "natural_usable_band_hz": [round(natural_low, 1), round(natural_high, 1)],
        "correction_band_hz": [correction_low_hz, correction_high_hz],
        "guarded_boost_bins": guarded_boost_bins,
        "narrow_notch_guarded_bins": narrow_notch_guarded_bins,
        "maximum_narrow_notch_boost_db": round(maximum_narrow_notch_boost_db, 3),
        "max_room_boost_db": max_boost_db,
        "max_relative_compensation_db": max_boost_db,
        "max_room_cut_db": max_cut_db,
        "preference": {"bass_db_at_20_hz": bass_tilt_db, "treble_db_at_20_khz": treble_tilt_db},
        "woofer": woofer,
        "woofer_trim_db": woofer_trim_db if woofer else 0,
        "crossover": {
            "enabled": crossover_role is not None,
            "role": crossover_role,
            "frequency_hz": crossover_frequency_hz if crossover_role is not None else None,
            "alignment_requirement": "LR4 branches sum flat only after acoustic polarity, phase and arrival alignment",
        },
        "decay_control": "reliable T20 bass decay; cut-only, maximum 3 dB; late reverb is not inverted",
    }
    return impulse, finalize_graph_with_fir(graph, impulse, fft)


def fir_metrics(channels: list[list[float]], fft: FFTBackend) -> dict[str, Any]:
    metrics = []
    for values in channels:
        absolute = [abs(value) for value in values]
        energy = [value * value for value in values]
        total = sum(energy)
        cumulative = 0.0
        energy50 = 0
        for index, value in enumerate(energy):
            cumulative += value
            if cumulative >= total * 0.5:
                energy50 = index
                break
        peak_tap = max(range(len(values)), key=absolute.__getitem__)
        response = fft.rfft(values, TAPS * 2)
        maximum = max(abs(value) for value in response)
        metrics.append({
            "taps": len(values),
            "peak_tap": peak_tap,
            "peak_delay_ms": round(peak_tap * 1000.0 / RATE, 3),
            "energy50_tap": energy50,
            "maximum_transfer_db": round(20.0 * math.log10(max(maximum, 1e-15)), 3),
            "finite": all(math.isfinite(value) for value in values),
            "transfer_pass": maximum <= 1.0001,
            "early_impulse_pass": peak_tap <= MAX_PHASE_SHIFT + 960 + 32,
        })
    return {"left": metrics[0], "right": metrics[1]}


def fir_energy_delay(values: list[float]) -> int:
    """Return the first tap containing half of a FIR's total energy."""
    total = sum(value * value for value in values)
    if total <= 0.0:
        return 0
    accumulated = 0.0
    for index, value in enumerate(values):
        accumulated += value * value
        if accumulated >= total * 0.5:
            return index
    return max(0, len(values) - 1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def delay_fir(values: list[float], samples: int) -> list[float]:
    if samples <= 0:
        return values
    samples = min(samples, TAPS - 1)
    return [0.0] * samples + values[:TAPS - samples]


def fir_value(spectrum: list[complex], frequency: float) -> complex:
    bin_value = max(0.0, min(float(len(spectrum) - 1), frequency * (TAPS * 2) / RATE))
    lower = min(len(spectrum) - 2, max(0, int(bin_value)))
    blend = bin_value - lower
    return spectrum[lower] * (1.0 - blend) + spectrum[lower + 1] * blend


def response_complex(response: dict[str, Any], frequency: float) -> complex:
    magnitude_db = interpolate_log(response["frequencies"], response["db"], frequency)
    phase = interpolate_linear(
        [float(value) for value in response["frequencies"]],
        [float(value) for value in response.get("phase_rad", [0.0] * len(response["frequencies"]))],
        frequency,
    )
    phase -= 2.0 * math.pi * frequency * float(response.get("bulk_delay_samples", 0.0)) / RATE
    magnitude = 10.0 ** (magnitude_db / 20.0)
    return magnitude * complex(math.cos(phase), math.sin(phase))


def load_phase_reference_results(directory: Path, positions_total: int) -> list[dict[str, Any]]:
    results = []
    for position in range(1, positions_total + 1):
        path = directory / f"p{position}_phase_reference.json"
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(value, dict) or not value.get("reliable"):
            return []
        if not all(source in value.get("sources", {}) for source in ("left", "right", "woofer")):
            return []
        results.append(value)
    return results


def phase_referenced_response_complex(
    response: dict[str, Any],
    phase_reference: dict[str, Any] | None,
    source: str,
    frequency: float,
) -> complex:
    """Use ESS magnitude with same-recording phase; never renormalize a branch."""
    if phase_reference is None:
        return response_complex(response, frequency)
    source_phase = phase_reference.get("sources", {}).get(source, {})
    frequencies = source_phase.get("frequencies")
    phases = source_phase.get("phase_rad")
    if not isinstance(frequencies, list) or not isinstance(phases, list) or len(frequencies) != len(phases) or len(frequencies) < 2:
        return response_complex(response, frequency)
    if not float(frequencies[0]) <= frequency <= float(frequencies[-1]):
        return response_complex(response, frequency)
    magnitude_db = interpolate_log(response["frequencies"], response["db"], frequency)
    phase = interpolate_linear([float(value) for value in frequencies], [float(value) for value in phases], frequency)
    magnitude = 10.0 ** (magnitude_db / 20.0)
    return magnitude * complex(math.cos(phase), math.sin(phase))


def phase_reference_coverage(results: list[dict[str, Any]], sources: tuple[str, ...]) -> tuple[float, float] | None:
    if not results:
        return None
    lows, highs = [], []
    for result in results:
        for source in sources:
            frequencies = result.get("sources", {}).get(source, {}).get("frequencies", [])
            if not isinstance(frequencies, list) or len(frequencies) < 2:
                return None
            lows.append(float(frequencies[0]))
            highs.append(float(frequencies[-1]))
    low, high = max(lows), min(highs)
    return (low, high) if low < high else None


def closure_constrained_acoustic_pair(
    front_response: dict[str, Any],
    woofer_response: dict[str, Any],
    combined_response: dict[str, Any] | None,
    phase_reference: dict[str, Any] | None,
    side: str,
    frequency: float,
    woofer_measurement_scale: float,
) -> tuple[complex, complex, dict[str, Any]]:
    """Fuse all independent and summed captures without double-counting.

    The individual ESS responses provide |F| and |W|.  The simultaneous
    L/R/W multisine supplies the sign and unwrap of the relative phase.  The
    separately measured |F + aW| supplies the dense acoustic cross term

        cos(delta) = (|S|^2 - |F|^2 - |aW|^2) / (2 |F| |aW|).

    Because a magnitude-only sum has a +/- phase ambiguity, it is never used
    alone.  It softly constrains the same-recording phase according to branch
    balance and capture SNR.  This makes L+W/R+W genuine FIR-design evidence
    while avoiding normalization, response averaging, or counting W twice.
    """
    front = phase_referenced_response_complex(front_response, phase_reference, side, frequency)
    woofer = phase_referenced_response_complex(woofer_response, phase_reference, "woofer", frequency)
    details: dict[str, Any] = {
        "used": False,
        "reason": "physical sum or simultaneous phase reference unavailable",
    }
    if combined_response is None or phase_reference is None:
        return front, woofer, details

    side_phase = phase_reference.get("sources", {}).get(side, {})
    woofer_phase = phase_reference.get("sources", {}).get("woofer", {})
    side_frequencies = side_phase.get("frequencies", [])
    woofer_frequencies = woofer_phase.get("frequencies", [])
    if (
        len(side_frequencies) < 2
        or len(woofer_frequencies) < 2
        or not max(float(side_frequencies[0]), float(woofer_frequencies[0])) <= frequency
        <= min(float(side_frequencies[-1]), float(woofer_frequencies[-1]))
    ):
        details["reason"] = "frequency outside simultaneous phase coverage"
        return front, woofer, details

    front_magnitude = abs(front)
    scaled_woofer_magnitude = abs(woofer) * float(woofer_measurement_scale)
    larger = max(front_magnitude, scaled_woofer_magnitude)
    smaller = min(front_magnitude, scaled_woofer_magnitude)
    branch_balance = smaller / max(larger, 1.0e-15)
    denominator = 2.0 * front_magnitude * scaled_woofer_magnitude
    if branch_balance < 0.08 or denominator <= 1.0e-24:
        details.update({"reason": "one branch dominates the sum", "branch_balance": round(branch_balance, 5)})
        return front, woofer, details

    measured_sum_db = interpolate_log(
        [float(value) for value in combined_response["frequencies"]],
        [float(value) for value in combined_response["db"]],
        frequency,
    )
    measured_sum_magnitude = 10.0 ** (measured_sum_db / 20.0)
    observed_cosine_raw = (
        measured_sum_magnitude * measured_sum_magnitude
        - front_magnitude * front_magnitude
        - scaled_woofer_magnitude * scaled_woofer_magnitude
    ) / denominator
    # A value far outside [-1, 1] proves that the independently captured
    # levels/routing are inconsistent.  The pre-build closure gate reports
    # that failure; do not force an impossible phase into the FIR model.
    if observed_cosine_raw < -1.35 or observed_cosine_raw > 1.35:
        details.update({
            "reason": "physical sum cross term is inconsistent",
            "observed_cosine_raw": round(observed_cosine_raw, 5),
            "branch_balance": round(branch_balance, 5),
        })
        return front, woofer, details

    observed_cosine = max(-1.0, min(1.0, observed_cosine_raw))
    reference_delta = math.atan2(woofer.imag, woofer.real) - math.atan2(front.imag, front.real)
    reference_delta = math.atan2(math.sin(reference_delta), math.cos(reference_delta))
    reference_cosine = math.cos(reference_delta)
    capture_snr = combined_response.get("measurement_quality", {}).get("snr_db", MINIMUM_USABLE_SNR_DB)
    snr_confidence = max(0.20, min(1.0, (float(capture_snr) - MINIMUM_USABLE_SNR_DB) / max(1.0, RECOMMENDED_SNR_DB - MINIMUM_USABLE_SNR_DB)))
    observability = max(0.0, min(1.0, (branch_balance - 0.08) / 0.32))
    physical_weight = 0.80 * snr_confidence * observability
    fused_cosine = max(-1.0, min(1.0, (1.0 - physical_weight) * reference_cosine + physical_weight * observed_cosine))
    principal = math.acos(fused_cosine)
    candidates = [
        sign * principal + turns * 2.0 * math.pi
        for sign in (-1.0, 1.0)
        for turns in (-1, 0, 1)
    ]
    fused_delta = min(candidates, key=lambda value: abs(value - reference_delta))
    front_phase_value = math.atan2(front.imag, front.real)
    woofer_magnitude = abs(woofer)
    constrained_woofer = woofer_magnitude * complex(
        math.cos(front_phase_value + fused_delta),
        math.sin(front_phase_value + fused_delta),
    )
    details.update({
        "used": physical_weight > 0.0,
        "reason": None,
        "physical_weight": round(physical_weight, 5),
        "branch_balance": round(branch_balance, 5),
        "capture_snr_db": round(float(capture_snr), 3),
        "observed_cosine_raw": round(observed_cosine_raw, 5),
        "observed_cosine": round(observed_cosine, 5),
        "reference_relative_phase_deg": round(math.degrees(reference_delta), 3),
        "fused_relative_phase_deg": round(math.degrees(fused_delta), 3),
    })
    return front, constrained_woofer, details


def multiply_firs(first: list[float], second: list[float], fft: FFTBackend) -> list[float]:
    fft_length = TAPS * 2
    first_spectrum = fft.rfft(first, fft_length)
    second_spectrum = fft.rfft(second, fft_length)
    circular = fft.irfft([left * right for left, right in zip(first_spectrum, second_spectrum)], fft_length)
    result = circular[:TAPS]
    fade_start = int(TAPS * 0.90)
    for index in range(fade_start, TAPS):
        fraction = (index - fade_start) / max(1, TAPS - fade_start - 1)
        result[index] *= 0.5 + 0.5 * math.cos(math.pi * fraction)
    return result


def normalize_fir_bank(
    channels: list[list[float]],
    fft: FFTBackend,
) -> tuple[list[list[float]], dict[str, Any]]:
    """Apply one common no-preamp gain while preserving branch relationships."""
    if not channels or any(len(values) != TAPS for values in channels):
        raise MeasurementError("FIR bank normalization requires complete 32768-tap channels.")
    fft_length = TAPS * 2
    channel_peaks = [max(abs(value) for value in fft.rfft(channel, fft_length)) for channel in channels]
    peak = max(channel_peaks)
    scale = 1.0 / peak if peak > 1.0 else 1.0
    normalized = [[sample * scale for sample in channel] for channel in channels]
    normalized_peaks = [value * scale for value in channel_peaks]
    before_db = [20.0 * math.log10(max(value, 1.0e-15)) for value in channel_peaks]
    after_db = [20.0 * math.log10(max(value, 1.0e-15)) for value in normalized_peaks]
    relative_delta_errors = [
        abs((after_db[left] - after_db[right]) - (before_db[left] - before_db[right]))
        for left in range(len(channels))
        for right in range(left + 1, len(channels))
    ]
    gain_db = 20.0 * math.log10(max(scale, 1.0e-15))
    return normalized, {
        "method": "one common gain across the complete FIR bank",
        "reason": "preserve Front/Woofer and L/R relative levels while enforcing NoPreamp transfer <= 0 dB",
        "scope": "complete_l_r_woofer_bank" if len(channels) == 4 else "complete_l_r_bank",
        "zero_db_reference": "single_common_bank_peak",
        "independent_channel_normalization": False,
        "channels": len(channels),
        "channel_peak_transfer_before_db": [round(value, 4) for value in before_db],
        "channel_peak_transfer_after_db": [round(value, 4) for value in after_db],
        "peak_transfer_before_db": round(20.0 * math.log10(max(peak, 1.0e-15)), 4),
        "applied_common_gain_db": round(gain_db, 4),
        "peak_transfer_after_db": round(20.0 * math.log10(max(peak * scale, 1.0e-15)), 4),
        "relative_branch_gain_preserved": True,
        "maximum_relative_level_error_db": round(max(relative_delta_errors, default=0.0), 9),
    }


def apply_joint_crossover_guard(
    directory: Path,
    left_ir: list[float],
    right_ir: list[float],
    rear_channels: list[list[float]],
    *,
    target_name: str,
    preset: str,
    bass_tilt_db: int,
    treble_tilt_db: int,
    crossover_enabled: bool,
    crossover_frequency_hz: int,
    max_cut_db: int,
    shared_front_reference_db: float,
    shared_target_reference_db: float,
    woofer_measurement_attenuation_db: float,
    physical_sum_constraints_enabled: bool,
    time_alignment_reliable: bool,
    optimize_relative_phase: bool,
    positions_total: int,
    spatial_mode: str,
    fft: FFTBackend,
) -> tuple[list[float], list[float], list[list[float]], dict[str, Any]]:
    """Cut-only common branch EQ so separately designed branches cannot over-sum.

    This guard remains active when the LR4 crossover is OFF.  OFF means full-
    range overlap, not permission to grade Front and Woofer independently and
    ignore their acoustic sum.
    """
    positions = []
    for position in range(1, positions_total + 1):
        row = {}
        for source in ("left", "right", "woofer"):
            row[source] = json.loads((directory / f"p{position}_{source}_response.json").read_text(encoding="utf-8"))
        for source in ("left_woofer", "right_woofer"):
            path = directory / f"p{position}_{source}_response.json"
            if path.is_file():
                row[source] = json.loads(path.read_text(encoding="utf-8"))
        positions.append(row)

    def response_confidence_at(response: dict[str, Any], frequency: float) -> float:
        quality = response.get("frequency_quality", {})
        frequencies = quality.get("frequencies")
        confidence = quality.get("confidence")
        if not isinstance(frequencies, list) or not isinstance(confidence, list) or len(frequencies) != len(confidence) or not frequencies:
            return 1.0
        return max(0.0, min(1.0, interpolate_log(
            [float(value) for value in frequencies],
            [float(value) for value in confidence],
            frequency,
        )))

    def combined_position_weights(side: str, frequency: float) -> list[float]:
        geometric = spatial_position_weights(frequency, positions_total, spatial_mode)
        weighted = []
        for index, position in enumerate(positions):
            confidences = [
                response_confidence_at(position[side], frequency),
                response_confidence_at(position["woofer"], frequency),
            ]
            combined = position.get(f"{side}_woofer")
            if combined is not None:
                confidences.append(response_confidence_at(combined, frequency))
            weighted.append(geometric[index] * min(confidences))
        total = sum(weighted)
        if total <= 1.0e-9:
            return geometric
        return [value / total for value in weighted]
    phase_references = load_phase_reference_results(directory, positions_total)
    simultaneous_phase_coverage = phase_reference_coverage(
        phase_references, ("left", "right", "woofer")
    )
    simultaneous_phase_reliable = bool(
        len(phase_references) == positions_total and simultaneous_phase_coverage
    )

    def phase_available(frequency: float) -> bool:
        if simultaneous_phase_reliable and simultaneous_phase_coverage is not None:
            return simultaneous_phase_coverage[0] <= frequency <= simultaneous_phase_coverage[1]
        return bool(time_alignment_reliable)

    physical_sum_constraints_available = bool(
        physical_sum_constraints_enabled
        and simultaneous_phase_reliable
        and all(
            source in position
            for position in positions
            for source in ("left_woofer", "right_woofer")
        )
    )
    woofer_measurement_scale = 10.0 ** (float(woofer_measurement_attenuation_db) / 20.0)
    acoustic_pair_cache: dict[tuple[int, str, float], tuple[complex, complex, dict[str, Any]]] = {}
    sum_constraint_weights: list[float] = []
    sum_constraint_phase_adjustments: list[float] = []
    sum_constraint_rejections: dict[str, int] = {}

    def acoustic_pair(position_index: int, side: str, frequency: float) -> tuple[complex, complex]:
        key = (position_index, side, float(frequency))
        cached = acoustic_pair_cache.get(key)
        if cached is None:
            reference = phase_references[position_index] if simultaneous_phase_reliable else None
            combined = (
                positions[position_index].get(f"{side}_woofer")
                if physical_sum_constraints_available else None
            )
            front, woofer, details = closure_constrained_acoustic_pair(
                positions[position_index][side],
                positions[position_index]["woofer"],
                combined,
                reference,
                side,
                frequency,
                woofer_measurement_scale,
            )
            acoustic_pair_cache[key] = (front, woofer, details)
            if details.get("used"):
                sum_constraint_weights.append(float(details.get("physical_weight", 0.0)))
                sum_constraint_phase_adjustments.append(abs(
                    float(details.get("fused_relative_phase_deg", 0.0))
                    - float(details.get("reference_relative_phase_deg", 0.0))
                ))
            elif physical_sum_constraints_available:
                reason = str(details.get("reason") or "not used")
                sum_constraint_rejections[reason] = sum_constraint_rejections.get(reason, 0) + 1
            cached = acoustic_pair_cache[key]
        return cached[0], cached[1]
    frequency_grid = [float(value) for value in positions[0]["left"]["frequencies"]]
    target_f, target_db = target_curve(target_name)
    fft_length = TAPS * 2
    phase_optimization: dict[str, Any] = {
        "requested": bool(optimize_relative_phase),
        "evaluated": False,
        "reliable": False,
        "enabled": False,
        "polarity": 1,
        "relative_delay_samples": 0,
        "reason": "Phase 방식이 '음량만'입니다." if not optimize_relative_phase else None,
    }
    if optimize_relative_phase and time_alignment_reliable:
        initial_front_spectra = [fft.rfft(left_ir, fft_length), fft.rfft(right_ir, fft_length)]
        initial_rear_spectra = [fft.rfft(rear_channels[0], fft_length), fft.rfft(rear_channels[1], fft_length)]
        phase_low = max(30.0, crossover_frequency_hz * 0.55)
        phase_high = min(300.0, crossover_frequency_hz * 1.80)
        if simultaneous_phase_coverage is not None:
            phase_low = max(phase_low, simultaneous_phase_coverage[0])
            phase_high = min(phase_high, simultaneous_phase_coverage[1])
        phase_frequencies: list[float] = []
        for frequency in frequency_grid:
            if phase_low <= frequency <= phase_high and (
                not phase_frequencies or frequency / phase_frequencies[-1] >= 1.08
            ):
                phase_frequencies.append(frequency)

        def alignment_score(relative_delay_samples: int, polarity: int) -> tuple[float, float, float, float, float]:
            errors: list[float] = []
            cancellation_deficits: list[float] = []
            for channel, source in enumerate(("left", "right")):
                for frequency in phase_frequencies:
                    target_absolute = (
                        interpolate_log(target_f, target_db, frequency)
                        + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
                        + (bass_modifier_db(frequency, preset) if frequency <= 350.0 else 0.0)
                        - shared_target_reference_db
                        + shared_front_reference_db
                    )
                    front_delay = max(0, -relative_delay_samples)
                    rear_delay = max(0, relative_delay_samples)
                    front_rotation = complex(
                        math.cos(-2.0 * math.pi * frequency * front_delay / RATE),
                        math.sin(-2.0 * math.pi * frequency * front_delay / RATE),
                    )
                    rear_rotation = polarity * complex(
                        math.cos(-2.0 * math.pi * frequency * rear_delay / RATE),
                        math.sin(-2.0 * math.pi * frequency * rear_delay / RATE),
                    )
                    position_errors = []
                    position_cancellation = []
                    for position_index, position in enumerate(positions):
                        front_acoustic, woofer_acoustic = acoustic_pair(position_index, source, frequency)
                        main = (
                            front_acoustic
                            * fir_value(initial_front_spectra[channel], frequency)
                            * front_rotation
                        )
                        woofer = (
                            woofer_acoustic
                            * fir_value(initial_rear_spectra[channel], frequency)
                            * rear_rotation
                        )
                        level = 20.0 * math.log10(max(abs(main + woofer), 1.0e-15))
                        energy_level = 10.0 * math.log10(max(abs(main) ** 2 + abs(woofer) ** 2, 1.0e-30))
                        position_errors.append(abs(level - target_absolute))
                        position_cancellation.append(max(0.0, energy_level - level))
                    weights = combined_position_weights(source, frequency)
                    errors.append(sum(weight * error for weight, error in zip(weights, position_errors)))
                    cancellation_deficits.append(sum(
                        weight * deficit for weight, deficit in zip(weights, position_cancellation)
                    ))
            mae = sum(errors) / len(errors) if errors else float("inf")
            p90 = percentile(errors, 0.90) if errors else float("inf")
            cancellation_mae = (
                sum(cancellation_deficits) / len(cancellation_deficits)
                if cancellation_deficits else float("inf")
            )
            cancellation_p90 = percentile(cancellation_deficits, 0.90) if cancellation_deficits else float("inf")
            if not errors:
                return 0.0, 0.0, 0.0, 0.0, 0.0
            # Explicitly penalize a complex sum below the phase-agnostic energy
            # sum. Otherwise an anti-phase notch can look like successful EQ
            # when an over-loud Woofer happens to cancel toward the target.
            score = mae + 0.45 * p90 + 0.75 * cancellation_p90 + abs(relative_delay_samples) * 1.0e-6
            return score, mae, p90, cancellation_mae, cancellation_p90

        if len(phase_frequencies) < 3:
            time_alignment_reliable = False
            phase_optimization["reason"] = "동시 위상 기준의 crossover 공통 tone이 3개 미만이라 상대 지연을 적용하지 않았습니다."
        search_limit = min(MAX_PHASE_SHIFT, round(RATE * 0.015))
        search_center = 0
        search_radius = search_limit
        reference_delay_estimates = []
        reference_fit_residuals = []
        if simultaneous_phase_reliable:
            for reference in phase_references:
                for pair_key in ("left_woofer", "right_woofer"):
                    pair = reference.get("pairs", {}).get(pair_key, {})
                    delay = pair.get("second_minus_first_delay_samples")
                    residual = pair.get("delay_fit_residual_p90_deg")
                    if isinstance(delay, (int, float)) and isinstance(residual, (int, float)):
                        reference_delay_estimates.append(float(delay))
                        reference_fit_residuals.append(float(residual))
            if reference_delay_estimates and percentile(reference_fit_residuals, 0.90) <= 60.0:
                front_filter_delay = statistics.median((fir_energy_delay(left_ir), fir_energy_delay(right_ir)))
                rear_filter_delay = statistics.median((fir_energy_delay(rear_channels[0]), fir_energy_delay(rear_channels[1])))
                total_woofer_minus_front = statistics.median(reference_delay_estimates) + rear_filter_delay - front_filter_delay
                search_center = max(-search_limit, min(search_limit, round(-total_woofer_minus_front)))
                search_radius = min(search_limit, 240)
        coarse_step = 4
        candidates: list[tuple[float, int, int, float, float, float, float, int]] = []
        for polarity in (1, -1):
            for relative_delay in range(
                max(-search_limit, search_center - search_radius),
                min(search_limit, search_center + search_radius) + 1,
                coarse_step,
            ):
                score, mae, p90, cancellation_mae, cancellation_p90 = alignment_score(relative_delay, polarity)
                candidates.append((score, abs(relative_delay), relative_delay, mae, p90, cancellation_mae, cancellation_p90, polarity))
        coarse_best = min(candidates)
        best_relative = coarse_best[2]
        best_polarity = coarse_best[7]
        fine_candidates = []
        for relative_delay in range(
            max(-search_limit, best_relative - coarse_step),
            min(search_limit, best_relative + coarse_step) + 1,
        ):
            score, mae, p90, cancellation_mae, cancellation_p90 = alignment_score(relative_delay, best_polarity)
            fine_candidates.append((score, abs(relative_delay), relative_delay, mae, p90, cancellation_mae, cancellation_p90, best_polarity))
        best = min(fine_candidates)
        (
            best_relative, best_mae, best_p90, best_cancellation_mae,
            best_cancellation_p90, best_polarity,
        ) = best[2], best[3], best[4], best[5], best[6], best[7]
        (
            baseline_score, baseline_mae, baseline_p90,
            baseline_cancellation_mae, baseline_cancellation_p90,
        ) = alignment_score(0, 1)
        improvement = baseline_score - best[0]
        candidate_relative = best_relative
        candidate_polarity = best_polarity
        candidate_mae = best_mae
        candidate_p90 = best_p90
        candidate_cancellation_mae = best_cancellation_mae
        candidate_cancellation_p90 = best_cancellation_p90
        apply_alignment = (
            len(phase_frequencies) >= 3
            and math.isfinite(best[0])
            and (best_relative != 0 or best_polarity != 1)
            and improvement >= 0.25
            and best_p90 <= baseline_p90 + 0.25
            and best_cancellation_p90 <= baseline_cancellation_p90 + 0.25
        )
        if apply_alignment:
            if best_relative < 0:
                left_ir = delay_fir(left_ir, -best_relative)
                right_ir = delay_fir(right_ir, -best_relative)
            elif best_relative > 0:
                rear_channels = [delay_fir(channel, best_relative) for channel in rear_channels]
            if best_polarity < 0:
                rear_channels = [[-value for value in channel] for channel in rear_channels]
        else:
            best_relative, best_polarity = 0, 1
            best_mae, best_p90 = baseline_mae, baseline_p90
            best_cancellation_mae = baseline_cancellation_mae
            best_cancellation_p90 = baseline_cancellation_p90
        phase_optimization = {
            "requested": True,
            "evaluated": len(phase_frequencies) >= 3,
            "reliable": len(phase_frequencies) >= 3,
            "enabled": apply_alignment,
            "method": "same-recording relative-phase constrained robust search across L/R and all positions with destructive-cancellation penalty" if simultaneous_phase_reliable else "measured complex-sum robust search across both L/R and all positions with destructive-cancellation penalty",
            "evaluation_band_hz": [round(phase_low, 1), round(phase_high, 1)],
            "relative_delay_samples": best_relative,
            "relative_delay_ms": round(best_relative * 1000.0 / RATE, 4),
            "delayed_branch": "woofer" if best_relative > 0 else "front" if best_relative < 0 else "none",
            "polarity": best_polarity,
            "predicted_mae_db_before_guard": round(best_mae, 3),
            "predicted_p90_db_before_guard": round(best_p90, 3),
            "baseline_mae_db": round(baseline_mae, 3),
            "baseline_p90_db": round(baseline_p90, 3),
            "cancellation_deficit_mae_db": round(best_cancellation_mae, 3),
            "cancellation_deficit_p90_db": round(best_cancellation_p90, 3),
            "baseline_cancellation_deficit_mae_db": round(baseline_cancellation_mae, 3),
            "baseline_cancellation_deficit_p90_db": round(baseline_cancellation_p90, 3),
            "cancellation_non_regression_limit_db": 0.25,
            "candidate_relative_delay_samples": candidate_relative,
            "candidate_polarity": candidate_polarity,
            "candidate_target_mae_db": round(candidate_mae, 3),
            "candidate_target_p90_db": round(candidate_p90, 3),
            "candidate_cancellation_deficit_mae_db": round(candidate_cancellation_mae, 3),
            "candidate_cancellation_deficit_p90_db": round(candidate_cancellation_p90, 3),
            "score_improvement": round(max(0.0, improvement), 4),
            "reason": (
                None if apply_alignment else
                (
                    f"새 지연의 위상 상쇄 P90 {best_cancellation_p90:.2f} dB가 현재 "
                    f"{baseline_cancellation_p90:.2f} dB보다 0.25 dB 넘게 나빠 적용하지 않았습니다. "
                    "4 · FIR 계산에서 크로스오버 주파수를 한 단계 바꿔 비교하세요."
                )
                if len(phase_frequencies) >= 3 and best_cancellation_p90 > baseline_cancellation_p90 + 0.25 else
                "0.25 dB 이상의 안정적인 합산 개선이 없어 현재 지연·극성을 유지했습니다. 측정 실패가 아니라 변경 불필요 판정입니다."
                if len(phase_frequencies) >= 3 else
                "동시 위상 기준의 crossover 공통 tone이 3개 미만이라 상대 지연을 평가하지 못했습니다."
            ),
            "search_limit_samples": search_limit,
            "search_center_samples": search_center,
            "search_radius_samples": search_radius,
        }
    elif optimize_relative_phase:
        phase_optimization["reason"] = "직접음 bulk delay가 신뢰되지 않아 자동 극성·상대 지연 최적화를 적용하지 않았습니다."

    front_spectra = [fft.rfft(left_ir, fft_length), fft.rfft(right_ir, fft_length)]
    rear_spectra = [fft.rfft(rear_channels[0], fft_length), fft.rfft(rear_channels[1], fft_length)]
    phase_reliable = simultaneous_phase_reliable or (
        bool(time_alignment_reliable) and all(
            bool(position[source].get("bulk_delay_reliable", position[source].get("bulk_delay", {}).get("reliable", True)))
            for position in positions for source in ("left", "right", "woofer")
        )
    )
    # With LR4 enabled the overlap is intentionally local to the crossover.
    # With LR4 disabled both independently measured branches remain full range,
    # so ending the guard at 2.5*Fc would simply ignore their remaining sum.
    # In that mode derive the common cut-only guard over the complete measured
    # correction band; the natural Woofer roll-off makes it approach 0 dB once
    # its contribution is negligible.
    guard_high = (
        min(300.0, crossover_frequency_hz * 2.5)
        if crossover_enabled else
        min(20_000.0, max(frequency_grid))
    )
    fade_start = crossover_frequency_hz * 1.5 if crossover_enabled else guard_high
    raw_guards: list[list[float]] = [[], []]
    initial_upper_excess: list[list[float]] = [[], []]
    for channel, source in enumerate(("left", "right")):
        for frequency in frequency_grid:
            target_absolute = (
                interpolate_log(target_f, target_db, frequency)
                + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
                + (bass_modifier_db(frequency, preset) if frequency <= 350.0 else 0.0)
                - shared_target_reference_db
                + shared_front_reference_db
            )
            upper_values = []
            complex_values = []
            for position_index, position in enumerate(positions):
                front_acoustic, woofer_acoustic = acoustic_pair(position_index, source, frequency)
                main = front_acoustic * fir_value(front_spectra[channel], frequency)
                woofer = woofer_acoustic * fir_value(rear_spectra[channel], frequency)
                upper_values.append(20.0 * math.log10(max(abs(main) + abs(woofer), 1.0e-15)) - target_absolute)
                complex_values.append(20.0 * math.log10(max(abs(main + woofer), 1.0e-15)) - target_absolute)
            # Reliable complex measurements let us guard the maximum actually
            # observed L+W/R+W sum.  Cutting against |L|+|W| regardless of phase
            # can over-attenuate by ~6 dB and create a false target failure.
            # Fall back to the coherent mathematical upper bound only when the
            # phase reference is unavailable. In that case the conservative
            # bound is a deployable safety PASS and exact phase remains WARN.
            upper_excess = max(complex_values if phase_available(frequency) else upper_values)
            initial_upper_excess[channel].append(upper_excess)
            taper = 1.0
            if frequency < 20.0 or frequency > guard_high:
                taper = 0.0
            elif frequency > fade_start:
                fraction = (frequency - fade_start) / max(1.0e-9, guard_high - fade_start)
                taper = 0.5 + 0.5 * math.cos(math.pi * fraction)
            raw_guards[channel].append(-min(float(max_cut_db), max(0.0, upper_excess)) * taper)
    smoothed_guards = [variable_smooth(frequency_grid, values) for values in raw_guards]
    guard_impulses = []
    for channel in range(2):
        gains = []
        for bin_index in range(fft_length // 2 + 1):
            frequency = max(3.0, bin_index * RATE / fft_length)
            if 20.0 <= frequency <= guard_high:
                gains.append(interpolate_log(frequency_grid, smoothed_guards[channel], frequency))
            else:
                gains.append(0.0)
        guard_impulses.append(minimum_phase_fir(gains, fft, fft_length))
    left_ir = multiply_firs(left_ir, guard_impulses[0], fft)
    right_ir = multiply_firs(right_ir, guard_impulses[1], fft)
    rear_channels = [
        multiply_firs(rear_channels[0], guard_impulses[0], fft),
        multiply_firs(rear_channels[1], guard_impulses[1], fft),
    ]

    final_front_spectra = [fft.rfft(left_ir, fft_length), fft.rfft(right_ir, fft_length)]
    final_rear_spectra = [fft.rfft(rear_channels[0], fft_length), fft.rfft(rear_channels[1], fft_length)]
    channels: dict[str, Any] = {}
    all_upper_pass = True
    all_complex_pass = True
    all_target_estimate_pass = True
    for channel, source in enumerate(("left", "right")):
        upper_excesses = []
        complex_errors = []
        complex_signed_errors = []
        graph_frequency, graph_target, graph_complex, graph_energy, graph_upper, graph_guard = [], [], [], [], [], []
        for frequency, guard_db in zip(frequency_grid, smoothed_guards[channel]):
            if not 20.0 <= frequency <= 20_000.0:
                continue
            target_absolute = (
                interpolate_log(target_f, target_db, frequency)
                + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
                + (bass_modifier_db(frequency, preset) if frequency <= 350.0 else 0.0)
                - shared_target_reference_db
                + shared_front_reference_db
            )
            complex_levels, energy_levels, upper_levels = [], [], []
            for position_index, position in enumerate(positions):
                front_acoustic, woofer_acoustic = acoustic_pair(position_index, source, frequency)
                main = front_acoustic * fir_value(final_front_spectra[channel], frequency)
                woofer = woofer_acoustic * fir_value(final_rear_spectra[channel], frequency)
                complex_levels.append(20.0 * math.log10(max(abs(main + woofer), 1.0e-15)))
                energy_levels.append(10.0 * math.log10(max(abs(main) ** 2 + abs(woofer) ** 2, 1.0e-30)))
                upper_levels.append(20.0 * math.log10(max(abs(main) + abs(woofer), 1.0e-15)))
            position_weights = combined_position_weights(source, frequency)
            predicted = weighted_power_mean_db(complex_levels, position_weights)
            energy_sum = weighted_power_mean_db(energy_levels, position_weights)
            upper = max(upper_levels)
            reliable_here = phase_available(frequency)
            guarded_upper = max(complex_levels) if reliable_here else upper
            target_estimate = predicted if reliable_here else energy_sum
            signed_error = target_estimate - target_absolute
            if frequency <= guard_high:
                upper_excesses.append(max(0.0, guarded_upper - target_absolute))
                complex_signed_errors.append(signed_error)
                complex_errors.append(abs(signed_error))
            if not graph_frequency or frequency / graph_frequency[-1] >= 1.04:
                graph_frequency.append(round(frequency, 2))
                graph_target.append(round(target_absolute - shared_front_reference_db, 3))
                graph_complex.append(round(predicted - shared_front_reference_db, 3))
                graph_energy.append(round(energy_sum - shared_front_reference_db, 3))
                graph_upper.append(round(upper - shared_front_reference_db, 3))
                graph_guard.append(round(guard_db, 3))
        upper_p95 = percentile(upper_excesses, 0.95)
        complex_mae = sum(complex_errors) / len(complex_errors) if complex_errors else 0.0
        complex_p90 = percentile(complex_errors, 0.90)
        target_graph_values = graph_complex if phase_reliable else graph_energy
        crossover_dips = [
            (frequency, predicted - target)
            for frequency, predicted, target in zip(graph_frequency, target_graph_values, graph_target)
            if max(30.0, crossover_frequency_hz * 0.55) <= frequency <= min(300.0, crossover_frequency_hz * 2.0)
        ]
        deepest_dip_frequency, deepest_dip_db = min(crossover_dips, key=lambda item: item[1]) if crossover_dips else (None, 0.0)
        upper_pass = bool(upper_excesses) and upper_p95 <= 1.0
        target_estimate_pass = bool(complex_errors) and complex_mae <= 3.5 and complex_p90 <= 7.0
        complex_pass = phase_reliable and target_estimate_pass
        all_upper_pass = all_upper_pass and upper_pass
        all_complex_pass = all_complex_pass and complex_pass
        all_target_estimate_pass = all_target_estimate_pass and target_estimate_pass
        channels[source] = {
            "frequency": graph_frequency,
            "target_db": graph_target,
            "predicted_complex_db": graph_complex,
            "phase_agnostic_energy_db": graph_energy,
            "coherent_upper_db": graph_upper,
            "overlap_guard_db": graph_guard,
            "coherent_upper_excess_p95_db": round(upper_p95, 3),
            "guarded_upper_excess_p95_db": round(upper_p95, 3),
            "guard_basis": "six-capture closure-constrained complex sum inside phase coverage; phase-agnostic upper bound outside" if physical_sum_constraints_available else "same-recording complex sum inside phase coverage; phase-agnostic upper bound outside" if simultaneous_phase_reliable else "maximum measured complex sum" if phase_reliable else "phase-agnostic |Front|+|Woofer| upper bound",
            "complex_target_mae_db": round(complex_mae, 3),
            "complex_target_p90_db": round(complex_p90, 3),
            "complex_target_median_error_db": round(statistics.median(complex_signed_errors), 3) if phase_reliable and complex_signed_errors else None,
            "target_estimate_basis": "L/R/W magnitudes + L+W/R+W cross-term + simultaneous L/R/W phase; energy sum outside phase coverage" if physical_sum_constraints_available else "same-recording complex sum inside phase coverage; energy sum outside" if simultaneous_phase_reliable else "measured complex sum" if phase_reliable else "phase-agnostic energy sum",
            "target_estimate_mae_db": round(complex_mae, 3),
            "target_estimate_p90_db": round(complex_p90, 3),
            "target_estimate_median_error_db": round(statistics.median(complex_signed_errors), 3) if complex_signed_errors else None,
            "target_estimate_pass": target_estimate_pass,
            "complex_prediction_reliable": phase_reliable,
            "graph_range_hz": [20.0, 20_000.0],
            "metric_range_hz": [20.0, round(guard_high, 2)],
            "spatial_aggregation": {
                "method": "frequency-dependent spatial/SNR-weighted mean-square transfer power",
                "mode": spatial_mode,
                "safety_upper_envelope": "maximum across measured positions; never spatially averaged",
            },
            "deepest_crossover_dip_hz": round(deepest_dip_frequency, 1) if deepest_dip_frequency is not None else None,
            "deepest_crossover_dip_db": round(deepest_dip_db, 2),
            "deep_notch_detected": deepest_dip_db <= -6.0,
            "deep_notch_action": (
                "부스트하지 마세요. 4 · FIR 계산에서 위상 방식을 ‘저역 음량+excess phase’로 두고 크로스오버 주파수를 한 단계 바꿔 비교하세요. 계속되면 우퍼 극성·거리·위치를 확인하세요."
                if deepest_dip_db <= -6.0 else
                "추가 조치 없음"
            ),
            "pass": upper_pass and target_estimate_pass,
        }
    return left_ir, right_ir, rear_channels, {
        "enabled": bool(crossover_enabled),
        "embedded_in_fir": bool(crossover_enabled),
        "sum_guard_enabled": True,
        "sum_guard_embedded_in_fir": True,
        "type": (
            "Linkwitz-Riley 4th-order magnitude branches plus cut-only coherent-sum guard"
            if crossover_enabled else
            "full-range Front/Woofer overlap plus cut-only coherent-sum guard"
        ),
        "frequency_hz": crossover_frequency_hz,
        "additional_runtime_filters": 0,
        "additional_block_latency_samples": 0,
        "phase_alignment_reliable": phase_reliable,
        "simultaneous_phase_reference": simultaneous_phase_reliable,
        "simultaneous_phase_coverage_hz": (
            [round(simultaneous_phase_coverage[0], 3), round(simultaneous_phase_coverage[1], 3)]
            if simultaneous_phase_coverage is not None else None
        ),
        "physical_sum_constraints": {
            "requested": bool(physical_sum_constraints_enabled),
            "available": physical_sum_constraints_available,
            "used": bool(sum_constraint_weights),
            "method": "dense cross-term from L+W/R+W fused with simultaneous L/R/W relative phase; no branch normalization or response averaging",
            "unique_points_used": len(sum_constraint_weights),
            "median_weight": round(statistics.median(sum_constraint_weights), 4) if sum_constraint_weights else 0.0,
            "phase_adjustment_p90_deg": round(percentile(sum_constraint_phase_adjustments, 0.90), 3) if sum_constraint_phase_adjustments else 0.0,
            "rejections": sum_constraint_rejections,
            "woofer_measurement_attenuation_db": float(woofer_measurement_attenuation_db),
        },
        "relative_phase_optimization": phase_optimization,
        "sum_guard_basis": "all six captures: L/R/W magnitudes, L+W/R+W cross-terms and simultaneous L/R/W phase" if physical_sum_constraints_available else "same-recording L/R/W phase in crossover coverage plus conservative upper bound outside" if simultaneous_phase_reliable else "maximum measured complex sum" if phase_reliable else "phase-agnostic coherent upper bound",
        "coherent_upper_guard_pass": all_upper_pass,
        "complex_sum_target_pass": all_complex_pass if phase_reliable else None,
        "phase_agnostic_target_pass": all_target_estimate_pass if not phase_reliable else None,
        "overall_acoustic_prediction_pass": all_upper_pass and all_complex_pass if phase_reliable else False,
        "safe_deploy_pass": all_upper_pass and all_target_estimate_pass,
        "phase_verification_status": "pass" if phase_reliable and all_complex_pass else "fail" if phase_reliable else "limited",
        "status": "pass" if phase_reliable and all_upper_pass and all_complex_pass else "pass_safe_upper_phase_limited" if all_upper_pass and all_target_estimate_pass and not phase_reliable else "fail_target" if all_upper_pass else "fail_upper_guard",
        "channels": channels,
        "policy": "Never boost to repair a crossover null; cap the worst measured-position constructive upper envelope. Reliable phase additionally verifies the complex target; limited phase remains an explicit warning and uses the conservative cut-only upper bound instead of blocking a safe FIR.",
    }


def build_mimo_worker(state: dict[str, Any], options: dict[str, Any]) -> None:
    capability = platform_capabilities()
    if not capability["mimo_supported"]:
        raise MeasurementError(f"MIMO 계산을 시작할 수 없습니다: {capability.get('reason', '플랫폼 또는 timing reference 조건 미충족')}")
    directory = Path(state["session_dir"])
    session_path = directory / "session.json"
    options_path = directory / "mimo-build-options.json"
    atomic_json(session_path, state)
    atomic_json(options_path, options)
    update_current(state="processing", stage="2×4 MIMO 전달행렬 정규화", progress=8.0, eta_seconds=180)
    process = subprocess.run(
        [PYTHON, MIMO_ENGINE, "--measurement-engine", str(Path(__file__).resolve()), "build", str(session_path), str(directory), str(options_path)],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(process.stdout.strip() or "MIMO 계산 결과를 읽을 수 없습니다.") from exc
    if process.returncode or result.get("error"):
        raise MeasurementError(result.get("error", "MIMO 계산 실패"))
    result["algorithm_revision"] = RESULT_ALGORITHM_REVISION
    woofer_attenuation_db = int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB))
    result["measurement_output"] = {
        "mode": state.get("mode"),
        "signal_path": "each physical actuator measured independently",
        "white_noise_level_dbfs": int(state.get("noise_level_dbfs", state.get("level_dbfs", DEFAULT_NOISE_LEVEL_DBFS))),
        "sweep_level_dbfs": int(state.get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS)),
        "woofer_relative_level_db": woofer_attenuation_db,
        "effective_woofer_sweep_dbfs": int(state.get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS)) + woofer_attenuation_db,
        "woofer_level_semantics": "reference-compensated measurement attenuation; affects SNR, not recovered transfer magnitude",
    }
    result["correction_limits"] = {
        "low_hz": options["correction_low_hz"],
        "high_hz": options["correction_high_hz"],
        "max_room_boost_db": options["max_boost_db"],
        "max_relative_compensation_db": options["max_boost_db"],
        "max_room_cut_db": options["max_cut_db"],
        "semantics": "one common 0 dB reference across the complete MIMO L/R/Woofer bank",
    }
    updated = update_current(
        state="built", stage="2×4 MIMO 32768탭 bank 생성 완료 · 보고서와 예측을 확인하세요.",
        progress=100.0, eta_seconds=None, worker_pid=None, result=result,
    )
    atomic_json(session_path, updated)


def build_worker(target_name: str, preset: str, woofer_trim_db: int, phase_mode: str, phase_cutoff: int, spatial_mode: str = "equal", bass_tilt_db: int = 0, treble_tilt_db: int = 0, correction_low_hz: int = 20, correction_high_hz: int = 20_000, max_boost_db: int = 10, max_cut_db: int = 18, mimo_high_hz: int = 150, mimo_strength: str = "balanced", mimo_support_penalty_db: int = 6, crossover_enabled: bool = True, crossover_frequency_hz: int = 100) -> None:
    state = load_current()
    positions_total = session_position_count(state)
    if int(state.get("positions_completed", 0)) != positions_total:
        raise MeasurementError(f"선택한 {positions_total}위치 측정을 먼저 완료하세요.")
    if spatial_mode not in ("equal", "center") or not -6 <= bass_tilt_db <= 6 or not -6 <= treble_tilt_db <= 2:
        raise MeasurementError("공간 평균 또는 음색 선호값이 범위를 벗어났습니다.")
    if correction_low_hz not in (20, 30, 40, 60, 80) or correction_high_hz not in (300, 500, 1000, 5000, 20_000) or correction_low_hz >= correction_high_hz:
        raise MeasurementError("보정 주파수 범위가 잘못되었습니다.")
    if max_boost_db not in (0, 3, 6, 9, 10) or max_cut_db not in (6, 9, 12, 18, 24):
        raise MeasurementError("최대 상대 보상/감쇄 값이 잘못되었습니다.")
    if mimo_high_hz not in (80, 120, 150) or mimo_strength not in ("safe", "balanced", "maximum") or mimo_support_penalty_db not in (3, 6, 9, 12):
        raise MeasurementError("MIMO 보정 범위·강도·지원 제어원 제한값이 잘못되었습니다.")
    if not isinstance(crossover_enabled, bool) or crossover_frequency_hz not in CROSSOVER_FREQUENCIES:
        raise MeasurementError("디지털 crossover 설정이 잘못되었습니다.")
    if state.get("mode") == "lr" and crossover_enabled:
        raise MeasurementError(
            "디지털 crossover ON은 L/R/W 개별 측정이 필요합니다. "
            "L+우퍼/R+우퍼 합산 모드는 프런트와 우퍼를 독립 HPF/LPF로 나눌 수 없습니다."
        )
    if state.get("mode") in MIMO_MODES:
        build_mimo_worker(state, {
            "target": target_name, "preset": preset, "woofer_trim_db": woofer_trim_db,
            "phase_mode": phase_mode, "phase_cutoff": phase_cutoff, "spatial_mode": spatial_mode,
            "bass_tilt_db": bass_tilt_db, "treble_tilt_db": treble_tilt_db,
            "correction_low_hz": correction_low_hz, "correction_high_hz": correction_high_hz,
            "max_boost_db": max_boost_db, "max_cut_db": max_cut_db,
            "mimo_high_hz": mimo_high_hz, "mimo_strength": mimo_strength,
            "mimo_support_penalty_db": mimo_support_penalty_db,
            "crossover_enabled": crossover_enabled,
            "crossover_frequency_hz": crossover_frequency_hz,
        })
        return
    estimates = platform_capabilities()["offline_estimates_seconds"]
    build_eta = int(estimates["fir_bass_phase"] if phase_mode == "bass" else estimates["fir_magnitude"])
    measured_woofer_attenuation_db = int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB))
    if state.get("mode") == "lr" and woofer_trim_db != measured_woofer_attenuation_db:
        raise MeasurementError(
            "L+우퍼/R+우퍼 합산 측정에서는 최종 우퍼 트림이 "
            f"측정 상대레벨({measured_woofer_attenuation_db} dB)과 같아야 합니다."
        )
    directory = Path(state["session_dir"])
    phase_reference_results = load_phase_reference_results(directory, positions_total)
    phase_reference_reliable = bool(
        len(phase_reference_results) == positions_total
        and phase_reference_coverage(phase_reference_results, ("left", "right", "woofer"))
    )
    premeasured_sum_model = None
    if state.get("mode") in PREMEASURED_SUM_MODES:
        premeasured_sum_model = evaluate_premeasured_sum_model(
            directory,
            positions_total,
            measured_woofer_attenuation_db,
            crossover_frequency_hz,
        )
        if not phase_reference_reliable:
            raise MeasurementError(
                "여섯 측정 공동 FIR 계산에 필요한 L+R+우퍼 동시 위상 기준이 없거나 신뢰도 기준을 통과하지 못했습니다. "
                "3 · 위치 측정에서 표시된 위치의 ‘다시 측정’을 실행하세요. 위상 기준은 위치마다 한 번씩 자동 포함됩니다."
            )
        if not premeasured_sum_model.get("pass"):
            raise MeasurementError(
                "L+우퍼/R+우퍼 합산 측정이 개별 L/R/우퍼와 일치하지 않아 공동 FIR 계산을 중단했습니다. "
                + str(premeasured_sum_model.get("action") or "3 · 위치 측정에서 해당 위치를 다시 측정하세요.")
            )
    update_current(state="processing", stage="공간 평균 응답 계산", progress=5.0, eta_seconds=build_eta)
    fft = FFTBackend()
    left_source, right_source = (
        ("left_woofer", "right_woofer") if state.get("mode") == "lr" else ("left", "right")
    )
    left_response = load_average_response(directory, left_source, spatial_mode, positions_total)
    right_response = load_average_response(directory, right_source, spatial_mode, positions_total)
    left_f, left_db = left_response["frequencies"], left_response["average_db"]
    right_f, right_db = right_response["frequencies"], right_response["average_db"]
    front_reference_values = [
        value
        for frequencies, levels in ((left_f, left_db), (right_f, right_db))
        for frequency, value in zip(frequencies, levels)
        if 500.0 <= frequency <= 2000.0
    ]
    shared_front_reference_db = statistics.median(front_reference_values)
    target_f, target_db = target_curve(target_name)
    shared_target_reference_db = statistics.median(
        interpolate_log(target_f, target_db, frequency)
        + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
        for frequency in left_f
        if 500.0 <= frequency <= 2000.0
    )
    preferred_target_db = [
        value + preference_modifier_db(frequency, bass_tilt_db, treble_tilt_db)
        for frequency, value in zip(target_f, target_db)
    ]
    (
        left_confidence,
        right_confidence,
        left_rolloff_floor,
        right_rolloff_floor,
        stereo_rolloff_summary,
    ) = stereo_broad_rolloff_confidence(
        left_f,
        left_db,
        left_response["frequency_confidence"],
        right_f,
        right_db,
        right_response["frequency_confidence"],
        target_f,
        preferred_target_db,
        shared_front_reference_db,
        shared_target_reference_db,
    )
    update_current(stage="Left 32768탭 최소위상 FIR 계산", progress=22.0, eta_seconds=round(build_eta * 0.80))
    common = {"spatial_mode": spatial_mode, "bass_tilt_db": bass_tilt_db, "treble_tilt_db": treble_tilt_db, "correction_low_hz": correction_low_hz, "correction_high_hz": correction_high_hz, "max_boost_db": max_boost_db, "max_cut_db": max_cut_db, "fft": fft}
    front_design_phase_mode = "magnitude" if phase_mode == "bass" else phase_mode
    crossover_role = "highpass" if state.get("mode") in SEPARATE_WOOFER_MODES and crossover_enabled else None
    left_ir, left_graph = design_channel(left_f, left_db, left_response["spatial_std_db"], left_response["center_phase_rad"], target_name, preset, woofer=False, woofer_trim_db=0, phase_mode=front_design_phase_mode, phase_cutoff=phase_cutoff, crossover_role=crossover_role, crossover_frequency_hz=crossover_frequency_hz, decay_frequency_hz=left_response["decay_frequency_hz"], decay_t20_rt60_s=left_response["decay_t20_rt60_s"], frequency_confidence=left_confidence, corroborated_rolloff_confidence=left_rolloff_floor, shared_reference_measure_db=shared_front_reference_db, shared_reference_target_db=shared_target_reference_db, **common)
    update_current(stage="Right 32768탭 최소위상 FIR 계산", progress=50.0, eta_seconds=round(build_eta * 0.52))
    right_ir, right_graph = design_channel(right_f, right_db, right_response["spatial_std_db"], right_response["center_phase_rad"], target_name, preset, woofer=False, woofer_trim_db=0, phase_mode=front_design_phase_mode, phase_cutoff=phase_cutoff, crossover_role=crossover_role, crossover_frequency_hz=crossover_frequency_hz, decay_frequency_hz=right_response["decay_frequency_hz"], decay_t20_rt60_s=right_response["decay_t20_rt60_s"], frequency_confidence=right_confidence, corroborated_rolloff_confidence=right_rolloff_floor, shared_reference_measure_db=shared_front_reference_db, shared_reference_target_db=shared_target_reference_db, **common)
    front_phase_reliable = bool(
        left_response.get("center_bulk_delay_reliable", True)
        and right_response.get("center_bulk_delay_reliable", True)
    )
    if phase_mode == "bass" and front_phase_reliable:
        update_current(stage="L/R 공통 저역 phase · 동일 지연 안전 투영", progress=62.0, eta_seconds=round(build_eta * 0.40))
        left_ir, right_ir, common_phase_details = apply_common_lr_low_frequency_phase(
            left_ir,
            right_ir,
            left_f,
            left_db,
            right_db,
            left_response["center_phase_rad"],
            right_response["center_phase_rad"],
            phase_cutoff,
            fft,
        )
        left_graph["phase"] = {**common_phase_details, "channel": "left"}
        right_graph["phase"] = {**common_phase_details, "channel": "right"}
        left_graph = finalize_graph_with_fir(left_graph, left_ir, fft)
        right_graph = finalize_graph_with_fir(right_graph, right_ir, fft)
    elif phase_mode == "bass":
        phase_fallback = {
            "requested_mode": "bass",
            "effective_mode": "magnitude",
            "enabled": False,
            "reason": "L/R direct impulse delay is not reliable; common excess-phase correction was disabled",
        }
        left_graph["phase"] = {**phase_fallback, "channel": "left"}
        right_graph["phase"] = {**phase_fallback, "channel": "right"}
    front = directory / "Generated_Front_LR_32768.wav"
    rear = None
    rear_graph = None
    rear_channels = None
    if state["mode"] in SEPARATE_WOOFER_MODES:
        update_current(stage="우퍼 32768탭 FIR 계산", progress=72.0, eta_seconds=round(build_eta * 0.30))
        woofer_response = load_average_response(directory, "woofer", spatial_mode, positions_total)
        woofer_f, woofer_db = woofer_response["frequencies"], woofer_response["average_db"]
        woofer_phase_reliable = bool(woofer_response.get("center_bulk_delay_reliable", True))
        woofer_phase_mode = phase_mode if woofer_phase_reliable else "magnitude"
        woofer_ir, rear_graph = design_channel(
            woofer_f, woofer_db, woofer_response["spatial_std_db"], woofer_response["center_phase_rad"],
            target_name, preset, woofer=True, woofer_trim_db=woofer_trim_db,
            phase_mode=woofer_phase_mode, phase_cutoff=phase_cutoff,
            decay_frequency_hz=woofer_response["decay_frequency_hz"],
            decay_t20_rt60_s=woofer_response["decay_t20_rt60_s"],
            frequency_confidence=woofer_response["frequency_confidence"],
            shared_reference_measure_db=shared_front_reference_db,
            shared_reference_target_db=shared_target_reference_db,
            crossover_role="lowpass" if crossover_enabled else None,
            crossover_frequency_hz=crossover_frequency_hz,
            **common,
        )
        if phase_mode == "bass" and not woofer_phase_reliable:
            rear_graph["phase"] = {
                "requested_mode": "bass",
                "effective_mode": "magnitude",
                "enabled": False,
                "reason": "Woofer direct impulse delay is not reliable; Woofer excess-phase correction was disabled",
                "bulk_delay": woofer_response.get("center_bulk_delay"),
            }
        rear_channels = [woofer_ir, woofer_ir]
    decay_summary = {
        "left": dict(zip(left_response["decay_frequency_hz"], left_response["decay_t20_rt60_s"])),
        "right": dict(zip(right_response["decay_frequency_hz"], right_response["decay_t20_rt60_s"])),
        "woofer": dict(zip(woofer_response["decay_frequency_hz"], woofer_response["decay_t20_rt60_s"])) if state["mode"] in SEPARATE_WOOFER_MODES else None,
    }
    time_alignment = {
        "requested": state["mode"] in SEPARATE_WOOFER_MODES and phase_mode == "bass",
        "enabled": False,
        "aligned": None,
        "front_delay_samples": 0,
        "rear_delay_samples": 0,
    }
    if state["mode"] in SEPARATE_WOOFER_MODES and phase_mode == "bass" and rear_channels is not None:
        alignment_limit = MAX_PHASE_SHIFT + 960
        alignment_reliable = bool(
            PHASE_CLOCK_SHARED
            and front_phase_reliable
            and woofer_response.get("center_bulk_delay_reliable", True)
        )
        if not alignment_reliable:
            time_alignment.update({
                "reliable": False,
                "reason": "U7/UMIK hardware clock 또는 프런트/우퍼 직접음 지연 기준이 공유·검증되지 않아 상대 지연을 적용하지 않았습니다.",
                "front_bulk_delay_reliable": front_phase_reliable,
                "rear_bulk_delay_reliable": bool(woofer_response.get("center_bulk_delay_reliable", True)),
                "rear_bulk_delay": woofer_response.get("center_bulk_delay"),
                "limit_samples": alignment_limit,
                "limit_ms": round(alignment_limit * 1000.0 / RATE, 3),
            })
        else:
            front_acoustic = round(statistics.median((left_response["center_bulk_delay_samples"], right_response["center_bulk_delay_samples"])))
            rear_acoustic = int(woofer_response["center_bulk_delay_samples"])
            front_filter = round(statistics.median((fir_energy_delay(left_ir), fir_energy_delay(right_ir))))
            rear_filter = round(statistics.median((fir_energy_delay(rear_channels[0]), fir_energy_delay(rear_channels[1]))))
            front_total = front_acoustic + front_filter
            rear_total = rear_acoustic + rear_filter
            required_delay = abs(front_total - rear_total)
            if required_delay > alignment_limit:
                time_alignment.update({
                    "reliable": True,
                    "reason": "required relative delay exceeds the causal safety limit; no partial delay was applied",
                    "front_acoustic_delay_samples": front_acoustic,
                    "rear_acoustic_delay_samples": rear_acoustic,
                    "front_fir_energy_delay_samples": front_filter,
                    "rear_fir_energy_delay_samples": rear_filter,
                    "front_total_before_alignment_samples": front_total,
                    "rear_total_before_alignment_samples": rear_total,
                    "required_delay_samples": required_delay,
                    "limit_samples": alignment_limit,
                    "limit_ms": round(alignment_limit * 1000.0 / RATE, 3),
                })
            else:
                target_delay = max(front_total, rear_total)
                front_delay = max(0, target_delay - front_total)
                rear_delay = max(0, target_delay - rear_total)
                left_ir = delay_fir(left_ir, front_delay)
                right_ir = delay_fir(right_ir, front_delay)
                rear_channels = [delay_fir(rear_channels[0], rear_delay), delay_fir(rear_channels[1], rear_delay)]
                aligned_front_total = front_total + front_delay
                aligned_rear_total = rear_total + rear_delay
                time_alignment = {
                    "requested": True,
                    "enabled": True,
                    "reliable": True,
                    "front_acoustic_delay_samples": front_acoustic,
                    "rear_acoustic_delay_samples": rear_acoustic,
                    "front_fir_energy_delay_samples": front_filter,
                    "rear_fir_energy_delay_samples": rear_filter,
                    "front_total_before_alignment_samples": front_total,
                    "rear_total_before_alignment_samples": rear_total,
                    "front_delay_samples": front_delay,
                    "rear_delay_samples": rear_delay,
                    "front_delay_ms": round(front_delay * 1000.0 / RATE, 3),
                    "rear_delay_ms": round(rear_delay * 1000.0 / RATE, 3),
                    "residual_total_delay_samples": abs(aligned_front_total - aligned_rear_total),
                    "aligned": aligned_front_total == aligned_rear_total,
                    "limit_samples": alignment_limit,
                    "limit_ms": round(alignment_limit * 1000.0 / RATE, 3),
                    "method": "center acoustic bulk delay plus FIR energy-median delay",
                }
                left_graph = finalize_graph_with_fir(left_graph, left_ir, fft)
                right_graph = finalize_graph_with_fir(right_graph, right_ir, fft)
                rear_graph = finalize_graph_with_fir(rear_graph, rear_channels[0], fft)
    crossover_summary: dict[str, Any] = {
        "enabled": False,
        "embedded_in_fir": False,
        "frequency_hz": None,
        "additional_runtime_filters": 0,
        "additional_block_latency_samples": 0,
        "status": "disabled",
        "reason": "사용자가 크로스오버를 꺼짐으로 선택했습니다." if not crossover_enabled else "이 측정 구성에는 독립 프런트/우퍼 분기가 없습니다.",
    }
    if state["mode"] in SEPARATE_WOOFER_MODES and rear_channels is not None:
        update_current(stage="프런트+우퍼 전체 합산 안전 투영", progress=86.0, eta_seconds=round(build_eta * 0.10))
        left_ir, right_ir, rear_channels, crossover_summary = apply_joint_crossover_guard(
            directory,
            left_ir,
            right_ir,
            rear_channels,
            target_name=target_name,
            preset=preset,
            bass_tilt_db=bass_tilt_db,
            treble_tilt_db=treble_tilt_db,
            crossover_enabled=crossover_enabled,
            crossover_frequency_hz=crossover_frequency_hz,
            max_cut_db=max_cut_db,
            shared_front_reference_db=shared_front_reference_db,
            shared_target_reference_db=shared_target_reference_db,
            woofer_measurement_attenuation_db=measured_woofer_attenuation_db,
            physical_sum_constraints_enabled=bool(
                premeasured_sum_model and premeasured_sum_model.get("pass")
            ),
            # Same-recording relative timing is sufficient for crossover
            # complex prediction. It does not claim a shared absolute clock.
            time_alignment_reliable=bool(
                phase_reference_reliable
                or (PHASE_CLOCK_SHARED and front_phase_reliable and woofer_phase_reliable)
            ),
            optimize_relative_phase=phase_mode == "bass",
            positions_total=positions_total,
            spatial_mode=spatial_mode,
            fft=fft,
        )
        phase_optimization = crossover_summary.get("relative_phase_optimization", {})
        if phase_optimization.get("requested") and phase_optimization.get("reliable"):
            relative_delay = int(phase_optimization.get("relative_delay_samples", 0))
            alignment_applied = bool(phase_optimization.get("enabled"))
            time_alignment = {
                "requested": phase_mode == "bass",
                "enabled": alignment_applied,
                "reliable": True,
                "aligned": True,
                "evaluated": True,
                "no_change_needed": not alignment_applied,
                "front_delay_samples": max(0, -relative_delay),
                "rear_delay_samples": max(0, relative_delay),
                "relative_delay_samples": relative_delay,
                "relative_delay_ms": round(relative_delay * 1000.0 / RATE, 4),
                "woofer_polarity": int(phase_optimization.get("polarity", 1)),
                "method": phase_optimization.get("method"),
                "reason": phase_optimization.get("reason"),
                "reference": (
                    "simultaneous L/R/W multisine at every microphone position"
                    if phase_reference_reliable else
                    "shared hardware timing reference"
                ),
            }
        for graph, channel_name in ((left_graph, "left"), (right_graph, "right"), (rear_graph, "left")):
            guard_graph = crossover_summary["channels"][channel_name]
            guard_frequencies = guard_graph["frequency"]
            guard_values = guard_graph["overlap_guard_db"]
            graph["correction_db"] = [
                float(value) + (
                    interpolate_log(guard_frequencies, guard_values, float(frequency))
                    if guard_frequencies[0] <= float(frequency) <= guard_frequencies[-1] else 0.0
                )
                for frequency, value in zip(graph["frequency"], graph["correction_db"])
            ]
    # Normalize only after every magnitude, phase, delay and crossover stage is
    # complete. A single shared gain keeps the independently measured Front and
    # Woofer branches in the exact relative balance calculated above.
    bank_channels = [left_ir, right_ir]
    if rear_channels is not None:
        bank_channels.extend(rear_channels)
    normalized_bank, bank_normalization = normalize_fir_bank(bank_channels, fft)
    common_attenuation_db = max(0.0, -float(bank_normalization["applied_common_gain_db"]))
    bank_normalization.update({
        "max_relative_compensation_db": max_boost_db,
        "common_attenuation_db": round(common_attenuation_db, 4),
        "relative_compensation_limit_pass": common_attenuation_db <= float(max_boost_db) + 0.25,
    })
    left_ir, right_ir = normalized_bank[:2]
    if rear_channels is not None:
        rear_channels = normalized_bank[2:4]
    left_graph = finalize_graph_with_fir(left_graph, left_ir, fft)
    right_graph = finalize_graph_with_fir(right_graph, right_ir, fft)
    if state["mode"] in SEPARATE_WOOFER_MODES and isinstance(rear_graph, dict) and rear_channels is not None:
        rear_graph = finalize_graph_with_fir(rear_graph, rear_channels[0], fft)
    common_level_reference = apply_common_graph_reference(
        left_graph,
        right_graph,
        rear_graph if state["mode"] in SEPARATE_WOOFER_MODES and isinstance(rear_graph, dict) else None,
    )
    high_frequency_compensation = summarize_high_frequency_compensation(
        left_graph,
        right_graph,
        float(max_boost_db),
        common_attenuation_db,
    )
    if state["mode"] == "lr":
        # The shared-filter topology convolves once, then copies to Rear with a
        # mixer trim. Creating a scaled Rear WAV here would waste two Conv paths
        # and would no longer match the simultaneous system measurement.
        rear_graph = {
            "copied_front": True,
            "woofer_trim_db": woofer_trim_db,
            "runtime_mixer_trim": True,
        }
    elif rear_channels is None and woofer_trim_db != 0:
        # Preserve the Front correction while baking a Rear-only level trim.
        scale = 10.0 ** (woofer_trim_db / 20.0)
        rear = directory / "Generated_Rear_LR_32768.wav"
        rear_channels = [[value * scale for value in left_ir], [value * scale for value in right_ir]]
        rear_graph = {"copied_front": True, "woofer_trim_db": woofer_trim_db}
    write_float_stereo(front, left_ir, right_ir)
    front_metrics = fir_metrics([left_ir, right_ir], fft)
    rear_metrics = None
    if rear_channels is not None:
        rear = directory / "Generated_Rear_LR_32768.wav"
        write_float_stereo(rear, rear_channels[0], rear_channels[1])
        rear_metrics = fir_metrics(rear_channels, fft)
    lr_differences = [abs(l_value - r_value) for frequency, l_value, r_value in zip(left_f, left_db, right_db) if 80.0 <= frequency <= 10_000.0]
    all_variation = [value for frequency, value in zip(left_f, left_response["spatial_std_db"]) if 20.0 <= frequency <= 20_000.0] + [value for frequency, value in zip(right_f, right_response["spatial_std_db"]) if 20.0 <= frequency <= 20_000.0]
    measurement_snrs = []
    measurement_peaks = []
    reused_measurements = []
    reused_keys: set[tuple[int, str]] = set()
    seen_response_signatures: dict[str, tuple[int, str]] = {}
    for position in range(1, positions_total + 1):
        for source in state["sources"]:
            response_path = directory / f"p{position}_{source}_response.json"
            response_value = json.loads(response_path.read_text(encoding="utf-8"))
            quality = response_value.get("measurement_quality", {})
            if isinstance(quality.get("snr_db"), (int, float)):
                measurement_snrs.append(float(quality["snr_db"]))
            if isinstance(quality.get("peak_dbfs"), (int, float)):
                measurement_peaks.append(float(quality["peak_dbfs"]))
            reused_from = response_value.get("measurement", {}).get("reused_from_position")
            if reused_from is not None:
                reused_measurements.append({"position": position, "source": source, "reused_from_position": reused_from, "detected_by": "capture_metadata"})
                reused_keys.add((position, source))
            # Older sessions may have copied response JSON without adding the
            # reuse marker. Exact equality of the frequency, magnitude and
            # phase vectors cannot result from an independent acoustic capture,
            # so detect it before claiming three-position spatial stability.
            signature_payload = {
                "frequencies": response_value.get("frequencies"),
                "db": response_value.get("db"),
                "phase_rad": response_value.get("phase_rad"),
            }
            signature = hashlib.sha256(
                json.dumps(signature_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            previous = seen_response_signatures.get(source)
            if previous is not None and previous[1] == signature and (position, source) not in reused_keys:
                reused_measurements.append({
                    "position": position,
                    "source": source,
                    "reused_from_position": previous[0],
                    "detected_by": "exact_response_vector_duplicate",
                })
                reused_keys.add((position, source))
            else:
                seen_response_signatures[source] = (position, signature)
    target_fit = {
        "left": left_graph["target_fit"],
        "right": right_graph["target_fit"],
        "woofer": rear_graph.get("target_fit") if isinstance(rear_graph, dict) else None,
    }
    target_fit_required = {
        key: value for key, value in target_fit.items()
        if not (state["mode"] in SEPARATE_WOOFER_MODES and key == "woofer")
    }
    implementation = {
        "left": left_graph["fir_implementation"],
        "right": right_graph["fir_implementation"],
        "woofer": rear_graph.get("fir_implementation") if isinstance(rear_graph, dict) else None,
    }
    channel_metrics = [front_metrics["left"], front_metrics["right"]]
    if rear_metrics:
        channel_metrics += [rear_metrics["left"], rear_metrics["right"]]
    common_reference_graphs = [
        graph
        for graph in (
            left_graph,
            right_graph,
            rear_graph if state["mode"] in SEPARATE_WOOFER_MODES else None,
        )
        if isinstance(graph, dict)
    ]
    common_reference_signature = (
        common_level_reference.get("scope"),
        tuple(common_level_reference.get("reference_band_hz") or ()),
        common_level_reference.get("predicted_reference_db"),
        common_level_reference.get("target_reference_db"),
        common_level_reference.get("independent_channel_normalization"),
    )
    core_checks = {
        "exact_32768_taps": all(item["taps"] == TAPS for item in channel_metrics),
        "finite_samples": all(item["finite"] for item in channel_metrics),
        "no_positive_transfer": all(item["transfer_pass"] for item in channel_metrics),
        "early_impulse": all(item["early_impulse_pass"] for item in channel_metrics),
        "fir_matches_design": all(
            item is None or item["pass"] for item in implementation.values()
        ),
        "time_alignment_safe": not time_alignment.get("enabled") or bool(time_alignment.get("aligned")),
        "one_common_level_reference": (
            len(common_reference_graphs) == (3 if state["mode"] in SEPARATE_WOOFER_MODES else 2)
            and common_reference_signature[-1] is False
            and all(
                (
                    reference.get("scope"),
                    tuple(reference.get("reference_band_hz") or ()),
                    reference.get("predicted_reference_db"),
                    reference.get("target_reference_db"),
                    reference.get("independent_channel_normalization"),
                ) == common_reference_signature
                for reference in (
                    graph.get("common_reference") or {}
                    for graph in common_reference_graphs
                )
            )
        ),
        "one_common_bank_gain": (
            bank_normalization.get("independent_channel_normalization") is False
            and bank_normalization.get("zero_db_reference") == "single_common_bank_peak"
            and bank_normalization.get("scope") == (
                "complete_l_r_woofer_bank"
                if state["mode"] in SEPARATE_WOOFER_MODES else "complete_l_r_bank"
            )
        ),
        "relative_branch_level_preserved": (
            bool(bank_normalization.get("relative_branch_gain_preserved"))
            and float(bank_normalization.get("maximum_relative_level_error_db", 999.0)) <= 1.0e-6
        ),
        "relative_compensation_limit": bool(bank_normalization.get("relative_compensation_limit_pass")),
        "narrow_null_boost_guard": all(
            float(graph.get("maximum_narrow_notch_boost_db", 0.0)) <= 3.01
            for graph in (left_graph, right_graph)
        ),
    }
    core_target_pass = (
        all(core_checks.values())
        and not reused_measurements
        and all(item is None or item.get("pass") for item in target_fit_required.values())
    )
    crossover_required = bool(state["mode"] in SEPARATE_WOOFER_MODES)
    if not crossover_required:
        crossover_sum_pass: bool | None = None
        crossover_sum_status = crossover_summary.get("status")
    elif state.get("mode") in PREMEASURED_SUM_MODES:
        crossover_sum_pass = bool(
            premeasured_sum_model
            and premeasured_sum_model.get("pass")
            and crossover_summary.get("safe_deploy_pass")
        )
        if crossover_sum_pass:
            precise_phase_pass = bool(
                (premeasured_sum_model or {}).get("phase_verification_status") == "pass"
                and crossover_summary.get("phase_verification_status") == "pass"
            )
            crossover_sum_status = "pass_premeasured_complex_model" if precise_phase_pass else "pass_safe_sum_phase_limited"
        else:
            crossover_sum_status = str(
                (premeasured_sum_model or {}).get("status")
                if not (premeasured_sum_model or {}).get("pass")
                else crossover_summary.get("status") or "fail_model"
            )
    else:
        # The independently measured L/R/W responses provide a deployable
        # conservative upper-bound prediction even when U7 playback and UMIK
        # capture do not provide a reliable shared phase reference. Precision
        # mode adds physical L+W/R+W captures before the build.
        prediction_status = str(crossover_summary.get("status") or "")
        crossover_sum_pass = bool(crossover_summary.get("safe_deploy_pass"))
        crossover_sum_status = (
            ("pass_independent_complex_model" if crossover_summary.get("phase_verification_status") == "pass" else "pass_safe_upper_phase_limited")
            if crossover_sum_pass else (prediction_status or "fail_model")
        )
    overall_pass = core_target_pass and (
        not crossover_required or crossover_sum_pass is True
    ) and (
        state.get("mode") not in PREMEASURED_SUM_MODES
        or bool(premeasured_sum_model and premeasured_sum_model.get("pass"))
    )
    spatial_aggregation = {
        "method": "noise-confidence weighted acoustic transfer power mean",
        "left": left_response.get("spatial_aggregation"),
        "right": right_response.get("spatial_aggregation"),
        "woofer": woofer_response.get("spatial_aggregation") if state["mode"] in SEPARATE_WOOFER_MODES else None,
    }
    legacy_response_count = sum(
        int((item or {}).get("legacy_response_count", 0))
        for item in (spatial_aggregation["left"], spatial_aggregation["right"], spatial_aggregation["woofer"])
        if isinstance(item, dict)
    )
    spatial_aggregation["legacy_response_count"] = legacy_response_count
    spatial_aggregation["raw_reprocess_recommended"] = legacy_response_count > 0
    diagnostics = {
        "lr_median_difference_db": round(statistics.median(lr_differences), 2) if lr_differences else None,
        "spatial_std_median_db": round(statistics.median(all_variation), 2) if all_variation else None,
        "spatial_high_variance_percent": round(100.0 * sum(value >= 6.0 for value in all_variation) / len(all_variation), 1) if all_variation else None,
        "measurement_snr_min_db": round(min(measurement_snrs), 2) if measurement_snrs else None,
        "measurement_snr_median_db": round(statistics.median(measurement_snrs), 2) if measurement_snrs else None,
        "measurement_peak_max_dbfs": round(max(measurement_peaks), 2) if measurement_peaks else None,
        "warnings": [],
    }
    if diagnostics["lr_median_difference_db"] is not None and diagnostics["lr_median_difference_db"] > 4.0:
        diagnostics["warnings"].append("L/R 차이가 큽니다. 마이크 중심과 스피커 거리·toe-in을 확인하세요.")
    if diagnostics["spatial_high_variance_percent"] is not None and diagnostics["spatial_high_variance_percent"] > 15.0:
        diagnostics["warnings"].append("위치별 편차가 큰 대역이 많아 boost를 강하게 제한했습니다.")
    if diagnostics["measurement_snr_min_db"] is not None and diagnostics["measurement_snr_min_db"] < 15.0:
        diagnostics["warnings"].append("일부 sweep SNR이 권장 15 dB보다 낮습니다. 실제 측정에서는 레벨 또는 sweep 시간을 올리세요.")
    if legacy_response_count:
        diagnostics["warnings"].append(
            f"응답 {legacy_response_count}개가 이전 dB-domain smoothing 결과입니다. 원본 WAV는 유지되므로 "
            "3 · 위치 측정의 ‘원본 재계산’을 실행하면 새 power-domain smoothing을 소리 없이 적용할 수 있습니다."
        )
    if (
        high_frequency_compensation["ceiling_reached"]
        and high_frequency_compensation["worst_abs_residual_db_15_20khz"] > 3.0
    ):
        diagnostics["warnings"].append(
            f"15–20 kHz 잔여 오차가 최대 {high_frequency_compensation['worst_abs_residual_db_15_20khz']:.1f} dB입니다. "
            f"‘최대 상대 보상’ {max_boost_db} dB를 이미 모두 사용했습니다. 더 평탄하게 하려면 한도를 올려야 하지만 전체 재생 음량도 같은 방향으로 더 낮아집니다."
        )
    if reused_measurements:
        diagnostics["warnings"].append("하나 이상의 위치 응답이 다른 위치 측정을 재사용했습니다. 기능 시험에는 쓸 수 있지만 3위치 acoustic 검증으로 판정하지 않습니다.")
    if positions_total == 1:
        diagnostics["warnings"].append("빠른 측정 1위치 결과입니다. 기준 청취점은 보정하지만 위치 이동에 따른 공간 안정성은 검증하지 않았습니다.")
    if not all(item is None or item.get("applicable") is False or item.get("pass") for item in target_fit.values()):
        diagnostics["warnings"].append("안전 제한 때문에 일부 채널이 선택 타겟을 허용 오차 안에서 완전히 달성하지 못했습니다.")
    if time_alignment.get("requested") and not time_alignment.get("enabled") and not time_alignment.get("reliable"):
        diagnostics["warnings"].append(f"프런트/우퍼 시간 정렬 미적용: {time_alignment.get('reason', '신뢰도 부족')}")
    if state["mode"] == "lrw":
        diagnostics["warnings"].append(
                "크로스오버 ON/OFF와 관계없이 L/R/우퍼 응답으로 전체 합산을 예측하고 cut-only 가드를 WAV에 반영했습니다. 별도 사후 스윕은 필수가 아니며, 물리 L+우퍼/R+우퍼 합산까지 확인하려면 다음 세션에서 ‘정밀 분리+합산’을 선택하세요."
        )
    elif state["mode"] in PREMEASURED_SUM_MODES:
        if crossover_sum_pass:
            diagnostics["warnings"].append(
                "정밀 측정의 L+우퍼/R+우퍼가 개별 L/R/우퍼 합산 모델과 일치해 추가 사후 스윕 없이 합산 안전성 검증을 통과했습니다."
            )
        elif premeasured_sum_model and premeasured_sum_model.get("pass"):
            diagnostics["warnings"].append(
                "필터 전 복소 합산 모델은 PASS했지만 현재 4단계 타깃/트림/억제/크로스오버 조합의 최종 합산 예측이 허용 오차를 벗어났습니다. 4단계 설정을 조정해 다시 계산하세요."
            )
        else:
            diagnostics["warnings"].append(
                f"정밀 합산 모델 미통과: {(premeasured_sum_model or {}).get('action', '3단계 측정을 확인하세요.')}"
            )
    long_bass_decay = [
        value for channels in decay_summary.values() if isinstance(channels, dict)
        for frequency, value in channels.items() if float(frequency) <= 125.0 and value > 0.70
    ]
    if long_bass_decay:
        diagnostics["warnings"].append("125 Hz 이하 잔향이 길어 해당 공진 대역에 최대 3 dB cut-only 감쇄를 적용했습니다.")
    acquisition_revision = state.get("measurement_acquisition_revision")
    unity_acquisition = acquisition_revision == "u7-pcm-unity-v1"
    result = {
        "algorithm_revision": RESULT_ALGORITHM_REVISION,
        "measurement_output_reference": {
            "acquisition_revision": acquisition_revision or "legacy-listening-volume-dependent",
            "u7_pcm_hardware_gain_db": MEASUREMENT_OUTPUT_GAIN_DB if unity_acquisition else None,
            "listening_volume_ignored_during_sweep": unity_acquisition,
            "safety_order": (
                ["input_off", "u7_pcm_unity", "sweep", "restore_volume", "input_on"]
                if unity_acquisition else None
            ),
        },
        "preset": preset,
        "target": target_name,
        "woofer_trim_db": woofer_trim_db,
        "woofer_level_control": {
            "measurement_attenuation_compensated": state["mode"] in SEPARATE_WOOFER_MODES,
            "reference": "Front L/R spatial response median at 500-2000 Hz" if state["mode"] in SEPARATE_WOOFER_MODES else "measured combined system response",
            "shared_front_reference_db": round(shared_front_reference_db, 3),
            "automatic_target_cut_median_db_40_120": rear_graph.get("automatic_target_cut_median_db_40_120") if state["mode"] in SEPARATE_WOOFER_MODES and isinstance(rear_graph, dict) else None,
            "automatic_boost_allowed": False,
            "processing_order": ["target-relative automatic cut", "bass-control preset", "user woofer trim", "optional embedded LR4 crossover", "mandatory joint cut-only sum guard for separate Front/Woofer"],
        },
        "crossover": crossover_summary,
        "phase_mode": phase_mode,
        "phase_cutoff_hz": phase_cutoff if phase_mode == "bass" else None,
        "spatial_mode": spatial_mode,
        "measurement_coverage": {
            "positions": positions_total,
            "mode": "fast_single_position" if positions_total == 1 else "standard_three_position",
            "spatial_stability_applicable": positions_total == POSITIONS,
            "spatial_stability_pass": None if positions_total == 1 else not reused_measurements,
            "note": "기준점 최적화; 위치 이동 시 과보정 가능" if positions_total == 1 else "중앙+좌우 공통 문제 보정; 저역/crossover/좌석 안정성 권장",
        },
        "position_weights": left_response["position_weights"],
        "smoothing": left_response["smoothing"],
        "spatial_aggregation": spatial_aggregation,
        "preference": {"bass_db_at_20_hz": bass_tilt_db, "treble_db_at_20_khz": treble_tilt_db},
        "correction_limits": {
            "low_hz": correction_low_hz,
            "high_hz": correction_high_hz,
            "max_room_boost_db": max_boost_db,
            "max_relative_compensation_db": max_boost_db,
            "max_room_cut_db": max_cut_db,
            "semantics": "highest trusted correction becomes 0 dB; one common attenuation is applied to the complete L/R/Woofer bank",
        },
        "front": front.name,
        "rear": rear.name if rear else None,
        "taps": TAPS,
        "sample_rate": RATE,
        "fft_backend": fft.kind,
        "front_sha256": sha256(front),
        "rear_sha256": sha256(rear) if rear else None,
        "front_metrics": front_metrics,
        "rear_metrics": rear_metrics,
        "time_alignment": time_alignment,
        "filter_bank_normalization": bank_normalization,
        "common_level_reference": common_level_reference,
        "high_frequency_compensation": high_frequency_compensation,
        "stereo_broad_rolloff_corroboration": stereo_rolloff_summary,
        "integration": premeasured_sum_model or integration_summary(
            directory,
            state.get("validation"),
            measured_woofer_attenuation_db,
        ),
        "measurement_output": {
            "mode": state.get("mode"),
            "physical_profile": state.get("measurement_profile"),
            "physical_label": (state.get("measurement_output") or {}).get("label"),
            "signal_path": (
                "L+Woofer / R+Woofer simultaneous"
                if state.get("mode") == "lr" else
                "Front L / Front R / Woofer separately plus premeasured L+Woofer / R+Woofer model closure"
                if state.get("mode") in PREMEASURED_SUM_MODES else
                "Front L / Front R / Woofer separately"
            ),
            "white_noise_level_dbfs": int(state.get("noise_level_dbfs", state.get("level_dbfs", DEFAULT_NOISE_LEVEL_DBFS))),
            "sweep_level_dbfs": int(state.get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS)),
            "woofer_relative_level_db": measured_woofer_attenuation_db,
            "effective_woofer_sweep_dbfs": int(state.get("level_dbfs", DEFAULT_SWEEP_LEVEL_DBFS)) + measured_woofer_attenuation_db,
            "woofer_level_semantics": (
                "measured system balance and final runtime trim"
                if state.get("mode") == "lr"
                else "reference-compensated measurement attenuation; affects SNR, not recovered transfer magnitude"
            ),
        },
        "diagnostics": diagnostics,
        "room_decay": {
            "t20_rt60_s_by_channel": decay_summary,
            "policy": "신뢰 가능한 300 Hz 이하 장시간 공진만 최대 3 dB 추가 감쇄; late reverb 역보정 없음",
        },
        "self_validation": {
            "overall_pass": overall_pass,
            "core_checks": core_checks,
            "independent_positions": {
                "pass": not reused_measurements,
                "reused_measurements": reused_measurements,
                "positions": positions_total,
                "spatial_stability_applicable": positions_total == POSITIONS,
            },
            "target_fit": target_fit,
            "premeasured_sum_model": premeasured_sum_model,
            "crossover_sum": {
                "required": crossover_required,
                "pass": crossover_sum_pass,
                "status": crossover_sum_status,
                "prediction_status": crossover_summary.get("status"),
                "verification": "premeasured_complex_model" if state.get("mode") in PREMEASURED_SUM_MODES else "independent_same_clock_complex_model" if crossover_required else "not_applicable",
            },
            "fir_implementation": implementation,
            "measurement_snr_db": {
                "minimum": diagnostics["measurement_snr_min_db"],
                "median": diagnostics["measurement_snr_median_db"],
                "maximum_peak_dbfs": diagnostics["measurement_peak_max_dbfs"],
                "recommended_minimum": 15.0,
            },
        },
        "graphs": {"left": left_graph, "right": right_graph, "woofer": rear_graph},
        "bass_reference": {
            "primus360": "96 Hz -7 dB, Q 3",
            "strong": "Primus360 + 140 Hz low shelf -9 dB + 63 Hz -5 dB",
        },
        "algorithm": {
            "prototype": "noise-confidence weighted mean-square acoustic transfer response; exact power-domain spatial and fractional-octave aggregation",
            "regularization": "frequency-dependent weighted spatial variance, narrow-null reliability, stereo broad-rolloff corroboration and bounded relative compensation",
            "rolloff_guard": "half-octave median -10 dB natural usable-band estimator; only L/R-corroborated broad roll-off may extend through the edge",
            "phase": "minimum phase; optional low-frequency excess phase with acausality limit",
            "target": "named target plus optional bass/treble house-curve preference",
            "verification": "actual 32768-tap FIR FFT, one common L/R/Woofer reference target-fit error, relative-level preservation, narrow-null and relative-compensation limits, absolute non-normalized premeasured complex-sum closure, transfer/causality/SNR invariants",
            "reverberation": "octave-band noise-compensated Schroeder EDT/T20; reliable low-frequency decay controls cut-only damping",
            "crossover": "embedded LR4 Front HPF/Woofer LPF plus measured-position coherent upper-envelope cut guard; precision mode validates H(L/R)+H(W) against premeasured physical sums; no extra runtime filter stage or block latency",
        },
    }
    result["room_tuning_audit"] = build_room_tuning_audit(directory, state, mimo=False)
    for audit_item in result["room_tuning_audit"]:
        if audit_item.get("id") == "crossover_integration":
            audit_item["status"] = crossover_sum_status if state.get("mode") in SEPARATE_WOOFER_MODES else "not_applicable"
            audit_item["evidence"] = {
                "enabled": crossover_summary.get("enabled"),
                "frequency_hz": crossover_summary.get("frequency_hz"),
                "embedded_in_fir": crossover_summary.get("embedded_in_fir"),
                "additional_block_latency_samples": crossover_summary.get("additional_block_latency_samples", 0),
                "phase_alignment_reliable": crossover_summary.get("phase_alignment_reliable"),
                "coherent_upper_guard_pass": crossover_summary.get("coherent_upper_guard_pass"),
                "complex_sum_target_pass": crossover_summary.get("complex_sum_target_pass"),
                "safe_deploy_pass": crossover_summary.get("safe_deploy_pass"),
                "phase_verification_status": crossover_summary.get("phase_verification_status"),
                "premeasured_sum_model": premeasured_sum_model,
            }
            if state.get("mode") in PREMEASURED_SUM_MODES:
                audit_item["action"] = (
                    "3단계에서 위치별 L/R/우퍼와 L+우퍼/R+우퍼를 모두 측정하고 절대 복소 합산 모델을 검증. "
                    "통과 시 WAV 내장 LR4/guard 예측을 같은 모델로 판정하므로 별도 사후 sweep을 요구하지 않음"
                )
            elif state.get("mode") in SEPARATE_WOOFER_MODES:
                audit_item["action"] = "같은 clock의 L/R/W 절대 복소응답으로 WAV 내장 LR4 HPF/LPF와 cut-only 합산 guard를 계산. 별도 사후 sweep은 필수가 아니며, 물리 합산 model closure가 필요하면 다음 Session에서 정밀 분리+합산을 선택"
            else:
                audit_item["action"] = "독립 프런트/우퍼 출력이 없어 디지털 크로스오버를 적용하지 않음"
    result["report_json"] = "Room_Tuning_Report.json"
    result["report_md"] = "Room_Tuning_Report.md"
    atomic_json(directory / result["report_json"], result)
    write_room_tuning_report(directory / result["report_md"], state, result)
    state = update_current(state="built", stage="32768탭 FIR 생성 완료 · 그래프 확인 후 프로필에 적용하세요.", progress=100.0, eta_seconds=None, worker_pid=None, result=result)
    atomic_json(directory / "session.json", state)


def validate_result_profile(state: dict[str, Any], profile: str) -> None:
    """Keep a measured result attached to the physical output that produced it."""
    if profile not in OUTPUT_PROFILE_LABELS:
        raise MeasurementError("프로필이 잘못되었습니다.")
    measured_profile = state.get("measurement_profile")
    if measured_profile in OUTPUT_PROFILE_LABELS and profile != measured_profile:
        raise MeasurementError(
            f"이 FIR은 {OUTPUT_PROFILE_LABELS[measured_profile]}에서 측정되었습니다. "
            f"{OUTPUT_PROFILE_LABELS[profile]}에는 적용할 수 없습니다."
        )


def validate_result_revision(result: dict[str, Any]) -> None:
    """Do not audition or apply output produced by superseded safety rules."""
    if result.get("algorithm_revision") != RESULT_ALGORITHM_REVISION:
        raise MeasurementError("보정 알고리즘이 변경되었습니다. 저장된 측정값으로 FIR을 다시 계산하세요.")


def apply_result(profile: str) -> dict[str, Any]:
    state = load_current()
    if state.get("state") != "built" or not state.get("result"):
        raise MeasurementError("먼저 FIR을 생성하세요.")
    validate_result_revision(state["result"])
    if not state["result"].get("self_validation", {}).get("overall_pass"):
        raise MeasurementError("FIR 자체 검증 또는 타겟/합산 검증을 통과하지 않아 정식 적용을 차단했습니다.")
    validate_result_profile(state, profile)
    directory = Path(state["session_dir"])
    if state["result"].get("kind") == "mimo_2x4":
        if profile != "speaker":
            raise MeasurementError("MIMO 2×4 결과는 Speaker 출력에만 적용할 수 있습니다.")
        arguments = [PYTHON, MANAGER, "install-mimo", profile, str(directory / state["result"]["mimo_manifest"])]
        process = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise MeasurementError(process.stdout.strip() or "MIMO 적용 실패") from exc
        if process.returncode:
            raise MeasurementError(payload.get("error", "MIMO 적용 실패"))
        update_current(stage="speaker 프로필에 MIMO bank 정식 적용 완료", applied_profile=profile, preview_active=False)
        return payload
    front = directory / state["result"]["front"]
    rear_name = state["result"].get("rear")
    arguments = [PYTHON, MANAGER, "install-pair", profile, str(front)]
    if rear_name:
        arguments.append(str(directory / rear_name))
    # Separate Rear WAVs contain their trim. Shared-filter results use the
    # runtime copy mixer so the measured L+Woofer relationship is preserved.
    arguments += ["--woofer-trim", str(0 if rear_name else int(state["result"].get("woofer_trim_db", 0)))]
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(result.stdout.strip() or "프로필 적용 실패") from exc
    if result.returncode:
        raise MeasurementError(payload.get("error", "프로필 적용 실패"))
    update_current(stage=f"{profile} 프로필에 정식 적용 완료", applied_profile=profile, preview_active=False)
    return payload


def preview_result(profile: str) -> dict[str, Any]:
    state = load_current()
    if state.get("state") != "built" or not state.get("result"):
        raise MeasurementError("먼저 FIR을 생성하세요.")
    validate_result_revision(state["result"])
    validate_result_profile(state, profile)
    if state.get("measurement_profile") in OUTPUT_PROFILE_LABELS:
        ensure_measurement_output_path(state)
    directory = Path(state["session_dir"])
    if state["result"].get("kind") == "mimo_2x4":
        if profile != "speaker":
            raise MeasurementError("MIMO 2×4 테스트는 Speaker 출력에서만 가능합니다.")
        arguments = [PYTHON, MANAGER, "preview-mimo", profile, str(directory / state["result"]["mimo_manifest"])]
        process = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise MeasurementError(process.stdout.strip() or "MIMO 테스트 적용 실패") from exc
        if process.returncode:
            raise MeasurementError(payload.get("error", "MIMO 테스트 적용 실패"))
        update_current(stage="speaker · 이번 MIMO 튜닝 테스트 중", preview_active=True, preview_profile=profile)
        return payload
    front = directory / state["result"]["front"]
    arguments = [PYTHON, MANAGER, "preview-pair", profile, str(front)]
    rear_name = state["result"].get("rear")
    if rear_name:
        arguments.append(str(directory / rear_name))
    arguments += ["--woofer-trim", str(0 if rear_name else int(state["result"].get("woofer_trim_db", 0)))]
    process = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(process.stdout.strip() or "테스트 적용 실패") from exc
    if process.returncode:
        raise MeasurementError(payload.get("error", "테스트 적용 실패"))
    update_current(stage=f"{profile} · 이번 튜닝 테스트 중", preview_active=True, preview_profile=profile)
    return payload


def restore_result() -> dict[str, Any]:
    process = subprocess.run([PYTHON, MANAGER, "restore-profile"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(process.stdout.strip() or "기존 튜닝 복귀 실패") from exc
    if process.returncode:
        raise MeasurementError(payload.get("error", "기존 튜닝 복귀 실패"))
    update_current(stage="기존 정식 튜닝으로 복귀", preview_active=False, preview_profile=None)
    return payload


def cancel() -> dict[str, Any]:
    state = load_current()
    if state.get("state") not in ("running", "processing", "cancelling"):
        raise MeasurementError("취소할 측정 작업이 실행 중이 아닙니다.")
    state["cancel_requested"] = True
    state["state"] = "cancelling"
    state["stage"] = "현재 재생/녹음 구간이 끝난 뒤 취소합니다."
    save_current(state)
    return state


def worker_guard(action) -> int:
    previous_handlers: dict[int, Any] = {}

    def request_safe_stop(signum: int, _frame: Any) -> None:
        raise MeasurementError(f"측정 worker 종료 신호({signum})를 받아 안전 복원 후 중단합니다.")

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_safe_stop)
        # The parent records our PID while holding the state lock. Waiting here
        # closes the launch race without delaying normal workers noticeably.
        for _ in range(100):
            if load_current().get("worker_pid") == os.getpid():
                break
            time.sleep(0.02)
        else:
            raise MeasurementError("측정 worker 시작 상태를 확인할 수 없습니다.")
        action()
        return 0
    except Exception as exc:
        state = load_current()
        state.update({"state": "error", "stage": "측정 작업 오류", "error": str(exc), "eta_seconds": None, "worker_pid": None, "active_pids": []})
        save_current(state)
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def self_test() -> dict[str, Any]:
    fft = FFTBackend()
    length = 1024
    values = [math.sin(2 * math.pi * 17 * index / length) for index in range(length)]
    spectrum = fft.rfft(values, length)
    restored = fft.irfft(spectrum, length)
    error = max(abs(a - b) for a, b in zip(values, restored))
    if error > 2e-5:
        raise MeasurementError(f"FFTW round-trip error: {error}")
    for preset in ("none", "primus360", "strong"):
        values_db = [bass_modifier_db(frequency, preset) for frequency in (20, 63, 96, 140, 1000)]
        if max(values_db) > 1e-4:
            raise MeasurementError(f"Bass preset contains boost: {preset}")
    return {"result": "PASS", "fft_backend": fft.kind, "roundtrip_error": error, "rate": RATE, "taps": TAPS}


def target_matrix_self_test() -> dict[str, Any]:
    """Exercise targets, preferences and crossover invariants without playback."""
    fft = FFTBackend()
    frequencies = [20.0 * (1000.0 ** (index / 511.0)) for index in range(512)]
    measured = [
        1.6 * math.sin(math.log(max(frequency, 20.0) / 20.0) * 2.1)
        + 2.5 * math.exp(-0.5 * (math.log2(frequency / 95.0) / 0.22) ** 2)
        - 1.4 * math.exp(-0.5 * (math.log2(frequency / 3100.0) / 0.35) ** 2)
        for frequency in frequencies
    ]
    variation = [0.5 + 0.8 * math.exp(-0.5 * (math.log2(frequency / 80.0) / 0.7) ** 2) for frequency in frequencies]
    phase = [0.0] * len(frequencies)
    matrix = []
    all_pass = True
    for target_name in TARGET_FILES:
        target_f, target_db = target_curve(target_name)
        shared_measure_reference = statistics.median(
            value for frequency, value in zip(frequencies, measured) if 500.0 <= frequency <= 2_000.0
        )
        shared_target_reference = statistics.median(
            interpolate_log(target_f, target_db, frequency)
            for frequency in frequencies if 500.0 <= frequency <= 2_000.0
        )
        for preset in ("none", "primus360", "strong"):
            front, front_graph = design_channel(
                frequencies, measured, variation, phase, target_name, preset,
                woofer=False, woofer_trim_db=0, phase_mode="magnitude", phase_cutoff=200,
                crossover_role="highpass", crossover_frequency_hz=100,
                shared_reference_measure_db=shared_measure_reference,
                shared_reference_target_db=shared_target_reference, fft=fft,
            )
            woofer, woofer_graph = design_channel(
                frequencies, measured, variation, phase, target_name, preset,
                woofer=True, woofer_trim_db=0, phase_mode="magnitude", phase_cutoff=200,
                crossover_role="lowpass", crossover_frequency_hz=100,
                shared_reference_measure_db=shared_measure_reference,
                shared_reference_target_db=shared_target_reference,
                fft=fft,
            )
            bank, normalization = normalize_fir_bank([front, front, woofer, woofer], fft)
            front, woofer = bank[0], bank[2]
            front_graph = finalize_graph_with_fir(front_graph, front, fft)
            woofer_graph = finalize_graph_with_fir(woofer_graph, woofer, fft)
            apply_common_graph_reference(front_graph, front_graph, woofer_graph)
            front_metrics = fir_metrics([front, front], fft)["left"]
            woofer_metrics = fir_metrics([woofer, woofer], fft)["left"]
            front_pass = bool(
                front_metrics["finite"] and front_metrics["transfer_pass"]
                and front_metrics["early_impulse_pass"]
                and front_graph["fir_implementation"]["pass"]
                and front_graph["target_fit"]["pass"]
            )
            woofer_pass = bool(
                woofer_metrics["finite"] and woofer_metrics["transfer_pass"]
                and woofer_metrics["early_impulse_pass"]
                and woofer_graph["fir_implementation"]["pass"]
                and woofer_graph["target_fit"].get("applicable") is False
            )
            all_pass = all_pass and front_pass and woofer_pass and normalization["relative_branch_gain_preserved"]
            matrix.append({
                "target": target_name,
                "preset": preset,
                "channels": {
                    "front": {
                        "pass": front_pass,
                        "target_fit": front_graph["target_fit"],
                        "fir_implementation": front_graph["fir_implementation"],
                        "maximum_transfer_db": front_metrics["maximum_transfer_db"],
                        "peak_tap": front_metrics["peak_tap"],
                    },
                    "woofer": {
                        "pass": woofer_pass,
                        "target_fit": woofer_graph["target_fit"],
                        "branch_level_diagnostic": woofer_graph.get("branch_level_diagnostic"),
                        "fir_implementation": woofer_graph["fir_implementation"],
                        "maximum_transfer_db": woofer_metrics["maximum_transfer_db"],
                        "peak_tap": woofer_metrics["peak_tap"],
                    },
                },
                "bank_normalization": normalization,
            })

    algebra_frequencies = [20.0, 40.0, 60.0, 80.0, 100.0, 120.0, 200.0, 1_000.0, 20_000.0]
    lr4_errors = [
        abs(
            linkwitz_riley_4_magnitude(frequency, 100.0, "highpass")
            + linkwitz_riley_4_magnitude(frequency, 100.0, "lowpass")
            - 1.0
        )
        for frequency in algebra_frequencies
    ]
    lr4_pass = max(lr4_errors) <= 1.0e-12
    all_pass = all_pass and lr4_pass

    target_semantics = []
    for target_name in TARGET_FILES:
        target_f, target_db = target_curve(target_name)
        raw = [interpolate_log(target_f, target_db, frequency) for frequency in frequencies]
        raw_reference = statistics.median(
            value for frequency, value in zip(frequencies, raw) if 500.0 <= frequency <= 2_000.0
        )
        expected_baseline = [value - raw_reference for value in raw]
        for preset in ("none", "primus360", "strong"):
            for trim_db in (0, -4, -9):
                result = {
                    "target": target_name,
                    "preset": preset,
                    "woofer_trim_db": trim_db,
                    "preference": {"bass_db_at_20_hz": 0, "treble_db_at_20_khz": 0},
                    "crossover": {"enabled": True, "frequency_hz": 100},
                }
                effective = effective_combined_target(result, frequencies)
                baseline_error = (
                    max(abs(left - right) for left, right in zip(effective, expected_baseline))
                    if preset == "none" and trim_db == 0 else None
                )
                passed = all(math.isfinite(value) for value in effective) and (
                    baseline_error is None or baseline_error <= 1.0e-9
                )
                all_pass = all_pass and passed
                target_semantics.append({
                    "target": target_name,
                    "preset": preset,
                    "woofer_trim_db": trim_db,
                    "pass": passed,
                    "baseline_target_max_error_db": baseline_error,
                })

    preference_anchors = {
        "bass_at_20_hz": preference_modifier_db(20.0, 3, -2),
        "treble_at_20_khz": preference_modifier_db(20_000.0, 3, -2),
        "window_at_20_hz": tone_preference_window(20.0),
        "window_at_20_khz": tone_preference_window(20_000.0),
    }
    preference_pass = (
        abs(preference_anchors["bass_at_20_hz"] - 3.0) <= 1.0e-9
        and abs(preference_anchors["treble_at_20_khz"] + 2.0) <= 1.0e-9
        and preference_anchors["window_at_20_hz"] == 1.0
        and preference_anchors["window_at_20_khz"] == 1.0
    )
    all_pass = all_pass and preference_pass

    # Exercise every selectable SISO correction value with an actual 32768-tap
    # Front/Woofer FIR pair.  A scenario may be target-limited by safe boost,
    # cut or natural extension; that is a valid *rejection* only when the WAV
    # remains structurally safe and the target-fit field reports the limit.
    option_values: dict[str, tuple[Any, ...]] = {
        "target": tuple(TARGET_FILES),
        "preset": ("none", "primus360", "strong"),
        "woofer_trim_db": tuple(range(0, -19, -1)),
        "phase_mode": ("magnitude", "bass"),
        "phase_cutoff": PHASE_CUTOFFS,
        "spatial_mode": ("equal", "center"),
        "bass_tilt_db": tuple(range(-6, 7)),
        "treble_tilt_db": tuple(range(-6, 3)),
        "correction_low_hz": (20, 30, 40, 60, 80),
        "correction_high_hz": (300, 500, 1000, 5000, 20_000),
        "max_boost_db": (0, 3, 6, 9, 10),
        "max_cut_db": (6, 9, 12, 18, 24),
        "crossover_enabled": (False, True),
        "crossover_frequency_hz": CROSSOVER_FREQUENCIES,
    }
    baseline_options: dict[str, Any] = {
        "target": "flat", "preset": "none", "woofer_trim_db": 0,
        "phase_mode": "magnitude", "phase_cutoff": 200, "spatial_mode": "equal",
        "bass_tilt_db": 0, "treble_tilt_db": 0,
        "correction_low_hz": 20, "correction_high_hz": 20_000,
        "max_boost_db": 10, "max_cut_db": 18,
        "crossover_enabled": True, "crossover_frequency_hz": 100,
    }
    scenario_requests: list[tuple[str, str, Any, dict[str, Any]]] = []
    for option, values in option_values.items():
        for value in values:
            updates = {option: value}
            if option == "phase_cutoff":
                updates["phase_mode"] = "bass"
            scenario_requests.append(("single_value", option, value, updates))
    for target_name in TARGET_FILES:
        for preset in ("none", "primus360", "strong"):
            scenario_requests.append(("target_x_preset", "target_preset", f"{target_name}/{preset}", {"target": target_name, "preset": preset}))
    for preset in ("none", "primus360", "strong"):
        for trim_db in (0, -4, -9, -18):
            scenario_requests.append(("preset_x_trim_boundary", "preset_trim", f"{preset}/{trim_db}", {"preset": preset, "woofer_trim_db": trim_db}))
    for crossover_hz in CROSSOVER_FREQUENCIES:
        for phase_mode in ("magnitude", "bass"):
            scenario_requests.append(("crossover_x_phase", "crossover_phase", f"{crossover_hz}/{phase_mode}", {"crossover_frequency_hz": crossover_hz, "phase_mode": phase_mode}))

    unique_scenarios: dict[str, tuple[str, str, Any, dict[str, Any]]] = {}
    for family, option, value, updates in scenario_requests:
        settings = dict(baseline_options)
        settings.update(updates)
        signature = json.dumps(settings, sort_keys=True)
        unique_scenarios.setdefault(signature, (family, option, value, settings))

    option_matrix = []
    shared_measure_reference = statistics.median(
        value for frequency, value in zip(frequencies, measured) if 500.0 <= frequency <= 2_000.0
    )
    for family, option, value, settings in unique_scenarios.values():
        target_f_selected, target_db_selected = target_curve(str(settings["target"]))
        shared_target_reference = statistics.median(
            interpolate_log(target_f_selected, target_db_selected, frequency)
            + preference_modifier_db(frequency, int(settings["bass_tilt_db"]), int(settings["treble_tilt_db"]))
            for frequency in frequencies if 500.0 <= frequency <= 2_000.0
        )
        front_role = "highpass" if settings["crossover_enabled"] else None
        woofer_role = "lowpass" if settings["crossover_enabled"] else None
        design_common = {
            "spatial_mode": settings["spatial_mode"],
            "bass_tilt_db": settings["bass_tilt_db"],
            "treble_tilt_db": settings["treble_tilt_db"],
            "correction_low_hz": settings["correction_low_hz"],
            "correction_high_hz": settings["correction_high_hz"],
            "max_boost_db": settings["max_boost_db"],
            "max_cut_db": settings["max_cut_db"],
        }
        front_ir, front_graph = design_channel(
            frequencies, measured, variation, phase, settings["target"], settings["preset"],
            woofer=False, woofer_trim_db=0, phase_mode=settings["phase_mode"],
            phase_cutoff=settings["phase_cutoff"], crossover_role=front_role,
            crossover_frequency_hz=settings["crossover_frequency_hz"],
            shared_reference_measure_db=shared_measure_reference,
            shared_reference_target_db=shared_target_reference,
            fft=fft, **design_common,
        )
        woofer_ir, woofer_graph = design_channel(
            frequencies, measured, variation, phase, settings["target"], settings["preset"],
            woofer=True, woofer_trim_db=settings["woofer_trim_db"], phase_mode=settings["phase_mode"],
            phase_cutoff=settings["phase_cutoff"], crossover_role=woofer_role,
            crossover_frequency_hz=settings["crossover_frequency_hz"],
            shared_reference_measure_db=shared_measure_reference,
            shared_reference_target_db=shared_target_reference,
            fft=fft, **design_common,
        )
        bank, bank_normalization = normalize_fir_bank([front_ir, front_ir, woofer_ir, woofer_ir], fft)
        front_ir, woofer_ir = bank[0], bank[2]
        front_graph = finalize_graph_with_fir(front_graph, front_ir, fft)
        woofer_graph = finalize_graph_with_fir(woofer_graph, woofer_ir, fft)
        apply_common_graph_reference(front_graph, front_graph, woofer_graph)
        front_metrics = fir_metrics([front_ir, front_ir], fft)["left"]
        woofer_metrics = fir_metrics([woofer_ir, woofer_ir], fft)["left"]
        core_pass = bool(
            len(front_ir) == TAPS and len(woofer_ir) == TAPS
            and front_metrics["finite"] and woofer_metrics["finite"]
            and front_metrics["transfer_pass"] and woofer_metrics["transfer_pass"]
            and front_metrics["early_impulse_pass"] and woofer_metrics["early_impulse_pass"]
            and front_graph["fir_implementation"]["pass"] and woofer_graph["fir_implementation"]["pass"]
            and bank_normalization["relative_branch_gain_preserved"]
        )
        semantic_checks = {
            "target_curve_finite": bool(front_graph.get("target_db")) and all(math.isfinite(item) for item in front_graph["target_db"]),
            "preset_curve_finite": all(math.isfinite(bass_modifier_db(frequency, settings["preset"])) for frequency in (20.0, 40.0, 63.0, 96.0, 140.0, 300.0)),
            "woofer_trim_recorded": woofer_graph.get("woofer_trim_db") == settings["woofer_trim_db"],
            "spatial_mode_recorded": front_graph.get("spatial_mode") == settings["spatial_mode"],
            "correction_band_recorded": front_graph.get("correction_band_hz") == [settings["correction_low_hz"], settings["correction_high_hz"]],
            "boost_limit_recorded": front_graph.get("max_room_boost_db") == settings["max_boost_db"],
            "cut_limit_recorded": front_graph.get("max_room_cut_db") == settings["max_cut_db"],
            "bass_anchor": abs(preference_modifier_db(20.0, settings["bass_tilt_db"], settings["treble_tilt_db"]) - settings["bass_tilt_db"]) <= 1.0e-9,
            "treble_anchor": abs(preference_modifier_db(20_000.0, settings["bass_tilt_db"], settings["treble_tilt_db"]) - settings["treble_tilt_db"]) <= 1.0e-9,
            "preset_cut_only": max(bass_modifier_db(frequency, settings["preset"]) for frequency in (20.0, 40.0, 63.0, 96.0, 140.0, 300.0)) <= 1.0e-6,
        }
        if settings["crossover_enabled"]:
            semantic_checks.update({
                "front_crossover_role": front_graph.get("crossover", {}).get("role") == "highpass",
                "woofer_crossover_role": woofer_graph.get("crossover", {}).get("role") == "lowpass",
                "crossover_frequency_recorded": front_graph.get("crossover", {}).get("frequency_hz") == settings["crossover_frequency_hz"],
                "crossover_minus_6db": abs(crossover_transfer_db(settings["crossover_frequency_hz"], settings["crossover_frequency_hz"], "highpass") + 6.020599913) <= 1.0e-5,
            })
        else:
            semantic_checks["crossover_disabled"] = front_graph.get("crossover", {}).get("enabled") is False and woofer_graph.get("crossover", {}).get("enabled") is False
        target_status = "pass" if front_graph["target_fit"].get("pass") else "expected_safety_limited"
        woofer_80_index = min(range(len(woofer_graph["frequency"])), key=lambda index: abs(woofer_graph["frequency"][index] - 80.0))
        woofer_80_level = float(woofer_graph["effective_target_db"][woofer_80_index])
        scenario_pass = core_pass and all(semantic_checks.values())
        all_pass = all_pass and scenario_pass
        option_matrix.append({
            "family": family, "option": option, "value": value,
            "settings": settings, "pass": scenario_pass,
            "target_status": target_status,
            "front_target_fit": front_graph["target_fit"],
            "woofer_target_fit": woofer_graph["target_fit"],
            "core_pass": core_pass, "semantic_checks": semantic_checks,
            "front_maximum_transfer_db": front_metrics["maximum_transfer_db"],
            "woofer_maximum_transfer_db": woofer_metrics["maximum_transfer_db"],
            "woofer_80hz_effective_target_db": woofer_80_level,
            "bank_normalization": bank_normalization,
        })

    # Explicit monotonic expectations for the controls that intentionally
    # reduce low-frequency output.
    preset_levels = {name: bass_modifier_db(80.0, name) for name in ("none", "primus360", "strong")}
    preset_monotonic = preset_levels["strong"] <= preset_levels["primus360"] <= preset_levels["none"] + 1.0e-9
    trim_response = sorted([
        (
            int(item["settings"]["woofer_trim_db"]),
            float(item["woofer_80hz_effective_target_db"]),
        )
        for item in option_matrix
        if all(item["settings"][key] == value for key, value in baseline_options.items() if key != "woofer_trim_db")
    ], reverse=True)
    trim_monotonic = len(trim_response) == len(option_values["woofer_trim_db"]) and all(
        lower[1] <= upper[1] + 1.0e-6 for upper, lower in zip(trim_response, trim_response[1:])
    )
    all_pass = all_pass and preset_monotonic and trim_monotonic

    phase_impulse, phase_graph = design_channel(
        frequencies, measured, variation, phase, "harman", "strong",
        woofer=False, woofer_trim_db=0, phase_mode="bass", phase_cutoff=200, fft=fft,
    )
    phase_bank, phase_normalization = normalize_fir_bank([phase_impulse, phase_impulse], fft)
    phase_impulse = phase_bank[0]
    phase_graph = finalize_graph_with_fir(phase_graph, phase_impulse, fft)
    apply_common_graph_reference(phase_graph, phase_graph, None)
    phase_metrics = fir_metrics([phase_impulse, phase_impulse], fft)["left"]
    phase_pass = bool(
        phase_metrics["finite"]
        and phase_metrics["transfer_pass"]
        and phase_metrics["early_impulse_pass"]
        and phase_graph["fir_implementation"]["pass"]
    )
    all_pass = all_pass and phase_pass
    guidance_baseline = next(
        (item for item in option_matrix if item.get("settings") == baseline_options),
        None,
    )
    if not guidance_baseline or not guidance_baseline.get("pass"):
        raise MeasurementError("Web FAIL 안내의 SISO 권장 기준 조합을 실제 FIR로 재계산했지만 PASS하지 못했습니다.")
    return {
        "result": "PASS" if all_pass else "FAIL",
        "targets": list(TARGET_FILES),
        "presets": ["none", "primus360", "strong"],
        "combinations": len(matrix),
        "matrix": matrix,
        "target_semantics": {
            "combinations": len(target_semantics),
            "matrix": target_semantics,
            "baseline_rule": "preset none + Woofer trim 0 dB equals the selected full-system target",
        },
        "crossover_algebra": {
            "pass": lr4_pass,
            "maximum_linear_sum_error": max(lr4_errors),
            "rule": "LR4 acoustic HP magnitude + LP magnitude = 1 only with matched polarity, phase and arrival",
        },
        "preference_anchors": {"pass": preference_pass, **preference_anchors},
        "option_value_matrix": {
            "result": "PASS" if all(item["pass"] for item in option_matrix) and preset_monotonic and trim_monotonic else "FAIL",
            "scenarios": len(option_matrix),
            "every_selectable_value": {key: list(values) for key, values in option_values.items()},
            "interaction_families": ["target_x_preset", "preset_x_trim_boundary", "crossover_x_phase"],
            "preset_80hz_db": preset_levels,
            "preset_monotonic_cut": preset_monotonic,
            "trim_80hz_actual_response_db": trim_response,
            "trim_monotonic_cut": trim_monotonic,
            "matrix": option_matrix,
        },
        "failure_guidance_reset": {
            "settings": baseline_options,
            "executed": True,
            "structural_pass": bool(guidance_baseline.get("pass")),
            "front_target_status": guidance_baseline.get("target_status"),
        },
        "bass_phase": {
            "pass": phase_pass,
            "phase": phase_graph["phase"],
            "fir_implementation": phase_graph["fir_implementation"],
            "metrics": phase_metrics,
            "bank_normalization": phase_normalization,
        },
        "rate": RATE,
        "taps": TAPS,
        "fft_backend": fft.kind,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("targets")
    sub.add_parser("list-sessions")
    session_note_parser = sub.add_parser("set-session-note")
    session_note_parser.add_argument("note")
    load_session_parser = sub.add_parser("load-session")
    load_session_parser.add_argument("session_id")
    delete_session_parser = sub.add_parser("delete-session")
    delete_session_parser.add_argument("session_id")
    install = sub.add_parser("install-cal")
    install.add_argument("orientation", choices=("0", "90"))
    install.add_argument("source", type=Path)
    validate_cal = sub.add_parser("validate-cal")
    validate_cal.add_argument("orientation", choices=("0", "90"))
    validate_cal.add_argument("source", type=Path)
    calibration_changed_parser = sub.add_parser("calibration-changed")
    calibration_changed_parser.add_argument("orientation", choices=("0", "90"))
    validate_preferences = sub.add_parser("validate-preferences")
    validate_preferences.add_argument("source", type=Path)
    install_preferences = sub.add_parser("install-preferences")
    install_preferences.add_argument("source", type=Path)
    new = sub.add_parser("new")
    new.add_argument("mode", choices=tuple(SOURCES))
    new.add_argument("orientation", choices=("0", "90"))
    new.add_argument("level_dbfs", type=int)
    new.add_argument("sweep_seconds", type=int)
    new.add_argument("noise_level_dbfs", type=int, nargs="?", default=None)
    new.add_argument("woofer_measurement_attenuation_db", type=int, nargs="?", default=None)
    new.add_argument("position_count", type=int, choices=ALLOWED_POSITION_COUNTS, nargs="?", default=POSITIONS)
    configure = sub.add_parser("configure")
    configure.add_argument("mode", choices=tuple(SOURCES))
    configure.add_argument("orientation", choices=("0", "90"))
    configure.add_argument("level_dbfs", type=int)
    configure.add_argument("sweep_seconds", type=int)
    configure.add_argument("noise_level_dbfs", type=int, nargs="?", default=None)
    configure.add_argument("woofer_measurement_attenuation_db", type=int, nargs="?", default=None)
    configure.add_argument("position_count", type=int, choices=ALLOWED_POSITION_COUNTS, nargs="?", default=None)
    sub.add_parser("start-level")
    sub.add_parser("start-level-reprocess")
    sub.add_parser("start-position")
    sub.add_parser("restart-positions")
    sub.add_parser("start-phase-reference")
    sub.add_parser("recover-phase-reference")
    sub.add_parser("start-validation")
    post_validation = sub.add_parser("start-post-validation")
    post_validation.add_argument("level_dbfs", type=int, choices=range(-54, -11))
    sub.add_parser("reprocess-post-validation")
    sub.add_parser("reset-post-validation")
    inspect_recording = sub.add_parser("inspect-recording")
    inspect_recording.add_argument("position", type=int, choices=range(1, POSITIONS + 1))
    inspect_recording.add_argument("source", choices=tuple(SOURCE_LABELS))
    reprocess_recording = sub.add_parser("reprocess-recording")
    reprocess_recording.add_argument("position", type=int, choices=range(1, POSITIONS + 1))
    reprocess_recording.add_argument("source", choices=tuple(SOURCE_LABELS))
    sub.add_parser("start-reprocess-saved")
    sub.add_parser("_worker-reprocess-saved")
    build = sub.add_parser("start-build")
    build.add_argument("target", choices=tuple(TARGET_FILES))
    build.add_argument("preset", choices=("none", "primus360", "strong"))
    build.add_argument("woofer_trim_db", type=int, choices=range(-18, 1))
    build.add_argument("phase_mode", choices=("magnitude", "bass"))
    build.add_argument("phase_cutoff", type=int, choices=PHASE_CUTOFFS)
    build.add_argument("spatial_mode", choices=("equal", "center"), nargs="?", default="equal")
    build.add_argument("bass_tilt_db", type=int, choices=range(-6, 7), nargs="?", default=0)
    build.add_argument("treble_tilt_db", type=int, choices=range(-6, 3), nargs="?", default=0)
    build.add_argument("correction_low_hz", type=int, choices=(20, 30, 40, 60, 80), nargs="?", default=20)
    build.add_argument("correction_high_hz", type=int, choices=(300, 500, 1000, 5000, 20_000), nargs="?", default=20_000)
    build.add_argument("max_boost_db", type=int, choices=(0, 3, 6, 9, 10), nargs="?", default=10)
    build.add_argument("max_cut_db", type=int, choices=(6, 9, 12, 18, 24), nargs="?", default=18)
    build.add_argument("mimo_high_hz", type=int, choices=(80, 120, 150), nargs="?", default=150)
    build.add_argument("mimo_strength", choices=("safe", "balanced", "maximum"), nargs="?", default="balanced")
    build.add_argument("mimo_support_penalty_db", type=int, choices=(3, 6, 9, 12), nargs="?", default=6)
    build.add_argument("crossover_enabled", choices=("on", "off"), nargs="?", default="on")
    build.add_argument("crossover_frequency_hz", type=int, choices=CROSSOVER_FREQUENCIES, nargs="?", default=100)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("profile", choices=("speaker", "headphone"))
    preview_parser = sub.add_parser("preview")
    preview_parser.add_argument("profile", choices=("speaker", "headphone"))
    sub.add_parser("restore")
    sub.add_parser("cancel")
    sub.add_parser("_worker-level")
    sub.add_parser("_worker-level-reprocess")
    sub.add_parser("_worker-position")
    sub.add_parser("_worker-phase-reference")
    sub.add_parser("_worker-validation")
    post_validation_worker_parser = sub.add_parser("_worker-post-validation")
    post_validation_worker_parser.add_argument("level_dbfs", type=int)
    worker_build = sub.add_parser("_worker-build")
    worker_build.add_argument("target", choices=tuple(TARGET_FILES))
    worker_build.add_argument("preset", choices=("none", "primus360", "strong"))
    worker_build.add_argument("woofer_trim_db", type=int)
    worker_build.add_argument("phase_mode", choices=("magnitude", "bass"))
    worker_build.add_argument("phase_cutoff", type=int)
    worker_build.add_argument("spatial_mode", choices=("equal", "center"), nargs="?", default="equal")
    worker_build.add_argument("bass_tilt_db", type=int, nargs="?", default=0)
    worker_build.add_argument("treble_tilt_db", type=int, nargs="?", default=0)
    worker_build.add_argument("correction_low_hz", type=int, nargs="?", default=20)
    worker_build.add_argument("correction_high_hz", type=int, nargs="?", default=20_000)
    worker_build.add_argument("max_boost_db", type=int, nargs="?", default=10)
    worker_build.add_argument("max_cut_db", type=int, nargs="?", default=18)
    worker_build.add_argument("mimo_high_hz", type=int, nargs="?", default=150)
    worker_build.add_argument("mimo_strength", nargs="?", default="balanced")
    worker_build.add_argument("mimo_support_penalty_db", type=int, nargs="?", default=6)
    worker_build.add_argument("crossover_enabled", choices=("on", "off"), nargs="?", default="on")
    worker_build.add_argument("crossover_frequency_hz", type=int, nargs="?", default=100)
    sub.add_parser("self-test")
    sub.add_parser("self-test-targets")
    args = parser.parse_args()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    BASE.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "status":
            result = load_current()
        elif args.command == "targets":
            result = target_catalog()
        elif args.command == "list-sessions":
            result = list_sessions()
        elif args.command == "set-session-note":
            result = set_session_note(args.note)
        elif args.command == "load-session":
            result = load_session(args.session_id)
        elif args.command == "delete-session":
            result = delete_session(args.session_id)
        elif args.command == "install-cal":
            result = install_calibration(args.source, args.orientation)
            result.pop("frequencies", None)
            result.pop("corrections", None)
        elif args.command == "validate-cal":
            result = parse_calibration(args.source)
            result["orientation"] = args.orientation
            result.pop("frequencies", None)
            result.pop("corrections", None)
        elif args.command == "calibration-changed":
            result = calibration_changed(args.orientation)
        elif args.command in ("validate-preferences", "install-preferences"):
            try:
                preference_value = json.loads(args.source.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MeasurementError(f"보정 기본 설정 JSON 오류: {exc}") from exc
            result = normalize_correction_preferences(preference_value)
            if args.command == "install-preferences":
                result = save_correction_preferences(result)
        elif args.command == "new":
            result = new_session(
                args.mode, args.orientation, args.level_dbfs, args.sweep_seconds,
                args.noise_level_dbfs, args.woofer_measurement_attenuation_db, args.position_count,
            )
        elif args.command == "configure":
            result = reconfigure_session(
                args.mode, args.orientation, args.level_dbfs, args.sweep_seconds,
                args.noise_level_dbfs, args.woofer_measurement_attenuation_db, args.position_count,
            )
        elif args.command == "start-level":
            prepare_level_check()
            result = spawn_worker("_worker-level")
        elif args.command == "start-level-reprocess":
            state = load_current()
            if not (state.get("level_recording_inventory") or {}).get("can_reprocess_all"):
                raise MeasurementError("빠른 검사 저장 원본이 모두 있어야 무음 재계산할 수 있습니다.")
            result = spawn_worker("_worker-level-reprocess")
        elif args.command == "start-position":
            state = load_current()
            if state.get("orientation") != "90":
                raise MeasurementError("UMIK를 천장 방향 90°로 놓고 새 세션을 만드세요.")
            result = spawn_worker("_worker-position")
        elif args.command == "restart-positions":
            prepare_position_restart()
            result = spawn_worker("_worker-position")
        elif args.command == "start-phase-reference":
            prepare_phase_reference_remeasurement()
            result = spawn_worker("_worker-phase-reference")
        elif args.command == "recover-phase-reference":
            result = recover_pending_phase_reference()
        elif args.command == "start-validation":
            result = spawn_worker("_worker-validation")
        elif args.command == "start-post-validation":
            result = spawn_worker("_worker-post-validation", str(args.level_dbfs))
        elif args.command == "reprocess-post-validation":
            result = reprocess_post_filter_validation()
        elif args.command == "reset-post-validation":
            result = reset_post_filter_validation()
        elif args.command == "inspect-recording":
            result = inspect_saved_recording(args.position, args.source)
        elif args.command == "reprocess-recording":
            result = inspect_saved_recording(args.position, args.source, reprocess=True)
        elif args.command == "start-reprocess-saved":
            prepare_saved_reprocess()
            result = spawn_worker("_worker-reprocess-saved")
        elif args.command == "start-build":
            prepare_build()
            save_correction_preferences({
                "target": args.target,
                "preset": args.preset,
                "woofer_trim_db": args.woofer_trim_db,
                "phase_mode": args.phase_mode,
                "phase_cutoff": args.phase_cutoff,
                "spatial_mode": args.spatial_mode,
                "bass_tilt_db": args.bass_tilt_db,
                "treble_tilt_db": args.treble_tilt_db,
                "correction_low_hz": args.correction_low_hz,
                "correction_high_hz": args.correction_high_hz,
                "max_boost_db": args.max_boost_db,
                "max_cut_db": args.max_cut_db,
                "mimo_high_hz": args.mimo_high_hz,
                "mimo_strength": args.mimo_strength,
                "mimo_support_penalty_db": args.mimo_support_penalty_db,
                "crossover_enabled": args.crossover_enabled == "on",
                "crossover_frequency_hz": args.crossover_frequency_hz,
            })
            result = spawn_worker("_worker-build", args.target, args.preset, str(args.woofer_trim_db), args.phase_mode, str(args.phase_cutoff), args.spatial_mode, str(args.bass_tilt_db), str(args.treble_tilt_db), str(args.correction_low_hz), str(args.correction_high_hz), str(args.max_boost_db), str(args.max_cut_db), str(args.mimo_high_hz), args.mimo_strength, str(args.mimo_support_penalty_db), args.crossover_enabled, str(args.crossover_frequency_hz))
        elif args.command == "apply":
            result = apply_result(args.profile)
        elif args.command == "preview":
            result = preview_result(args.profile)
        elif args.command == "restore":
            result = restore_result()
        elif args.command == "cancel":
            result = cancel()
        elif args.command == "_worker-level":
            return worker_guard(level_check_worker)
        elif args.command == "_worker-level-reprocess":
            return worker_guard(level_check_reprocess_worker)
        elif args.command == "_worker-position":
            return worker_guard(measure_position_worker)
        elif args.command == "_worker-phase-reference":
            return worker_guard(measure_phase_reference_worker)
        elif args.command == "_worker-reprocess-saved":
            return worker_guard(reprocess_saved_recordings_worker)
        elif args.command == "_worker-validation":
            return worker_guard(validation_worker)
        elif args.command == "_worker-post-validation":
            return worker_guard(lambda: post_filter_validation_worker(args.level_dbfs))
        elif args.command == "_worker-build":
            return worker_guard(lambda: build_worker(args.target, args.preset, args.woofer_trim_db, args.phase_mode, args.phase_cutoff, args.spatial_mode, args.bass_tilt_db, args.treble_tilt_db, args.correction_low_hz, args.correction_high_hz, args.max_boost_db, args.max_cut_db, args.mimo_high_hz, args.mimo_strength, args.mimo_support_penalty_db, args.crossover_enabled == "on", args.crossover_frequency_hz))
        elif args.command == "self-test":
            result = self_test()
        else:
            result = target_matrix_self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
