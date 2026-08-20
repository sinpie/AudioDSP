#!/usr/bin/env python3
"""Small LAN-only AudioDSP FIR profile/status UI with inline SVG graphs."""

from __future__ import annotations

import cmath
from email import policy
from email.parser import BytesParser
import hashlib
import html
import io
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
from urllib.parse import parse_qs, urlencode, urlparse


def environment(suffix: str, default: str) -> str:
    """Read an AudioDSP runtime override."""
    return os.environ.get(f"AUDIODSP_{suffix}", default)


MANAGER = environment("PROFILE_MANAGER", "/usr/local/bin/audiodsp-profile-manager.py")
MEASUREMENT = environment("MEASUREMENT", "/usr/local/bin/audiodsp-measurement.py")
WEB_HOST = environment("WEB_HOST", "0.0.0.0")
WEB_PORT = int(environment("WEB_PORT", "8080"))
WEB_PROFILE_DIR = Path(environment("CONFIG_DIR", "/etc/camilladsp")) / "profiles"
STATE_DIR = Path(environment("STATE_DIR", "/var/lib/audiodsp"))
STAGING_DIR = Path(environment("STAGING_DIR", "/var/lib/audiodsp/upload-staging"))
MEASUREMENT_ROOT = Path(environment("MEASUREMENT_DIR", "/var/lib/audiodsp/measurements"))
MEASUREMENT_STATUS_PATH = MEASUREMENT_ROOT / "current.json"
SELECTOR_STATE_PATH = Path(environment("SELECTOR_STATE_PATH", str(STATE_DIR / "u7-selector-state.json")))
PREVIEW_STATE_PATH = Path(environment("PREVIEW_STATE_PATH", str(STATE_DIR / "fir-preview.json")))
CAL_DIR = Path(environment("CAL_DIR", "/var/lib/audiodsp/calibration"))
RESTORE_STAGING_ROOT = Path(environment("RESTORE_STAGING_DIR", str(STATE_DIR / "restore-staging")))
RESTORE_STATE_PATH = STATE_DIR / "restore-staging.json"
SYSTEM_BACKUP_DIR = STATE_DIR / "system-backups"
CORRECTION_PREFERENCES_PATH = Path(environment("PREFERENCES_PATH", str(STATE_DIR / "correction-preferences.json")))
AMIXER = environment("AMIXER", "/usr/bin/amixer")
U7_MIXER = environment("U7_MIXER", "hw:U7")
BACKUP_SCHEMA_VERSION = 2
QUICK_SWEEP_PASS_SNR_DB = 6.0
MEASUREMENT_RECOMMENDED_SNR_DB = 15.0


def measurement_algorithm_revision() -> str:
    """Use the measurement engine as the single revision source."""
    override = environment("RESULT_ALGORITHM_REVISION", "").strip()
    if override:
        return override
    try:
        match = re.search(
            r'^RESULT_ALGORITHM_REVISION\s*=\s*"([^"]+)"',
            Path(MEASUREMENT).read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    except OSError:
        pass
    # Mark saved results stale when the authoritative engine is unavailable.
    return "measurement-engine-unavailable"


RESULT_ALGORITHM_REVISION = measurement_algorithm_revision()
DEFAULT_CORRECTION_PREFERENCES = {
    "target": "flat", "preset": "none", "woofer_trim_db": 0,
    "phase_mode": "bass", "phase_cutoff": 200, "spatial_mode": "equal",
    "bass_tilt_db": 0, "treble_tilt_db": 0, "correction_low_hz": 20,
    "correction_high_hz": 20_000, "max_boost_db": 6, "max_cut_db": 18,
    "mimo_high_hz": 150, "mimo_strength": "balanced", "mimo_support_penalty_db": 6,
    "crossover_enabled": True, "crossover_frequency_hz": 100,
}


def json_safe(value):
    """Return strict-JSON data, replacing non-finite diagnostics with null.

    Python's json encoder otherwise emits NaN/Infinity tokens.  They are
    accepted by Python's decoder but rejected by the browser JSON parser,
    which used to stop live status updates and leave the result graph blank.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def strict_json_bytes(payload, *, indent: int | None = None) -> bytes:
    return (
        json.dumps(
            json_safe(payload),
            ensure_ascii=False,
            indent=indent,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
MAX_REQUEST = 33 * 1024 * 1024
GRAPH_CACHE: dict[tuple[str, str, bool], str] = {}
STAGING_LOCK = threading.Lock()
RESTORE_LOCK = threading.RLock()
STATUS_LOCK = threading.Lock()
STATUS_CACHE: dict[str, object] = {"signature": None, "value": None}
VOLUME_LOCK = threading.Lock()
VOLUME_CACHE: dict[str, object] = {"expires": 0.0, "value": None}
VOLUME_MIN_DB = -60
VOLUME_MAX_DB = 0
MEASUREMENT_DEFAULT: dict | None = None
PROFILE_BASENAMES = {
    "speaker": {"front": "Speaker_Front_LR.wav", "rear": "Speaker_Rear_LR.wav"},
    "headphone": {"front": "Headphone_Front_LR.wav", "rear": "Headphone_Rear_LR.wav"},
}
FACTORY_BASENAME = "Factory_Speaker_Front_LR.wav"
BACKUP_PROFILE_NAMES = (
    FACTORY_BASENAME,
    "Speaker_Front_LR.wav",
    "Speaker_Rear_LR.wav",
    "Headphone_Front_LR.wav",
    "Headphone_Rear_LR.wav",
)
BACKUP_CALIBRATION_NAMES = ("7200660.txt", "7200660_90deg.txt")
BACKUP_MIMO_NAMES = (
    "MIMO_Front_Left_LR_32768.wav",
    "MIMO_Front_Right_LR_32768.wav",
    "MIMO_Rear_Left_LR_32768.wav",
    "MIMO_Rear_Right_LR_32768.wav",
    "Speaker_MIMO.json",
    "Headphone_MIMO.json",
)


def manager(*arguments: str) -> dict:
    result = subprocess.run(
        ["/usr/bin/python3", MANAGER, *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45,
    )
    output = result.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(output or f"Profile manager exited with {result.returncode}") from exc
    if result.returncode != 0:
        raise RuntimeError(payload.get("error", output))
    return payload


def status_signature() -> tuple[tuple[str, int, int], ...]:
    """Track only files that can change live status, avoiding a Python spawn every second on Pi 2."""
    paths = [
        STATE_DIR / "profile-settings.json",
        SELECTOR_STATE_PATH,
        PREVIEW_STATE_PATH,
        *(WEB_PROFILE_DIR / basename for bands in PROFILE_BASENAMES.values() for basename in bands.values()),
        WEB_PROFILE_DIR / FACTORY_BASENAME,
        *(WEB_PROFILE_DIR / "mimo" / name for name in BACKUP_MIMO_NAMES),
    ]
    signature = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((str(path), -1, -1))
    return tuple(signature)


def cached_status(force: bool = False) -> dict:
    signature = status_signature()
    with STATUS_LOCK:
        if not force and STATUS_CACHE["value"] is not None and STATUS_CACHE["signature"] == signature:
            return STATUS_CACHE["value"]  # type: ignore[return-value]
        value = manager("status")
        # Compute the signature after manager returns in case it normalized a state file.
        STATUS_CACHE.update(signature=status_signature(), value=value)
        return value


def invalidate_volume_cache() -> None:
    with VOLUME_LOCK:
        VOLUME_CACHE.update(expires=0.0, value=None)


def read_output_volume(status_value: dict | None = None, force: bool = False) -> dict:
    """Read the real U7 PCM level while keeping polling inexpensive on Pi 2."""
    now = time.monotonic()
    with VOLUME_LOCK:
        cached = VOLUME_CACHE.get("value")
        if not force and cached is not None and now < float(VOLUME_CACHE.get("expires", 0.0)):
            return dict(cached)  # type: ignore[arg-type]
    current = status_value or cached_status()
    saved_db = current.get("settings", {}).get("output_volume_db", -10)
    base = {
        "available": False,
        "mixer": U7_MIXER,
        "control": "PCM,0",
        "saved_db": saved_db,
        "min_db": VOLUME_MIN_DB,
        "max_db": VOLUME_MAX_DB,
        "step_db": 1,
    }
    try:
        result = subprocess.run(
            [AMIXER, "-D", U7_MIXER, "cget", "numid=6"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=3,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout.strip() or f"amixer exited with {result.returncode}")
        metadata = re.search(r"values=(\d+),min=(-?\d+),max=(-?\d+)", result.stdout)
        raw_line = re.search(r"(?m)^\s*:\s*values=([0-9, -]+)\s*$", result.stdout)
        if metadata is None or raw_line is None:
            raise RuntimeError("Unexpected Xonar U7 mixer response")
        channel_count, raw_min, raw_max = (int(value) for value in metadata.groups())
        raw_values = [int(value.strip()) for value in raw_line.group(1).split(",") if value.strip()]
        if not raw_values or len(raw_values) != channel_count or raw_max <= raw_min:
            raise RuntimeError("Invalid Xonar U7 PCM channel values")
        db_metadata = re.search(r"dBminmax-min=(-?\d+),max=(-?\d+)", result.stdout)
        if db_metadata:
            hardware_min_db, hardware_max_db = (int(value) / 100.0 for value in db_metadata.groups())
        else:
            hardware_min_db, hardware_max_db = float(raw_min - raw_max), 0.0
        scale = (hardware_max_db - hardware_min_db) / (raw_max - raw_min)
        channel_db = [hardware_min_db + (raw - raw_min) * scale for raw in raw_values]
        actual_db = round(sum(channel_db) / len(channel_db), 1)
        raw_average = round(sum(raw_values) / len(raw_values), 2)
        base.update({
            "available": True,
            "actual_db": actual_db,
            "raw": raw_values[0] if len(set(raw_values)) == 1 else raw_average,
            "raw_channels": raw_values,
            "channels": channel_count,
            "uniform": len(set(raw_values)) == 1,
            "percent": round(100.0 * (raw_average - raw_min) / (raw_max - raw_min), 1),
            "hardware_min_db": hardware_min_db,
            "hardware_max_db": hardware_max_db,
        })
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        base["error"] = str(exc)
    with VOLUME_LOCK:
        VOLUME_CACHE.update(expires=time.monotonic() + 2.5, value=dict(base))
    return base


def measurement(*arguments: str) -> dict:
    result = subprocess.run(
        ["/usr/bin/python3", MEASUREMENT, *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
    )
    output = result.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(output or f"Measurement engine exited with {result.returncode}") from exc
    if result.returncode != 0:
        raise RuntimeError(payload.get("error", output))
    return payload


def normalize_level_check_status(level_check: dict, configured_sweep_level_dbfs: int, expected_sources: list[str]) -> dict:
    """Re-evaluate legacy saved quick checks without spawning the FFT engine."""
    result = dict(level_check)
    try:
        measured_snr = float(result["snr_db"])
        measured_peak = float(result.get("peak_dbfs", -300.0))
        checked_level = int(result.get("requested_level_dbfs", configured_sweep_level_dbfs))
    except (KeyError, TypeError, ValueError):
        return result
    level_delta = configured_sweep_level_dbfs - checked_level
    assessment_snr = measured_snr + level_delta
    assessment_peak = measured_peak + level_delta
    channels = result.get("channels") if isinstance(result.get("channels"), list) else []
    measured_sources = {str(item.get("source")) for item in channels if isinstance(item, dict)}
    expected = {str(source) for source in expected_sources}
    coverage_ok = not expected or measured_sources == expected
    if not coverage_ok:
        ok = False
        verdict = "FAIL · 이전 빠른 검사는 현재 측정 구성의 모든 출력을 확인하지 않았습니다. 빠른 검사를 다시 실행하세요."
    elif assessment_peak >= -1.0:
        ok = False
        verdict = "FAIL · 입력 클리핑 위험 · 스윕 출력을 낮추고 다시 검사하세요."
    elif assessment_snr < QUICK_SWEEP_PASS_SNR_DB:
        ok = False
        verdict = f"FAIL · 빠른 스윕 최저 SNR {assessment_snr:.1f} dB · 본 측정과 같은 사용 가능 하한 6 dB에 미달합니다."
    else:
        ok = True
        verdict = (
            f"PASS · 빠른 스윕 최저 {assessment_snr:.1f} dB · 권장 15 dB 이상"
            if assessment_snr >= MEASUREMENT_RECOMMENDED_SNR_DB else
            f"PASS · 빠른 스윕 최저 {assessment_snr:.1f} dB · 사용 가능, 권장 15 dB 미만"
        )
    required_raise = max(0, int(math.ceil(QUICK_SWEEP_PASS_SNR_DB - assessment_snr)))
    quality_raise = max(0, int(math.ceil(MEASUREMENT_RECOMMENDED_SNR_DB - assessment_snr)))
    safe_raise = max(0, int(math.floor(-6.0 - assessment_peak)))
    applied_raise = min(required_raise, safe_raise)
    if not coverage_ok:
        recommended_level = configured_sweep_level_dbfs
        action = "2 · 출력 설정과 빠른 검사에서 현재 측정 구성의 모든 출력 조합을 다시 검사하세요."
    elif assessment_peak >= -1.0:
        recommended_level = max(-54, min(0, configured_sweep_level_dbfs + int(math.floor(-6.0 - assessment_peak))))
        action = f"2 · 레벨 확인에서 스윕 출력을 {configured_sweep_level_dbfs} → {recommended_level} dBFS로 낮추고 다시 검사하세요."
    elif required_raise:
        recommended_level = max(-54, min(0, configured_sweep_level_dbfs + applied_raise))
        if applied_raise >= required_raise:
            action = (
                f"2 · 레벨 확인에서 스윕 출력을 {configured_sweep_level_dbfs} → {recommended_level} dBFS "
                f"(+{applied_raise} dB)로 올리고 빠른 스윕을 다시 실행하세요."
            )
        else:
            action = (
                f"입력 여유를 고려한 스윕 안전 상한은 {recommended_level} dBFS(+{applied_raise} dB)입니다. "
                "그래도 6 dB가 안 되면 주변 소음을 줄이거나 마이크/기기 레벨을 확인하세요."
            )
    elif quality_raise:
        optional_raise = min(quality_raise, safe_raise)
        recommended_level = max(-54, min(0, configured_sweep_level_dbfs + optional_raise))
        action = (
            f"PASS · 현재 {configured_sweep_level_dbfs} dBFS로 본 측정 가능. 권장 15 dB 품질이 필요하면 "
            f"{recommended_level} dBFS(+{optional_raise} dB)까지 단계적으로 올릴 수 있습니다."
        )
    else:
        recommended_level = configured_sweep_level_dbfs
        action = f"현재 스윕 출력 {configured_sweep_level_dbfs} dBFS를 유지하세요."
    result.update({
        "required_snr_db": QUICK_SWEEP_PASS_SNR_DB,
        "minimum_measurement_snr_db": 6.0,
        "recommended_measurement_snr_db": MEASUREMENT_RECOMMENDED_SNR_DB,
        "preflight_target_snr_db": QUICK_SWEEP_PASS_SNR_DB,
        "preflight_safety_margin_db": 0.0,
        "assessment_snr_db": round(assessment_snr, 2),
        "assessment_peak_dbfs": round(assessment_peak, 2),
        "coverage_ok": coverage_ok,
        "measured_sources": sorted(measured_sources),
        "expected_sources": sorted(expected),
        "quality_recommended": coverage_ok and assessment_peak < -1.0 and assessment_snr >= MEASUREMENT_RECOMMENDED_SNR_DB,
        "recommended_sweep_level_dbfs": recommended_level,
        "recommended_raise_db": max(0, recommended_level - configured_sweep_level_dbfs),
        "required_raise_db": required_raise,
        "quality_raise_db": quality_raise,
        "level_action": action,
        "ok": ok,
        "verdict": verdict,
    })
    return result


def measurement_status() -> dict:
    """Read the atomically-written job JSON directly; spawning the FFT engine each second is costly on Pi 2."""
    global MEASUREMENT_DEFAULT
    try:
        value = json.loads(MEASUREMENT_STATUS_PATH.read_text(encoding="utf-8"))
        raw_preferences = value.get("correction_preferences")
        if not isinstance(raw_preferences, dict):
            try:
                raw_preferences = json.loads(CORRECTION_PREFERENCES_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw_preferences = {}
        merged_preferences = dict(DEFAULT_CORRECTION_PREFERENCES)
        if isinstance(raw_preferences, dict):
            merged_preferences.update({key: raw_preferences[key] for key in DEFAULT_CORRECTION_PREFERENCES if key in raw_preferences})
        value["correction_preferences"] = merged_preferences
        if isinstance(value.get("level_check"), dict):
            value["level_check"] = normalize_level_check_status(
                value["level_check"],
                int(value.get("level_dbfs", -42)),
                list(value.get("sources", ())),
            )
        session_dir = Path(str(value.get("session_dir", "")))
        expected = [
            (position, source)
            for position in range(1, int(value.get("positions_total", 0)) + 1)
            for source in value.get("sources", [])
        ]
        if session_dir.is_dir() and expected:
            raw = [item for item in expected if (session_dir / f"p{item[0]}_{item[1]}_recorded.wav").is_file()]
            responses = [item for item in expected if (session_dir / f"p{item[0]}_{item[1]}_response.json").is_file()]
            value["capture_inventory"] = {
                "expected": len(expected),
                "raw_count": len(raw),
                "response_count": len(responses),
                "can_reprocess_all": len(raw) == len(expected),
            }
        result = value.get("result")
        if isinstance(result, dict):
            actual_revision = result.get("algorithm_revision")
            value["result_revision_status"] = {
                "stale": actual_revision != RESULT_ALGORITHM_REVISION,
                "actual": actual_revision,
                "required": RESULT_ALGORITHM_REVISION,
            }
            token_source = "|".join(str(result.get(key, "")) for key in (
                "algorithm_revision", "front_sha256", "rear_sha256", "kind", "target", "preset",
            ))
            value["result_token"] = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:16]
        else:
            value["result_token"] = "none"
        return value
    except FileNotFoundError:
        if MEASUREMENT_DEFAULT is None:
            MEASUREMENT_DEFAULT = measurement("status")
        return MEASUREMENT_DEFAULT
    except (OSError, ValueError):
        # Atomic replace makes this unlikely; retain the last safe default rather than fail the UI.
        if MEASUREMENT_DEFAULT is None:
            MEASUREMENT_DEFAULT = measurement("status")
        return MEASUREMENT_DEFAULT


def stage_path(profile: str, band: str) -> Path:
    if profile not in ("speaker", "headphone") or band not in ("front", "rear"):
        raise ValueError("Invalid staged profile or band")
    return STAGING_DIR / f"{profile}-{band}.wav"


def stage_manifest_path(profile: str) -> Path:
    if profile not in ("speaker", "headphone"):
        raise ValueError("Invalid staged profile")
    return STAGING_DIR / f"{profile}.json"


def staging_status(profile: str) -> dict:
    manifest: dict = {}
    path = stage_manifest_path(profile)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        manifest = {}
    bands = {}
    for band in ("front", "rear"):
        staged = stage_path(profile, band)
        item = manifest.get("bands", {}).get(band, {}) if staged.is_file() else {}
        bands[band] = {**item, "present": staged.is_file(), "path": str(staged) if staged.is_file() else None}
    return {"profile": profile, "active": any(item["present"] for item in bands.values()), "bands": bands}


def save_stage_manifest(profile: str, manifest: dict) -> None:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    target = stage_manifest_path(profile)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def stage_upload(profile: str, band: str, source: Path, original_name: str) -> dict:
    metadata = manager("validate-wav", str(source))
    with STAGING_LOCK:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        target = stage_path(profile, band)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}")
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            current = staging_status(profile)
            manifest = {"profile": profile, "updated_unix": time.time(), "bands": {}}
            for name in ("front", "rear"):
                previous = current["bands"][name]
                if previous["present"]:
                    manifest["bands"][name] = {key: value for key, value in previous.items() if key not in ("present", "path")}
            manifest["bands"][band] = {"original_name": original_name, "metadata": metadata, "uploaded_unix": time.time()}
            save_stage_manifest(profile, manifest)
        finally:
            temporary.unlink(missing_ok=True)
    return staging_status(profile)


def baseline_paths(status: dict, profile: str) -> tuple[Path, Path | None]:
    other = "headphone" if profile == "speaker" else "speaker"
    front = WEB_PROFILE_DIR / PROFILE_BASENAMES[profile]["front"]
    effective = profile
    if not front.is_file():
        front = WEB_PROFILE_DIR / PROFILE_BASENAMES[other]["front"]
        effective = other
    if not front.is_file():
        front = WEB_PROFILE_DIR / FACTORY_BASENAME
        effective = "factory"
    rear = None
    if effective != "factory" and status["settings"]["rear_mode"][effective] == "separate":
        candidate = WEB_PROFILE_DIR / PROFILE_BASENAMES[effective]["rear"]
        rear = candidate if candidate.is_file() else None
    return front, rear


def staged_candidates(status: dict, profile: str) -> tuple[Path, Path | None, dict]:
    staged = staging_status(profile)
    baseline_front, baseline_rear = baseline_paths(status, profile)
    front = stage_path(profile, "front") if staged["bands"]["front"]["present"] else baseline_front
    if not front.is_file():
        raise RuntimeError("프런트 FIR이 없습니다. 먼저 프런트 WAV를 올려주세요.")
    staged_rear = stage_path(profile, "rear") if staged["bands"]["rear"]["present"] else None
    separate = staged_rear is not None or status["settings"]["rear_mode"][profile] == "separate"
    current_rear = WEB_PROFILE_DIR / PROFILE_BASENAMES[profile]["rear"]
    rear = staged_rear or (current_rear if separate and current_rear.is_file() else (baseline_rear if separate else None))
    return front, rear, staged


def discard_staging(profile: str) -> dict:
    with STAGING_LOCK:
        for band in ("front", "rear"):
            stage_path(profile, band).unlink(missing_ok=True)
        stage_manifest_path(profile).unlink(missing_ok=True)
    return staging_status(profile)


def atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup_archive(status: dict) -> tuple[bytes, str, dict]:
    preferences = measurement_status().get("correction_preferences", {})
    entries: dict[str, bytes] = {
        "profile-settings.json": (json.dumps(status["settings"], ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        "correction-preferences.json": (json.dumps(preferences, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    }
    for name in BACKUP_PROFILE_NAMES:
        path = WEB_PROFILE_DIR / name
        if path.is_file():
            entries[f"profiles/{name}"] = path.read_bytes()
    for name in BACKUP_MIMO_NAMES:
        path = WEB_PROFILE_DIR / "mimo" / name
        if path.is_file():
            entries[f"profiles/mimo/{name}"] = path.read_bytes()
    for name in BACKUP_CALIBRATION_NAMES:
        path = CAL_DIR / name
        if path.is_file():
            entries[f"calibration/{name}"] = path.read_bytes()
    inventory = {
        name: {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in sorted(entries.items())
    }
    manifest = {
        "format": "AudioDSP Backup",
        "schema_version": BACKUP_SCHEMA_VERSION,
        "app_version": "1.2.0",
        "created_unix": time.time(),
        "compatibility": "Older schema versions are migrated; newer schema versions are rejected without changing the device.",
        "files": inventory,
    }
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        for name, payload in entries.items():
            archive.writestr(name, payload)
        archive.writestr("README.txt", "AudioDSP versioned backup. Restore from Profile & Settings > Backup & Recovery.\n")
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    return memory.getvalue(), f"AudioDSP_backup_{stamp}.zip", manifest


def restore_staging_status() -> dict:
    try:
        value = json.loads(RESTORE_STATE_PATH.read_text(encoding="utf-8"))
        directory = Path(value["directory"])
        if not directory.is_dir() or directory.parent != RESTORE_STAGING_ROOT:
            raise ValueError("staging directory mismatch")
        value["active"] = True
        return value
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {"active": False}


def remove_restore_staging_directory(directory: Path) -> None:
    """Remove one server-created staging directory without following links."""
    try:
        root = RESTORE_STAGING_ROOT.resolve()
        if directory.is_symlink():
            raise RuntimeError("restore staging directory must not be a symbolic link")
        candidate = directory.resolve(strict=True)
    except FileNotFoundError:
        return
    if candidate == root or candidate.parent != root or not candidate.is_dir():
        raise RuntimeError("restore staging cleanup path is outside the managed directory")
    shutil.rmtree(candidate)


def latest_system_backup() -> Path | None:
    """Return only a server-created rollback archive from the managed directory."""
    try:
        candidates = [
            path for path in SYSTEM_BACKUP_DIR.glob("AudioDSP_backup_*.zip")
            if path.is_file() and path.parent == SYSTEM_BACKUP_DIR
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except OSError:
        return None


def inspect_restore_staging(directory: Path, original_name: str, version: int) -> dict:
    settings_report = manager("validate-settings", str(directory / "profile-settings.json"))
    if (directory / "correction-preferences.json").is_file():
        preference_report = measurement("validate-preferences", str(directory / "correction-preferences.json"))
    else:
        preference_report = measurement_status().get("correction_preferences", {})
    fir_report = {}
    for name in BACKUP_PROFILE_NAMES:
        path = directory / "profiles" / name
        if path.is_file():
            fir_report[name] = manager("validate-wav", str(path))
    mimo_report = {}
    for name in ("Speaker_MIMO.json", "Headphone_MIMO.json"):
        path = directory / "profiles" / "mimo" / name
        if path.is_file():
            mimo_report[name] = manager("validate-mimo", str(path))
    calibration_report = {}
    for name, orientation in (("7200660.txt", "0"), ("7200660_90deg.txt", "90")):
        path = directory / "calibration" / name
        if path.is_file():
            calibration_report[orientation] = measurement("validate-cal", orientation, str(path))
    return {
        "active": True,
        "directory": str(directory),
        "original_name": Path(original_name).name,
        "schema_version": version,
        "app_version": "1.2.0",
        "settings": settings_report["normalized"],
        "correction_preferences": preference_report,
        "unknown_settings": settings_report.get("ignored_unknown_keys", []),
        "firs": fir_report,
        "mimo": mimo_report,
        "calibrations": calibration_report,
        "staged_unix": time.time(),
    }


def _stage_restore_archive(payload: bytes, original_name: str) -> dict:
    previous_staging = restore_staging_status()
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("AudioDSP 백업 ZIP이 아닙니다.") from exc
    allowed = {"manifest.json", "profile-settings.json", "correction-preferences.json", "README.txt"}
    allowed.update(f"profiles/{name}" for name in BACKUP_PROFILE_NAMES)
    allowed.update(f"profiles/mimo/{name}" for name in BACKUP_MIMO_NAMES)
    allowed.update(f"calibration/{name}" for name in BACKUP_CALIBRATION_NAMES)
    infos = [info for info in archive.infolist() if not info.is_dir()]
    names = [info.filename for info in infos]
    if len(names) != len(set(names)) or any(name not in allowed for name in names):
        raise RuntimeError("백업 ZIP에 중복되거나 허용되지 않은 경로가 있습니다.")
    if sum(info.file_size for info in infos) > 64 * 1024 * 1024 or any(info.file_size > 32 * 1024 * 1024 for info in infos):
        raise RuntimeError("압축 해제 크기가 안전 한도를 넘습니다.")
    required = {"manifest.json", "profile-settings.json", f"profiles/{FACTORY_BASENAME}"}
    if not required.issubset(names):
        raise RuntimeError("백업에 manifest, 설정 또는 Factory FIR이 없습니다.")
    contents = {name: archive.read(name) for name in names}
    try:
        manifest = json.loads(contents["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("백업 manifest를 읽을 수 없습니다.") from exc
    if manifest.get("format") != "AudioDSP Backup":
        raise RuntimeError("AudioDSP 백업 형식이 아닙니다.")
    version = manifest.get("schema_version")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError("백업 schema version이 잘못되었습니다.")
    if version > BACKUP_SCHEMA_VERSION:
        raise RuntimeError(f"이 백업은 더 새 버전(schema {version})입니다. AudioDSP를 먼저 업데이트하세요.")
    inventory = manifest.get("files")
    if not isinstance(inventory, dict):
        raise RuntimeError("백업 파일 목록이 없습니다.")
    data_names = set(names) - {"manifest.json", "README.txt"}
    if set(inventory) != data_names:
        raise RuntimeError("manifest 파일 목록과 ZIP 내용이 다릅니다.")
    for name in data_names:
        item = inventory[name]
        if item.get("bytes") != len(contents[name]) or item.get("sha256") != hashlib.sha256(contents[name]).hexdigest():
            raise RuntimeError(f"백업 무결성 검증 실패: {name}")

    token = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}-{hashlib.sha256(payload).hexdigest()[:10]}"
    directory = RESTORE_STAGING_ROOT / token
    directory.mkdir(parents=True, exist_ok=False)
    try:
        for name in data_names:
            atomic_bytes(directory / name, contents[name])
        report = inspect_restore_staging(directory, original_name, version)
        report["app_version"] = manifest.get("app_version", "unknown")
        atomic_bytes(RESTORE_STATE_PATH, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    except Exception:
        remove_restore_staging_directory(directory)
        raise
    if previous_staging.get("active"):
        previous_directory = Path(previous_staging["directory"])
        if previous_directory != directory:
            remove_restore_staging_directory(previous_directory)
    return report


def stage_restore_archive(payload: bytes, original_name: str) -> dict:
    with RESTORE_LOCK:
        return _stage_restore_archive(payload, original_name)


def discard_restore_staging() -> None:
    with RESTORE_LOCK:
        _discard_restore_staging()


def _discard_restore_staging() -> None:
    staged = restore_staging_status()
    if staged.get("active"):
        remove_restore_staging_directory(Path(staged["directory"]))
    RESTORE_STATE_PATH.unlink(missing_ok=True)


def apply_restore_staging() -> dict:
    with RESTORE_LOCK:
        return _apply_restore_staging()


def _apply_restore_staging() -> dict:
    staged = restore_staging_status()
    if not staged.get("active"):
        raise RuntimeError("검토 중인 백업이 없습니다.")
    job = measurement_status()
    if job.get("state") in ("running", "processing", "cancelling"):
        raise RuntimeError("측정/계산 작업이 끝난 뒤 복원하세요.")
    current = cached_status(force=True)
    backup, filename, _manifest = backup_archive(current)
    SYSTEM_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    automatic_backup = SYSTEM_BACKUP_DIR / filename
    atomic_bytes(automatic_backup, backup)
    directory = Path(staged["directory"])
    previous_calibration = {
        name: (CAL_DIR / name).read_bytes() if (CAL_DIR / name).is_file() else None
        for name in BACKUP_CALIBRATION_NAMES
    }
    previous_preferences = CORRECTION_PREFERENCES_PATH.read_bytes() if CORRECTION_PREFERENCES_PATH.is_file() else None
    manager_rollback: Path | None = None
    try:
        for name in BACKUP_CALIBRATION_NAMES:
            source = directory / "calibration" / name
            target = CAL_DIR / name
            if source.is_file():
                atomic_bytes(target, source.read_bytes(), 0o644)
        preference_source = directory / "correction-preferences.json"
        if preference_source.is_file():
            measurement("install-preferences", str(preference_source))
        if current.get("preview", {}).get("active"):
            manager("restore-profile")
        result = manager("restore-snapshot", str(directory))
        rollback_value = result.get("restored_snapshot", {}).get("automatic_backup")
        manager_rollback = Path(rollback_value) if isinstance(rollback_value, str) else None
        if "90" in staged.get("calibrations", {}):
            try:
                measurement("calibration-changed", "90")
            except Exception:
                if manager_rollback is not None:
                    manager("restore-snapshot", str(manager_rollback))
                raise
    except Exception:
        for name, previous in previous_calibration.items():
            target = CAL_DIR / name
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                atomic_bytes(target, previous, 0o644)
        if previous_preferences is None:
            CORRECTION_PREFERENCES_PATH.unlink(missing_ok=True)
        else:
            atomic_bytes(CORRECTION_PREFERENCES_PATH, previous_preferences, 0o644)
        raise
    _discard_restore_staging()
    result["browser_restore"] = {"automatic_backup": str(automatic_backup), "source": staged["original_name"]}
    return result


def system_health() -> dict:
    load = [float(value) for value in Path("/proc/loadavg").read_text().split()[:3]]
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0])
    temperature_path = Path("/sys/class/thermal/thermal_zone0/temp")
    temperature = round(int(temperature_path.read_text()) / 1000.0, 1) if temperature_path.is_file() else None
    cards = Path("/proc/asound/cards").read_text(errors="ignore") if Path("/proc/asound/cards").is_file() else ""
    return {
        "load": load,
        "memory_used_percent": round(100.0 * (memory.get("MemTotal", 1) - memory.get("MemAvailable", 0)) / memory.get("MemTotal", 1), 1),
        "temperature_c": temperature,
        "camilladsp": service_active("camilladsp.service"),
        "profile_monitor": service_active_any("audiodsp-profile-monitor.service"),
        "umik1": "UMIK-1" in cards,
        "xonar_u7": "Xonar U7" in cards,
    }


def service_active(name: str) -> bool:
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", name], check=False, timeout=3
    ).returncode == 0


def service_active_any(*names: str) -> bool:
    return any(service_active(name) for name in names)


def read_stereo_fir(path: Path) -> tuple[list[float], list[float], int]:
    size = path.stat().st_size
    fmt: bytes | None = None
    data: bytes | None = None
    with path.open("rb") as handle:
        if handle.read(4) != b"RIFF":
            raise ValueError("Not RIFF")
        handle.read(4)
        if handle.read(4) != b"WAVE":
            raise ValueError("Not WAVE")
        while handle.tell() + 8 <= size:
            chunk_id = handle.read(4)
            chunk_size_raw = handle.read(4)
            if len(chunk_size_raw) != 4:
                break
            chunk_size = struct.unpack("<I", chunk_size_raw)[0]
            chunk_start = handle.tell()
            if chunk_id == b"fmt ":
                fmt = handle.read(chunk_size)
            elif chunk_id == b"data":
                data = handle.read(chunk_size)
            handle.seek(chunk_start + chunk_size + (chunk_size & 1))
    if fmt is None or data is None or len(fmt) < 16:
        raise ValueError("Missing WAV chunks")
    format_code, channels, rate, _byte_rate, block_align, bits = struct.unpack("<HHIIHH", fmt[:16])
    if format_code == 0xFFFE and len(fmt) >= 40:
        format_code = struct.unpack("<H", fmt[24:26])[0]
    if channels != 2 or rate != 48000:
        raise ValueError("Expected stereo 48 kHz FIR")
    left: list[float] = []
    right: list[float] = []
    if format_code == 3 and bits in (32, 64):
        code = "f" if bits == 32 else "d"
        values = (item[0] for item in struct.iter_unpack("<" + code, data))
    elif format_code == 1 and bits == 16:
        values = (item[0] / 32768.0 for item in struct.iter_unpack("<h", data))
    elif format_code == 1 and bits == 24:
        def pcm24_values():
            for offset in range(0, len(data), 3):
                raw = int.from_bytes(data[offset:offset + 3], "little", signed=False)
                if raw & 0x800000:
                    raw -= 1 << 24
                yield raw / 8388608.0
        values = pcm24_values()
    elif format_code == 1 and bits == 32:
        values = (item[0] / 2147483648.0 for item in struct.iter_unpack("<i", data))
    else:
        raise ValueError(f"Unsupported WAV format={format_code} bits={bits}")
    for index, value in enumerate(values):
        (left if index % 2 == 0 else right).append(float(value))
    if not left or len(left) != len(right) or len(data) % block_align:
        raise ValueError("Invalid interleaved FIR data")
    return left, right, rate


def fft(values: list[float]) -> list[complex]:
    length = 1
    while length < len(values):
        length <<= 1
    data = [complex(value, 0.0) for value in values]
    data.extend([0j] * (length - len(data)))
    j = 0
    for i in range(1, length):
        bit = length >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            data[i], data[j] = data[j], data[i]
    step = 2
    while step <= length:
        rotation = cmath.exp(-2j * math.pi / step)
        half = step // 2
        for start in range(0, length, step):
            twiddle = 1 + 0j
            for offset in range(half):
                even = data[start + offset]
                odd = data[start + offset + half] * twiddle
                data[start + offset] = even + odd
                data[start + offset + half] = even - odd
                twiddle *= rotation
        step <<= 1
    return data


def response_curves(status: dict, show_woofer: bool) -> tuple[list[float], dict[str, list[float]], bool]:
    resolved = status["resolved"]
    front_path = Path(resolved["front_path"])
    front_left, front_right, rate = read_stereo_fir(front_path)
    front_l_fft = fft(front_left)
    front_r_fft = fft(front_right)
    frequencies = [10.0 * ((24000.0 / 10.0) ** (index / 419.0)) for index in range(420)]

    def magnitudes(transform: list[complex]) -> list[float]:
        nfft = len(transform)
        result = []
        for frequency in frequencies:
            position = frequency * nfft / rate
            low = min(int(position), nfft // 2)
            high = min(low + 1, nfft // 2)
            fraction = position - low
            magnitude = abs(transform[low]) * (1.0 - fraction) + abs(transform[high]) * fraction
            result.append(magnitude)
        return result

    front_l_mag = magnitudes(front_l_fft)
    front_r_mag = magnitudes(front_r_fft)
    curves_mag: dict[str, list[float]] = {"L": front_l_mag, "R": front_r_mag}
    copied = resolved["effective_rear_mode"] != "separate"
    if show_woofer:
        if copied:
            rear_l_mag, rear_r_mag = front_l_mag, front_r_mag
        else:
            rear_left, rear_right, rear_rate = read_stereo_fir(Path(resolved["rear_path"]))
            if rear_rate != rate:
                raise ValueError("우퍼 FIR 샘플레이트가 프런트 FIR과 다릅니다.")
            rear_l_mag = magnitudes(fft(rear_left))
            rear_r_mag = magnitudes(fft(rear_right))
        curves_mag["우퍼"] = [
            math.sqrt((left * left + right * right) / 2.0)
            for left, right in zip(rear_l_mag, rear_r_mag)
        ]
    curves_db = {
        name: [max(-120.0, 20.0 * math.log10(max(value, 1e-12))) for value in values]
        for name, values in curves_mag.items()
    }
    return frequencies, curves_db, copied


def svg_graph(status: dict, show_woofer: bool) -> str:
    front_hash = status["files"].get(status["resolved"]["effective_profile"], {}).get("front")
    front_key = front_hash.get("sha256", "factory") if isinstance(front_hash, dict) else "factory"
    rear_info = status["resolved"].get("rear_path") or "copy"
    rear_key = str(rear_info)
    cache_key = (front_key, rear_key, show_woofer)
    if cache_key in GRAPH_CACHE:
        return GRAPH_CACHE[cache_key]
    frequencies, curves, copied = response_curves(status, show_woofer)
    width, height = 980, 430
    left, right, top, bottom = 64, 24, 24, 48
    plot_w, plot_h = width - left - right, height - top - bottom
    min_db, max_db = -60.0, 12.0

    def x_of(frequency: float) -> float:
        return left + math.log(frequency / 10.0) / math.log(24000.0 / 10.0) * plot_w

    def y_of(db: float) -> float:
        clipped = max(min_db, min(max_db, db))
        return top + (max_db - clipped) / (max_db - min_db) * plot_h

    parts = [f'<svg class="response" viewBox="0 0 {width} {height}" role="img" aria-label="FIR frequency response">']
    parts.append(f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#111827" rx="8"/>')
    for db in (12, 6, 0, -6, -12, -24, -36, -48, -60):
        y = y_of(db)
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#334155" stroke-width="1"/>')
        parts.append(f'<text x="{left - 9}" y="{y + 4:.2f}" text-anchor="end" fill="#94a3b8" font-size="12">{db}</text>')
    ticks = ((10, "10"), (20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"),
             (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k"))
    for frequency, label in ticks:
        x = x_of(frequency)
        parts.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_h}" stroke="#334155" stroke-width="1"/>')
        parts.append(f'<text x="{x:.2f}" y="{top + plot_h + 22}" text-anchor="middle" fill="#94a3b8" font-size="12">{label}</text>')
    parts.append(f'<text x="18" y="{top + plot_h / 2}" transform="rotate(-90 18 {top + plot_h / 2})" text-anchor="middle" fill="#94a3b8" font-size="12">dB</text>')
    parts.append(f'<text x="{left + plot_w / 2}" y="{height - 7}" text-anchor="middle" fill="#94a3b8" font-size="12">Hz</text>')
    colors = {"L": "#38bdf8", "R": "#fb7185", "우퍼": "#fbbf24"}
    for name, values in curves.items():
        points = " ".join(f"{x_of(f):.2f},{y_of(v):.2f}" for f, v in zip(frequencies, values))
        dash = ' stroke-dasharray="8 6"' if name == "우퍼" and copied else ""
        parts.append(f'<polyline points="{points}" fill="none" stroke="{colors[name]}" stroke-width="2.2" vector-effect="non-scaling-stroke"{dash}/>')
    legend_x = left + 16
    for index, name in enumerate(curves):
        x = legend_x + index * 130
        dash = ' stroke-dasharray="8 6"' if name == "우퍼" and copied else ""
        label = "우퍼 (프런트 복사)" if name == "우퍼" and copied else name
        parts.append(f'<line x1="{x}" y1="{top + 18}" x2="{x + 28}" y2="{top + 18}" stroke="{colors[name]}" stroke-width="3"{dash}/>')
        parts.append(f'<text x="{x + 36}" y="{top + 22}" fill="#e2e8f0" font-size="13">{label}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    GRAPH_CACHE[cache_key] = svg
    return svg


def client_svg_graph(show_woofer: bool, rear_mode: str, bypass: bool) -> str:
    markup = r'''<div class="graph-scroll" tabindex="0" role="region" aria-label="현재 FIR 주파수 응답 그래프. 좁은 화면에서는 좌우로 스크롤할 수 있습니다."><svg id="fir-response" class="response" viewBox="0 0 980 430" role="img" aria-label="FIR frequency response"></svg></div>
    <p id="graph-status" class="muted">브라우저에서 FIR 응답을 계산하는 중…</p>
    <script>
    (() => {
      const SHOW_WOOFER = __SHOW_WOOFER__;
      const REAR_MODE = "__REAR_MODE__";
      const BYPASS = __BYPASS__;
      const NS = "http://www.w3.org/2000/svg";
      const svg = document.getElementById("fir-response");
      const status = document.getElementById("graph-status");
      const W=980,H=430,L=64,R=24,T=24,B=48,PW=W-L-R,PH=H-T-B,MIN=-60,MAX=12;
      const xOf=f => L + Math.log(f/10)/Math.log(24000/10)*PW;
      const yOf=db => T + (MAX-Math.max(MIN,Math.min(MAX,db)))/(MAX-MIN)*PH;
      const add=(tag,attrs,text="")=>{const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;svg.appendChild(n);return n;};
      function grid(){
        add("rect",{x:L,y:T,width:PW,height:PH,fill:"var(--graph-bg)",rx:8});
        [12,6,0,-6,-12,-24,-36,-48,-60].forEach(db=>{const y=yOf(db);add("line",{x1:L,y1:y,x2:L+PW,y2:y,stroke:"var(--graph-grid)"});add("text",{x:L-9,y:y+4,"text-anchor":"end",fill:"var(--graph-text)","font-size":12},db);});
        [[10,"10"],[20,"20"],[50,"50"],[100,"100"],[200,"200"],[500,"500"],[1000,"1k"],[2000,"2k"],[5000,"5k"],[10000,"10k"],[20000,"20k"]].forEach(([f,label])=>{const x=xOf(f);add("line",{x1:x,y1:T,x2:x,y2:T+PH,stroke:"var(--graph-grid)"});add("text",{x:x,y:T+PH+22,"text-anchor":"middle",fill:"var(--graph-text)","font-size":12},label);});
        add("text",{x:18,y:T+PH/2,transform:`rotate(-90 18 ${T+PH/2})`,"text-anchor":"middle",fill:"var(--graph-text)","font-size":12},"dB");
        add("text",{x:L+PW/2,y:H-7,"text-anchor":"middle",fill:"var(--graph-text)","font-size":12},"Hz");
      }
      function wave(buffer){
        const d=new DataView(buffer);const str=(o,n)=>String.fromCharCode(...new Uint8Array(buffer,o,n));
        if(str(0,4)!=="RIFF"||str(8,4)!=="WAVE")throw Error("WAV 형식이 아닙니다");
        let p=12,fmt=null,dataOffset=0,dataSize=0;
        while(p+8<=d.byteLength){const id=str(p,4),n=d.getUint32(p+4,true),s=p+8;if(id==="fmt ")fmt={o:s,n};if(id==="data"){dataOffset=s;dataSize=n;}p=s+n+(n&1);}
        if(!fmt||!dataOffset)throw Error("WAV chunk 오류");
        let code=d.getUint16(fmt.o,true),channels=d.getUint16(fmt.o+2,true),rate=d.getUint32(fmt.o+4,true),align=d.getUint16(fmt.o+12,true),bits=d.getUint16(fmt.o+14,true);
        if(code===0xfffe)code=d.getUint16(fmt.o+24,true);if(channels!==2||rate!==48000)throw Error("48kHz stereo FIR만 지원합니다");
        const frames=Math.floor(dataSize/align),left=new Float64Array(frames),right=new Float64Array(frames);
        const sample=(o)=>{if(code===3&&bits===32)return d.getFloat32(o,true);if(code===3&&bits===64)return d.getFloat64(o,true);if(code===1&&bits===16)return d.getInt16(o,true)/32768;if(code===1&&bits===24){let v=d.getUint8(o)|(d.getUint8(o+1)<<8)|(d.getUint8(o+2)<<16);if(v&0x800000)v-=0x1000000;return v/8388608;}if(code===1&&bits===32)return d.getInt32(o,true)/2147483648;throw Error("지원하지 않는 WAV bit depth");};
        const bytes=(bits+7)>>3;for(let i=0;i<frames;i++){const o=dataOffset+i*align;left[i]=sample(o);right[i]=sample(o+bytes);}return {left,right,rate};
      }
      function fft(values){
        let n=1;while(n<values.length)n<<=1;const re=new Float64Array(n),im=new Float64Array(n);re.set(values);
        for(let i=1,j=0;i<n;i++){let bit=n>>1;for(;j&bit;bit>>=1)j^=bit;j^=bit;if(i<j){[re[i],re[j]]=[re[j],re[i]];}}
        for(let len=2;len<=n;len<<=1){const ang=-2*Math.PI/len,wr0=Math.cos(ang),wi0=Math.sin(ang),half=len>>1;for(let base=0;base<n;base+=len){let wr=1,wi=0;for(let k=0;k<half;k++){const a=base+k,b=a+half,tr=re[b]*wr-im[b]*wi,ti=re[b]*wi+im[b]*wr,ur=re[a],ui=im[a];re[a]=ur+tr;im[a]=ui+ti;re[b]=ur-tr;im[b]=ui-ti;const nw=wr*wr0-wi*wi0;wi=wr*wi0+wi*wr0;wr=nw;}}}return {re,im,n};
      }
      const freqs=Array.from({length:420},(_,i)=>10*Math.pow(2400,i/419));
      function magnitude(samples,rate){const f=fft(samples);return freqs.map(hz=>{const pos=hz*f.n/rate,lo=Math.min(Math.floor(pos),f.n/2),hi=Math.min(lo+1,f.n/2),q=pos-lo,a=Math.hypot(f.re[lo],f.im[lo]),b=Math.hypot(f.re[hi],f.im[hi]);return a*(1-q)+b*q;});}
      const db=a=>a.map(v=>Math.max(-120,20*Math.log10(Math.max(v,1e-12))));
      function curve(name,values,color,dash=false,index=0){const pts=values.map((v,i)=>`${xOf(freqs[i]).toFixed(2)},${yOf(v).toFixed(2)}`).join(" ");add("polyline",{points:pts,fill:"none",stroke:color,"stroke-width":2.2,"vector-effect":"non-scaling-stroke",...(dash?{"stroke-dasharray":"8 6"}:{})});const x=L+16+index*150;add("line",{x1:x,y1:T+18,x2:x+28,y2:T+18,stroke:color,"stroke-width":3,...(dash?{"stroke-dasharray":"8 6"}:{})});add("text",{x:x+36,y:T+22,fill:"var(--text)","font-size":13},name);}
      grid();
      if(BYPASS){
        const flat=freqs.map(()=>0);
        curve("L",flat,"var(--curve-l)",false,0);
        curve("R",flat,"var(--curve-r)",false,1);
        if(SHOW_WOOFER)curve("우퍼 (프런트 복사)",flat,"var(--curve-w)",true,2);
        status.textContent="DSP 바이패스 · FIR 연산 없음 · 원본 L/R을 프런트/우퍼로 복사";
        return;
      }
      const requests=[fetch("/api/fir/front").then(r=>{if(!r.ok)throw Error("프런트 FIR 읽기 실패");return r.arrayBuffer();})];
      if(SHOW_WOOFER&&REAR_MODE==="separate")requests.push(fetch("/api/fir/rear").then(r=>{if(!r.ok)throw Error("우퍼 FIR 읽기 실패");return r.arrayBuffer();}));
      Promise.all(requests).then(buffers=>{const front=wave(buffers[0]),lm=magnitude(front.left,front.rate),rm=magnitude(front.right,front.rate);curve("L",db(lm),"var(--curve-l)",false,0);curve("R",db(rm),"var(--curve-r)",false,1);if(SHOW_WOOFER){let wl=lm,wr=rm;if(REAR_MODE==="separate"){const rear=wave(buffers[1]);wl=magnitude(rear.left,rear.rate);wr=magnitude(rear.right,rear.rate);}const woofer=wl.map((v,i)=>Math.sqrt((v*v+wr[i]*wr[i])/2));curve(REAR_MODE==="separate"?"우퍼":"우퍼 (프런트 복사)",db(woofer),"var(--curve-w)",REAR_MODE!=="separate",2);}status.textContent=`SVG 벡터 그래프 · ${front.left.length.toLocaleString()}탭 · 브라우저 계산`;}).catch(e=>{status.textContent="그래프 오류: "+e.message;status.classList.add("bad");});
    })();</script>'''
    return (markup
            .replace("__SHOW_WOOFER__", "true" if show_woofer else "false")
            .replace("__REAR_MODE__", rear_mode)
            .replace("__BYPASS__", "true" if bypass else "false"))


def staged_compare_graph(profile: str, candidate_rear: bool) -> str:
    """Client-side vector response comparison; keeps FFT work off the Pi 2."""
    safe = html.escape(profile)
    markup = r'''<div class="staged-compare"><h3>기존 / 업로드 FIR 응답 비교</h3><div class="graph-scroll" tabindex="0" role="region" aria-label="기존 FIR과 업로드 FIR 응답 비교 그래프. 좁은 화면에서는 좌우로 스크롤할 수 있습니다."><svg id="stage-graph-__PROFILE__" class="response" viewBox="0 0 980 430" role="img" aria-label="Existing and staged FIR response comparison"></svg></div><p id="stage-graph-status-__PROFILE__" class="muted">브라우저에서 기존/업로드 FIR 응답을 계산하는 중…</p></div>
    <script>(()=>{
      const PROFILE="__PROFILE__",HAS_REAR=__HAS_REAR__,NS="http://www.w3.org/2000/svg";
      const svg=document.getElementById(`stage-graph-${PROFILE}`),status=document.getElementById(`stage-graph-status-${PROFILE}`);
      const W=980,H=430,L=64,R=24,T=24,B=48,PW=W-L-R,PH=H-T-B,MIN=-60,MAX=12;
      const xOf=f=>L+Math.log(f/10)/Math.log(24000/10)*PW,yOf=v=>T+(MAX-Math.max(MIN,Math.min(MAX,v)))/(MAX-MIN)*PH;
      const add=(tag,a,t="")=>{const n=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(a))n.setAttribute(k,v);if(t)n.textContent=t;svg.appendChild(n);return n;};
      const str=(b,o,n)=>String.fromCharCode(...new Uint8Array(b,o,n));
      function wave(b){const d=new DataView(b);if(str(b,0,4)!=="RIFF"||str(b,8,4)!=="WAVE")throw Error("WAV 형식 오류");let p=12,fmt=null,data=0,size=0;while(p+8<=d.byteLength){const id=str(b,p,4),n=d.getUint32(p+4,true),s=p+8;if(id==="fmt ")fmt={o:s,n};if(id==="data"){data=s;size=n;}p=s+n+(n&1);}if(!fmt||!data)throw Error("WAV chunk 오류");let code=d.getUint16(fmt.o,true),ch=d.getUint16(fmt.o+2,true),rate=d.getUint32(fmt.o+4,true),align=d.getUint16(fmt.o+12,true),bits=d.getUint16(fmt.o+14,true);if(code===0xfffe)code=d.getUint16(fmt.o+24,true);if(ch!==2||rate!==48000)throw Error("48 kHz stereo만 지원");const frames=Math.floor(size/align),l=new Float64Array(frames),r=new Float64Array(frames),bytes=(bits+7)>>3;const sample=o=>{if(code===3&&bits===32)return d.getFloat32(o,true);if(code===3&&bits===64)return d.getFloat64(o,true);if(code===1&&bits===16)return d.getInt16(o,true)/32768;if(code===1&&bits===24){let v=d.getUint8(o)|(d.getUint8(o+1)<<8)|(d.getUint8(o+2)<<16);if(v&0x800000)v-=0x1000000;return v/8388608;}if(code===1&&bits===32)return d.getInt32(o,true)/2147483648;throw Error("지원하지 않는 bit depth");};for(let i=0;i<frames;i++){const o=data+i*align;l[i]=sample(o);r[i]=sample(o+bytes);}return{l,r,rate,frames};}
      function fft(v){let n=1;while(n<v.length)n<<=1;const re=new Float64Array(n),im=new Float64Array(n);re.set(v);for(let i=1,j=0;i<n;i++){let bit=n>>1;for(;j&bit;bit>>=1)j^=bit;j^=bit;if(i<j)[re[i],re[j]]=[re[j],re[i]];}for(let len=2;len<=n;len<<=1){const a=-2*Math.PI/len,cr=Math.cos(a),ci=Math.sin(a),half=len>>1;for(let base=0;base<n;base+=len){let wr=1,wi=0;for(let k=0;k<half;k++){const x=base+k,y=x+half,tr=re[y]*wr-im[y]*wi,ti=re[y]*wi+im[y]*wr,ur=re[x],ui=im[x];re[x]=ur+tr;im[x]=ui+ti;re[y]=ur-tr;im[y]=ui-ti;const nw=wr*cr-wi*ci;wi=wr*ci+wi*cr;wr=nw;}}}return{re,im,n};}
      const freqs=Array.from({length:420},(_,i)=>10*Math.pow(2400,i/419));
      function mag(samples,rate){const z=fft(samples);return freqs.map(hz=>{const p=hz*z.n/rate,lo=Math.min(Math.floor(p),z.n/2),hi=Math.min(lo+1,z.n/2),q=p-lo;return Math.hypot(z.re[lo],z.im[lo])*(1-q)+Math.hypot(z.re[hi],z.im[hi])*q;});}
      const db=v=>v.map(x=>Math.max(-120,20*Math.log10(Math.max(x,1e-12))));
      function grid(){add("rect",{x:L,y:T,width:PW,height:PH,fill:"var(--graph-bg)",rx:8});[12,6,0,-6,-12,-24,-36,-48,-60].forEach(v=>{const y=yOf(v);add("line",{x1:L,y1:y,x2:L+PW,y2:y,stroke:"var(--graph-grid)"});add("text",{x:L-9,y:y+4,"text-anchor":"end",fill:"var(--graph-text)","font-size":12},v);});[[10,"10"],[20,"20"],[50,"50"],[100,"100"],[200,"200"],[500,"500"],[1000,"1k"],[2000,"2k"],[5000,"5k"],[10000,"10k"],[20000,"20k"]].forEach(([f,t])=>{const x=xOf(f);add("line",{x1:x,y1:T,x2:x,y2:T+PH,stroke:"var(--graph-grid)"});add("text",{x,y:T+PH+22,"text-anchor":"middle",fill:"var(--graph-text)","font-size":12},t);});}
      let legend=0;function curve(name,v,color,dash){const pts=v.map((x,i)=>`${xOf(freqs[i]).toFixed(2)},${yOf(x).toFixed(2)}`).join(" ");add("polyline",{points:pts,fill:"none",stroke:color,"stroke-width":dash?1.5:2.5,"vector-effect":"non-scaling-stroke",...(dash?{"stroke-dasharray":"8 6"}:{})});const row=Math.floor(legend/3),col=legend%3,x=L+12+col*255,y=T+18+row*19;legend++;add("line",{x1:x,y1:y,x2:x+26,y2:y,stroke:color,"stroke-width":2.5,...(dash?{"stroke-dasharray":"8 6"}:{})});add("text",{x:x+33,y:y+4,fill:"var(--graph-text)","font-size":12},name);}
      const get=u=>fetch(u,{cache:"no-store"}).then(r=>{if(!r.ok)throw Error(`${u} 읽기 실패`);return r.arrayBuffer();});grid();
      Promise.all([get(`/api/profile/${PROFILE}/front`),get(`/api/staging/${PROFILE}/candidate/front`),...(HAS_REAR?[get(`/api/profile/${PROFILE}/rear`).catch(()=>null),get(`/api/staging/${PROFILE}/candidate/rear`)]:[])]).then(b=>{const old=wave(b[0]),next=wave(b[1]),ol=mag(old.l,old.rate),or=mag(old.r,old.rate),nl=mag(next.l,next.rate),nr=mag(next.r,next.rate);curve("기존 L",db(ol),"var(--curve-l)",true);curve("업로드 L",db(nl),"var(--curve-l)",false);curve("기존 R",db(or),"var(--curve-r)",true);curve("업로드 R",db(nr),"var(--curve-r)",false);if(HAS_REAR){const oldRear=b[2]?wave(b[2]):old,newRear=wave(b[3]),owl=mag(oldRear.l,oldRear.rate),owr=mag(oldRear.r,oldRear.rate),nwl=mag(newRear.l,newRear.rate),nwr=mag(newRear.r,newRear.rate),ow=owl.map((v,i)=>Math.hypot(v,owr[i])/Math.SQRT2),nw=nwl.map((v,i)=>Math.hypot(v,nwr[i])/Math.SQRT2);curve("기존 우퍼",db(ow),"var(--curve-w)",true);curve("업로드 우퍼",db(nw),"var(--curve-w)",false);}status.textContent=`SVG 벡터 그래프 · ${next.frames.toLocaleString()}탭 · 브라우저 FFT`;}).catch(e=>{status.textContent="그래프 오류: "+e.message;status.classList.add("bad");});
    })();</script>'''
    return markup.replace("__PROFILE__", safe).replace("__HAS_REAR__", "true" if candidate_rear else "false")


def file_summary(info: dict | None) -> str:
    if not info:
        return '<span class="missing">없음 — fallback/copy 규칙 사용</span>'
    if "error" in info:
        return f'<span class="bad">오류: {html.escape(info["error"])}</span>'
    return (
        f'{info["frames"]:,} taps · {info["format"]}{info["bits"]} · '
        f'{info["bytes"] / 1024:.1f} KiB<br><code>{html.escape(info["sha256"][:16])}…</code>'
    )


MEASUREMENT_MODE_OPTIONS = (
    ("lr", "L+우퍼 / R+우퍼 · 합산 SISO"),
    ("lrw_sum", "정밀 분리+합산 · L/R/우퍼/L+우퍼/R+우퍼 · 권장"),
    ("lrw", "표준 분리 SISO · L/R/우퍼"),
    ("mimo_stereo", "MIMO 스테레오 · 프런트 L/R · Pi4/5"),
    ("mimo_one_sub", "MIMO 2.1 · 프런트 L/R+T5S · Pi4/5"),
    ("mimo_dual_sub", "MIMO 2.2 · 프런트 L/R+우퍼 2대 · Pi4/5"),
)
MEASUREMENT_MODE_HELP = {
    "lr": "각 스윕에서 프런트 L+우퍼, 프런트 R+우퍼가 함께 재생됩니다. 공통 FIR을 프런트에 한 번 처리한 뒤 우퍼로 복사합니다.",
    "lrw_sum": "L/R/우퍼를 설계용으로 따로 측정하고 같은 마이크 위치에서 L+우퍼/R+우퍼도 측정합니다. 추가 두 응답은 보정에 중복 사용하지 않고 합산 크기·극성 의심·상대 레벨을 FIR 계산 전에 검증합니다.",
    "lrw": "프런트 L, 프런트 R, 우퍼를 각각 측정합니다. 위상 기준이 제한되면 보수적인 합산 상한과 에너지 합으로 FIR·레벨·크로스오버를 검증합니다. 실제 물리 합산까지 확인하려면 ‘정밀 분리+합산’을 선택하세요.",
    "mimo_stereo": "프런트 L/R을 각각 독립 측정해 두 스피커를 공동 최적화합니다. 우퍼 상대 레벨은 사용하지 않습니다.",
    "mimo_one_sub": "프런트 L, 프런트 R, T5S 한 대를 세 독립 물리 제어원으로 측정합니다. T5S 스테레오 입력은 한 우퍼로 취급합니다.",
    "mimo_dual_sub": "프런트 L/R과 서로 다른 위치·배선의 우퍼 두 대를 네 독립 제어원으로 측정합니다.",
}


def measurement_mode_options(selected: str, mimo_supported: bool) -> str:
    return "".join(
        f'<option value="{value}" data-help="{html.escape(MEASUREMENT_MODE_HELP[value], quote=True)}" '
        f'{"selected" if selected == value else ""} '
        f'{"disabled" if value.startswith("mimo_") and not mimo_supported else ""}>{label}</option>'
        for value, label in MEASUREMENT_MODE_OPTIONS
    )


MEASUREMENT_LEVEL_SCRIPT = """<script>(()=>{
  const refresh=(form)=>{
    if(!form) return;
    form.querySelectorAll('.level-slider input[type=range]').forEach(input=>{
      const output=input.closest('.level-slider')?.querySelector('output');
      if(output) output.textContent=`${input.value} ${input.dataset.unit || 'dBFS'}`;
    });
    const sweep=form.querySelector('[name=level_dbfs]');
    const woofer=form.querySelector('[name=woofer_measurement_attenuation_db]');
    const effective=form.querySelector('.effective-woofer-level');
    const sweepVal = Number(sweep ? sweep.value : -42);
    if(woofer&&effective) effective.textContent=`${sweepVal + Number(woofer.value)} dBFS`;
    const mode=form.querySelector('[name=mode]');
    const route=form.querySelector('.mode-route');
    if(mode&&route){
      const option=mode.tagName==='SELECT'?mode.options[mode.selectedIndex]:null;
      route.textContent=option?.dataset.help||'';
    }
    if(mode){
      const wooferBox=form.querySelector('.woofer-level-slider');
      if(wooferBox){
        const heading=wooferBox.querySelector('.woofer-level-heading');
        const stereoOnly=mode.value==='mimo_stereo';
        wooferBox.classList.toggle('not-used',stereoOnly);
        if(heading) heading.textContent=mode.value==='lr'?'우퍼 재생 트림':stereoOnly?'우퍼 설정 사용 안 함':'우퍼 측정 감쇄';
      }
    }
    const warning=form.querySelector('.output-level-warning');
    if(warning&&sweep&&woofer){
      const stereoOnly=mode?.value==='mimo_stereo';
      const effectiveWoofer=sweepVal+Number(woofer.value);
      const loud=sweepVal>-18||(!stereoOnly&&effectiveWoofer>-24);
      warning.classList.toggle('loud',loud);
      warning.innerHTML=loud
        ? '<b>높은 출력 주의:</b> 실제 음압을 확인하고 한 단계씩 올리세요. 야간에는 권장하지 않습니다.'
        : '<b>안전 시작 범위:</b> 레벨 검사를 먼저 실행하고, NOT OK일 때만 출력을 조금씩 올리세요.';
    }
  };
  document.querySelectorAll('.measure-form, .level-check-form, .measurement-output-form').forEach(form=>{
    form.addEventListener('input',()=>refresh(form));
    form.addEventListener('change',()=>refresh(form));
    refresh(form);
  });
  document.querySelectorAll('.level-slider input[type=range]').forEach(input=>{
    input.addEventListener('input',()=>{
      const output=input.closest('.level-slider')?.querySelector('output');
      if(output) output.textContent=`${input.value} ${input.dataset.unit || 'dBFS'}`;
    });
  });
  const filter=document.getElementById('session-filter-input');
  if(filter){
    const cards=[...document.querySelectorAll('.saved-session')];
    const empty=document.querySelector('.session-filter-empty');
    const count=document.querySelector('.session-tools summary .pill');
    const applyFilter=()=>{
      const query=filter.value.trim().toLocaleLowerCase();
      let shown=0;
      cards.forEach(card=>{const visible=!query||card.dataset.sessionSearch.includes(query);card.hidden=!visible;if(visible)shown++;});
      if(empty)empty.hidden=shown!==0;
      if(count)count.textContent=query?`${shown}/${cards.length}개`:`${cards.length}개`;
    };
    filter.addEventListener('input',applyFilter);
  }
  const noteForm=document.querySelector('.session-note-form');
  const noteInput=noteForm?.querySelector('textarea');
  const saveState=noteForm?.querySelector('.session-save-state');
  let noteDirty=false;
  if(noteForm&&noteInput&&saveState){
    const initial=noteInput.value;
    noteInput.addEventListener('input',()=>{
      noteDirty=noteInput.value!==initial;
      saveState.textContent=noteDirty?'저장되지 않은 주석 · 저장 버튼을 누르세요':saveState.dataset.savedLabel;
      saveState.classList.toggle('dirty',noteDirty);
    });
    noteForm.addEventListener('submit',()=>{noteDirty=false;});
    addEventListener('beforeunload',event=>{if(noteDirty){event.preventDefault();event.returnValue='';}});
  }
})();</script>"""


PROFILE_UI = {
    "speaker": {
        "title": "스피커 출력 체인",
        "short": "스피커 출력",
        "detail": "U7 스피커 출력에 연결된 스피커 체인",
    },
    "headphone": {
        "title": "헤드폰 잭 출력 체인",
        "short": "헤드폰 잭",
        "detail": "U7 헤드폰 잭에 연결된 별도 스피커 체인",
    },
}


def ui_icon(name: str, title: str = "", decorative: bool = True) -> str:
    """Small dependency-free SVG icon used by the signal-console UI."""
    paths = {
        "input": '<path d="M3 12h5m8 0h5M8 8v8m8-8v8"/><circle cx="12" cy="12" r="4"/>',
        "dsp": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4M9 13l2-3 2 5 2-4"/>',
        "route": '<path d="M4 6h5c3 0 3 6 6 6h5M4 18h5c3 0 3-6 6-6"/><path d="m18 9 3 3-3 3"/>',
        "speaker": '<path d="M4 9h4l5-4v14l-5-4H4zM17 9c1.5 1.7 1.5 4.3 0 6m2.7-8.5c3 3 3 8 0 11"/>',
        "woofer": '<rect x="5" y="3" width="14" height="18" rx="2"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="7" r="1.3"/>',
        "selector": '<path d="M5 5h5l4 7h5M5 19h5l4-7"/><circle cx="5" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="12" r="2"/>',
        "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4m-4 0h8"/>',
        "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
        "warning": '<path d="M12 3 2.5 20h19zM12 9v5m0 3h.01"/>',
        "wave": '<path d="M2 12h3l2-6 4 12 3-9 3 6 2-3h3"/>',
        "trash": '<path d="M4 7h16M9 7V4h6v3m-9 0 1 14h10l1-14M10 11v6m4-6v6"/>',
    }
    content = paths.get(name, paths["wave"])
    if decorative:
        return f'<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{content}</svg>'
    label = html.escape(title or name)
    return f'<svg class="ui-icon" viewBox="0 0 24 24" role="img" aria-label="{label}"><title>{label}</title>{content}</svg>'


def selector_ui_label(selector: dict, physical: str | None) -> str:
    if physical in PROFILE_UI:
        return PROFILE_UI[physical]["short"]
    if not selector.get("stale") and selector.get("state_byte"):
        return f"미확인 ({selector['state_byte']})"
    return "감지 대기"


def signal_flow_diagram(status: dict) -> str:
    """Render the live audio path as connected interface-style processing blocks."""
    settings = status.get("settings", {})
    resolved = status.get("resolved", {})
    selector = status.get("u7_selector", {})
    physical = selector.get("profile") if not selector.get("stale", True) else None
    effective = str(resolved.get("effective_profile", "speaker"))
    effective_ui = PROFILE_UI.get(effective, PROFILE_UI["speaker"])
    physical_ui = PROFILE_UI.get(str(physical), None)
    output_label = selector_ui_label(selector, physical)
    output_detail = physical_ui["detail"] if physical_ui else "제조사 전용 HID 값은 추정하지 않습니다. U7 상단 버튼을 한 번 눌러 확정하세요."
    rear_mode = str(resolved.get("effective_rear_mode", "copy_front"))
    rear_label = "프런트 FIR 후 복사" if rear_mode == "copy_front" else "별도 우퍼 FIR"
    bypass = bool(resolved.get("bypass"))
    dsp_label = "바이패스 · 원본 복사" if bypass else f"{effective_ui['short']} FIR"
    active_class = " is-active" if physical else " is-waiting"
    return f'''
    <section class="signal-console card-wide" aria-label="현재 오디오 신호 흐름">
      <div class="section-head"><div><h2>{ui_icon('wave', 'Audio signal')} 오디오 신호 흐름</h2><p class="muted">각 박스는 실제 처리 단계입니다. 선을 따라 현재 입력부터 물리 출력까지 확인할 수 있습니다.</p></div><span class="signal-live"><i></i> LIVE</span></div>
      <div class="signal-flow">
        <div class="signal-node">{ui_icon('input', 'Analog input')}<div><small>INPUT · 2 CH</small><b>U7 Line input</b><span>48 kHz · 24-bit device / 32-bit container</span></div></div>
        <div class="signal-wire" aria-hidden="true"><svg viewBox="0 0 70 20"><path d="M2 10h60"/><path d="m56 4 8 6-8 6"/></svg></div>
        <div class="signal-node dsp-node">{ui_icon('dsp', 'CamillaDSP')}<div><small>DSP</small><b>{html.escape(dsp_label)}</b><span>{resolved.get('convolution_channels', 0)}ch convolution · chunk {settings.get('chunksize', '?')}</span></div></div>
        <div class="signal-wire" aria-hidden="true"><svg viewBox="0 0 70 20"><path d="M2 10h60"/><path d="m56 4 8 6-8 6"/></svg></div>
      <div class="signal-node route-node">{ui_icon('route', '채널 라우팅')}<div><small>라우팅 · 4채널</small><b>프런트 + 우퍼</b><span>프런트 L/R · 우퍼 L/R<br>{html.escape(rear_label)}</span></div></div>
        <div class="signal-wire" aria-hidden="true"><svg viewBox="0 0 70 20"><path d="M2 10h60"/><path d="m56 4 8 6-8 6"/></svg></div>
        <div class="signal-node selector-node{active_class}">{ui_icon('selector', 'U7 출력 선택')}<div><small>U7 물리 출력 선택</small><b id="u7-flow-output">{html.escape(output_label)}</b><span>{html.escape(output_detail)}</span></div></div>
        <div class="signal-wire" aria-hidden="true"><svg viewBox="0 0 70 20"><path d="M2 10h60"/><path d="m56 4 8 6-8 6"/></svg></div>
        <div class="signal-node output-node{active_class}">{ui_icon('speaker', '스피커 출력 체인')}<div><small>물리 출력</small><b>{html.escape(output_label if physical else '스피커 출력 체인')}</b><span>두 U7 경로 모두 스피커에 연결됨</span></div></div>
      </div>
      <div class="signal-legend"><span>{ui_icon('speaker', '프런트')} 프런트 L/R</span><span>{ui_icon('woofer', '우퍼')} 우퍼 L/R</span><span>{ui_icon('selector', '하드웨어 출력 선택')} 출력 전환은 U7 상단 버튼</span></div>
    </section>'''


def measurement_panel(job: dict, preview: dict) -> str:
    state = str(job.get("state", "idle"))
    busy = state in ("running", "processing", "cancelling")
    try:
        saved_sessions = list((measurement("list-sessions") or {}).get("sessions", []))
    except Exception:
        saved_sessions = []
    positions = int(job.get("positions_completed", 0))
    total = int(job.get("positions_total", 3))
    position_count_options = ''.join(
        f'<option value="{value}" {"selected" if value == total else ""}>{label}</option>'
        for value, label in ((1, "빠른 측정 · 기준점 1위치"), (3, "표준 측정 · 중앙+좌우 3위치 · 권장"))
    )
    position_count_note = (
        '<p class="form-note position-count-note"><b>빠른 측정 · 1위치:</b> 선택한 모든 출력 조합을 청취 기준점에서 각 1회 측정합니다. 빠르고 그 한 점은 잘 맞지만, 머리를 움직일 때 생기는 딥과 봉우리를 구분하지 못해 과보정 위험이 큽니다.<br>'
        '<b>표준 측정 · 3위치:</b> 중앙과 가까운 좌우 위치에서 공통 문제만 보정합니다. 저역 부밍·크로스오버 합산과 좌석 안정성 검증에 권장합니다.</p>'
    )
    progress = max(0.0, min(100.0, float(job.get("progress", 0.0))))
    eta = job.get("eta_seconds")
    eta_text = f" · 예상 {int(eta)}초" if isinstance(eta, (int, float)) else ""
    calibration = job.get("calibration") or {}
    result = job.get("result") or {}
    stale_result = bool(job.get("result_revision_status", {}).get("stale"))
    level = job.get("level_check") or {}
    level_recording_inventory = job.get("level_recording_inventory") or {}
    capture_inventory = job.get("capture_inventory") or {}
    raw_capture_count = int(capture_inventory.get("raw_count", 0))
    response_count = int(capture_inventory.get("response_count", 0))
    expected_capture_count = int(capture_inventory.get("expected", total * len(job.get("sources") or ())))
    preferences = job.get("correction_preferences") or {}
    installed = job.get("installed_calibrations") or {}
    capabilities = job.get("capabilities") or {}
    mimo_supported = bool(capabilities.get("mimo_supported"))
    selector = job.get("output_selector") or {}
    current_profile = selector.get("profile") if not selector.get("stale", True) else None
    measured_profile = job.get("measurement_profile")
    path_match = job.get("measurement_output_match")
    current_path_ui = PROFILE_UI.get(str(current_profile))
    measured_path_ui = PROFILE_UI.get(str(measured_profile))
    if measured_path_ui and path_match is True:
        path_class = "path-ok"
        path_icon = ui_icon("check", "경로 일치")
        path_title = "측정 출력 경로 고정됨"
        path_note = f"현재 U7 출력과 일치 · {measured_path_ui['detail']}"
    elif measured_path_ui:
        path_class = "path-error"
        path_icon = ui_icon("warning", "경로 불일치")
        path_title = "U7 출력이 측정 경로와 다름"
        path_note = f"필요: {measured_path_ui['short']} · 현재: {current_path_ui['short'] if current_path_ui else '감지 불가'} · 원래 경로로 되돌리기 전에는 측정과 A/B 비교를 차단합니다."
    else:
        path_class = "path-wait"
        path_icon = ui_icon("selector", "출력 경로 선택")
        path_title = "레벨 검사에서 출력 경로를 고정합니다"
        path_note = f"현재: {current_path_ui['detail'] if current_path_ui else 'U7 물리 출력 감지 대기'}"
    path_lock_html = f'''<div class="measurement-path-lock {path_class}" data-measurement-path="{html.escape(str(measured_profile or 'unbound'))}" data-measurement-step-content="2">{path_icon}<div><small>측정 출력 고정</small><b>{html.escape(path_title)}</b><span>{html.escape(path_note)}</span></div></div>'''
    cal90 = installed.get("90") or {}
    cal0 = installed.get("0") or {}
    premeasurement_sum = job.get("premeasured_sum_validation") or {}
    premeasurement_validation_failed = (
        str(job.get("mode")) == "lrw_sum"
        and positions == total
        and premeasurement_sum.get("pass") is False
    )
    if job.get("applied_profile"):
        current_step = 6
    elif result and not stale_result:
        current_step = 5
    elif premeasurement_validation_failed:
        current_step = 3
    elif positions == total:
        current_step = 4
    elif state != "idle" and level.get("ok"):
        current_step = 3
    elif state != "idle":
        current_step = 2
    else:
        current_step = 1
    result_validation = result.get("self_validation", {}) if result else {}
    result_crossover_check = result_validation.get("crossover_sum") or {}
    result_validation_pending = bool(result_crossover_check.get("required")) and result_crossover_check.get("pass") is None
    result_validation_failed = bool(result) and not stale_result and not result_validation_pending and not bool(result_validation.get("overall_pass"))
    workflow_items = []
    step_labels = ((1, "출력 설정"), (2, "레벨 확인"), (3, "위치 측정"), (4, "FIR 계산"), (5, "A/B 검토"), (6, "정식 적용"))
    for number, label in step_labels:
        classes = "current" if number == current_step else "done" if number < current_step else "future"
        step_has_failure = (result_validation_failed and number == 5) or (premeasurement_validation_failed and not result and number == 3)
        if step_has_failure:
            classes += " validation-error"
        content = f'<span>{number}</span><b>{label}</b>{"<em>FAIL</em>" if step_has_failure else ""}'
        current_attr = ' aria-current="step"' if number == current_step else ""
        selected = "true" if number == current_step else "false"
        tabindex = "0" if number == current_step else "-1"
        workflow_items.append(
            f'<button type="button" role="tab" class="flow-step {classes}" id="measurement-tab-{number}" '
            f'aria-controls="measurement-panel-{number}" aria-selected="{selected}" tabindex="{tabindex}" '
            f'data-measurement-tab="{number}"{current_attr} title="측정값을 유지하고 {number}단계 화면 열기">{content}</button>'
        )
    workflow = "".join(workflow_items)
    state_labels = {
        "idle": "활성 세션 없음", "ready": "측정 준비", "running": "측정 실행 중",
        "processing": "응답·FIR 계산 중", "measured": "측정 완료", "built": "FIR 생성 완료",
        "cancelling": "취소 처리 중", "error": "확인 필요",
    }
    active_session_id = str(job.get("session_id", ""))
    if active_session_id:
        active_mode_label = dict(MEASUREMENT_MODE_OPTIONS).get(str(job.get("mode", "lrw")), str(job.get("mode", "lrw")))
        created_unix = float(job.get("created_unix", 0) or 0)
        updated_unix = float(job.get("updated_unix", 0) or 0)
        created_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(created_unix)) if created_unix else "기록 없음"
        updated_text = time.strftime("%H:%M:%S", time.localtime(updated_unix)) if updated_unix else "기록 없음"
        result_text = "FIR 결과 있음" if result else "FIR 결과 없음"
        note = html.escape(str(job.get("session_note", "")))
        session_overview = f'''
        <section class="session-overview" aria-labelledby="active-session-title">
          <div class="session-overview-head"><div><small>활성 세션 · 자동 저장</small><h3 id="active-session-title">{html.escape(active_session_id)}</h3></div><span class="pill">{html.escape(state_labels.get(state, state))}</span></div>
          <div class="session-meta-grid">
            <div><small>측정 구성</small><b>{html.escape(active_mode_label)}</b></div><div><small>생성</small><b>{created_text}</b></div><div><small>완료 위치</small><b>{positions}/{total}</b></div>
            <div><small>이어갈 단계</small><b>{current_step} · {dict(step_labels)[current_step]}</b></div>
            <div><small>결과</small><b>{result_text}</b></div>
          </div>
          <form method="post" action="/measurement/session-note" class="session-note-form">
            <label for="active-session-note"><b>세션 주석</b><span>주석만 저장하며 1–6단계 진행 상태와 측정값은 그대로 유지합니다.</span></label>
            <textarea id="active-session-note" name="note" rows="2" maxlength="500" placeholder="예: 청취 위치 중앙, 야간 저레벨, 우퍼 노브 11시">{note}</textarea>
            <div><small class="session-save-state" data-saved-label="마지막 자동 저장 {updated_text}" role="status" aria-live="polite">마지막 자동 저장 {updated_text}</small><button type="submit" class="secondary">주석 저장</button></div>
          </form>
        </section>'''
    else:
        session_overview = '''<section class="session-overview empty" aria-labelledby="active-session-title"><div class="session-overview-head"><div><small>활성 세션</small><h3 id="active-session-title">활성 세션 없음</h3></div><span class="pill neutral">1단계에서 생성</span></div><p>1단계에서 새 세션을 만들거나 저장된 세션을 불러오면 완료 지점과 주석이 계속 표시됩니다.</p></section>'''

    session_cards = []
    for saved in saved_sessions:
        saved_id = str(saved.get("session_id", ""))
        if not saved_id:
            continue
        saved_positions = int(saved.get("positions_completed", 0))
        saved_total = int(saved.get("positions_total", 3))
        has_result = bool(saved.get("has_result"))
        applied = bool(saved.get("applied_profile"))
        level_ok_saved = bool(saved.get("level_ok"))
        completed_step = 6 if applied else 4 if has_result else 3 if saved_positions >= saved_total else 2 if level_ok_saved else 1
        resume_step = 6 if applied else 5 if has_result else 4 if saved_positions >= saved_total else 3 if level_ok_saved else 2
        created = float(saved.get("created_unix", 0) or 0)
        created_label = time.strftime("%Y-%m-%d %H:%M", time.localtime(created)) if created else "날짜 없음"
        saved_note = str(saved.get("note", "")).strip()
        saved_mode = str(saved.get("mode", "lrw"))
        saved_mode_label = dict(MEASUREMENT_MODE_OPTIONS).get(saved_mode, saved_mode)
        note_html = html.escape(saved_note) if saved_note else '<span class="muted">주석 없음</span>'
        search_token = html.escape(f"{saved_id} {created_label} {saved.get('mode', 'lrw')} {saved_note}".lower())
        is_active = bool(saved.get("active")) or saved_id == active_session_id
        load_action = '<span class="pill">현재 세션</span>' if is_active else f'''<form method="post" action="/measurement/load-session" onsubmit="return confirm('현재 세션은 자동 저장됩니다. 선택한 세션을 불러올까요?')"><input type="hidden" name="session_id" value="{html.escape(saved_id)}"><button class="secondary"{' disabled' if busy else ''}>이어하기 · {resume_step}단계</button></form>'''
        delete_text = (
            f"세션 {saved_id}을 삭제합니다. 위치 {saved_positions}/{saved_total}, "
            f"{'FIR 결과 있음' if has_result else 'FIR 결과 없음'}, 주석: {saved_note or '없음'}. "
            "측정 원본과 이 세션의 생성 파일은 복구할 수 없지만 현재 정식 프로필 FIR은 변경되지 않습니다. 삭제할까요?"
        )
        delete_message = html.escape(json.dumps(delete_text, ensure_ascii=False), quote=True)
        delete_action = f'''<form method="post" action="/measurement/delete-session" onsubmit="return confirm({delete_message})"><input type="hidden" name="session_id" value="{html.escape(saved_id)}"><button class="danger session-delete"{' disabled' if busy else ''}>{ui_icon('trash', '세션 삭제')}<span>삭제</span></button></form>'''
        action = f'<div class="saved-session-actions">{load_action}{delete_action}</div>'
        progress_dots = "".join(f'<i class="{"done" if number <= completed_step else ""}"></i>' for number in range(1, 7))
        session_cards.append(f'''
        <article class="saved-session{' active' if is_active else ''}" data-session-search="{search_token}">
          <div class="saved-session-head"><div><b>{html.escape(saved_id)}</b><small>{created_label} · {html.escape(saved_mode_label)}</small></div>{action}</div>
          <p class="session-note-preview">{note_html}</p>
          <div class="saved-session-progress" aria-label="6단계 중 {completed_step}단계까지 완료">{progress_dots}<span>{completed_step}/6 완료 · 위치 {saved_positions}/{saved_total}{' · FIR 있음' if has_result else ''}</span></div>
        </article>''')
    session_library = f'''
    <details class="session-tools" data-measurement-step-content="1" open>
      <summary>저장된 세션 · 이어하기 <span class="pill neutral">{len(session_cards)}개</span></summary>
      <p class="muted">세션은 자동 저장됩니다. 불러오면 완료 단계·측정값·FIR 결과를 그대로 이어갑니다.</p>
      {f'<label class="session-filter" for="session-filter-input"><span>세션 ID·주석 검색</span><input id="session-filter-input" type="search" placeholder="날짜, ID, 주석으로 찾기" autocomplete="off"></label>' if session_cards else ''}
      <div class="session-library">{''.join(session_cards) if session_cards else '<p class="measurement-panel-empty">저장된 세션이 없습니다.</p>'}</div>
      <p class="measurement-panel-empty session-filter-empty" hidden>일치하는 세션이 없습니다.</p>
    </details>'''
    cal90_summary = (
        f"일련번호 {html.escape(str(cal90.get('serial')))} · {cal90.get('points')}점 · 감도 {cal90.get('sensitivity_db')} dB"
        if cal90.get("available") else "90° 보정 파일 없음"
    )
    cal0_summary = (
        f"일련번호 {html.escape(str(cal0.get('serial')))} · {cal0.get('points')}점 · 감도 {cal0.get('sensitivity_db')} dB"
        if cal0.get("available") else "0° 보정 파일 없음"
    )
    controls = ""
    if state == "idle":
        mode_options = measurement_mode_options("lrw_sum", mimo_supported)
        controls = f"""
        <form method="post" action="/measurement/new" class="measure-form session-settings" data-measurement-step-content="1">
          <label>측정 구성<select name="mode" class="measurement-mode-select">{mode_options}</select></label>
          <label>UMIK 방향<select name="orientation"><option value="90" selected>90° · 천장 방향 · 권장</option></select></label>
          <label>청취 위치 범위<select name="position_count">{position_count_options}</select></label>
          <input type="hidden" name="noise_level_dbfs" value="-42"><input type="hidden" name="level_dbfs" value="-42"><input type="hidden" name="woofer_measurement_attenuation_db" value="-9"><input type="hidden" name="sweep_seconds" value="8">
          <button>세션 생성</button>
          {position_count_note}
          <p class="form-note mode-route">{html.escape(MEASUREMENT_MODE_HELP['lrw_sum'])}</p>
          <p class="form-note"><b>이 단계에서 정하는 값:</b> 어떤 출력 조합을 몇 위치에서 측정할지 정합니다. 빠른 검사와 본 측정의 스윕 출력은 2단계에서 한 번만 설정합니다.</p>
        </form>
        {session_library}"""
    else:
        disabled = " disabled" if busy else ""
        level_ok = bool(level.get("ok"))
        position_disabled = " disabled" if busy or not level_ok or path_match is not True else ""
        mode = str(job.get("mode", "lrw"))
        source_sequence = {
            "lr": "L+우퍼 → R+우퍼",
            "lrw": "프런트 L → 우퍼 → 프런트 R",
            "lrw_sum": "프런트 L → 우퍼 → 프런트 R → L+우퍼 → R+우퍼",
            "mimo_stereo": "프런트 L → 프런트 R",
            "mimo_one_sub": "프런트 L → 우퍼 → 프런트 R",
            "mimo_dual_sub": "프런트 L → 우퍼 1 → 프런트 R → 우퍼 2",
        }.get(mode, "선택한 출력 순서")
        source_count = len(job.get("sources") or ())
        mode_options = measurement_mode_options(mode, mimo_supported)
        level_dbfs = int(job.get("level_dbfs", -42))
        noise_level_dbfs = int(job.get("noise_level_dbfs", job.get("level_dbfs", -42)))
        woofer_measurement_attenuation_db = int(job.get("woofer_measurement_attenuation_db", -9))
        sweep_seconds = int(job.get("sweep_seconds", 8))
        level_html = ""
        if level:
            lvl_ok = bool(level.get('ok'))
            snr_val = level.get('assessment_snr_db', level.get('snr_db', '?'))
            level_action = html.escape(str(level.get('level_action', '현재 설정 유지')))
            level_channels = level.get("channels") or []
            level_channel_html = ""
            if level_channels:
                rows = "".join(
                    f'''<div class="{'validation-pass' if channel.get('ok') else 'validation-fail'}"><small>{html.escape(str(channel.get('source_label', channel.get('source', '출력'))))}</small><b>{'PASS' if channel.get('ok') else 'FAIL'} · {channel.get('assessment_snr_db', channel.get('snr_db', '?'))} dB</b><span>{'–'.join(str(value) for value in (channel.get('analysis_band_hz') or ['?', '?']))} Hz</span></div>'''
                    for channel in level_channels
                )
                level_channel_html = f'<div class="diagnostic-grid level-channel-grid">{rows}</div>'
            level_html = f'''<div class="level-result {'ok' if lvl_ok else 'not-ok'}" style="margin-top:14px">
              <div class="level-verdict">
                <b>{'PASS · 사전 확인' if lvl_ok else 'FAIL · 레벨 조정'}</b>
                <span>{html.escape(str(level.get('verdict', '')))}</span>
              </div>
              <div class="metric-grid" style="margin-top:10px">
                <div><small>빠른 스윕 최저 SNR</small><b style="color:{'var(--success)' if lvl_ok else 'var(--danger)'}">{snr_val} dB</b><span>합격 6 dB</span></div>
                <div><small>권장 품질</small><b>15 dB 이상</b><span>필수 합격선이 아닌 노이즈 내성 권장값</span></div>
                <div><small>본 측정 판정</small><b>같은 하한 6 dB</b><span>출력 조합별로 다시 확인</span></div>
                <div><small>배경 RMS</small><b>{level.get('background_rms_dbfs', '?')} dBFS</b></div>
                <div><small>입력 Peak</small><b>{level.get('peak_dbfs', '?')} dBFS</b></div>
              </div>
              {level_channel_html}
              <p class="level-action"><b>권장:</b> {level_action}</p>
              <p class="muted">빠른 검사와 본 측정은 모든 출력 조합에 같은 6 dB 하한을 적용합니다. 15 dB는 FIR 계산을 막는 기준이 아니라 생활소음에 대한 여유를 늘리는 권장 품질입니다.</p>
              {f'<div class="step-nav-bar" style="margin-top:16px"><button type="button" class="validation-jump primary-jump" data-measurement-jump="3">위치 측정으로</button></div>' if lvl_ok else ''}
            </div>'''

        session_settings = f"""
        <form method="post" action="/measurement/configure" class="measure-form session-settings" data-measurement-step-content="1" onsubmit="return confirm('측정 구성 변경을 적용하면 영향을 받는 레벨 검사·위치 측정·FIR 결과만 초기화합니다. 적용할까요?')">
          <label>측정 구성<select name="mode" class="measurement-mode-select">{mode_options}</select></label>
          <label>UMIK 방향<select name="orientation"><option value="90" selected>90° · 천장 방향 · 권장</option></select></label>
          <label>청취 위치 범위<select name="position_count">{position_count_options}</select></label>
          <input type="hidden" name="level_dbfs" value="{level_dbfs}"><input type="hidden" name="noise_level_dbfs" value="{noise_level_dbfs}"><input type="hidden" name="woofer_measurement_attenuation_db" value="{woofer_measurement_attenuation_db}"><input type="hidden" name="sweep_seconds" value="{sweep_seconds}">
          <button>구성 적용</button>
          {position_count_note}
          <p class="form-note mode-route">{html.escape(MEASUREMENT_MODE_HELP.get(mode, ''))}</p>
          <p class="form-note">탭을 오가거나 값을 선택만 해서는 측정값이 지워지지 않습니다. 이 버튼으로 실제 변경할 때 영향받는 이후 단계만 초기화합니다.</p>
        </form>
        <div class="session-new-action" data-measurement-step-content="1"><div><b>새 세션</b><p class="muted">현재 세션은 자동 저장되어 다시 불러올 수 있습니다.</p></div><form method="post" action="/measurement/new" onsubmit="return confirm('현재 세션을 저장하고 같은 설정으로 새 측정을 시작할까요?')"><input type="hidden" name="mode" value="{mode}"><input type="hidden" name="orientation" value="90"><input type="hidden" name="level_dbfs" value="{level_dbfs}"><input type="hidden" name="noise_level_dbfs" value="{noise_level_dbfs}"><input type="hidden" name="woofer_measurement_attenuation_db" value="{woofer_measurement_attenuation_db}"><input type="hidden" name="sweep_seconds" value="{sweep_seconds}"><input type="hidden" name="position_count" value="{total}"><button class="secondary"{' disabled' if busy else ''}>새 세션</button></form></div><div class="step-nav-bar" style="margin-top:14px" data-measurement-step-content="1"><button type="button" class="validation-jump primary-jump" data-measurement-jump="2">레벨 확인으로</button></div>"""

        if positions >= total:
            position_control = f'''<form method="post" action="/measurement/restart-positions" id="measurement-step-3" onsubmit="return confirm(\'{total}곳 측정을 처음부터 다시 시작합니다. 기존 측정·검증·생성 FIR 결과를 초기화할까요?\')"><button{position_disabled}>{total}곳 처음부터 재측정</button></form>
            <div class="step-nav-bar"><button type="button" class="validation-jump primary-jump" data-measurement-jump="4">FIR 계산으로</button></div>'''
        else:
            woofer_effective = level_dbfs + woofer_measurement_attenuation_db
            pos_num = positions + 1
            if total == 1:
                pos_guide = "위치 1/1: 마이크를 [중앙 기준 좌석]에 천장 방향(90°)으로 고정하고 시작하세요."
                pos_button_text = "위치 1/1 측정"
            else:
                if pos_num == 1:
                    pos_guide = "1/3 위치: 마이크를 [중앙 기준 좌석]에 천장 방향(90°)으로 고정하고 시작하세요."
                    pos_button_text = "위치 1/3 측정"
                elif pos_num == 2:
                    pos_guide = "2/3 위치: 마이크를 [중앙에서 좌측으로 약 15~20cm] 이동하여 천장 방향(90°)으로 두고 시작하세요."
                    pos_button_text = "위치 2/3 측정"
                else:
                    pos_guide = "3/3 위치: 마이크를 [중앙에서 우측으로 약 15~20cm] 이동하여 천장 방향(90°)으로 두고 시작하세요."
                    pos_button_text = "위치 3/3 측정"
            pos_confirm_msg = f"{pos_guide}\n\n[재생 정보]\n{source_count}개 조합 순차 재생 ({source_sequence})\nDAC 기준: {level_dbfs} dBFS (우퍼 실효 {woofer_effective} dBFS)\nU7 청취 볼륨은 자동으로 무시하고 종료 후 복원합니다.\n\n준비되셨으면 확인을 누르세요."
            pos_confirm_escaped = html.escape(json.dumps(pos_confirm_msg, ensure_ascii=False), quote=True)
            position_control = f'<form method="post" action="/measurement/position" id="measurement-step-3" onsubmit="return confirm({pos_confirm_escaped})"><button{position_disabled}>{pos_button_text}</button></form>'
            if positions > 0:
                position_control += f'<form method="post" action="/measurement/restart-positions" onsubmit="return confirm(\'완료한 위치 측정을 버리고 위치 1부터 다시 시작할까요?\')"><button{disabled} class="secondary">{total}곳 처음부터 다시</button></form>'

        pre_sum = job.get("premeasured_sum_validation") or {}
        pre_sum_html = ""
        if mode == "lrw_sum" and positions == total:
            pre_sum_pass = bool(pre_sum.get("pass"))
            pre_sum_channels = pre_sum.get("channels") or {}
            pre_sum_metrics = "".join(
                f'<div><small>{"L" if side == "left" else "R"} 모델 일치</small><b>MAE {values.get("magnitude_mae_db", "?")} / P90 {values.get("magnitude_p90_abs_error_db", "?")} dB</b><span>위상 중앙값 {values.get("phase_median_abs_error_deg", "?")}° · P90 {values.get("phase_p90_abs_error_deg", "?")}°</span></div>'
                for side, values in pre_sum_channels.items()
            )
            pre_sum_phase_limited = pre_sum.get("phase_verification_status") == "limited"
            pre_sum_html = f'''<section class="pre-sum-card {'pass' if pre_sum_pass else 'fail'}" data-measurement-step-content="3"><div class="section-head"><div><h4>필터 전 합산 일치 검증</h4><p>L/R/우퍼 예측과 같은 위치의 L+우퍼/R+우퍼 실측 비교 · 개별 정규화 없음</p></div><span class="status-badge {'pass' if pre_sum_pass else 'fail'}">{'PASS' if pre_sum_pass else 'FAIL'}</span></div><div class="diagnostic-grid">{pre_sum_metrics}</div>{'<p class="diagnostic-note"><b>위상 정밀도 제한</b> · 합산 크기와 SNR은 통과했습니다. U7 출력과 UMIK-1 입력은 하드웨어 clock을 공유하지 않아 절대 위상은 참고값이며, FIR은 보수적인 감쇄 전용 합산 상한으로 보호합니다.</p>' if pre_sum_phase_limited else ''}{'' if pre_sum_pass else f'<p class="failure"><b>실행할 조치</b> · {html.escape(str(pre_sum.get("action", "3단계 측정을 다시 확인하세요.")))}</p>'}</section>'''

        capture_recovery_html = ""
        if expected_capture_count:
            recovery_action = ""
            if bool(capture_inventory.get("can_reprocess_all")) and response_count < expected_capture_count and not busy:
                recovery_action = '''<form method="post" action="/measurement/reprocess-saved"><button class="secondary">원본 재계산</button></form>'''
            capture_recovery_html = f'''<section class="capture-recovery" data-measurement-step-content="3"><div><small>저장 상태</small><b>녹음 {raw_capture_count}/{expected_capture_count} · 응답 {response_count}/{expected_capture_count}</b><span>‘원본 재계산’은 저장 WAV만 사용하며 소리를 재생하지 않습니다.</span></div>{recovery_action}</section>'''

        controls = f"""
        {session_settings}
        {session_library}
        <div class="level-step-card" data-measurement-step-content="2">
          <section class="sub-card">
            <h4>2단계 · 출력 설정과 빠른 검사</h4>
            <p class="muted">현재 측정 구성의 모든 출력 조합을 각 2초씩 검사합니다. 본 측정과 같은 15 Hz–22 kHz 스윕·라우팅·SNR 계산을 사용하며 응답/FIR 계산만 생략합니다.</p>
            <form method="post" action="/measurement/configure-level" class="measure-form level-check-form measurement-output-form" onsubmit="return confirm('선택한 모든 출력 조합을 각 2초씩 재생합니다. 마이크 주변을 조용히 유지해주세요.')">
              <input type="hidden" name="mode" value="{html.escape(mode)}"><input type="hidden" name="orientation" value="90"><input type="hidden" name="position_count" value="{total}">
              <div class="level-controls-grid">
                <label class="level-slider">DAC 기준 스윕 출력 <output>{level_dbfs} dBFS</output>
                  <input name="level_dbfs" type="range" min="-54" max="0" step="1" value="{level_dbfs}" data-unit="dBFS">
                  <input type="hidden" name="noise_level_dbfs" value="{level_dbfs}">
                  <small>빠른 검사·본 측정 모두 입력 OFF → U7 PCM 0 dB → sweep → 원래 볼륨 복원 → 입력 복귀 순서로 실행합니다. 현재 청취 볼륨은 측정 출력에 더해지지 않습니다.</small>
                </label>
              <label class="level-slider woofer-level-slider"><span class="woofer-level-heading">우퍼 측정 감쇄</span> <output>{woofer_measurement_attenuation_db} dB</output>
                  <input name="woofer_measurement_attenuation_db" type="range" min="-18" max="0" step="1" value="{woofer_measurement_attenuation_db}" data-unit="dB">
                <small>우퍼 과부하 방지 · 실효 <b class="effective-woofer-level">{level_dbfs + woofer_measurement_attenuation_db} dBFS</b> · 역컨볼루션에서 자동 복원</small>
                </label>
              </div>
              <label>본 스윕 길이<select name="sweep_seconds">{''.join(f'<option value="{value}" {"selected" if value == sweep_seconds else ""}>{value}초{" · 빠른 시험" if value == 4 else " · 표준 · 권장" if value == 8 else " · 고정밀 · SNR 향상" if value == 12 else " · 저음량 고정밀"}</option>' for value in (4, 8, 12, 14))}</select><small>3단계 위치 측정에 사용합니다.</small></label>
              <div class="output-level-warning"></div>
              <button{disabled}>빠른 검사</button>
            </form>
            {level_html}
          </section>
        </div>
        {f'<div class="measure-actions" data-measurement-step-content="2"><form method="post" action="/measurement/cancel"><button class="danger">작업 취소</button></form></div>' if busy else ''}
        <div class="measure-actions" data-measurement-step-content="3">
          {position_control}
        </div>
        {capture_recovery_html}
        {pre_sum_html}
            <p class="muted" data-measurement-step-content="3"><b>위치당 측정 순서:</b> {html.escape(source_sequence)}. 한 위치의 {source_count}개 스윕을 DSP 재시작이나 FFT 대기 없이 먼저 연속 녹음하고, 소리가 모두 멈춘 뒤 응답을 일괄 계산합니다. 완료된 채널은 재시도할 때 다시 재생하지 않습니다.</p><p class="form-note" data-measurement-step-content="3"><b>정밀 모드 계산 원칙:</b> L/R/우퍼만 FIR 설계에 사용합니다. L+우퍼/R+우퍼는 물리 합산 일치 검증에만 한 번 사용하므로 보정·평균·정규화가 중복되지 않습니다.</p>"""

        if positions == total:
            build_open = f'<fieldset class="build-fieldset"{" disabled" if busy else ""}>'
            build_status = '<p class="form-note build-running-note"><b>FIR 계산 진행 중:</b> 현재 선택값을 표시하고 있습니다. 중복 계산을 막기 위해 완료될 때까지 옵션만 잠급니다.</p>' if busy else ''
            build_button_label = "계산 중…" if busy else "FIR 계산"
            target_labels = (("harman", "Harman Kardon"), ("rtings", "RTINGS"), ("acoustix", "AcoustiX Default"), ("toole", "Not Dr. Toole"), ("bk", "Brüel & Kjær"), ("flat", "Flat"))
            target_options = ''.join(f'<option value="{value}" {"selected" if preferences.get("target", "flat") == value else ""}>{label}</option>' for value, label in target_labels)
            preset_options = ''.join(f'<option value="{value}" {"selected" if preferences.get("preset", "none") == value else ""}>{label}</option>' for value, label in (("none", "추가 억제 없음 · 타깃 기준"), ("primus360", "Primus 360 수준"), ("strong", "T5S 강한 억제 · 현재 선호")))
            phase_options = ''.join(f'<option value="{value}" {"selected" if preferences.get("phase_mode", "bass") == value else ""}>{label}</option>' for value, label in (("bass", "저역 음량+위상"), ("magnitude", "음량만 · 최소위상")))
            if mode == "lr":
                woofer_trim_control = f'<label>우퍼 최종 트림<input type="hidden" name="woofer_trim_db" value="{woofer_measurement_attenuation_db}"><output>{woofer_measurement_attenuation_db} dB · 합산 측정값과 고정</output></label>'
            else:
                woofer_trim_control = '<label>우퍼 최종 트림<select name="woofer_trim_db">' + "".join(f'<option value="{value}" {"selected" if value == preferences.get("woofer_trim_db", 0) else ""}>{value} dB{" · 선택 타깃 기준" if value == 0 else " · 기준 저역 추가 감쇄"}</option>' for value in range(0, -19, -1)) + '</select></label>'
            if mode in ("lrw", "lrw_sum", "mimo_one_sub", "mimo_dual_sub"):
                crossover_control = '<label>디지털 크로스오버<select name="crossover_enabled">' + "".join(
                    f'<option value="{value}" {"selected" if enabled == bool(preferences.get("crossover_enabled", True)) else ""}>{label}</option>'
                    for value, enabled, label in (("on", True, "ON · 권장/기본"), ("off", False, "OFF · full-range 중첩"))
                ) + '</select></label><label>크로스오버 주파수<select name="crossover_frequency_hz">' + "".join(
                    f'<option value="{value}" {"selected" if value == int(preferences.get("crossover_frequency_hz", 100)) else ""}>{value} Hz</option>'
                    for value in (60, 70, 80, 90, 100, 120)
                ) + '</select></label>'
                if mode == "lrw_sum":
                    crossover_note = f'<p class="form-note"><b>기본 켜짐 · 정밀 검증:</b> 프런트 LR4 HPF와 우퍼 LR4 LPF를 32768탭 WAV에 내장합니다. 선택한 {total}위치에서 L/R/우퍼 예측이 실제 L+우퍼/R+우퍼와 일치하는지 먼저 확인하고, 최종 합산 상한을 감쇄 전용으로 보호합니다. 추가 CamillaDSP 처리 단계나 블록 지연은 없습니다.</p>'
                else:
                    crossover_note = f'<p class="form-note"><b>기본 켜짐:</b> 프런트 LR4 HPF와 우퍼 LR4 LPF를 32768탭 WAV에 내장합니다. 선택한 {total}위치의 신뢰 가능한 시간 기준으로 합산 타깃과 상한을 검증합니다. L+우퍼/R+우퍼 물리 합산과 모델 일치까지 확인하려면 다음 세션에서 ‘정밀 분리+합산’을 선택하세요.</p>'
            else:
                crossover_control = '<input type="hidden" name="crossover_enabled" value="off"><input type="hidden" name="crossover_frequency_hz" value="100">'
                crossover_note = '<p class="form-note"><b>크로스오버 꺼짐:</b> 이 모드는 프런트/우퍼 독립 분기가 없어 HPF/LPF를 나눌 수 없습니다. 디지털 크로스오버를 쓰려면 L/R/우퍼 개별 측정을 선택하세요.</p>'
            controls += """
            <form method="post" action="/measurement/build" id="measurement-step-4" class="measure-form build-options" data-measurement-step-content="4" onsubmit="return confirm('측정 원본은 유지하고 기존 생성 FIR/A-B 임시 결과만 초기화한 뒤 다시 계산합니다. 계속할까요?')">
              """ + build_open + build_status + """
              <p class="form-note baseline-note"><b>타깃 기준 조합:</b> 우퍼 과잉 억제 ‘추가 억제 없음’ + 우퍼 트림 0 dB + 추가 저음/고음 0 dB이면 최종 L+우퍼/R+우퍼 합산 음압이 선택 타깃을 목표로 합니다. 측정 출력 dBFS는 이 기준을 바꾸지 않습니다.</p>
              <label>기준 음색 타깃<select name="target" id="target-choice">""" + target_options + """</select></label>
              <label>음색 시작점<select id="voicing-quick"><option value="current" selected>현재 세부값 유지</option><option value="neutral">타깃 그대로 · +0 / +0 dB</option><option value="clear">맑은 고음 · 저음 +0 / 고음 +1 dB</option><option value="warm">따뜻한 균형 · 저음 +2 / 고음 −1 dB</option><option value="night">야간 균형 · 저음 −2 / 우퍼 −3 dB</option></select><span>넓은 대역 음색의 빠른 시작점입니다. 고급 설정과 타깃 그래프가 즉시 바뀌며, A/B 비교 후 적용하세요.</span></label>
              <label>우퍼 과잉 억제<select name="preset">""" + preset_options + """</select></label>
              """ + woofer_trim_control + """
              <label>위상 방식<select name="phase_mode">""" + phase_options + """</select></label>
              """ + crossover_control + crossover_note + """
              <button>""" + build_button_label + """</button>
              <details class="advanced"><summary>고급 보정 설정 · 기본값은 안전 권장값</summary><div class="advanced-grid">
                <label>공간 대표 응답<select name="spatial_mode">""" + ''.join(f'<option value="{value}" {"selected" if preferences.get("spatial_mode", "equal") == value else ""}>{label}</option>' for value, label in (("equal", "세 위치 균등 · 넓은 청취영역"), ("center", "중앙 우선 · 고역 중심 가중"))) + """</select></label>
                <label>추가 저음 취향<select name="bass_tilt_db">""" + "".join(f'<option value="{value}" {"selected" if value == preferences.get("bass_tilt_db", 0) else ""}>{value:+d} dB @ 20 Hz</option>' for value in range(-6, 7)) + """</select></label>
                <label>추가 고음 경사<select name="treble_tilt_db">""" + "".join(f'<option value="{value}" {"selected" if value == preferences.get("treble_tilt_db", 0) else ""}>{value:+d} dB @ 20 kHz</option>' for value in range(-6, 3)) + """</select></label>
                <label>룸보정 하한<select name="correction_low_hz">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("correction_low_hz", 20) else ""}>{value} Hz</option>' for value in (20, 30, 40, 60, 80)) + """</select></label>
                <label>룸보정 상한<select name="correction_high_hz">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("correction_high_hz", 20000) else ""}>{value // 1000 if value >= 1000 else value}{" kHz" if value >= 1000 else " Hz"}{" · 자연 고역 권장" if value == 5000 else " · 전대역" if value == 20000 else ""}</option>' for value in (300, 500, 1000, 5000, 20000)) + """</select></label>
                <label>최대 룸 부스트<select name="max_boost_db">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("max_boost_db", 6) else ""}>+{value} dB</option>' for value in (0, 3, 6, 9)) + """</select></label>
                <label>최대 룸 감쇄<select name="max_cut_db">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("max_cut_db", 18) else ""}>−{value} dB</option>' for value in (6, 9, 12, 18, 24)) + """</select></label>
                <label>저역 위상 상한<select name="phase_cutoff">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("phase_cutoff", 200) else ""}>{value} Hz</option>' for value in (80, 120, 160, 200, 250)) + """</select></label>
                <label>MIMO 공동제어 상한<select name="mimo_high_hz">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("mimo_high_hz", 150) else ""}>{value} Hz</option>' for value in (80, 120, 150)) + """</select></label>
                <label>MIMO 강도<select name="mimo_strength">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("mimo_strength", "balanced") else ""}>{label}</option>' for value, label in (("safe", "Safe · 높은 안정성"), ("balanced", "Balanced · 권장"), ("maximum", "Maximum · 측정영역 우선"))) + """</select></label>
                <label>지원 제어원 제한<select name="mimo_support_penalty_db">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("mimo_support_penalty_db", 6) else ""}>{value} dB</option>' for value in (3, 6, 9, 12)) + """</select></label>
              </div><p class="muted">자연 roll-off 밖과 위치별 편차가 큰 null은 최대 boost보다 우선하여 보호됩니다. MIMO 항목은 MIMO 측정 구성에만 쓰이며 Pi4/5에서 chunksize 1024 이상으로 동작합니다.</p></details></fieldset>
            </form>
            <div class="target-preview" data-measurement-step-content="4"><b>선택 타깃 곡선 · 1 kHz 기준</b><svg id="target-graph" viewBox="0 0 760 230" role="img" aria-label="타깃 주파수 응답"></svg></div>"""
    result_html = ""
    if result:
        preview_active = bool(preview.get("active")) and not bool(preview.get("stale"))
        preview_profile = html.escape(str(preview.get("profile") or ""))
        preview_label = f"이번 튜닝 테스트 중 · {preview_profile}" if preview_active else "기존 정식 튜닝 재생 중"
        front_metrics = result.get("front_metrics", {})
        left = front_metrics.get("left", {})
        diagnostics = result.get("diagnostics", {})
        self_validation = result.get("self_validation", {})
        target_fit = self_validation.get("target_fit", {})
        fit_items = []
        for channel in ("left", "right", "woofer"):
            item = target_fit.get(channel)
            channel_ui = {"left": "L", "right": "R", "woofer": "우퍼"}[channel]
            if item:
                if item.get("applicable") is False:
                    fit_items.append(f'<span class="pill neutral"><b>해당 없음</b> · {channel_ui} 단독은 전체 타깃 판정 제외</span>')
                    continue
                fit_pass = bool(item.get("pass"))
                fit_items.append(f'<span class="pill {"" if fit_pass else "error"}"><b>{"PASS" if fit_pass else "FAIL"}</b> · {channel_ui} MAE {item.get("mae_db", "?")} dB · P90 {item.get("p90_abs_error_db", "?")} dB</span>')
        fit_html = "".join(fit_items)
        decay_channels = result.get("room_decay", {}).get("t20_rt60_s_by_channel", {})
        decay_cards = []
        for channel in ("left", "right", "woofer"):
            values = decay_channels.get(channel)
            if isinstance(values, dict) and values:
                rows = "".join(
                    f'<span><b>{float(frequency):g} Hz</b> {float(seconds):.2f} s</span>'
                    for frequency, seconds in sorted(values.items(), key=lambda item: float(item[0]))
                )
                channel_ui = {"left": "L", "right": "R", "woofer": "우퍼"}[channel]
                decay_cards.append(f'<div><small>{channel_ui} T20→RT60</small>{rows}</div>')
        decay_html = "".join(decay_cards)
        warnings = diagnostics.get("warnings") or []
        warning_html = "".join(f'<li>{html.escape(str(item))}</li>' for item in warnings) or "<li>자동 진단에서 큰 위험 신호가 발견되지 않았습니다.</li>"
        audit_rows = "".join(
            f'<tr><td><b>{html.escape(str(item.get("label", item.get("id", ""))))}</b></td><td><code>{html.escape(str(item.get("classification", "")))}</code></td><td>{html.escape(str(item.get("status", "")))}</td><td>{html.escape(str(item.get("action", "")))}</td></tr>'
            for item in result.get("room_tuning_audit", [])
        )
        audit_html = f'<details class="audit-report" open><summary>보정 가능 / 한계 / 미측정 전체 분류</summary><div class="table-scroll"><table><thead><tr><th>요소</th><th>분류</th><th>상태</th><th>해석·조치</th></tr></thead><tbody>{audit_rows}</tbody></table></div></details>' if audit_rows else ""
        limits = result.get("correction_limits", {})
        preference = result.get("preference", {})
        crossover = result.get("crossover", {})
        crossover_check = self_validation.get("crossover_sum", {})
        crossover_pending = bool(crossover_check.get("required")) and crossover_check.get("pass") is None
        crossover_failed = bool(crossover_check.get("required")) and crossover_check.get("pass") is False
        core_failed = any(not bool(value) for value in (self_validation.get("core_checks") or {}).values())
        required_fit_failed = any(
            isinstance(item, dict) and item.get("applicable") is not False and item.get("pass") is False
            for item in target_fit.values()
        )
        independent_check = self_validation.get("independent_positions")
        independent_failed = (
            isinstance(independent_check, dict)
            and independent_check.get("spatial_stability_applicable") is not False
            and not bool(independent_check.get("pass"))
        )
        premeasured_model = self_validation.get("premeasured_sum_model")
        premeasured_failed = isinstance(premeasured_model, dict) and premeasured_model.get("pass") is False
        phase_limited = bool(
            crossover.get("phase_verification_status") == "limited"
            or (isinstance(premeasured_model, dict) and premeasured_model.get("phase_verification_status") == "limited")
        )
        snr_summary = self_validation.get("measurement_snr_db") or {}
        snr_minimum_value = snr_summary.get("minimum")
        snr_blocking = isinstance(snr_minimum_value, (int, float)) and float(snr_minimum_value) < 6.0
        snr_warning = isinstance(snr_minimum_value, (int, float)) and 6.0 <= float(snr_minimum_value) < 15.0
        validation_failed = (core_failed or required_fit_failed or independent_failed or premeasured_failed or crossover_failed or snr_blocking) and not stale_result
        validation_rows = []

        def ui_terms(value: object) -> str:
            text = str(value)
            for before, after in (
                ("설정으로 32768탭 FIR 생성", "FIR 계산"),
                ("이 출력 설정으로 레벨 검사 시작", "레벨 확인"),
                ("측정 구성 변경 적용", "구성 적용"),
                ("연결·Cal", "출력 설정"),
                ("Woofer 최종 trim", "우퍼 최종 트림"),
                ("Crossover", "크로스오버"),
                ("Target", "타깃"),
                ("Sweep", "스윕"),
                ("Session", "세션"),
                ("Woofer", "우퍼"),
                ("Front", "프런트"),
                ("Rear", "후면"),
                ("Fast", "빠른 측정"),
                ("Standard", "표준 측정"),
            ):
                text = text.replace(before, after)
            return text

        def add_validation_row(label: str, verdict: str, detail: str, guide: str = "") -> None:
            label, detail, guide = ui_terms(label), ui_terms(detail), ui_terms(guide)
            token = verdict if verdict in ("pass", "fail", "warn", "pending", "na") else "na"
            badge = {"pass": "PASS", "fail": "FAIL", "warn": "권장", "pending": "대기", "na": "해당 없음"}[token]
            guide_label = "해결 방법" if token == "fail" else "권장 조치" if token == "warn" else "다음 단계"
            guide_html = ""
            if token in ("fail", "warn", "pending") and guide:
                step_names = {"1": "출력 설정", "2": "레벨 확인", "3": "위치 측정", "4": "FIR 계산", "5": "A/B 검토", "6": "정식 적용"}
                guide_steps = list(dict.fromkeys(re.findall(r"(?<!\d)([1-6])\s*·", guide)))
                jump_buttons = "".join(
                    f'<button type="button" class="secondary validation-jump" data-measurement-jump="{step}">{step} · {step_names[step]}</button>'
                    for step in guide_steps
                )
                guide_html = f'<p><b>{guide_label}</b> · {html.escape(guide)}</p>' + (f'<div class="validation-jumps">{jump_buttons}</div>' if jump_buttons else "")
            validation_rows.append(
                f'<div class="validation-row {token}"><span class="status-badge {token}">{badge}</span>'
                f'<div><b>{html.escape(label)}</b><small>{html.escape(detail)}</small>{guide_html}</div></div>'
            )

        core_labels = {
            "exact_32768_taps": ("32768탭 길이", "4 · FIR 계산에서 ‘FIR 계산’을 다시 누르세요. 반복되면 5 · A/B 검토에서 ‘결과 JSON’을 내려받아 오류로 보고하세요."),
            "finite_samples": ("FIR 유한 샘플", "4 · FIR 계산 > ‘고급 보정 설정’에서 ‘최대 룸 부스트’를 한 단계 낮추고 ‘추가 저음 취향’과 ‘추가 고음 경사’를 0 dB에 가깝게 바꾼 뒤 ‘FIR 계산’을 누르세요."),
            "no_positive_transfer": ("0 dB 초과 전달이득 방지", "4 · FIR 계산 > ‘고급 보정 설정’에서 ‘최대 룸 부스트’를 한 단계 낮추고 ‘FIR 계산’을 누르세요."),
            "early_impulse": ("앞쪽 임펄스·낮은 지연", "4 · FIR 계산에서 ‘위상 방식’을 ‘음량만 · 최소위상’으로 바꾸고 ‘FIR 계산’을 누르세요."),
            "fir_matches_design": ("설계 응답과 실제 WAV 일치", "4 · FIR 계산에서 같은 설정으로 ‘FIR 계산’을 다시 누르세요. 계속 실패하면 정식 적용하지 말고 5 · A/B 검토에서 ‘결과 JSON’을 내려받아 진단하세요."),
            "time_alignment_safe": ("프런트/우퍼 시간 정렬 안전성", "4 · FIR 계산에서 ‘위상 방식’을 ‘음량만 · 최소위상’으로 바꾸거나 ‘크로스오버 주파수’를 한 단계 낮춘 뒤 ‘FIR 계산’을 누르세요. 정밀 측정이면 3 · 위치 측정의 ‘필터 전 합산 일치 검증’도 확인하세요."),
            "finite": ("MIMO FIR 유한 샘플", "4 · FIR 계산 > ‘고급 보정 설정’에서 ‘MIMO 강도’를 ‘Safe · 높은 안정성’으로, ‘최대 룸 부스트’를 한 단계 낮춘 뒤 ‘FIR 계산’을 누르세요."),
            "correlated_input_headroom": ("MIMO 상관 입력 여유", "4 · FIR 계산 > ‘고급 보정 설정’에서 ‘MIMO 강도’를 ‘Safe · 높은 안정성’으로, ‘지원 제어원 제한’을 9 dB 또는 12 dB로 바꾼 뒤 ‘FIR 계산’을 누르세요."),
            "common_causality": ("MIMO 공통 인과 지연", "4 · FIR 계산 > ‘고급 보정 설정’에서 ‘MIMO 강도’를 ‘Safe · 높은 안정성’으로 하고 ‘MIMO 공동제어 상한’을 한 단계 낮춘 뒤 고급 설정을 접고 ‘설정으로 32768탭 FIR 생성’을 누르세요."),
            "predicted_target_and_spatial_non_regression": ("MIMO 타겟·좌석 편차 비악화", "4 · FIR 계산 > ‘고급 보정 설정’에서 먼저 ‘MIMO 강도’를 ‘Safe · 높은 안정성’으로 바꾸고, 계속 FAIL이면 ‘MIMO 공동제어 상한’을 한 단계 낮추거나 ‘지원 제어원 제한’을 한 단계 높인 뒤 고급 설정을 접고 ‘설정으로 32768탭 FIR 생성’을 누르세요. 그래도 실패하면 이 측정에서는 MIMO가 SISO보다 낫지 않으므로 1 · 연결·Cal > ‘측정 구성’에서 SISO 구성을 선택하고 ‘측정 구성 변경 적용’을 누르세요."),
            "predicted_modal_tail_non_regression": ("MIMO 저역 임펄스 꼬리 비악화", "4 · FIR 계산에서 ‘기준 음색 타깃’=‘Flat’, ‘우퍼 과잉 억제’=‘추가 억제 없음 · 타깃 기준’, ‘우퍼 최종 트림’=‘0 dB’로 되돌리세요. 이어 ‘고급 보정 설정’에서 ‘MIMO 강도’=‘Balanced · 권장’, ‘MIMO 공동제어 상한’=‘150 Hz’, ‘지원 제어원 제한’=‘6 dB’로 바꾸고, 본문의 ‘크로스오버 주파수’=‘100 Hz’로 설정한 뒤 ‘FIR 계산’을 누르세요. 기준 조합이 PASS하면 원하는 음색 값을 한 번에 하나씩 다시 바꾸세요. 기준도 실패하면 우퍼 위치를 바꿔 3 · 위치 측정의 ‘3위치 처음부터 재측정’을 실행하거나 SISO 구성을 사용하세요."),
        }
        for key, value in (self_validation.get("core_checks") or {}).items():
            label, guide = core_labels.get(str(key), (str(key).replace("_", " ").title(), "4 · FIR 계산에서 각 항목의 권장 기본값으로 되돌린 뒤 ‘FIR 계산’을 누르세요."))
            add_validation_row(label, "pass" if bool(value) else "fail", "내보낸 FIR 파일 자체의 무결성 검사", guide)
        independent = self_validation.get("independent_positions")
        if isinstance(independent, dict):
            reused = independent.get("reused_measurements") or []
            if independent.get("spatial_stability_applicable") is False:
                add_validation_row("좌석 공간 안정성", "na", "빠른 측정 1위치는 기준점만 최적화하므로 머리 이동에 대한 안정성은 판정하지 않습니다.")
            else:
                add_validation_row(
                    "서로 다른 3위치 측정", "pass" if independent.get("pass") else "fail",
                    "세 위치가 독립 측정입니다." if independent.get("pass") else f"재사용 응답 {len(reused)}개가 검출되었습니다.",
                    "1 · 출력 설정에서 ‘청취 위치 범위’를 ‘표준 측정 · 중앙+좌우 3위치 · 권장’으로 확인하고 ‘구성 적용’을 누른 뒤, 3 · 위치 측정에서 ‘3곳 처음부터 재측정’을 눌러 마이크를 서로 다른 세 위치로 옮겨 측정하세요.",
                )
        if isinstance(premeasured_model, dict):
            model_channels = premeasured_model.get("channels") or {}
            model_detail = " · ".join(
                f"{'L' if side == 'left' else 'R'} MAE {values.get('magnitude_mae_db', '?')} / P90 {values.get('magnitude_p90_abs_error_db', '?')} dB · 위상 {values.get('phase_median_abs_error_deg', '?')}°"
                for side, values in model_channels.items()
            ) or str(premeasured_model.get("status", "자료 없음"))
            add_validation_row(
                "필터 전 L/R/우퍼 합산 모델",
                "pass" if premeasured_model.get("pass") else "fail",
                model_detail,
                str(premeasured_model.get("action", f"3 · 위치 측정에서 ‘{total}위치 처음부터 재측정’을 실행하세요.")),
            )
        else:
            add_validation_row("필터 전 L/R/우퍼 합산 모델", "na", "‘정밀 분리+합산’ 측정 구성에서만 판정합니다.")
        for channel in ("left", "right", "woofer"):
            item = target_fit.get(channel)
            channel_label = {"left": "L 타깃 달성", "right": "R 타깃 달성", "woofer": "우퍼 타깃 달성"}[channel]
            if not isinstance(item, dict):
                add_validation_row(channel_label, "na", "이 측정 구성에서는 별도 판정하지 않습니다.")
                continue
            if item.get("applicable") is False:
                if channel == "woofer":
                    branch = (result.get("graphs", {}).get("woofer") or {}).get("branch_level_diagnostic") or {}
                    branch_band = branch.get("evaluation_band_hz") or ["?", "?"]
                    branch_error = branch.get("median_error_db")
                    branch_status = str(branch.get("status", "insufficient_data"))
                    detail = (
                        f"해당 없음 · 독립 LPF 분기는 전체 타깃 판정 제외 · {branch_band[0]}–{branch_band[1]} Hz 상대 레벨 오차 "
                        f"{float(branch_error):+.1f} dB ({branch_status})"
                        if isinstance(branch_error, (int, float)) else
                        "해당 없음 · 독립 LPF 분기는 전체 타깃 판정 제외 · 유효 저역 상대 레벨 자료 부족"
                    )
                else:
                    detail = str(item.get("reason", "이 구성의 전체 시스템 타깃 판정 대상이 아닙니다."))
                add_validation_row(channel_label, "na", detail)
                continue
            fit_detail = f"MAE {item.get('mae_db', '?')} dB / P90 {item.get('p90_abs_error_db', '?')} dB · 허용 ≤3.5 / ≤7 dB"
            if channel == "woofer":
                fit_guide = f"4 · FIR 계산에서 결과가 높으면 ‘우퍼 최종 트림’을 더 음수로 또는 ‘우퍼 과잉 억제’를 더 강하게 선택하고 ‘FIR 계산’을 누르세요. 낮으면 T5S 물리 볼륨·극성을 확인한 뒤 3 · 위치 측정의 ‘{total}위치 처음부터 재측정’을 실행하세요. 깊은 룸 딥에는 ‘최대 룸 부스트’를 올리지 마세요."
            else:
                fit_guide = f"스피커 거리·토인·주변 반사를 확인하고 3 · 위치 측정의 ‘{total}위치 처음부터 재측정’을 실행하세요. 깊은 딥이면 스피커/청취 위치를 옮기고 4 · FIR 계산 > ‘최대 룸 부스트’를 무작정 올리지 마세요."
            add_validation_row(channel_label, "pass" if item.get("pass") else "fail", fit_detail, fit_guide)
        if crossover_check.get("required"):
            crossover_status = str(crossover_check.get("status", ""))
            if crossover_check.get("pass") is None:
                add_validation_row("프런트+우퍼 전체 합산", "pending", "이전 알고리즘 결과라 합산 안전성 판정이 없습니다.", "4 · FIR 계산에서 현재 설정을 확인하고 ‘FIR 계산’을 다시 누르세요. 새 계산은 보수적인 감쇄 전용 합산 상한으로 판정하며 사후 스윕을 요구하지 않습니다. 물리 합산 검증까지 원하면 다음 세션의 1 · 출력 설정 > ‘측정 구성’에서 ‘정밀 분리+합산’을 선택하세요.")
            else:
                if crossover_status == "fail_premeasured_sum_snr" and isinstance(premeasured_model, dict):
                    crossover_detail = f"정밀 합산 측정 SNR {premeasured_model.get('minimum_snr_db', '?')} dB · 사용 기준 {premeasured_model.get('thresholds', {}).get('minimum_snr_db', 6)} dB"
                    crossover_guide = str(premeasured_model.get("action", "2 · 레벨 확인과 3 · 위치 측정을 다시 실행하세요."))
                elif crossover_status == "fail_premeasured_sum_phase_reference" and isinstance(premeasured_model, dict):
                    crossover_detail = "시간·위상 기준 신뢰도 부족 · 위상 미검증 상태"
                    crossover_guide = str(premeasured_model.get("action", "3 · 위치 측정을 다시 실행하세요."))
                elif crossover_status == "fail_premeasured_complex_sum" and isinstance(premeasured_model, dict):
                    crossover_detail = "L/R/우퍼 예측과 실제 L+우퍼/R+우퍼 합산이 허용 오차를 벗어남"
                    crossover_guide = str(premeasured_model.get("action", "T5S 극성·LPF 노브와 배선을 확인하세요."))
                elif crossover_status == "fail_upper_guard":
                    upper_values = [
                        float(values.get("guarded_upper_excess_p95_db"))
                        for values in (crossover.get("channels") or {}).values()
                        if isinstance(values, dict)
                        and isinstance(values.get("guarded_upper_excess_p95_db"), (int, float))
                    ]
                    upper_worst = max(upper_values) if upper_values else None
                    crossover_detail = (
                        f"합산 안전 상한 P95 {upper_worst:.3f} dB · 허용 ≤1.0 dB"
                        if upper_worst is not None else "합산 안전 상한 허용치 초과"
                    )
                    if (
                        str(result.get("preset", "none")) == "strong"
                        and int(crossover.get("frequency_hz", 100)) == 120
                    ):
                        crossover_guide = (
                            "현재 ‘우퍼 과잉 억제’가 가장 강한 ‘T5S 강한 억제’이고 ‘크로스오버 주파수’도 120 Hz입니다. "
                            "‘우퍼 최종 트림’을 한 단계 내려 다시 계산해도 상한이 거의 줄지 않으면 프런트 분기가 지배하는 구간이라 더 내리지 마세요. "
                            "4 · FIR 계산에서 ‘우퍼 과잉 억제’를 ‘Primus 360 수준’으로 한 단계 완화하거나, 프런트/우퍼 위치·극성·T5S LPF 노브를 조정한 뒤 3 · 위치 측정을 다시 실행하세요."
                        )
                    else:
                        crossover_guide = (
                            "4 · FIR 계산에서 ‘우퍼 최종 트림’을 한 단계 더 음수로 하거나 ‘우퍼 과잉 억제’를 한 단계 강하게 하고 ‘FIR 계산’을 누르세요. "
                            "‘디지털 크로스오버’가 꺼져 있으면 ON, 이미 ON이면 ‘크로스오버 주파수’ 120 Hz도 비교하세요."
                        )
                elif crossover_status in ("limited_unverified_phase", "pass_safe_upper_phase_limited", "pass_safe_sum_phase_limited"):
                    crossover_detail = "합산 안전성 PASS · 절대 위상 정밀도 제한 · 감쇄 전용 상한 사용"
                    crossover_guide = "정식 적용은 가능합니다. 더 정확한 확인이 필요하면 5 · A/B 검토에서 이번 튜닝을 듣고 선택적으로 합산 실측을 실행하세요."
                else:
                    crossover_detail = crossover_status or "합산 판정"
                    signed_errors = [
                        values.get("complex_target_median_error_db")
                        if isinstance(values.get("complex_target_median_error_db"), (int, float))
                        else values.get("target_estimate_median_error_db")
                        for values in (crossover.get("channels") or {}).values()
                        if isinstance(values, dict) and isinstance(
                            values.get("complex_target_median_error_db")
                            if isinstance(values.get("complex_target_median_error_db"), (int, float))
                            else values.get("target_estimate_median_error_db"),
                            (int, float),
                        )
                    ]
                    signed_error = statistics.median(signed_errors) if signed_errors else None
                    if signed_error is not None and signed_error > 1.0:
                        direction_guide = f"합산 중앙 오차가 타깃보다 {signed_error:+.1f} dB 높습니다. ‘우퍼 최종 트림’을 한 단계 더 음수로 바꾸거나 ‘우퍼 과잉 억제’를 한 단계 강하게 하세요."
                    elif signed_error is not None and signed_error < -1.0:
                        if int(result.get("woofer_trim_db", 0)) == 0 and str(result.get("preset", "none")) == "none":
                            channel_values = [
                                values for values in (crossover.get("channels") or {}).values()
                                if isinstance(values, dict)
                            ]
                            worst = max(
                                channel_values,
                                key=lambda values: float(values.get("target_estimate_p90_db", -1.0)),
                                default={},
                            )
                            dip_hz = worst.get("deepest_crossover_dip_hz")
                            p90 = worst.get("target_estimate_p90_db")
                            direction_guide = (
                                f"현재 ‘우퍼 최종 트림’ 0 dB와 ‘우퍼 과잉 억제’ 추가 억제 없음이라 되돌릴 저음 감쇄가 없습니다. "
                                f"최악 P90 {p90} dB"
                                + (f", 딥 약 {dip_hz} Hz" if isinstance(dip_hz, (int, float)) else "")
                                + "입니다. ‘크로스오버 주파수’를 80 Hz 또는 120 Hz로 바꿔 각각 ‘FIR 계산’하고 PASS 결과를 비교하세요. "
                                "둘 다 FAIL이면 ‘기준 음색 타깃’을 Flat으로 바꾸거나 T5S 위치·극성·LPF 노브를 조정하세요. ‘최대 룸 부스트’를 올려 깊은 딥을 메우지 마세요."
                            )
                        else:
                            direction_guide = f"합산 중앙 오차가 타깃보다 {signed_error:+.1f} dB 낮습니다. ‘우퍼 최종 트림’을 0 dB 쪽으로 되돌리고 ‘우퍼 과잉 억제’를 한 단계 약하게 하세요. 깊은 딥이면 부스트하지 말고 우퍼 위치·극성을 확인하세요."
                    else:
                        direction_guide = "평균 레벨보다 크로스오버 부근의 굴곡·딥이 원인입니다. ‘위상 방식’을 ‘저역 음량+excess phase’로 바꾸거나 ‘크로스오버 주파수’를 한 단계 변경하세요."
                    crossover_guide = f"4 · FIR 계산에서 {direction_guide} 이어 ‘FIR 계산’을 누르세요. 정밀 모드의 ‘필터 전 합산 일치 검증’이 PASS이면 원측정은 다시 하지 않습니다."
                add_validation_row("프런트+우퍼 전체 합산", "pass" if crossover_check.get("pass") else "fail", crossover_detail, crossover_guide)
                if crossover_check.get("pass") and crossover.get("phase_verification_status") == "limited":
                    add_validation_row(
                        "최종 합산 절대 위상 정밀도",
                        "warn",
                        "위상 비의존 상한으로 안전성을 통과했습니다. 그래프의 깊은 복소 딥은 확정값으로 표시하지 않습니다.",
                        "정식 적용은 가능합니다. 실제 방에서 최종 합산을 확인하려면 5 · A/B 검토의 선택적 합산 실측을 사용하세요.",
                    )
        else:
            add_validation_row("프런트+우퍼 전체 합산", "na", "독립 우퍼 분기가 없는 합산 SISO 구성이라 별도 판정에서 제외합니다.")
        snr_check = snr_summary
        snr_minimum = snr_check.get("minimum")
        snr_required = float(snr_check.get("recommended_minimum", 15.0))
        if isinstance(snr_minimum, (int, float)):
            configured_level = int(job.get("level_dbfs", -42))
            desired_raise_db = max(0, int(math.ceil(snr_required - float(snr_minimum))))
            maximum_peak = snr_check.get("maximum_peak_dbfs")
            safe_raise_db = (
                max(0, int(math.floor(-6.0 - float(maximum_peak))))
                if isinstance(maximum_peak, (int, float)) else desired_raise_db
            )
            raise_db = min(desired_raise_db, safe_raise_db)
            recommended_level = min(0, configured_level + raise_db)
            snr_verdict = "fail" if float(snr_minimum) < 6.0 else "warn" if float(snr_minimum) < snr_required else "pass"
            if desired_raise_db > safe_raise_db:
                snr_guide = (
                    f"2 · 레벨 확인에서 스윕 출력을 {configured_level} → {recommended_level} dBFS "
                    f"(+{raise_db} dB, 입력 피크 -6 dBFS 여유 기준)까지만 올리고 ‘레벨 확인’을 누르세요. "
                    "권장 15 dB에 계속 못 미치면 주변 소음을 줄이거나 UMIK-1 입력 상태를 확인하세요."
                )
            else:
                snr_guide = (
                    f"2 · 레벨 확인에서 스윕 출력을 {configured_level} → {recommended_level} dBFS (+{raise_db} dB)로 "
                    f"바꾸고 ‘레벨 확인’을 누르세요. 권장 품질로 다시 만들려면 3 · 위치 측정에서 ‘{total}곳 처음부터 재측정’을 실행하세요."
                )
            add_validation_row(
                "측정 SNR", snr_verdict,
                f"현재 최소 {float(snr_minimum):.1f} dB · 사용 최소 6 dB · 권장 {snr_required:.0f} dB",
                snr_guide,
            )
        else:
            add_validation_row("측정 SNR", "na", "저장된 결과에 SNR 판정값이 없습니다.")
        checklist_state = "FAIL 있음" if validation_failed else "사후 실측 대기" if crossover_pending else "PASS · 권장 개선 있음" if snr_warning or phase_limited else "전체 PASS"
        validation_checklist_html = f'''<section class="validation-checklist"><div class="section-head"><div><h4>자동 검증</h4><p class="muted">PASS는 완료, FAIL은 정식 적용 차단, 권장은 적용 가능하지만 품질 개선 여지가 있음, 대기는 다음 실행 필요, 해당 없음은 판정 대상이 아님을 뜻합니다. MAE는 평균 절대오차, P90은 측정 지점 90%가 들어오는 오차 경계입니다. 둘 다 작을수록 타깃에 가깝습니다.</p></div><span class="pill {'error' if validation_failed else 'neutral' if crossover_pending or snr_warning else ''}">{checklist_state}</span></div><div>{''.join(validation_rows)}</div></section>'''
        relative_phase = crossover.get("relative_phase_optimization") or {}
        if relative_phase.get("enabled"):
            phase_polarity = "반전" if int(relative_phase.get("polarity", 1)) < 0 else "정상"
            phase_branch = {"front": "프런트", "woofer": "우퍼", "none": "추가 지연 없음"}.get(
                str(relative_phase.get("delayed_branch", "none")),
                str(relative_phase.get("delayed_branch", "none")),
            )
            phase_alignment_card = (
                f'<div><small>우퍼 복소합 정렬</small><b>극성 {phase_polarity} · {phase_branch}</b>'
                f'<span>{abs(int(relative_phase.get("relative_delay_samples", 0)))} 샘플 · '
                f'{abs(float(relative_phase.get("relative_delay_ms", 0.0))):.3f} ms · MAE '
                f'{relative_phase.get("predicted_mae_db_before_guard", "?")} dB</span></div>'
            )
        else:
            phase_alignment_card = '<div><small>우퍼 복소합 정렬</small><b>자동 정렬 미사용</b><span>‘위상 방식’이 음량만이거나 직접음 위상 신뢰도가 부족합니다.</span></div>'
        crossover_status_text = {
            "fail_premeasured_sum_snr": "정밀 합산 측정 SNR 부족",
            "fail_premeasured_sum_phase_reference": "시간·위상 기준 부족",
            "fail_premeasured_complex_sum": "실측 합산 오차 초과",
            "limited_unverified_phase": "위상 미검증",
            "pass_safe_upper_phase_limited": "합산 안전성 통과 · 위상 정밀도 제한",
            "pass_safe_sum_phase_limited": "정밀 합산 안전성 통과 · 위상 정밀도 제한",
            "pass_premeasured_complex_sum": "정밀 합산 검증 통과",
            "pass_premeasured_complex_model": "정밀 합산 검증 통과",
            "pass_independent_complex_model": "독립 응답 합산 검증 통과",
            "pass": "합산 검증 통과",
            "fail_target": "합산 타깃 오차 초과",
            "fail_upper_guard": "합산 안전 상한 초과",
        }.get(str(crossover.get("status", "")), str(crossover.get("status", "판정 없음")))
        crossover_label = (
            f'{crossover.get("frequency_hz", "?")} Hz · FIR 내장 · 추가 블록 지연 {crossover.get("additional_block_latency_samples", 0)} 샘플 · {crossover_status_text}'
            if crossover.get("enabled") else
            f'LR4 꺼짐 · 전대역 중첩 · 합산 보호 {"켜짐" if crossover.get("sum_guard_enabled") else "해당 없음"} · {crossover_status_text}'
        )
        crossover_channels = crossover.get("channels") or {}
        phase_unverified = bool(crossover_channels) and any(
            isinstance(values, dict) and not bool(values.get("complex_prediction_reliable"))
            for values in crossover_channels.values()
        )
        phase_graph_note = (
            '<p class="diagnostic-note"><b>위상 정밀도 제한 그래프</b> · 개별 측정의 시간 기준 신뢰도가 부족해 복소 합산 딥을 사실로 표시하지 않습니다. 현재 그래프는 프런트와 우퍼가 최대로 더해질 때의 안전 상한을 표시합니다. 이전에 보인 약 150 Hz 딥은 검증된 실측 딥이 아닙니다.</p>'
            if phase_unverified else ""
        )
        if measured_profile in PROFILE_UI:
            result_profile_ui = PROFILE_UI[measured_profile]
            result_path_note = f'<p class="measurement-result-path"><b>{ui_icon("check", "측정 경로")} 이 결과의 전용 경로</b><span>{html.escape(result_profile_ui["detail"])}</span></p>'
            final_apply_target = f'{result_profile_ui["title"]} · {result_profile_ui["detail"]}'
            preview_actions = f'<form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="{measured_profile}"><button>이번 튜닝</button></form>'
            apply_actions = f'<form method="post" action="/measurement/apply" onsubmit="return confirm(\'{html.escape(result_profile_ui["title"])}의 기존 FIR WAV를 새 결과로 덮어씁니다. 기존 파일은 자동 백업됩니다. 정식 적용할까요?\')"><input type="hidden" name="profile" value="{measured_profile}"><button>정식 적용</button></form>'
        else:
            result_path_note = '<p class="measurement-result-path legacy"><b>이전 세션</b><span>측정 당시 U7 물리 경로가 기록되지 않았습니다. 경로를 직접 확인한 뒤 사용하세요.</span></p>'
            final_apply_target = "측정 경로를 직접 확인해야 하는 이전 세션"
            preview_actions = '<form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="speaker"><button>스피커 A/B</button></form><form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="headphone"><button>헤드폰 A/B</button></form>'
            apply_actions = '<form method="post" action="/measurement/apply" onsubmit="return confirm(\'스피커 출력 체인의 기존 FIR을 덮어씁니다. 정식 적용할까요?\')"><input type="hidden" name="profile" value="speaker"><button>스피커 적용</button></form><form method="post" action="/measurement/apply" onsubmit="return confirm(\'헤드폰 출력 체인의 기존 FIR을 덮어씁니다. 정식 적용할까요?\')"><input type="hidden" name="profile" value="headphone"><button>헤드폰 적용</button></form>'
        stale_result_notice = ""
        if stale_result:
            stale_result_notice = '<div class="failure" role="alert"><b>이전 알고리즘으로 계산된 결과</b><br>측정 원본은 유지되어 있습니다. 4단계에서 FIR 계산만 다시 실행해야 Preview와 정식 적용을 사용할 수 있습니다.</div>'
            preview_actions = '<button disabled>A/B 대기</button>'
            apply_actions = '<button disabled>적용 대기</button>'
        elif crossover_pending and not validation_failed:
            stale_result_notice = '<div class="diagnostic-note" role="status"><b>이전 합산 판정 · FIR 재계산 필요</b><br>4 · FIR 계산에서 현재 설정으로 다시 계산하세요. 새 계산은 모든 필수 측정을 3단계에서 끝내며, FIR 생성 뒤 추가 sweep을 요구하지 않습니다.</div>'
            apply_actions = '<button disabled>재계산 필요</button>'
        elif not self_validation.get("overall_pass"):
            stale_result_notice = '<div class="failure" role="alert"><b>타겟/합산 셀프검증 미통과</b><br>WAV 다운로드와 A/B 확인은 가능하지만 정식 적용은 차단됩니다. 진단 항목을 확인하고 설정을 조정해 다시 계산하세요.</div>'
            apply_actions = '<button disabled>적용 차단</button>'
        validation_label = "이전 계산 · 재계산 필요" if stale_result else ("PASS" if self_validation.get("overall_pass") else "사후 실측 대기" if crossover_pending and not validation_failed else "FAIL · 정식 적용 차단")
        post_validation_html = ""
        if (
            crossover_check.get("required")
            and (
                job.get("mode") in ("lrw", "lrw_sum")
                or (job.get("mode") in ("mimo_one_sub", "mimo_dual_sub") and result.get("kind") == "mimo_2x4")
            )
        ):
            post = job.get("post_filter_validation") or {}
            post_completed = int(post.get("positions_completed", 0))
            post_total = int(post.get("positions_total", total))
            post_level = int(post.get("level_dbfs", max(-48, min(-24, int(job.get("level_dbfs", -42))))))
            level_options = ''.join(
                f'<option value="{value}" {"selected" if value == post_level else ""}>{value} dBFS</option>'
                for value in (-48, -42, -36, -30, -24, -18, -12)
            )
            post_evaluation = post.get("evaluation") or self_validation.get("post_filter_sum") or {}
            if post_evaluation:
                post_channels = post_evaluation.get("channels", {})
                post_metrics = ''.join(
                    f'<div><small>{"L" if side == "left" else "R"}+우퍼 실측 타깃</small><b>MAE {values.get("target_mae_db", "?")} / P90 {values.get("target_p90_abs_error_db", "?")} dB</b><span>크로스오버 MAE {values.get("crossover_mae_db", "?")} dB · 물리 한계 {values.get("physical_extension_limit_hz", "?")} Hz</span></div>'
                    for side, values in post_channels.items()
                )
                post_result = f'<div class="diagnostic-grid post-validation-metrics">{post_metrics}<div><small>L/R 일치</small><b>{post_evaluation.get("lr_match", {}).get("median_shape_difference_db", "?")} dB</b></div><div><small>사후 SNR 최소</small><b>{post_evaluation.get("snr", {}).get("minimum_db", "?")} dB</b></div></div><p class="{"success" if post_evaluation.get("overall_pass") else "failure"}"><b>{"PASS" if post_evaluation.get("overall_pass") else "FAIL"}</b> · 실제 Preview FIR을 통과한 합산 음압 판정</p>'
            else:
                post_result = '<p class="muted">아직 실제 FIR을 통과한 합산 음압 결과가 없습니다.</p>'
            next_position = min(post_total, post_completed + 1)
            post_button_disabled = " disabled" if not preview_active or busy or post_completed >= post_total else ""
            post_action_note = (
                "먼저 위의 ‘이번 튜닝 테스트’를 눌러 Preview를 적용하세요."
                if not preview_active else
                f"마이크를 {'기준점' if post_total == 1 else f'검증 위치 {next_position}/{post_total}'}에 놓고 실행하세요. 원측정과 생성 FIR은 지워지지 않습니다."
            )
            post_level_hidden = (
                f'<input type="hidden" name="level_dbfs" value="{post_level}">'
                if post_completed else ""
            )
            post_reset_control = ""
            if post_completed:
                post_reset_control = '''<form method="post" action="/measurement/reset-post-validation" onsubmit="return confirm('사후 합산 검증 진행값만 초기화합니다. 원측정과 생성 FIR은 유지됩니다. 계속할까요?')"><button class="secondary">검증 초기화</button></form>'''
            post_validation_html = f'''
            <section class="post-validation-card" aria-labelledby="post-validation-title">
              <div class="section-head"><div><h4 id="post-validation-title">선택 사항 · Preview FIR 적용 후 합산 실측</h4><p class="muted">실제 스테레오 입력 → 현재 프런트/우퍼 FIR → U7 4채널 → 방 → UMIK-1 경로를 측정합니다. 정식 적용 필수 단계가 아니며 원측정과 생성 FIR은 유지됩니다.</p></div><span class="pill {'error' if post_evaluation and not post_evaluation.get('overall_pass') else ''}">{post_completed}/{post_total} 위치</span></div>
              <form method="post" action="/measurement/post-validation" class="measure-form" onsubmit="return confirm('현재 Preview FIR을 통과한 L+우퍼/R+우퍼 검증 스윕을 재생합니다. 원측정과 생성 FIR은 유지됩니다. 시작할까요?')">
                <label>DAC 기준 검증 출력<select name="level_dbfs"{' disabled' if post_completed else ''}>{level_options}</select>{post_level_hidden}<span>U7 청취 볼륨과 무관한 실제 DAC 기준입니다. 입력 OFF → PCM 0 dB → sweep → 원래 볼륨 복원 → 입력 복귀 순서로 실행하며, dBFS는 역컨볼루션에서 복원되어 타깃 레벨에는 영향을 주지 않습니다.</span></label>
              <button{post_button_disabled}>위치 {next_position}/{post_total} 합산 측정</button>
              </form>
              <p class="form-note">{html.escape(post_action_note)}</p>
              {post_result}
              {post_reset_control}
            </section>'''
        result_html = f"""
        <div class="result-box" id="measurement-step-5" data-measurement-step-content="5"><h3>적용 전 검토 · 생성 결과</h3>
          {stale_result_notice}
          {result_path_note}
          <p><b>{html.escape(dict(target_labels).get(str(result.get('target')), str(result.get('target'))))}</b> · {html.escape(dict((('none', '추가 억제 없음'), ('primus360', 'Primus 360 수준'), ('strong', 'T5S 강한 억제'))).get(str(result.get('preset')), str(result.get('preset'))))} · {result.get('taps')}탭 · 프런트 피크 {left.get('peak_tap', '?')}탭 ({left.get('peak_delay_ms', '?')} ms)</p>
          <p><code>{html.escape(str(result.get('front_sha256', '')))}</code></p>
          <div class="diagnostic-grid"><div><small>측정 범위</small><b>{'빠른 측정 · 1위치' if int(result.get('measurement_coverage', {}).get('positions', total)) == 1 else '표준 측정 · 3위치'}</b></div><div><small>공간 평균</small><b>{html.escape(str(result.get('spatial_mode', 'equal')))}</b></div><div><small>룸보정 범위</small><b>{limits.get('low_hz', '?')}–{limits.get('high_hz', '?')} Hz</b></div><div class="{'validation-fail' if crossover_failed else ''}"><small>디지털 크로스오버 합산</small><b>{'FAIL · ' if crossover_failed else '실측 대기 · ' if crossover_pending else ''}{html.escape(crossover_label)}</b></div>{phase_alignment_card}<div><small>추가 취향</small><b>저음 {preference.get('bass_db_at_20_hz', 0):+} / 고음 {preference.get('treble_db_at_20_khz', 0):+} dB</b></div><div><small>L/R 중앙값 차이</small><b>{diagnostics.get('lr_median_difference_db', '?')} dB</b></div><div><small>공간 편차 중앙값</small><b>{diagnostics.get('spatial_std_median_db', '?')} dB</b></div><div><small>측정 SNR 최소/중앙</small><b>{diagnostics.get('measurement_snr_min_db', '?')} / {diagnostics.get('measurement_snr_median_db', '?')} dB</b></div><div class="{'validation-fail' if validation_failed else ''}"><small>FIR 셀프검증</small><b>{validation_label}</b></div><div><small>우퍼 최종 트림</small><b>{result.get('woofer_trim_db', 0):+} dB</b></div><div><small>측정 시 우퍼 감쇄</small><b>{job.get('woofer_measurement_attenuation_db', -9):+} dB · 응답에서 복원됨</b></div></div>
            <div class="graph-toolbar"><div><b>응답 비교</b><small>전체 대역이 기본이며 저역 확대에서 크로스오버 딥을 확인합니다.</small></div><div role="group" aria-label="그래프 주파수 범위"><button type="button" class="secondary selected" data-result-range="full" aria-pressed="true">전체</button><button type="button" class="secondary" data-result-range="bass" aria-pressed="false">저역</button></div></div>
          <svg id="measurement-result-graph" data-result-target="{html.escape(str(result.get('target', 'harman')))}" viewBox="0 0 760 250" role="img" aria-label="보정 전후 및 합산 주파수 응답"></svg>
          <p id="measurement-result-summary" class="muted" aria-live="polite"></p>
          {phase_graph_note}
          <div class="measure-actions target-fit">{fit_html}</div>
          {f'<details class="decay-report"><summary>잔향/공진 T20→RT60 보기</summary><div class="decay-grid">{decay_html}</div><p class="muted">late reverb는 불안정한 역보정을 하지 않습니다. 신뢰 가능한 300 Hz 이하 장시간 공진만 최대 3 dB 추가 감쇄합니다.</p></details>' if decay_html else ''}
          {validation_checklist_html}
          <div class="diagnostic-note"><b>추가 자동 진단 메모</b><ul>{warning_html}</ul></div>
          {audit_html}
          <p class="muted">여기서 타깃과 적용 후 예상은 청취 위치 음압입니다. 현황/설정의 FIR 그래프는 이 목표를 만들기 위한 보정 전달함수이므로 타깃 모양과 같지 않습니다. 점선은 튜닝 전 측정, 실선은 32768탭 FIR 적용 후 예상 응답입니다.</p>
          <div class="measure-actions"><a class="button" download href="/api/measurement/download/front">프런트 WAV</a>
          {('<a class="button" download href="/api/measurement/download/rear">우퍼 WAV</a>' if result.get('rear') else '')}
          {('<a class="button" download href="/api/measurement/download/all">전체 ZIP</a>' if result.get('rear') else '')}
          {('<a class="button secondary" download href="/api/measurement/download/report-md">보고서 MD</a>' if result.get('report_md') else '')}
          {('<a class="button secondary" download href="/api/measurement/download/report-json">결과 JSON</a>' if result.get('report_json') else '')}</div>
          <div class="result-box"><b>A/B 청취 비교</b><p class="muted">현재 상태: <span class="pill">{preview_label}</span> · 테스트 적용은 프로필 WAV와 설정을 덮어쓰지 않습니다.</p><div class="measure-actions">
          {preview_actions}
          <form method="post" action="/measurement/restore"><button>기존 튜닝</button></form></div></div>
          {post_validation_html}
          <section class="final-apply-card" id="measurement-step-6" data-measurement-step-content="6"><div><h4>검토한 결과를 정식 프로필에 적용</h4><p><b>현재 판정: {validation_label}</b><br>대상: {html.escape(final_apply_target)}<br>적용 직전 기존 FIR을 자동 백업하며, A/B Preview와 달리 이 단계에서만 프로필 WAV를 교체합니다.</p></div><div class="measure-actions">{apply_actions}</div></section>
        </div>"""
        if result.get("kind") == "mimo_2x4":
            mimo = result.get("mimo", {})
            prediction = mimo.get("prediction", {})
            metric_cards = "".join(
                f'<div><small>{channel.title()} target MAE</small><b>{values.get("before_target_mae_db", "?")} → {values.get("after_target_mae_db", "?")} dB</b><small>좌석 편차 {values.get("before_spatial_std_db", "?")} → {values.get("after_spatial_std_db", "?")} dB<br>저역 late/early {values.get("before_modal_tail_db", "?")} → {values.get("after_modal_tail_db", "?")} dB · 낮을수록 양호</small></div>'
                for channel, values in prediction.items()
            )
            topology = html.escape(str(mimo.get("topology", "mimo")))
            headroom = mimo.get("headroom", {})
            diversity = mimo.get("actuator_diversity", {})
            normalization = mimo.get("target_level_normalization", {})
            target_offsets = normalization.get("target_offset_db", {})
            resource_budget = mimo.get("resource_budget", {})
            mimo_crossover = result.get("crossover", mimo.get("crossover", {}))
            mimo_validation_failed = validation_failed
            mimo_crossover_failed = bool(crossover_check.get("required")) and crossover_check.get("pass") is False
            mimo_crossover_pending = bool(crossover_check.get("required")) and crossover_check.get("pass") is None and not validation_failed
            mimo_validation_label = "FAIL · 적용 차단" if mimo_validation_failed else "사후 실측 대기" if mimo_crossover_pending else "PASS"
            mimo_crossover_label = (
                f'{mimo_crossover.get("frequency_hz", "?")} Hz · FIR bank 내장 · 추가 block latency {mimo_crossover.get("additional_block_latency_samples", 0)} samples'
                if mimo_crossover.get("enabled") else "비적용"
            )
            if stale_result:
                mimo_preview_actions = '<button disabled>A/B 대기</button>'
                mimo_apply_actions = '<button disabled>적용 대기</button>'
            else:
                mimo_preview_actions = '<form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="speaker"><button>이번 튜닝</button></form>'
                if mimo_crossover_pending:
                    mimo_apply_actions = '<button disabled>검증 대기</button>'
                elif not self_validation.get("overall_pass"):
                    mimo_apply_actions = '<button disabled>적용 차단</button>'
                else:
                    mimo_apply_actions = '<form method="post" action="/measurement/apply" onsubmit="return confirm(\'검증된 MIMO 필터 묶음을 스피커 프로필에 설치합니다. 기존 설정은 자동 백업됩니다. 정식 적용할까요?\')"><input type="hidden" name="profile" value="speaker"><button>정식 적용</button></form>'
            result_html = f"""
            <div class="result-box" id="measurement-step-5" data-measurement-step-content="5"><h3>MIMO 2×4 적용 전 검토</h3>
              {stale_result_notice}
              {result_path_note}
              <p><b>{topology}</b> · {result.get('taps')} taps × 8 convolution paths · 공동 제어 {mimo.get('frequency_range_hz', ['?', '?'])[0]}–{mimo.get('frequency_range_hz', ['?', '?'])[1]} Hz</p>
        <div class="diagnostic-grid">{metric_cards}<div class="{'validation-fail' if mimo_crossover_failed else ''}"><small>디지털 크로스오버 합산</small><b>{'FAIL · ' if mimo_crossover_failed else '실측 대기 · ' if mimo_crossover_pending else ''}{html.escape(mimo_crossover_label)}</b><small>{html.escape(str(crossover_check.get('status', mimo_crossover.get('status', ''))))}</small></div><div><small>최악 상관입력 행 합계</small><b>{headroom.get('maximum_correlated_input_row_sum', '?')}</b><small>전체 {headroom.get('global_scale_db', '?')} dB · 우퍼 트림 실제 전달 제한</small></div><div><small>제어원 최대 코히어런스</small><b>{diversity.get('maximum_coherence', '?')}</b><small>1에 가까우면 독립성 부족</small></div><div><small>저역 기준 레벨 고정</small><b>L {target_offsets.get('left', '?')} / R {target_offsets.get('right', '?')} dB</b><small>{normalization.get('reference_band_hz', ['?', '?'])[0]}–{normalization.get('reference_band_hz', ['?', '?'])[1]} Hz 기존 SISO 기준</small></div><div><small>MIMO 해 강도</small><b>{html.escape(str(mimo.get('strength', '?')))} · 혼합 {mimo.get('solution_blend', '?')}</b><small>기존 안정 해와 공동제어 해의 혼합</small></div><div><small>메모리 계획값</small><b>실시간 {resource_budget.get('runtime_dsp_planning_mib', '?')} / 생성 {resource_budget.get('filter_generation_planning_mib', '?')} MiB</b><small>실측 부하가 아닌 보수적 상한 · CPU/XRUN은 별도 검증</small></div><div class="{'validation-fail' if mimo_validation_failed else ''}"><small>자체 검증</small><b>{mimo_validation_label}</b></div></div>
              <div class="graph-toolbar"><div><b>응답 비교</b><small>전체 대역 / 저역 확대</small></div><div role="group" aria-label="그래프 주파수 범위"><button type="button" class="secondary selected" data-result-range="full" aria-pressed="true">전체</button><button type="button" class="secondary" data-result-range="bass" aria-pressed="false">저역</button></div></div>
              <svg id="measurement-result-graph" data-result-target="{html.escape(str(result.get('target', 'harman')))}" viewBox="0 0 760 250" role="img" aria-label="MIMO 보정 예상 응답"></svg><p id="measurement-result-summary" class="muted" aria-live="polite"></p>
              {validation_checklist_html}<div class="diagnostic-note"><b>추가 자동 진단 메모</b><ul>{warning_html}</ul></div>{audit_html}
              <p class="muted">예측은 측정한 세 위치의 선형 모델에만 유효합니다. 평활 전달함수 기반 impulse-tail proxy가 1.5 dB 넘게 악화되면 적용을 차단합니다. 이 값은 실제 RT60/잔향 예측이 아니며, 실제 적용 전 Preview와 이후 별도 위치 재측정·XRUN/CPU 확인이 필요합니다.</p>
              <div class="measure-actions"><a class="button" download href="/api/measurement/download/all">MIMO WAV 4개 + 보고서 ZIP</a><a class="button secondary" download href="/api/measurement/download/report-md">한계 포함 보고서 MD</a><a class="button secondary" download href="/api/measurement/download/report-json">전체 결과 JSON</a></div>
              <div class="result-box"><b>A/B 청취 비교</b><p class="muted">현재 상태: <span class="pill">{preview_label}</span> · MIMO는 실제 4채널 스피커 출력 전용입니다.</p><div class="measure-actions">{mimo_preview_actions}<form method="post" action="/measurement/restore"><button>기존 튜닝</button></form></div></div>
              {post_validation_html}
              <section class="final-apply-card" id="measurement-step-6" data-measurement-step-content="6"><div><h4>MIMO bank 정식 적용</h4><p><b>현재 판정: {mimo_validation_label}</b><br>대상: Speaker 4채널 출력 · 32768탭 × 8 convolution paths<br>적용 직전 기존 MIMO bank와 설정을 자동 백업합니다.</p></div><div class="measure-actions">{mimo_apply_actions}</div></section>
            </div>"""
    error = ""
    if job.get("error"):
        last_quality = job.get("last_measurement_quality") or {}
        quality_message = str(last_quality.get("message") or job.get("error"))
        recovery_buttons = []
        if bool(level_recording_inventory.get("can_reprocess_all")) and not level and not busy:
            recovery_buttons.append(
                '<form method="post" action="/measurement/reprocess-level"><button>빠른 검사 원본 재계산</button></form>'
            )
        if bool(capture_inventory.get("can_reprocess_all")) and response_count < expected_capture_count and not busy:
            recovery_buttons.append(
                '<form method="post" action="/measurement/reprocess-saved"><button>위치 측정 원본 재계산</button></form>'
            )
        recovery_button = ''.join(recovery_buttons)
        error = f'''<section class="failure recovery-card" role="alert" tabindex="-1"><div><b>측정 확인 필요</b><p>{html.escape(quality_message)}</p><small>저장 상태 · 빠른 검사 {level_recording_inventory.get("complete_count", 0)}/{level_recording_inventory.get("expected", 0)} · 위치 녹음 {raw_capture_count}/{expected_capture_count} · 응답 {response_count}/{expected_capture_count}</small></div>{recovery_button}</section>'''
    mode = str(job.get("mode", "lrw"))
    mode_label = dict(MEASUREMENT_MODE_OPTIONS).get(mode, mode)
    configured_output = ""
    if state != "idle":
        configured_sweep = int(job.get("level_dbfs", -42))
        configured_noise = int(job.get("noise_level_dbfs", job.get("level_dbfs", -42)))
        configured_woofer = int(job.get("woofer_measurement_attenuation_db", -9))
        woofer_semantics = "합산 응답 조건이며 최종 재생 트림과 동일" if mode == "lr" else ("사용하지 않음" if mode == "mimo_stereo" else "측정 감쇄는 역컨볼루션에서 복원되며 SNR에만 영향")
        configured_output = f'''<div class="measurement-output-summary" data-measurement-step-content="2"><div><small>측정 구성</small><b>{html.escape(mode_label)}</b><span>{html.escape(MEASUREMENT_MODE_HELP.get(mode, ''))}</span></div><div><small>DAC 기준 출력</small><b>출력별 빠른 스윕 2초 · 본 스윕 {configured_sweep} dBFS</b><span>U7 청취 볼륨 무시·자동 복원 · 우퍼 감쇄 {configured_woofer} dB · 우퍼 실효 {configured_sweep + configured_woofer} dBFS<br>{html.escape(woofer_semantics)}</span></div></div>'''
    path_match_token = "" if path_match is None else str(path_match).lower()
    panel_empty = {
        1: "출력 조합과 측정 위치 수를 선택합니다.",
        2: "세션을 만든 뒤 출력 레벨을 확인합니다.",
        3: "레벨 검사를 통과하면 청취 위치 측정이 활성화됩니다.",
        4: "필요한 위치 측정을 마치면 FIR 계산 옵션이 표시됩니다.",
        5: "FIR 계산이 끝나면 전후 그래프·진단·A/B 비교가 표시됩니다.",
        6: "검증된 FIR 결과가 있어야 정식 적용할 수 있습니다.",
    }
    panels = "".join(
        f'''<section class="measurement-panel" id="measurement-panel-{number}" role="tabpanel" aria-labelledby="measurement-tab-{number}" tabindex="0" {'' if number == current_step else 'hidden'}>
          <div class="measurement-panel-heading"><span>{number}</span><div><b>{label}</b><small>{panel_empty[number]}</small></div></div>
          <div class="measurement-panel-content" data-measurement-panel-content="{number}"></div>
          <div class="measurement-panel-empty" data-measurement-panel-empty="{number}">{panel_empty[number]}</div>
        </section>'''
        for number, label in step_labels
    )
    return f"""
    <section class="measurement card-wide" data-job-state="{html.escape(state)}" data-job-position="{positions}" data-job-post-position="{int((job.get('post_filter_validation') or {}).get('positions_completed', 0))}" data-job-updated="{job.get('updated_unix', 0)}" data-job-result-token="{html.escape(str(job.get('result_token', 'none')))}" data-job-path-match="{path_match_token}" data-job-output="{html.escape(str(current_profile or ''))}">
      <div class="section-head"><div><h2>UMIK-1 측정 · 32768탭 자동 보정</h2><p class="muted">선택한 청취 위치 {total}곳 · 위치당 {len(job.get('sources') or ()) if state != 'idle' else '선택 구성'} sweep · UMIK 천장 방향 90°. 재생 중 CamillaDSP direct bypass 및 U7 입력 OFF.</p></div><span class="pill">{'UMIK 연결' if job.get('umik_connected') else 'UMIK 없음'}</span></div>
      {error}<div class="job-status"><div class="job-status-head"><div role="status" aria-live="polite" aria-atomic="true"><b id="job-stage">{html.escape(str(job.get('stage', '대기')))}</b><span id="job-eta">{eta_text}</span></div><span id="job-live-state" class="job-live-state"><i aria-hidden="true"></i>실시간</span></div><progress id="job-progress" max="100" value="{progress:.2f}"></progress><small id="job-percent">{progress:.0f}%</small></div>
      {session_overview}
      <nav class="workflow" role="tablist" aria-label="측정·보정 6단계 탭">{workflow}</nav>
      <div class="measurement-tab-panels">{panels}</div>
      <div class="measurement-step-sources">
      {path_lock_html}
      {configured_output}
      <details class="cal-card" id="measurement-step-1" data-measurement-step-content="1" open><summary class="cal-head"><span class="state-icon">μ</span><div><b>UMIK-1 마이크 보정</b><p class="muted">0°/90° 보정 파일 상태 및 교체 · 단계 이동만으로는 값이 지워지지 않습니다.</p></div></summary><div class="cal-slots">
        <form method="post" action="/measurement/calibration" enctype="multipart/form-data" class="cal-slot" onsubmit="return confirm('90° 보정 파일을 바꾸면 영향받는 측정과 FIR 결과가 초기화됩니다. 계속할까요?')"><input type="hidden" name="orientation" value="90"><div><b>90° · 천장 방향</b><span class="pill">룸 측정용 · 권장</span></div><p>{cal90_summary}</p><label>miniDSP 90° TXT<input required type="file" name="file" accept="text/plain,.txt"></label><button>90° 교체</button></form>
        <form method="post" action="/measurement/calibration" enctype="multipart/form-data" class="cal-slot"><input type="hidden" name="orientation" value="0"><div><b>0° · 마이크 정면</b><span class="pill neutral">근접 진단용</span></div><p>{cal0_summary}</p><label>miniDSP 0° TXT<input required type="file" name="file" accept="text/plain,.txt"></label><button>0° 교체</button></form>
      </div></details>
      {'' if mimo_supported else f'<p class="diagnostic-note" data-measurement-step-content="1"><b>MIMO 비활성</b> · {html.escape(str(capabilities.get("reason", "플랫폼 또는 timing reference 조건 미충족")))} SISO L/R/우퍼 보정은 그대로 사용할 수 있습니다.</p>'}
      {controls}
      {result_html}{MEASUREMENT_LEVEL_SCRIPT}
      <details data-measurement-step-content="4"><summary>알고리즘과 안전 제한</summary><p>세 위치 대표 응답에는 저역 1/12-oct, 중역 1/6-oct, 고역 1/3-oct 가변 smoothing을 사용합니다. Sweep 정합 deconvolution에 더해 pre/post-roll noise PSD로 대역별 SNR을 계산하고, 6–15 dB 신뢰도 ramp로 오염된 위치·대역의 보정과 boost를 줄입니다. 순간 생활소음은 100 ms sweep-envelope 이상치로 표시하며 원본 impulse/잔향을 잘라내지 않습니다. 위치 편차가 큰 null은 주파수별 regularization으로 boost를 축소하고, 반 옥타브 중앙값으로 추정한 스피커 자연 roll-off 밖은 boost하지 않습니다. 옥타브별 noise-compensated Schroeder EDT/T20으로 잔향을 진단하고 신뢰 가능한 300 Hz 이하 장시간 공진만 cut-only로 최대 3 dB 더 감쇄합니다. late reverb는 역보정하지 않습니다. 고역은 magnitude 위주이며 L/R은 공통 phase를 사용합니다. 저역 phase 모드는 FIR 자체 지연과 음향 도달 지연을 합산해 Front/Woofer를 정렬하고, L/R 잔차가 안전 한계를 넘으면 phase 보정을 자동 축소·해제합니다. MIMO는 제거했던 제어원별 bulk delay를 복원한 복소 응답으로 계산하고, 기존 SISO 저역 레벨을 기준으로 타깃을 고정하며 spectral continuity·기존 해 혼합·저역 late/early 비악화 검사를 적용합니다. 모든 최종 FIR의 최대 전달 이득은 0 dB 이하입니다.</p></details>
      </div>
      <noscript><style>.measurement-tab-panels{{display:none!important}}.measurement-step-sources{{display:block!important}}</style><p class="failure">단계 탭에는 JavaScript가 필요합니다. 아래에 전체 단계를 순서대로 표시합니다.</p></noscript>
    </section>"""


def render_page(status: dict, message: str = "", error: str = "", show_woofer: bool = False, view: str = "status") -> bytes:
    settings = status["settings"]
    resolved = status["resolved"]
    selected = settings["requested_profile"]
    effective = resolved["effective_profile"]
    selector = status.get("u7_selector", {})
    physical = selector.get("profile") if not selector.get("stale", True) else None
    physical_label = selector_ui_label(selector, physical)
    selected_label = PROFILE_UI.get(str(selected), {}).get("short", str(selected))
    effective_label = PROFILE_UI.get(str(effective), {}).get("short", str(effective))
    chunksize = settings["chunksize"]
    saved_volume_db = int(settings.get("output_volume_db", -10))
    graph = client_svg_graph(show_woofer, resolved["effective_rear_mode"], resolved["bypass"]) if view == "status" else ""
    measurement_html = measurement_panel(measurement_status(), status.get("preview", {})) if view == "measure" else ""
    cards = []
    for profile, korean in (("speaker", "스피커 출력 체인"), ("headphone", "헤드폰 잭 출력 체인")):
        files = status["files"][profile]
        mode = settings["rear_mode"][profile]
        bypass = settings["bypass"][profile]
        mimo_enabled = bool(settings.get("mimo_enabled", {}).get(profile, False))
        mimo_info = status.get("mimo", {}).get(profile)
        capability = status.get("capabilities", {})
        woofer_trim = settings.get("woofer_trim_db", {}).get(profile, 0)
        is_active = physical == profile
        staged = staging_status(profile)
        staged_front = staged["bands"]["front"]
        staged_rear = staged["bands"]["rear"]
        _candidate_front_path, candidate_rear_path, _candidate_stage = staged_candidates(status, profile)
        candidate_rear = candidate_rear_path is not None
        staged_front_name = html.escape(str(staged_front.get("original_name", "")))
        staged_rear_name = html.escape(str(staged_rear.get("original_name", "")))
        mimo_control = ""
        if profile == "speaker":
            if mimo_enabled:
                mimo_status_text = "켜짐 · 컨볼루션 8경로"
            elif mimo_info and mimo_info.get("valid"):
                mimo_status_text = "설치됨 · 현재 SISO"
            elif capability.get("mimo_supported"):
                mimo_status_text = "Pi4/5 사용 가능 · bank 없음"
            else:
                mimo_status_text = html.escape(str(capability.get("reason", "Pi4/5 전용")))
            mimo_disabled = " disabled" if not mimo_enabled and not (mimo_info and mimo_info.get("valid") and capability.get("mimo_supported")) else ""
            mimo_control = f'''<form method="post" action="/mimo-enabled" class="bypass {'enabled' if mimo_enabled else ''}"><input type="hidden" name="profile" value="speaker"><input type="hidden" name="enabled" value="{'off' if mimo_enabled else 'on'}"><div><b>MIMO 2×4 필터 묶음</b><small>{mimo_status_text}</small></div><button{mimo_disabled}>{'끄기' if mimo_enabled else '켜기'}</button></form>'''
        stage_html = ""
        if staged["active"]:
            preview_active = bool(status.get("preview", {}).get("active")) and not bool(status.get("preview", {}).get("stale"))
            previewing = preview_active and status.get("preview", {}).get("profile") == profile
            stage_html = f"""
            <div class="stage-workflow" aria-label="WAV 적용 단계">
              <div class="stage-step done"><span>1</span><b>WAV 선택</b><small>임시 보관 완료</small></div>
              <div class="stage-step current"><span>2</span><b>응답 비교</b><small>기존 점선 · 새 값 실선</small></div>
              <div class="stage-step"><span>3</span><b>A/B 청취</b><small>프로필은 그대로</small></div>
              <div class="stage-step"><span>4</span><b>정식 적용</b><small>백업 후 교체</small></div>
            </div>
            <div class="stage-summary"><span class="state-icon">∿</span><div><b>적용 대기 중</b><p>{('프런트 · ' + staged_front_name) if staged_front['present'] else '프런트 · 기존값 유지'}<br>{('우퍼 · ' + staged_rear_name) if staged_rear['present'] else ('우퍼 · 기존값 유지' if candidate_rear else '우퍼 · 프런트 복사')}</p></div><span class="pill">{'업로드값 테스트 중' if previewing else '기존값 재생 중'}</span></div>
            {staged_compare_graph(profile, candidate_rear)}
            <div class="stage-actions"><div><b>3 · 소리로 확인</b><p class="muted">업로드값 테스트는 설정과 정식 WAV를 바꾸지 않습니다.</p></div><div class="measure-actions">
              <form method="post" action="/staging/preview"><input type="hidden" name="profile" value="{profile}"><button>업로드 듣기</button></form>
              <form method="post" action="/staging/restore"><button class="secondary">기존값 듣기</button></form>
            </div></div>
            <div class="stage-actions final"><div><b>4 · 확인 후 정식 적용</b><p class="muted">기존 WAV는 자동 백업됩니다. 이 버튼을 누르기 전에는 덮어쓰지 않습니다.</p></div><div class="measure-actions">
              <form method="post" action="/staging/apply" onsubmit="return confirm('{korean}의 기존 FIR을 업로드한 값으로 교체합니다. 정식 적용할까요?')"><input type="hidden" name="profile" value="{profile}"><button>정식 적용</button></form>
              <form method="post" action="/staging/discard"><input type="hidden" name="profile" value="{profile}"><button class="secondary">업로드 취소</button></form>
            </div></div>"""
        cards.append(f"""
        <section class="card {'active-profile' if is_active else ''}" data-profile="{profile}">
          <div class="profile-title"><div><h2><span class="profile-icon">{ui_icon('speaker' if profile == 'speaker' else 'input', PROFILE_UI[profile]['short'])}</span>{korean}</h2><p class="profile-subtitle">{html.escape(PROFILE_UI[profile]['detail'])} · 현재 실제 연결은 스피커</p></div>{'<span class="active-badge">U7 현재 출력</span>' if is_active else ''}</div>
          <div class="profile-mini-flow" aria-label="{html.escape(PROFILE_UI[profile]['short'])} 처리 흐름"><span>{ui_icon('dsp', 'FIR')} 프런트 FIR</span><i>→</i><span>{ui_icon('route', '라우팅')} {html.escape('프런트→우퍼 복사' if mode == 'copy_front' else '프런트/우퍼 분리')}</span><i>→</i><span>{ui_icon('speaker', '스피커 체인')} 스피커 체인</span></div>
          <form method="post" action="/bypass" class="bypass {'enabled' if bypass else ''}">
            <input type="hidden" name="profile" value="{profile}">
            <input type="hidden" name="enabled" value="{'off' if bypass else 'on'}">
            <div><b>DSP 바이패스</b><small>{'켜짐 · FIR 0채널, 원본 L/R 복사' if bypass else '꺼짐 · FIR 프로필 사용'}</small></div>
            <button>{'끄기' if bypass else '켜기'}</button>
          </form>
          {mimo_control}
          <div class="file"><b>프런트 L/R FIR</b><p>{file_summary(files['front'])}</p>
            <form method="post" action="/upload-stage" enctype="multipart/form-data">
              <input type="hidden" name="profile" value="{profile}"><input type="hidden" name="band" value="front">
              <label class="file-picker-label" for="{profile}-front-wav-input">프런트 FIR WAV 선택</label><input id="{profile}-front-wav-input" required type="file" name="wav" accept="audio/wav,.wav"><button>프런트 WAV</button>
            </form>
          </div>
          <div class="file"><b>우퍼 L/R FIR</b><p>{file_summary(files['rear'])}</p>
            <form method="post" action="/upload-stage" enctype="multipart/form-data">
              <input type="hidden" name="profile" value="{profile}"><input type="hidden" name="band" value="rear">
              <label class="file-picker-label" for="{profile}-rear-wav-input">우퍼 FIR WAV 선택</label><input id="{profile}-rear-wav-input" required type="file" name="wav" accept="audio/wav,.wav"><button>우퍼 WAV</button>
            </form>
          </div>
          {stage_html}
          <form method="post" action="/rear-mode" class="mode">
            <input type="hidden" name="profile" value="{profile}">
            <label><input type="radio" name="mode" value="copy_front" {'checked' if mode == 'copy_front' else ''}> 프런트 처리 후 우퍼로 복사 · 2채널 컨볼루션</label>
            <label><input type="radio" name="mode" value="separate" {'checked' if mode == 'separate' else ''}> 별도 우퍼 FIR · 4채널 컨볼루션</label>
            <button>모드 적용</button>
          </form>
          <form method="post" action="/woofer-trim" class="mode">
            <input type="hidden" name="profile" value="{profile}">
            <label>실시간 우퍼 트림 <select name="trim_db">{''.join(f'<option value="{value}" {"selected" if value == woofer_trim else ""}>{value} dB</option>' for value in range(0, -19, -1))}</select></label>
            <button>트림 적용</button>
          </form>
        </section>""")
    camilla = "정상" if service_active("camilladsp.service") else "중지/오류"
    monitor = "정상" if service_active_any("audiodsp-profile-monitor.service") else "중지/오류"
    notice = f'<div class="notice" role="status" aria-live="polite">{html.escape(message)}</div>' if message else ""
    failure = f'<div class="failure" role="alert" tabindex="-1">{html.escape(error)}</div>' if error else ""
    woofer_query = "0" if show_woofer else "1"
    nav_parts = []
    for key, path, label in (("status", "/", f"{ui_icon('wave', '현황')}<span>현황</span>"), ("measure", "/measure", f"{ui_icon('mic', '측정')}<span>측정·보정</span>"), ("settings", "/settings", f"{ui_icon('dsp', '설정')}<span>프로필·설정</span>")):
        current_page = ' aria-current="page"' if view == key else ""
        nav_parts.append(f'<a class="{"active" if view == key else ""}" href="{path}"{current_page}>{label}</a>')
    nav = "".join(nav_parts)
    status_html = ""
    if view == "status":
        job = measurement_status()
        job_state = str(job.get("state", "idle"))
        job_positions = int(job.get("positions_completed", 0))
        if job_state in ("running", "processing", "cancelling"):
            next_label, next_href, next_note = "진행 상황 보기", "/measure", str(job.get("stage", "측정 작업 진행 중"))
        elif job_state == "idle":
            next_label, next_href, next_note = "룸 보정 시작", "/measure#measurement-step-1", "UMIK 보정 파일을 확인한 뒤 새 세션을 만듭니다."
        elif not (job.get("level_check") or {}).get("ok"):
            next_label, next_href, next_note = "빠른 검사로 이동", "/measure#measurement-step-2", "모든 출력 조합을 각 2초씩 재생해 본 측정과 같은 방식으로 SNR과 클리핑을 확인합니다."
        elif job_positions < int(job.get("positions_total", 3)):
            next_label, next_href, next_note = f"위치 {job_positions + 1} 측정으로 이동", "/measure#measurement-step-3", "완료한 위치는 유지됩니다. 실행 버튼을 누를 때만 측정합니다."
        elif not job.get("result"):
            next_label, next_href, next_note = "FIR 설정·계산으로 이동", "/measure#measurement-step-4", "원본 측정값은 유지하고 보정 설정을 선택합니다."
        elif not job.get("applied_profile"):
            next_label, next_href, next_note = "A/B 검토로 이동", "/measure#measurement-step-5", "다운로드와 A/B 비교 후에만 정식 적용합니다."
        else:
            next_label, next_href, next_note = "적용 결과 확인", "/measure#measurement-step-6", f"{job.get('applied_profile')} 프로필에 정식 적용된 상태입니다."
        graph_note = (
            "MIMO 활성 상태입니다. 아래 곡선은 전이 대역 위에서 사용하는 기본 SISO FIR만 보여줍니다. 저역의 실제 합산 예상은 측정·보정 결과 그래프와 MIMO 보고서에서 확인하세요."
            if resolved.get("mimo_paths") else
            "프런트 L/R은 개별 곡선입니다. 우퍼는 우퍼 L/R 크기의 에너지 평균이며, 프런트 복사 모드에서는 점선입니다."
        )
        status_html = f"""
        <section class="next-action"><div><small>지금 할 일</small><h2>{html.escape(next_label)}</h2><p>{html.escape(next_note)}</p></div><div class="measure-actions"><a class="button" href="{next_href}">{html.escape(next_label)}</a><a class="button secondary" href="/settings">프로필 · 백업 설정</a></div></section>
        {signal_flow_diagram(status)}
        <section class="card-wide output-volume" id="output-volume-control" data-saved-volume="{saved_volume_db}">
        <div class="section-head"><div><h2>출력 볼륨</h2><p class="muted">Xonar U7의 프런트/우퍼 전체 PCM 출력에 즉시 적용됩니다. FIR과 CamillaDSP는 재시작하지 않습니다.</p></div><output id="output-volume-value" for="output-volume-slider">{saved_volume_db} dB</output></div>
          <form method="post" action="/volume" id="output-volume-form">
            <input id="output-volume-slider" name="db" type="range" min="-60" max="0" step="1" value="{saved_volume_db}" aria-label="Xonar U7 output volume in decibels">
            <div class="volume-actions">
              <button type="button" class="secondary" data-volume-step="-1">−1 dB</button>
              <button type="button" class="secondary" data-volume-step="1">+1 dB</button>
              <span class="volume-presets" aria-label="볼륨 빠른 선택">
                <button type="button" class="secondary" data-volume="-40">−40</button><button type="button" class="secondary" data-volume="-30">−30</button><button type="button" class="secondary" data-volume="-20">−20</button><button type="button" class="secondary" data-volume="-10">−10 dB</button>
              </span>
              <button type="submit">저장 · 적용</button>
            </div>
          </form>
          <p class="muted volume-note" id="output-volume-status" role="status" aria-live="polite" aria-atomic="true">저장값 {saved_volume_db} dB · U7 실제 볼륨 확인 중…</p>
          <p class="muted">U7 물리 노브로 바꾼 값도 약 3초 안에 표시됩니다. 물리 노브 변경은 저장값을 바꾸지 않으므로 재부팅하면 마지막 웹/API 저장값으로 돌아옵니다.</p>
        </section>
        <section class="status"><h2>현재 설정</h2><table>
          <tr><td>U7 실제 출력</td><td><span class="pill" id="u7-physical" role="status" aria-live="polite" aria-atomic="true">{html.escape(physical_label)}</span> <span class="muted">(표시 전용 · U7 상단 버튼으로 변경 · 두 경로 모두 스피커 연결)</span></td></tr>
          <tr><td>DSP 요청 프로필</td><td id="dsp-requested">{html.escape(selected_label)}</td></tr>
          <tr><td>실제 적용 프로필</td><td id="dsp-effective" role="status" aria-live="polite" aria-atomic="true">{html.escape(effective_label)}{' (fallback)' if selected != effective else ''}</td></tr>
        <tr><td>DSP 바이패스</td><td>{'켜짐 · 원본 L/R 복사' if resolved['bypass'] else '꺼짐'}</td></tr>
          <tr><td>A/B 청취 상태</td><td>{('이번 튜닝 테스트 중 · ' + html.escape(str(status.get('preview', {}).get('profile')))) if status.get('preview', {}).get('active') and not status.get('preview', {}).get('stale') else '기존 정식 튜닝'}</td></tr>
        <tr><td>우퍼 처리</td><td>{html.escape(resolved['effective_rear_mode'])}</td></tr>
          <tr><td>컨볼루션</td><td>{resolved['convolution_channels']}채널</td></tr>
          <tr><td>MIMO bank</td><td>{'활성 · ' + html.escape(str(resolved.get('mimo_topology'))) if resolved.get('mimo_paths') else ('설정됨, 현재 플랫폼에서 비활성: ' + html.escape(str(resolved.get('mimo_unavailable_reason'))) if resolved.get('mimo_unavailable_reason') else '비활성')}</td></tr>
          <tr><td>CamillaDSP / HID 감시</td><td>{camilla} / {monitor}</td></tr>
          <tr><td>시스템 상태</td><td id="system-health">확인 중…</td></tr>
          <tr><td>오디오</td><td>48 kHz · 입력 2ch · 출력 4ch · chunksize {chunksize}</td></tr>
        </table></section>
        <section class="graphbox"><h2>현재 FIR 보정 전달함수</h2>
        <a class="button" href="/?woofer={woofer_query}">{'우퍼 숨기기' if show_woofer else '우퍼 표시'}</a>
        <p class="muted"><b>목표 청취 음압 그래프가 아닙니다.</b> 측정 응답에 곱하는 보정량이라 Harman 타깃과 같은 모양이 되지 않습니다. 타깃/적용 후 예상은 측정·보정 결과에서 확인하세요.<br>{graph_note}</p>{graph}</section>"""
    settings_html = ""
    if view == "settings":
        chunk_options = ''.join(f'<option value="{size}" {"selected" if chunksize == size else ""}>{size} · {label}</option>' for size, label in ((512, '최저 지연 / 고부하'), (1024, 'Pi4/Pi5 권장'), (2048, 'Pi2 권장'), (4096, '최대 여유')))
        restore_stage = restore_staging_status()
        automatic_backup = latest_system_backup()
        automatic_backup_html = (
            f'<a class="button secondary" download href="/api/backup/latest">최근 자동 복구 백업 받기</a>'
            f'<small class="muted">{html.escape(automatic_backup.name)}</small>'
            if automatic_backup else '<small class="muted">아직 자동 복구 백업이 없습니다.</small>'
        )
        if restore_stage.get("active"):
            restored_settings = restore_stage.get("settings", {})
            restore_detail = f"""
            <div class="restore-review">
              <div class="stage-workflow" aria-label="백업 복원 단계"><div class="stage-step done"><span>1</span><b>ZIP 선택</b><small>임시 보관</small></div><div class="stage-step done"><span>2</span><b>무결성 검사</b><small>해시·WAV·Cal</small></div><div class="stage-step current"><span>3</span><b>내용 확인</b><small>아직 미적용</small></div><div class="stage-step"><span>4</span><b>복원 확정</b><small>자동 백업 후</small></div></div>
              <div class="diagnostic-grid"><div><small>백업 버전</small><b>schema {restore_stage.get('schema_version')} · app {html.escape(str(restore_stage.get('app_version')))}</b></div><div><small>요청 프로필</small><b>{html.escape(str(restored_settings.get('requested_profile')))}</b></div><div><small>chunksize</small><b>{restored_settings.get('chunksize')}</b></div><div><small>FIR</small><b>{len(restore_stage.get('firs', {}))}개 검증</b></div><div><small>Calibration</small><b>{len(restore_stage.get('calibrations', {}))}개 검증</b></div></div>
              <p class="muted">{html.escape(str(restore_stage.get('original_name')))} · 현재 설정은 아직 바뀌지 않았습니다. 복원 직전 현재 전체 상태를 Pi의 복구용 ZIP으로 자동 백업합니다.</p>
          <div class="measure-actions"><form method="post" action="/backup/apply" onsubmit="return confirm('현재 전체 설정을 자동 백업한 뒤 검증된 ZIP을 복원합니다. 오디오가 잠시 재시작됩니다. 계속할까요?')"><button>전체 복원</button></form><form method="post" action="/backup/discard"><button class="secondary">복원 취소</button></form></div>
            </div>"""
        else:
            restore_detail = '<p class="muted">복원 ZIP을 선택하면 먼저 임시 검토만 합니다. 검증 완료 후 별도 확정 버튼을 눌러야 실제 설정이 바뀝니다.</p>'
        settings_html = f"""
        <section class="card-wide backup-panel"><div class="section-head"><div><h2>전체 백업 · 안전 복원</h2><p class="muted">프로필 설정, Speaker/Headphones/Factory FIR, 선택적 MIMO bank, 0°/90° UMIK calibration을 버전형 ZIP 하나로 관리합니다.</p></div><span class="pill neutral">schema v{BACKUP_SCHEMA_VERSION}</span></div>
        <div class="backup-actions"><div><b>현재 상태 보관</b><p>다운로드는 오디오를 바꾸지 않습니다.</p><a class="button" download href="/api/backup/download">전체 백업 ZIP</a>{automatic_backup_html}</div><form method="post" action="/backup/stage" enctype="multipart/form-data"><b>백업에서 복원</b><p>업로드 → 검사 → 확인 → 복원</p><label class="file-picker-label" for="backup-zip-input">AudioDSP 백업 ZIP 선택</label><input id="backup-zip-input" required type="file" name="backup" accept="application/zip,.zip"><button>ZIP 검토</button></form></div>
          {restore_detail}
        </section>
        <section class="status"><h2>엔진 설정</h2><table>
          <tr><td>오디오 형식</td><td>48 kHz · 입력 2ch · 출력 4ch</td></tr>
          <tr><td>처리 블록</td><td><form method="post" action="/chunksize"><select name="chunksize" aria-label="CamillaDSP chunksize">{chunk_options}</select> <button>적용</button></form><span class="muted">변경 시 오디오가 잠시 재시작됩니다. Pi 2는 2048, Pi 4/5는 1024를 권장합니다.</span></td></tr>
        </table></section>
        <div class="grid">{''.join(cards)}</div>
        <p class="muted">업로드 조건: stereo, 48 kHz, PCM/IEEE-float WAV, 최대 262,144 taps. LAN 내부 포트 8080에서만 사용하세요.</p>"""
    page_title = {"status": "현황 · AudioDSP", "measure": "측정 · 보정 · AudioDSP", "settings": "프로필 · 설정 · AudioDSP"}.get(view, "AudioDSP")
    body = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{page_title}</title>
    <script>(()=>{{const t=localStorage.getItem('audiodsp-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}})();</script><style>
    :root{{--bg:#f3f6fb;--bg-glow:#dbeafe;--surface:rgba(255,255,255,.88);--surface-strong:#fff;--text:#13213a;--muted:#61708a;--border:#d8e0eb;--accent:#2563eb;--accent-hover:#1d4ed8;--accent-soft:#dbeafe;--on-accent:#fff;--step-accent:#7c3aed;--step-soft:#ede9fe;--on-step:#fff;--success:#047857;--success-bg:#d1fae5;--danger:#b42318;--danger-bg:#fee4e2;--warning:#a15c00;--warning-bg:#fff4d6;--shadow:0 18px 45px rgba(32,55,92,.10);--graph-bg:#111827;--graph-grid:#334155;--graph-text:#94a3b8;--curve-l:#0284c7;--curve-r:#e11d48;--curve-w:#d97706;color-scheme:light}}
    :root[data-theme="dark"]{{--bg:#080d18;--bg-glow:#172554;--surface:rgba(19,29,48,.90);--surface-strong:#151f33;--text:#e8eef8;--muted:#9babc2;--border:#2b3a54;--accent:#38bdf8;--accent-hover:#7dd3fc;--accent-soft:#0c4a6e;--on-accent:#0f172a;--step-accent:#c4b5fd;--step-soft:#312e57;--on-step:#171126;--success:#5eead4;--success-bg:#123f3a;--danger:#fda4af;--danger-bg:#4c1720;--warning:#fbbf24;--warning-bg:#493514;--shadow:0 22px 55px rgba(0,0,0,.30);--graph-bg:#090f1c;--graph-grid:#2b3a54;--graph-text:#9babc2;--curve-l:#38bdf8;--curve-r:#fb7185;--curve-w:#fbbf24;color-scheme:dark}}
    @media(prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#080d18;--bg-glow:#172554;--surface:rgba(19,29,48,.90);--surface-strong:#151f33;--text:#e8eef8;--muted:#9babc2;--border:#2b3a54;--accent:#38bdf8;--accent-hover:#7dd3fc;--accent-soft:#0c4a6e;--on-accent:#0f172a;--step-accent:#c4b5fd;--step-soft:#312e57;--on-step:#171126;--success:#5eead4;--success-bg:#123f3a;--danger:#fda4af;--danger-bg:#4c1720;--warning:#fbbf24;--warning-bg:#493514;--shadow:0 22px 55px rgba(0,0,0,.30);--graph-bg:#090f1c;--graph-grid:#2b3a54;--graph-text:#9babc2;--curve-l:#38bdf8;--curve-r:#fb7185;--curve-w:#fbbf24;color-scheme:dark}}}}
    *{{box-sizing:border-box}}html{{min-height:100%;overflow-x:hidden;scroll-padding-top:18px}}body{{margin:0;min-height:100%;overflow-x:hidden;background:radial-gradient(circle at 12% -10%,var(--bg-glow),transparent 34rem),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI','Malgun Gothic',sans-serif;transition:background .25s,color .25s}}
    main{{width:100%;max-width:1160px;min-width:0;margin:auto;padding:clamp(16px,3vw,36px);overflow-x:clip}}h1{{margin:0;font-size:clamp(1.65rem,4vw,2.35rem);letter-spacing:-.04em}}h2{{margin:0 0 14px;font-size:1.15rem;letter-spacing:-.02em;overflow-wrap:anywhere}}p,code{{overflow-wrap:anywhere}}code{{color:var(--accent);font-size:.82rem}}
    .topbar{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}}.subtitle{{color:var(--muted);margin:5px 0 0}}
    .skip-link{{position:fixed;z-index:100;left:16px;top:12px;padding:10px 14px;border-radius:10px;background:var(--accent);color:var(--on-accent);font-weight:800;text-decoration:none;transform:translateY(-160%);transition:transform .15s}}.skip-link:focus{{transform:translateY(0)}}
    .app-nav{{display:flex;gap:7px;overflow-x:auto;padding:5px;margin:0 0 16px;background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow)}}.app-nav a{{position:relative;display:flex;align-items:center;justify-content:center;gap:8px;flex:1;min-width:max-content;padding:11px 16px;border-radius:10px;text-decoration:none;color:var(--muted);font-weight:800;text-align:center;touch-action:manipulation}}.app-nav a.active{{background:var(--accent-soft);color:var(--text);box-shadow:inset 0 -3px 0 var(--accent)}}.app-nav a:hover{{color:var(--text)}}.ui-icon{{display:inline-block;width:1.25em;height:1.25em;flex:0 0 auto;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;vertical-align:-.2em}}
    .theme-switch{{display:flex;padding:4px;background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}}.theme-switch button{{padding:7px 10px;background:transparent;color:var(--muted);box-shadow:none}}.theme-switch button[aria-pressed="true"]{{background:var(--accent-soft);color:var(--text)}}
    .status,.card,.graphbox{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:clamp(16px,2.5vw,24px);margin:16px 0;box-shadow:var(--shadow);backdrop-filter:blur(16px)}}
    .grid{{display:grid;grid-template-columns:1fr;gap:16px}}.grid .card{{margin:0}}.card.active-profile{{border:2px solid var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 13%,transparent),var(--shadow)}}.profile-title{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}}.profile-title h2{{display:flex;align-items:center;gap:9px;margin:0}}.profile-title>div{{min-width:0}}.profile-subtitle{{margin:5px 0 0 41px;color:var(--muted);font-size:.82rem}}.profile-icon,.state-icon{{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:10px;background:var(--accent-soft);color:var(--accent);font-weight:900;font-size:.9rem}}.profile-icon .ui-icon{{width:19px;height:19px}}.active-badge{{display:inline-flex;padding:5px 9px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:.78rem;font-weight:800;white-space:nowrap}}.profile-mini-flow{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:15px 0;padding:10px 12px;border:1px solid var(--border);border-radius:12px;background:color-mix(in srgb,var(--surface-strong) 74%,transparent);color:var(--muted);font-size:.78rem}}.profile-mini-flow span{{display:flex;align-items:center;gap:5px}}.profile-mini-flow i{{color:var(--accent);font-style:normal;font-weight:900}}
    .pill{{display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;background:var(--success-bg);color:var(--success);font-weight:700;text-transform:capitalize}}.pill.warn{{background:color-mix(in srgb,var(--warning) 14%,var(--surface-strong));color:var(--warning)}}.pill.error{{border:1px solid var(--danger);background:var(--danger-bg);color:var(--danger)}}.muted{{color:var(--muted);overflow-wrap:anywhere}}
    .file{{border-top:1px solid var(--border);padding:15px 0}}.file p{{min-height:42px;color:var(--muted)}}form{{margin:9px 0;min-width:0}}.file-picker-label{{display:block;margin-top:7px;color:var(--muted);font-size:.82rem;font-weight:700}}input[type=file]{{max-width:100%;margin:7px 0;color:var(--muted)}}input::file-selector-button{{min-height:36px;margin-right:9px;padding:7px 11px;border:1px solid var(--border);border-radius:8px;background:var(--surface-strong);color:var(--text);font:inherit;font-weight:700;cursor:pointer}}select{{max-width:100%;min-width:0;min-height:40px;font:inherit;background:var(--surface-strong);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:9px 12px}}
    button,.button{{min-height:40px;font:inherit;font-weight:700;background:var(--accent);color:var(--on-accent);border:0;border-radius:10px;padding:9px 14px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;box-shadow:0 6px 14px color-mix(in srgb,var(--accent) 25%,transparent);transition:transform .15s,background .15s;touch-action:manipulation}}
    button:hover,.button:hover{{background:var(--accent-hover);transform:translateY(-1px)}}button.secondary,.button.secondary{{background:transparent;color:var(--text);border:1px solid var(--border);box-shadow:none}}button.secondary:hover,.button.secondary:hover{{background:var(--accent-soft);color:var(--text)}}button:focus-visible,.button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,.app-nav a:focus-visible,.flow-step:focus-visible,.graph-scroll:focus-visible{{outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:2px}}
    .mode{{background:color-mix(in srgb,var(--surface-strong) 72%,transparent);border:1px solid var(--border);border-radius:12px;padding:12px}}.mode label{{display:block;margin:9px 0;line-height:1.45}}.bypass{{display:flex;align-items:center;justify-content:space-between;gap:12px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent);border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:14px}}.bypass.enabled{{border-color:var(--warning);background:color-mix(in srgb,var(--warning) 9%,var(--surface-strong))}}.bypass small{{display:block;color:var(--muted);margin-top:3px}}.missing{{color:var(--warning)}}.bad,.failure{{color:var(--danger)}}
    .notice{{background:var(--success-bg);color:var(--success);padding:13px 15px;border-radius:11px;margin:12px 0}}.failure{{background:var(--danger-bg);padding:13px 15px;border-radius:11px;margin:12px 0}}.graphbox>.muted{{word-break:keep-all}}.graph-scroll{{overflow-x:auto;overscroll-behavior-inline:contain;border-radius:12px}}.response{{display:block;width:100%;height:auto;margin-top:10px;border-radius:12px}}
    .card-wide{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:clamp(16px,2.5vw,24px);margin:16px 0;box-shadow:var(--shadow)}}.next-action{{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 20px;margin:16px 0;border:1px solid color-mix(in srgb,var(--accent) 48%,var(--border));border-radius:18px;background:linear-gradient(135deg,var(--accent-soft),var(--surface));box-shadow:var(--shadow)}}.next-action small{{color:var(--accent);font-weight:900;text-transform:uppercase;letter-spacing:.08em}}.next-action h2{{margin:4px 0}}.next-action p{{margin:0;color:var(--muted)}}.section-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}}.section-head h2{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}.signal-console{{overflow:hidden;background:linear-gradient(145deg,var(--surface),color-mix(in srgb,var(--accent-soft) 34%,var(--surface)))}}.signal-live{{display:inline-flex;align-items:center;gap:7px;padding:6px 9px;border:1px solid color-mix(in srgb,var(--success) 35%,var(--border));border-radius:999px;color:var(--success);font-size:.72rem;font-weight:900;letter-spacing:.08em}}.signal-live i{{width:7px;height:7px;border-radius:50%;background:var(--success);box-shadow:0 0 0 5px color-mix(in srgb,var(--success) 14%,transparent)}}.signal-flow{{display:grid;grid-template-columns:minmax(135px,1fr) 42px minmax(150px,1.1fr) 42px minmax(145px,1fr) 42px minmax(155px,1.15fr) 42px minmax(135px,1fr);align-items:stretch;gap:0;margin-top:18px}}.signal-node{{position:relative;display:flex;gap:10px;align-items:flex-start;min-width:0;padding:14px 12px;border:1px solid var(--border);border-radius:13px;background:var(--surface-strong);box-shadow:0 8px 24px color-mix(in srgb,var(--text) 6%,transparent)}}.signal-node>.ui-icon{{width:25px;height:25px;padding:4px;border-radius:7px;background:var(--accent-soft);color:var(--accent)}}.signal-node>div{{display:grid;gap:3px;min-width:0}}.signal-node small{{color:var(--muted);font-size:.62rem;font-weight:900;letter-spacing:.07em}}.signal-node b{{font-size:.88rem;overflow-wrap:anywhere}}.signal-node span{{color:var(--muted);font-size:.69rem;line-height:1.35;overflow-wrap:anywhere}}.signal-node.is-active{{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 13%,transparent)}}.signal-node.is-waiting{{border-style:dashed}}.signal-wire{{display:grid;place-items:center;color:color-mix(in srgb,var(--accent) 72%,var(--muted))}}.signal-wire svg{{display:block;width:100%;height:20px;overflow:visible;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}.signal-legend{{display:flex;gap:16px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px solid var(--border);color:var(--muted);font-size:.76rem}}.signal-legend span{{display:flex;align-items:center;gap:5px}}.measurement-path-lock{{display:flex;align-items:flex-start;gap:12px;padding:14px;margin:12px 0;border:1px solid var(--border);border-radius:14px;background:var(--surface-strong)}}.measurement-path-lock>.ui-icon{{width:28px;height:28px;padding:4px;border-radius:8px;background:var(--accent-soft);color:var(--accent)}}.measurement-path-lock>div{{display:grid;gap:3px}}.measurement-path-lock small{{color:var(--muted);font-size:.67rem;font-weight:900;letter-spacing:.08em}}.measurement-path-lock span{{color:var(--muted);font-size:.82rem;line-height:1.45}}.measurement-path-lock.path-ok{{border-color:var(--success)}}.measurement-path-lock.path-ok>.ui-icon{{background:var(--success-bg);color:var(--success)}}.measurement-path-lock.path-error{{border-color:var(--danger);background:var(--danger-bg)}}.measurement-path-lock.path-error>.ui-icon{{background:transparent;color:var(--danger)}}.measurement-result-path{{display:flex;align-items:center;gap:12px;padding:12px;border:1px solid var(--success);border-radius:12px;background:var(--success-bg);color:var(--success)}}.measurement-result-path b,.measurement-result-path span{{display:flex;align-items:center;gap:6px}}.measurement-result-path.legacy{{border-color:var(--warning);background:var(--warning-bg);color:var(--warning)}}.output-volume output{{min-width:6ch;color:var(--accent);font-size:clamp(1.7rem,5vw,2.5rem);font-weight:900;text-align:right;font-variant-numeric:tabular-nums}}.output-volume form{{display:grid;gap:12px;margin-top:14px}}.output-volume input[type=range]{{width:100%;height:28px;accent-color:var(--accent);cursor:pointer}}.volume-actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}.volume-presets{{display:flex;gap:6px;flex:1;flex-wrap:wrap}}.volume-note{{margin-bottom:4px;font-weight:700}}.job-status{{padding:14px;border:1px solid var(--border);border-radius:12px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent);margin:14px 0}}.session-overview{{display:grid;gap:14px;padding:16px;margin:14px 0 0;border:1px solid color-mix(in srgb,var(--step-accent) 48%,var(--border));border-radius:15px;background:linear-gradient(135deg,color-mix(in srgb,var(--step-soft) 58%,var(--surface)),var(--surface-strong))}}.session-overview.empty{{border-style:dashed}}.session-overview.empty p{{margin:0;color:var(--muted)}}.session-overview-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}.session-overview-head small{{color:var(--step-accent);font-size:.67rem;font-weight:900;letter-spacing:.09em}}.session-overview h3{{margin:3px 0 0;font-size:1.05rem}}.session-meta-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}}.session-meta-grid>div{{display:grid;gap:3px;padding:10px;border:1px solid var(--border);border-radius:10px;background:color-mix(in srgb,var(--surface-strong) 78%,transparent)}}.session-meta-grid small{{color:var(--muted)}}.session-note-form{{display:grid;grid-template-columns:minmax(150px,.65fr) minmax(260px,1.35fr);gap:10px 14px;align-items:center}}.session-note-form label{{display:grid;gap:3px}}.session-note-form label span{{color:var(--muted);font-size:.76rem;line-height:1.4}}.session-note-form textarea{{width:100%;min-height:58px;resize:vertical}}.session-note-form>div{{grid-column:2;display:flex;align-items:center;justify-content:space-between;gap:10px}}.session-note-form>div small{{color:var(--muted)}}.session-new-action{{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px;margin:12px 0;border:1px dashed var(--border);border-radius:12px}}.session-new-action p{{margin:4px 0 0}}.session-tools>summary{{justify-content:space-between;gap:12px}}.session-library{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:10px}}.saved-session{{display:grid;gap:9px;padding:12px;border:1px solid var(--border);border-radius:12px;background:var(--surface-strong)}}.saved-session.active{{border-color:var(--step-accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--step-accent) 12%,transparent)}}.saved-session-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}}.saved-session-head>div{{display:grid;gap:3px}}.saved-session-head small{{color:var(--muted)}}.saved-session-head form{{margin:0}}.session-note-preview{{min-height:2.7em;margin:0;padding:8px;border-radius:8px;background:color-mix(in srgb,var(--step-soft) 45%,transparent);font-size:.82rem;line-height:1.4;white-space:pre-wrap}}.saved-session-progress{{display:grid;grid-template-columns:repeat(6,1fr);gap:4px;align-items:center}}.saved-session-progress i{{height:5px;border-radius:999px;background:var(--border)}}.saved-session-progress i.done{{background:var(--success)}}.saved-session-progress span{{grid-column:1/-1;color:var(--muted);font-size:.72rem}}progress{{width:100%;height:13px;accent-color:var(--accent);margin:10px 0 4px}}.measure-form{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;align-items:end;padding:14px;border:1px solid var(--border);border-radius:12px;margin:14px 0}}.measure-form label{{display:grid;gap:6px;color:var(--muted);font-size:.86rem}}.level-slider{{padding:10px;border:1px solid var(--border);border-radius:11px;background:var(--surface-strong)}}.level-slider output{{color:var(--accent);font-weight:900;font-variant-numeric:tabular-nums}}.level-slider input[type=range]{{width:100%;height:24px;accent-color:var(--accent);cursor:pointer}}.level-slider small{{line-height:1.35}}.level-slider.not-used{{opacity:.48}}.measurement-output-summary{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}}.measurement-output-summary>div{{display:grid;gap:4px;padding:12px;border:1px solid var(--border);border-radius:12px;background:var(--surface-strong)}}.measurement-output-summary small{{color:var(--muted)}}.measurement-output-summary span{{color:var(--muted);font-size:.8rem;line-height:1.4}}.output-safety-note,.output-level-warning{{padding:9px;border-radius:9px;background:var(--success-bg);color:var(--success)}}.output-level-warning{{grid-column:1/-1;margin:0}}.output-safety-note,.output-level-warning.loud{{background:var(--warning-bg);color:var(--warning)}}.build-options{{display:block}}.build-fieldset{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;align-items:end;min-width:0;margin:0;padding:0;border:0}}.build-fieldset[disabled]{{opacity:.78}}.build-running-note{{padding:10px;border-radius:10px;background:var(--warning-bg);color:var(--warning)}}.build-options .advanced{{grid-column:1/-1;margin:2px 0 0;padding:12px;border:1px solid var(--border);border-radius:12px;background:var(--surface-strong)}}.advanced-grid{{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:10px;margin-top:12px}}.measure-actions{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}.measure-actions form{{margin:0}}button.danger{{background:var(--danger)}}.target-preview,.result-box{{border-top:1px solid var(--border);margin-top:16px;padding-top:16px}}#target-graph,#measurement-result-graph{{display:block;width:100%;height:auto;background:var(--graph-bg);border-radius:12px;margin-top:10px}}details{{margin-top:14px;border-top:1px solid var(--border);padding-top:12px}}summary{{display:flex;align-items:center;min-height:44px;cursor:pointer;font-weight:700}}
    .workflow{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:3px;margin:18px 0 0;padding:4px;border:1px solid color-mix(in srgb,var(--step-accent) 38%,var(--border));border-radius:14px 14px 0 0;background:color-mix(in srgb,var(--surface-strong) 78%,transparent)}}.flow-step{{display:flex;min-width:0;align-items:center;justify-content:center;gap:7px;min-height:48px;padding:9px 8px;border:0;border-radius:10px;color:var(--muted);background:transparent;box-shadow:none;font-size:.78rem;text-align:center;text-decoration:none}}button.flow-step{{cursor:pointer}}button.flow-step:hover{{color:var(--text);background:var(--step-soft);transform:none}}.flow-step>span{{display:grid;place-items:center;min-width:24px;height:24px;border-radius:50%;background:var(--border);color:var(--text);font-weight:800}}.flow-step.done{{color:var(--success)}}.flow-step.done>span{{background:var(--success-bg);color:var(--success)}}.flow-step.current:not(.selected){{box-shadow:inset 0 -3px 0 var(--step-accent)}}.flow-step.current>span{{background:var(--step-accent);color:var(--on-step)}}.flow-step.selected{{color:var(--text);background:var(--step-soft);box-shadow:inset 0 -3px 0 var(--step-accent)}}.flow-step.future{{opacity:.66}}.measurement-tab-panels{{border:1px solid color-mix(in srgb,var(--step-accent) 38%,var(--border));border-top:0;border-radius:0 0 14px 14px;background:color-mix(in srgb,var(--surface-strong) 32%,transparent);min-height:240px}}.measurement-panel{{padding:clamp(14px,2.5vw,22px);outline:none}}.measurement-panel[hidden]{{display:none}}.measurement-panel:focus-visible{{outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:-3px}}.measurement-panel-heading{{display:flex;align-items:center;gap:11px;padding-bottom:12px;border-bottom:1px solid var(--border)}}.measurement-panel-heading>span{{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--step-accent);color:var(--on-step);font-weight:900}}.measurement-panel-heading>div{{display:grid;gap:2px;min-width:0}}.measurement-panel-heading small{{color:var(--muted);line-height:1.35}}.measurement-panel-content{{min-width:0}}.measurement-panel-empty{{margin-top:14px;padding:16px;border:1px dashed var(--border);border-radius:12px;color:var(--muted);background:var(--surface-strong)}}.measurement-step-sources{{display:none}}[id^="measurement-step-"]{{scroll-margin-top:18px}}.form-note{{grid-column:1/-1;margin:0;color:var(--muted);font-size:.8rem;line-height:1.45}}.cal-card{{display:grid;gap:14px;padding:14px;border:1px solid var(--border);border-radius:14px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent)}}.cal-head{{display:flex;gap:10px;align-items:center}}.cal-head p,.cal-card p{{margin:5px 0 0}}.cal-slots{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.cal-slot{{display:grid;grid-template-columns:1fr auto;gap:8px 12px;padding:12px;margin:0;border:1px solid var(--border);border-radius:12px;background:var(--surface-strong)}}.cal-slot>div{{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:8px}}.cal-slot>p{{grid-column:1/-1;color:var(--muted);font-size:.82rem}}.cal-slot label{{display:grid;gap:5px;color:var(--muted);font-size:.82rem}}.cal-slot input[type=file]{{margin:0}}.pill.neutral{{background:var(--border);color:var(--muted)}}
    .flow-step.validation-error,.flow-step.validation-error.selected{{border:1px solid var(--danger);background:var(--danger-bg);color:var(--danger);box-shadow:inset 0 -3px 0 var(--danger)}}.flow-step.validation-error>span{{background:var(--danger);color:#fff}}.flow-step>em{{padding:2px 5px;border-radius:999px;background:var(--danger);color:#fff;font-size:.58rem;font-style:normal;font-weight:900;letter-spacing:.04em}}.validation-jumps{{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}}.validation-jump{{min-height:34px;padding:6px 10px;font-size:.76rem}}
    .level-result{{margin:14px 0;padding:14px;border:1px solid var(--border);border-radius:14px}}.level-result.ok{{border-color:var(--success);background:color-mix(in srgb,var(--success-bg) 55%,transparent)}}.level-result.not-ok{{border-color:var(--warning);background:color-mix(in srgb,var(--warning) 8%,var(--surface-strong))}}.level-verdict{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}.level-verdict>b{{font-size:1.05rem}}.metric-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-top:12px}}.metric-grid>div{{display:grid;gap:3px;padding:9px;border-radius:10px;background:var(--surface-strong)}}.metric-grid small{{color:var(--muted)}}button:disabled{{cursor:not-allowed;opacity:.45;transform:none;box-shadow:none}}
    .diagnostic-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:12px 0}}.diagnostic-grid>div{{display:grid;gap:4px;padding:10px;border:1px solid var(--border);border-radius:10px;background:var(--surface-strong)}}.diagnostic-grid>div.validation-fail{{border-color:var(--danger);background:var(--danger-bg);color:var(--danger);box-shadow:0 0 0 2px color-mix(in srgb,var(--danger) 12%,transparent)}}.diagnostic-grid>div.validation-fail small{{color:var(--danger)}}.diagnostic-grid small{{color:var(--muted)}}.validation-checklist{{margin:16px 0;padding:14px;border:1px solid var(--border);border-radius:14px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent)}}.validation-checklist h4{{margin:0 0 4px;font-size:1rem}}.validation-checklist .section-head+div{{display:grid;gap:7px;margin-top:12px}}.validation-row{{display:grid;grid-template-columns:72px 1fr;gap:10px;align-items:start;padding:10px;border:1px solid var(--border);border-radius:10px;background:var(--surface-strong)}}.validation-row.fail{{border-color:var(--danger);background:var(--danger-bg)}}.validation-row.pending{{border-color:var(--warning);background:color-mix(in srgb,var(--warning) 9%,var(--surface-strong))}}.validation-row.na{{opacity:.82}}.validation-row>div{{display:grid;gap:3px}}.validation-row small{{color:var(--muted);line-height:1.4}}.validation-row.fail small{{color:color-mix(in srgb,var(--danger) 76%,var(--text))}}.validation-row p{{margin:5px 0 0;padding:8px;border-radius:8px;background:color-mix(in srgb,var(--surface-strong) 68%,transparent);color:var(--danger);font-size:.82rem;line-height:1.5}}.validation-row.pending p{{color:var(--warning)}}.status-badge{{display:inline-grid;place-items:center;min-height:25px;padding:4px 7px;border-radius:999px;font-size:.68rem;font-weight:950;letter-spacing:.04em}}.status-badge.pass{{background:var(--success-bg);color:var(--success)}}.status-badge.fail{{background:var(--danger);color:#fff}}.status-badge.pending{{background:var(--warning);color:#10151e}}.status-badge.na{{background:var(--border);color:var(--muted)}}.pre-sum-card{{margin:14px 0;padding:14px;border:1px solid var(--border);border-radius:14px;background:var(--surface-strong)}}.pre-sum-card.pass{{border-color:var(--success)}}.pre-sum-card.fail{{border-color:var(--danger);background:var(--danger-bg)}}.diagnostic-note{{padding:12px;border-left:4px solid var(--accent);border-radius:8px;background:var(--accent-soft)}}.diagnostic-note ul{{margin:6px 0 0;padding-left:20px}}.target-fit{{margin:10px 0}}.decay-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}}.decay-grid>div{{display:grid;gap:5px;padding:10px;border:1px solid var(--border);border-radius:10px;background:var(--surface-strong)}}.decay-grid small{{color:var(--muted);font-weight:800}}.decay-grid span{{display:flex;justify-content:space-between;gap:8px;font-size:.82rem}}
    .stage-workflow{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding-top:16px;border-top:1px solid var(--border)}}.stage-step{{display:grid;grid-template-columns:30px 1fr;column-gap:8px;align-items:center;padding:10px;border:1px solid var(--border);border-radius:12px;color:var(--muted)}}.stage-step span{{grid-row:1/3;display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--border);font-weight:900;color:var(--text)}}.stage-step b{{font-size:.88rem;color:var(--text)}}.stage-step small{{font-size:.72rem}}.stage-step.done span{{background:var(--success-bg);color:var(--success)}}.stage-step.current{{border-color:var(--accent);background:var(--accent-soft)}}.stage-step.current span{{background:var(--accent);color:var(--on-accent)}}.stage-summary{{display:flex;align-items:center;gap:12px;padding:14px;margin:12px 0;border-radius:13px;background:color-mix(in srgb,var(--accent-soft) 56%,var(--surface-strong));border:1px solid color-mix(in srgb,var(--accent) 45%,var(--border))}}.stage-summary>div{{flex:1}}.stage-summary p{{margin:4px 0 0;color:var(--muted);font-size:.84rem;line-height:1.45}}.staged-compare h3{{margin:18px 0 4px;font-size:1rem}}.stage-actions{{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px;margin-top:12px;border:1px solid var(--border);border-radius:13px;background:color-mix(in srgb,var(--surface-strong) 70%,transparent)}}.stage-actions p{{margin:4px 0 0}}.stage-actions.final{{border-color:color-mix(in srgb,var(--success) 52%,var(--border));background:color-mix(in srgb,var(--success-bg) 35%,var(--surface-strong))}}
    .backup-actions{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}}.backup-actions>div,.backup-actions>form{{display:grid;align-content:start;gap:8px;padding:14px;margin:0;border:1px solid var(--border);border-radius:13px;background:var(--surface-strong)}}.backup-actions p{{margin:0;color:var(--muted);font-size:.84rem}}.backup-actions .button,.backup-actions button{{justify-self:start}}.restore-review{{margin-top:14px;padding-top:14px;border-top:1px solid var(--border)}}
    table{{border-collapse:collapse;width:100%}}td{{padding:9px 6px;border-bottom:1px solid var(--border)}}td:first-child{{color:var(--muted);width:38%}}output,progress,table,code,.metric-grid,.diagnostic-grid{{font-variant-numeric:tabular-nums}}
    details{{border:1px solid var(--border);border-radius:12px;padding:0 12px;background:color-mix(in srgb,var(--surface-strong) 66%,transparent)}}summary{{display:flex;align-items:center;gap:10px;min-height:48px;padding:7px 2px;cursor:pointer;font-weight:700;list-style:none}}summary::-webkit-details-marker{{display:none}}summary::marker{{content:""}}summary::after{{content:"";flex:0 0 auto;width:10px;height:10px;margin-left:auto;margin-right:4px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg) translate(-2px,-2px);transition:transform .18s ease,border-color .18s ease}}details[open]>summary{{color:var(--step-accent);border-bottom:1px solid var(--border);margin-bottom:12px}}details[open]>summary::after{{border-color:var(--step-accent);transform:rotate(225deg) translate(-2px,-2px)}}summary:hover{{color:var(--step-accent)}}
    summary::after{{display:block}}details.cal-card{{padding:14px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent)}}
.session-filter{{display:grid;grid-template-columns:minmax(150px,.4fr) minmax(240px,1fr);gap:10px;align-items:center;margin-top:12px;color:var(--muted);font-size:.82rem}}.session-filter input{{width:100%}}.session-save-state.dirty{{color:var(--warning);font-weight:850}}.saved-session[hidden]{{display:none}}
.saved-session-actions{{display:flex!important;grid-auto-flow:column;align-items:center;justify-content:end;gap:6px}}.saved-session-actions form{{margin:0}}.session-delete{{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:44px}}.session-delete .ui-icon{{width:18px;height:18px}}.position-count-note,.baseline-note{{grid-column:1/-1;line-height:1.55}}.measure-form label>span{{font-size:.74rem;line-height:1.4}}.post-validation-card{{margin-top:14px;padding:15px;border:1px solid color-mix(in srgb,var(--accent) 45%,var(--border));border-radius:14px;background:color-mix(in srgb,var(--accent-soft) 28%,var(--surface-strong))}}.post-validation-card h4{{margin:0 0 4px}}.post-validation-card .section-head p{{margin:0}}.post-validation-card>.success{{color:var(--success);background:var(--success-bg);padding:12px;border-radius:10px}}.post-validation-metrics span{{color:var(--muted);font-size:.72rem}}
    .capture-recovery,.recovery-card,.graph-toolbar{{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 14px;border:1px solid var(--border);border-radius:12px;margin:12px 0;background:var(--surface-strong)}}.capture-recovery>div,.graph-toolbar>div:first-child{{display:grid;gap:3px}}.capture-recovery span,.graph-toolbar small{{color:var(--muted);font-size:.78rem}}.recovery-card{{border-color:var(--danger);background:var(--danger-bg)}}.recovery-card p{{margin:5px 0}}.graph-toolbar{{margin-top:18px}}.graph-toolbar>div:last-child{{display:flex;gap:6px}}.graph-toolbar button{{min-width:68px;padding:8px 12px}}.graph-toolbar button.selected{{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}}.level-action{{padding:10px;border-radius:10px;background:var(--accent-soft);color:var(--text)}}
    .job-status-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}.job-live-state{{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;color:var(--success);font-size:.72rem;font-weight:850}}.job-live-state i{{width:7px;height:7px;border-radius:50%;background:currentColor}}.job-live-state.retrying{{color:var(--warning)}}.job-live-state.refreshing{{color:var(--accent)}}
    .validation-row.warn{{border-color:var(--warning);background:color-mix(in srgb,var(--warning) 9%,var(--surface-strong))}}.validation-row.warn p{{color:var(--warning)}}.status-badge.warn{{background:var(--warning);color:#10151e}}
    @media(max-width:980px){{.signal-flow{{grid-template-columns:1fr;justify-items:stretch}}.signal-wire{{height:30px}}.signal-wire svg{{width:42px;transform:rotate(90deg)}}.signal-node{{min-height:76px}}}}
    @media(max-width:760px){{html{{scroll-padding-bottom:96px}}body{{padding-bottom:88px}}button,.button,select,input::file-selector-button,.app-nav a{{min-height:44px}}.grid{{grid-template-columns:1fr}}.topbar,.section-head,.stage-actions{{align-items:flex-start;flex-direction:column}}.app-nav{{position:fixed;z-index:20;left:10px;right:10px;bottom:max(8px,env(safe-area-inset-bottom));margin:0;padding:6px;background:color-mix(in srgb,var(--surface) 92%,transparent);box-shadow:0 8px 28px #0005;backdrop-filter:blur(14px)}}.app-nav a{{flex:1;text-align:center;padding:10px 5px;font-size:.78rem}}.theme-switch{{align-self:stretch;justify-content:center}}.theme-switch button{{flex:1}}td{{display:block;width:100%!important;padding:6px 2px}}td:first-child{{border-bottom:0;padding-top:11px}}.status,.card,.graphbox,.card-wide{{border-radius:15px}}.bypass{{align-items:stretch;flex-direction:column}}.bypass button{{width:100%}}.graph-scroll .response{{width:700px;max-width:none}}.measure-form,.advanced-grid,.backup-actions,.decay-grid,.measurement-output-summary{{grid-template-columns:1fr}}.measure-form button,.cal-slot button,.backup-actions .button,.backup-actions button{{width:100%}}.workflow{{position:sticky;z-index:12;top:6px;grid-template-columns:repeat(3,minmax(0,1fr));padding:6px;margin-inline:-6px;border-radius:14px 14px 0 0;background:color-mix(in srgb,var(--surface) 94%,transparent);box-shadow:var(--shadow);backdrop-filter:blur(12px)}}.flow-step{{min-height:46px;padding:7px 4px;gap:5px}}.flow-step>span{{min-width:22px;height:22px}}.measurement-tab-panels{{margin-inline:-6px}}.stage-workflow,.cal-slots{{grid-template-columns:1fr}}.cal-slot{{grid-template-columns:1fr}}.metric-grid,.diagnostic-grid{{grid-template-columns:1fr 1fr}}}}
@media(max-width:760px){{.session-library,.session-note-form,.session-filter{{grid-template-columns:1fr}}.session-note-form>div{{grid-column:1}}.session-new-action{{align-items:stretch;flex-direction:column}}.saved-session-head{{align-items:stretch;flex-direction:column}}.saved-session-actions{{width:100%;justify-content:stretch}}.saved-session-actions form{{flex:1}}.saved-session-head form button{{width:100%}}}}
    .topbar,.app-nav,.status,.card,.graphbox,.card-wide,.measurement,.backup-panel{{min-width:0;max-width:100%}}.flow-step{{min-width:0;overflow:hidden}}.flow-step b{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.cal-card>summary{{margin:0;padding:0;border:0;list-style:none}}.cal-card>summary::-webkit-details-marker{{display:none}}
    @media(max-width:760px){{.next-action{{align-items:flex-start;flex-direction:column}}.app-nav a{{min-width:0}}.theme-switch{{width:100%}}}}
    @media(max-width:600px){{.workflow{{grid-template-columns:repeat(3,minmax(0,1fr));width:auto}}.flow-step{{flex-direction:column;gap:2px;font-size:.7rem}}.flow-step b{{max-width:100%}}.topbar>*,.section-head>*,.backup-actions>*,.measure-form>*,.cal-slot>*{{min-width:0;max-width:100%}}.theme-switch{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%}}.theme-switch button{{min-width:0;padding-inline:4px}}input[type=file]{{width:100%}}button,.button{{text-align:center}}.measure-actions form,.measure-actions a{{width:100%}}.volume-actions>button[type=submit]{{width:100%}}.volume-presets{{order:3;flex-basis:100%}}.volume-presets button{{flex:1}}.app-nav a{{flex-direction:column;gap:2px;font-size:.68rem}}.app-nav .ui-icon{{width:19px;height:19px}}.measurement-result-path{{align-items:flex-start;flex-direction:column}}.profile-title{{align-items:flex-start;flex-direction:column}}.profile-subtitle{{margin-left:0}}.session-overview-head{{align-items:flex-start;flex-direction:column}}.session-meta-grid{{grid-template-columns:1fr 1fr}}.session-note-form>div{{align-items:stretch;flex-direction:column}}.session-note-form button{{width:100%}}.validation-row{{grid-template-columns:54px 1fr}}}}
    @media(max-width:600px){{.capture-recovery,.recovery-card,.graph-toolbar{{align-items:stretch;flex-direction:column}}.capture-recovery form,.capture-recovery button,.recovery-card form,.recovery-card button{{width:100%}}.graph-toolbar>div:last-child{{display:grid;grid-template-columns:1fr 1fr}}}}
    @media(prefers-reduced-motion:reduce){{*{{transition:none!important;scroll-behavior:auto!important}}}}
    </style></head><body data-physical="{physical or ''}" data-requested="{selected}" data-effective="{effective}"><a class="skip-link" href="#main-content">본문으로 바로가기</a><main id="main-content" tabindex="-1">
    <header class="topbar"><div><h1>AudioDSP</h1><p class="subtitle">Xonar U7 · 룸 보정 · FIR 프로필</p></div><div class="theme-switch" role="group" aria-label="색상 테마"><button type="button" data-theme-choice="auto">자동</button><button type="button" data-theme-choice="light">밝게</button><button type="button" data-theme-choice="dark">어둡게</button></div></header>
    <nav class="app-nav" aria-label="주요 화면">{nav}</nav>{notice}{failure}{status_html}{measurement_html}{settings_html}
    <script>(()=>{{const buttons=[...document.querySelectorAll('[data-theme-choice]')];const current=()=>localStorage.getItem('audiodsp-theme')||'auto';const paint=()=>buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.themeChoice===current())));buttons.forEach(b=>b.addEventListener('click',()=>{{const t=b.dataset.themeChoice;if(t==='auto'){{localStorage.removeItem('audiodsp-theme');delete document.documentElement.dataset.theme;}}else{{localStorage.setItem('audiodsp-theme',t);document.documentElement.dataset.theme=t;}}paint();}}));paint();const failure=document.querySelector('.failure');if(failure)failure.focus();}})();</script>
    <script>(()=>{{/* output_volume_control */const root=document.getElementById('output-volume-control');if(!root)return;const slider=document.getElementById('output-volume-slider'),value=document.getElementById('output-volume-value'),note=document.getElementById('output-volume-status'),form=document.getElementById('output-volume-form');let timer=0,writing=false;const clamp=db=>Math.max(-60,Math.min(0,Math.round(db)));const label=db=>`${{Number(db).toFixed(Number.isInteger(Number(db))?0:1)}} dB`;const paint=v=>{{const saved=Number(v.saved_db??root.dataset.savedVolume);root.dataset.savedVolume=String(saved);const actual=v.available?Number(v.actual_db):null;if(document.activeElement!==slider)slider.value=String(actual??saved);value.textContent=label(actual??saved);if(!v.available){{note.textContent=`U7 실제 볼륨을 읽을 수 없음 · 재부팅 저장값 ${{label(saved)}}${{v.error?' · '+v.error:''}}`;note.classList.add('bad');}}else if(Math.abs(actual-saved)>.05){{note.textContent=`U7 실제 ${{label(actual)}} · 재부팅 저장값 ${{label(saved)}} · 물리 노브 변경 감지`;note.classList.remove('bad');}}else{{note.textContent=`U7 실제·저장값 ${{label(actual)}} · ${{v.channels}}채널 동일 적용`;note.classList.remove('bad');}}}};const load=async()=>{{clearTimeout(timer);if(document.hidden){{timer=setTimeout(load,3000);return;}}try{{const r=await fetch('/api/volume',{{cache:'no-store'}});if(r.ok)paint(await r.json());}}catch(_e){{}}finally{{timer=setTimeout(load,3000);}}}};const write=async db=>{{if(writing)return;writing=true;const target=clamp(db);slider.value=String(target);value.textContent=label(target);note.textContent='U7에 적용 중…';try{{const r=await fetch('/api/volume',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{db:target}})}});const body=await r.json().catch(()=>({{}}));if(!r.ok)throw Error(body.error||`HTTP ${{r.status}}`);paint(body);}}catch(e){{note.textContent='볼륨 적용 실패 · '+e.message;note.classList.add('bad');}}finally{{writing=false;}}}};slider.addEventListener('input',()=>{{value.textContent=label(Number(slider.value));}});slider.addEventListener('change',()=>write(Number(slider.value)));form.addEventListener('submit',e=>{{e.preventDefault();write(Number(slider.value));}});root.querySelectorAll('[data-volume-step]').forEach(b=>b.addEventListener('click',()=>write(Number(slider.value)+Number(b.dataset.volumeStep))));root.querySelectorAll('[data-volume]').forEach(b=>b.addEventListener('click',()=>write(Number(b.dataset.volume))));document.addEventListener('visibilitychange',()=>{{if(!document.hidden)load();}});load();}})();</script>
    <script>(()=>{{/* live_u7_status_poll */const initial={{physical:document.body.dataset.physical,requested:document.body.dataset.requested,effective:document.body.dataset.effective}},labels={{speaker:'스피커 출력',headphone:'헤드폰 잭'}};let timer=0,busy=false,reloading=false;const badge=(card,on)=>{{card.classList.toggle('active-profile',on);let b=card.querySelector('.active-badge');if(on&&!b){{b=document.createElement('span');b.className='active-badge';b.textContent='U7 현재 출력';card.querySelector('.profile-title').append(b);}}else if(!on&&b)b.remove();}};const paint=s=>{{const q=s.u7_selector||{{}};const physical=q.stale?'':(q.profile||''),physicalLabel=labels[physical]||'감지 대기';const physicalNode=document.getElementById('u7-physical');if(physicalNode)physicalNode.textContent=physicalLabel;const flowNode=document.getElementById('u7-flow-output');if(flowNode)flowNode.textContent=physicalLabel;document.querySelectorAll('.selector-node,.output-node').forEach(n=>{{n.classList.toggle('is-active',!!physical);n.classList.toggle('is-waiting',!physical);}});document.querySelectorAll('.card[data-profile]').forEach(c=>badge(c,c.dataset.profile===physical));const requested=s.settings.requested_profile;const effective=s.resolved.effective_profile;const requestedNode=document.getElementById('dsp-requested');const effectiveNode=document.getElementById('dsp-effective');if(requestedNode)requestedNode.textContent=labels[requested]||requested;if(effectiveNode)effectiveNode.textContent=(labels[effective]||effective)+(requested!==effective?' (대체 사용)':'');if(!reloading&&(requested!==initial.requested||effective!==initial.effective)){{reloading=true;setTimeout(()=>location.reload(),180);}}}};const poll=async()=>{{clearTimeout(timer);if(document.hidden){{timer=setTimeout(poll,3000);return;}}if(busy)return;busy=true;try{{const r=await fetch('/api/status',{{cache:'no-store'}});if(r.ok)paint(await r.json());}}catch(_e){{}}finally{{busy=false;timer=setTimeout(poll,1500);}}}};document.addEventListener('visibilitychange',()=>{{if(!document.hidden)poll();}});timer=setTimeout(poll,500);}})();</script>
    <script>(()=>{{/* measurement_ui */
const panel=document.querySelector('.measurement');
if(!panel)return;
let reloading=false;
const initial={{
  state:panel.dataset.jobState,
  position:panel.dataset.jobPosition,
  postPosition:panel.dataset.jobPostPosition,
  updated:Number(panel.dataset.jobUpdated||0),
  resultToken:panel.dataset.jobResultToken||'none',
  pathMatch:panel.dataset.jobPathMatch,
  output:panel.dataset.jobOutput
}};
let wasBusy=['running','processing','cancelling'].includes(initial.state);

const draw=(svg,curves,minY,maxY,range=[20,20000])=>{{
  if(!svg)return;
  svg.replaceChildren();
  const W=760,H=250,L=48,R=12,T=12,B=28;
  const [minF,maxF]=range;
  const x=f=>L+(Math.log10(f)-Math.log10(minF))/Math.log10(maxF/minF)*(W-L-R);
  const y=d=>T+(maxY-d)/(maxY-minY)*(H-T-B);
  let markup='';
  for(const f of [20,30,50,70,100,150,200,250,500,1000,2000,5000,10000,20000].filter(f=>f>=minF&&f<=maxF)) markup+=`<line x1="${{x(f)}}" y1="${{T}}" x2="${{x(f)}}" y2="${{H-B}}" stroke="var(--graph-grid)"/><text x="${{x(f)}}" y="${{H-8}}" text-anchor="middle" fill="var(--graph-text)" font-size="10">${{f>=1000?(f/1000)+'k':f}}</text>`;
  for(let d=Math.ceil(minY/5)*5;d<=maxY;d+=5) markup+=`<line x1="${{L}}" y1="${{y(d)}}" x2="${{W-R}}" y2="${{y(d)}}" stroke="var(--graph-grid)"/><text x="${{L-6}}" y="${{y(d)+3}}" text-anchor="end" fill="var(--graph-text)" font-size="10">${{d}}</text>`;
  const colors=['var(--curve-l)','var(--curve-r)','var(--curve-w)'];
  curves.forEach((curve,i)=>{{
    const color=curve.color||colors[i%colors.length];
    const visible=curve.f.map((f,n)=>({{f,d:curve.d[n],band:curve.band?.[n]}})).filter(v=>v.f>=minF&&v.f<=maxF&&Number.isFinite(v.d));
    if(!visible.length)return;
    if(curve.band){{
      const upper=visible.map(v=>`${{x(v.f).toFixed(1)}},${{y(v.d+(v.band||0)).toFixed(1)}}`);
      const lower=visible.map(v=>`${{x(v.f).toFixed(1)}},${{y(v.d-(v.band||0)).toFixed(1)}}`).reverse();
      markup+=`<polygon points="${{upper.concat(lower).join(' ')}}" fill="${{color}}" opacity=".10"/>`;
    }}
    const points=visible.map(v=>`${{x(v.f).toFixed(1)}},${{y(v.d).toFixed(1)}}`).join(' ');
    markup+=`<polyline points="${{points}}" fill="none" stroke="${{color}}" stroke-width="${{curve.width||2}}" stroke-dasharray="${{curve.dash||''}}"/><text x="${{L+8}}" y="${{T+15+i*15}}" fill="${{color}}" font-size="10">${{curve.name}}</text>`;
  }});
  svg.innerHTML=markup;
}};

const targetSelect=document.getElementById('target-choice');
const bassSelect=document.querySelector('[name=bass_tilt_db]');
const trebleSelect=document.querySelector('[name=treble_tilt_db]');
const voicingSelect=document.getElementById('voicing-quick');
const trimSelect=document.querySelector('select[name=woofer_trim_db]');
const presetSelect=document.querySelector('select[name=preset]');
const voicings={{neutral:{{b:0,t:0,w:0}},clear:{{b:0,t:1,w:0}},warm:{{b:2,t:-1,w:0}},night:{{b:-2,t:0,w:-3}}}};

voicingSelect?.addEventListener('change',()=>{{
  const v=voicings[voicingSelect.value];
  if(!v)return;
  if(bassSelect)bassSelect.value=String(v.b);
  if(trebleSelect)trebleSelect.value=String(v.t);
  if(trimSelect)trimSelect.value=String(v.w);
  if(presetSelect)presetSelect.value='none';
  bassSelect?.dispatchEvent(new Event('change',{{bubbles:true}}));
}});

let catalog=null;
const pref=(f,b,t)=>{{
  let x=0;
  if(f<=20)x+=b;
  else if(f<250){{const p=Math.log(f/20)/Math.log(12.5);x+=b*(.5+.5*Math.cos(Math.PI*p));}}
  if(f>=20000)x+=t;
  else if(f>1000){{const p=Math.log(f/1000)/Math.log(20);x+=t*(.5-.5*Math.cos(Math.PI*p));}}
  return x;
}};

const paintTarget=()=>{{
  if(!catalog||!targetSelect)return;
  const t=catalog.targets[targetSelect.value];
  const b=Number(bassSelect?.value||0);
  const h=Number(trebleSelect?.value||0);
  const values=t.db.map((v,i)=>v+pref(t.frequency[i],b,h));
  draw(document.getElementById('target-graph'),[{{name:t.label+` · 저음 ${{b>=0?'+':''}}${{b}} / 고음 ${{h>=0?'+':''}}${{h}} dB`,f:t.frequency,d:values}}],-12,12);
}};

fetch('/api/targets',{{cache:'no-store'}}).then(r=>r.json()).then(j=>{{catalog=j;paintTarget();}}).catch(()=>{{}});
[targetSelect,bassSelect,trebleSelect].forEach(e=>e?.addEventListener('change',paintTarget));

let resultCurves=[];
let resultRange='full';
const paintResult=()=>{{
  if(!resultCurves.length)return;
  const range=resultRange==='bass'?[20,250]:[20,20000];
  const values=resultCurves.flatMap(curve=>curve.f.map((frequency,index)=>({{frequency,value:curve.d[index]}})))
    .filter(item=>item.frequency>=range[0]&&item.frequency<=range[1]&&Number.isFinite(item.value)).map(item=>item.value);
  if(!values.length)return;
  const minY=Math.floor((Math.min(-10,...values)-2)/5)*5;
  const maxY=Math.ceil((Math.max(10,...values)+2)/5)*5;
  draw(document.getElementById('measurement-result-graph'),resultCurves,minY,maxY,range);
  const summary=document.getElementById('measurement-result-summary');
  if(summary)summary.textContent=resultRange==='bass'?'20–250 Hz 확대 · 크로스오버 합산과 딥 진단':'20 Hz–20 kHz 전체 · L/R+우퍼 합산과 선택 타깃';
}};
document.querySelectorAll('[data-result-range]').forEach(button=>button.addEventListener('click',()=>{{
  resultRange=button.dataset.resultRange;
  document.querySelectorAll('[data-result-range]').forEach(item=>{{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));}});
  paintResult();
}}));

let timer=0;
const poll=async()=>{{
  clearTimeout(timer);
  if(document.hidden){{timer=setTimeout(poll,3000);return;}}
  let busy=false;
  const live=document.getElementById('job-live-state');
  const paintLive=(label,state='')=>{{if(!live)return;live.className='job-live-state'+(state?' '+state:'');live.lastChild.textContent=label;}};
  const controller=new AbortController();
  const abortTimer=setTimeout(()=>controller.abort(),4500);
  try{{
    const r=await fetch('/api/measurement/status',{{cache:'no-store',signal:controller.signal}});
    if(r.ok){{
      const j=await r.json();
      paintLive('실시간');
      const p=document.getElementById('job-progress');
      if(p)p.value=j.progress||0;
      const pct=document.getElementById('job-percent');
      if(pct)pct.textContent=Math.round(j.progress||0)+'%';
      const stage=document.getElementById('job-stage');
      if(stage)stage.textContent=j.stage||'';
      const eta=document.getElementById('job-eta');
      if(eta)eta.textContent=Number.isFinite(j.eta_seconds)?' · 예상 '+j.eta_seconds+'초':'';
      busy=['running','processing','cancelling'].includes(j.state);
      if(busy) wasBusy=true;
      const jobDone=wasBusy&&!busy;
      const posAdv=String(j.positions_completed||0)!==initial.position&&!busy;
      const postAdv=String(j.post_filter_validation?.positions_completed||0)!==initial.postPosition&&!busy;
      const resultChanged=String(j.result_token||'none')!==initial.resultToken&&!busy;
      const currentPathMatch=j.measurement_output_match==null?'':String(j.measurement_output_match);
      const currentOutput=String(j.output_selector?.profile||'');
      const measurementPathChanged=!busy&&(currentPathMatch!==initial.pathMatch||currentOutput!==initial.output);
      const terminalChanged=!busy&&Number(j.updated_unix||0)>initial.updated&&String(j.state)!==initial.state&&['ready','measured','built','error'].includes(String(j.state));
      if(!reloading&&(jobDone||posAdv||postAdv||resultChanged||measurementPathChanged||terminalChanged)){{
        reloading=true;
        paintLive('결과 반영 중','refreshing');
        const url=new URL(location.href);url.searchParams.set('updated',String(j.updated_unix||Date.now()));
        setTimeout(()=>location.replace(url),150);
        return;
      }}
      if(j.result?.graphs){{
        const curves=[];
        const measured=j.result.self_validation?.post_filter_sum?.channels;
        if(measured?.left?.frequency){{
          for(const [name,key,color] of [['L+우퍼 실측','left','var(--curve-l)'],['R+우퍼 실측','right','var(--curve-r)']]){{
            const c=measured[key];
            if(c)curves.push({{name,f:c.frequency,d:c.measured_sum_db,band:c.spatial_std_db,color,width:2.5}});
          }}
          const c=measured.left;
          if(c)curves.push({{name:'선택 타깃 · 실제 합산 판정',f:c.frequency,d:c.effective_target_db,color:'var(--graph-text)',dash:'2 4',width:1.5}});
        }}else if(j.result.crossover?.sum_guard_enabled&&j.result.crossover?.channels){{
          for(const [name,key,color] of [['L+우퍼 합산 예측','left','var(--curve-l)'],['R+우퍼 합산 예측','right','var(--curve-r)']]){{
            const c=j.result.crossover.channels[key];
            if(c?.frequency){{
              const reliable=Boolean(c.complex_prediction_reliable);
              curves.push({{name:reliable?name:name.replace('합산 예측','합산 안전 상한 · 위상 제한'),f:c.frequency,d:reliable?c.predicted_complex_db:(c.coherent_upper_db||c.phase_agnostic_energy_db||c.predicted_complex_db),color,width:2.5}});
            }}
          }}
          const c=j.result.crossover.channels.left;
          if(c?.frequency)curves.push({{name:'선택 타깃 · 합산 기준',f:c.frequency,d:c.target_db,color:'var(--graph-text)',dash:'2 4',width:1.5}});
        }}else{{
          const g=j.result.graphs;
          for(const [name,key,color] of [['Left','left','var(--curve-l)'],['Right','right','var(--curve-r)']]){{
            if(g[key]?.frequency)curves.push({{name:name+' · 후(예상)',f:g[key].frequency,d:g[key].predicted_db,color,width:2.5}});
          }}
          const first=g.left||g.right;
          if(first?.target_db)curves.push({{name:'적용 타깃',f:first.frequency,d:first.target_db,color:'var(--graph-text)',dash:'2 4',width:1.4}});
        }}
        resultCurves=curves;
        paintResult();
      }}
    }}
  }}catch(_e){{
    paintLive('연결 재시도','retrying');
  }}finally{{
    clearTimeout(abortTimer);
    panel.setAttribute('aria-busy',String(busy));
    if(!reloading)timer=setTimeout(poll,busy?500:1500);
  }}
}};

document.addEventListener('visibilitychange',()=>{{if(!document.hidden)poll();}});
poll();
}})();</script>
    <script>(()=>{{const paint=async()=>{{try{{const h=await fetch('/api/health',{{cache:'no-store'}}).then(r=>r.json());const e=document.getElementById('system-health');if(e)e.textContent=`CPU ${{h.load[0].toFixed(2)}} · ${{h.temperature_c??'?'}}°C · 메모리 ${{h.memory_used_percent}}% · U7 ${{h.xonar_u7?'연결':'없음'}} · UMIK ${{h.umik1?'연결':'없음'}}`;}}catch(_e){{}}}};paint();setInterval(()=>{{if(!document.hidden)paint();}},5000);}})();</script>
    <script>(()=>{{/* non_destructive_measurement_tabs */const root=document.querySelector('.measurement');if(!root)return;const tabs=[...root.querySelectorAll('[data-measurement-tab]')],panels=[...root.querySelectorAll('.measurement-panel')];root.querySelectorAll('[data-measurement-step-content]').forEach(node=>{{const step=node.dataset.measurementStepContent,host=root.querySelector(`[data-measurement-panel-content="${{step}}"]`);if(host)host.append(node);}});panels.forEach(panel=>{{const step=panel.id.rsplit?panel.id.rsplit('-',1)[1]:panel.id.split('-').pop(),content=panel.querySelector('.measurement-panel-content'),empty=panel.querySelector('.measurement-panel-empty');if(empty)empty.hidden=!!content?.children.length;}});const activate=(step,updateHash=true,moveFocus=false)=>{{const wanted=String(step);tabs.forEach(tab=>{{const selected=tab.dataset.measurementTab===wanted;tab.classList.toggle('selected',selected);tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;if(selected&&moveFocus)tab.focus();}});panels.forEach(panel=>{{panel.hidden=panel.id!==`measurement-panel-${{wanted}}`;}});if(updateHash){{const url=new URL(location.href);url.hash=`measurement-step-${{wanted}}`;history.replaceState(null,'',url);}}}};tabs.forEach((tab,index)=>{{tab.addEventListener('click',()=>activate(tab.dataset.measurementTab));tab.addEventListener('keydown',event=>{{let next=null;if(event.key==='ArrowRight'||event.key==='ArrowDown')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft'||event.key==='ArrowUp')next=(index-1+tabs.length)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;if(next!==null){{event.preventDefault();activate(tabs[next].dataset.measurementTab,true,true);}}}});}});root.querySelectorAll('[data-measurement-jump]').forEach(button=>button.addEventListener('click',()=>{{activate(button.dataset.measurementJump);root.querySelector(`#measurement-panel-${{button.dataset.measurementJump}}`)?.focus();}}));const hashMatch=location.hash.match(/^#measurement-step-([1-6])$/),current=root.querySelector('[data-measurement-tab][aria-current="step"]');activate(hashMatch?.[1]||current?.dataset.measurementTab||'1',false);}})();</script>
    <script>(()=>{{/* prevent_accidental_double_submit */document.addEventListener('submit',e=>{{if(e.defaultPrevented)return;const f=e.target;if(f.dataset.submitting==='1'){{e.preventDefault();return;}}f.dataset.submitting='1';queueMicrotask(()=>{{if(e.defaultPrevented){{delete f.dataset.submitting;return;}}const b=e.submitter||f.querySelector('button[type=submit],button:not([type])');if(b){{b.disabled=true;b.dataset.originalText=b.textContent;b.textContent='처리 중…';}}}});}});}})();</script>
    </main></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "AudioDSP/1.2"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Polling browsers can navigate away while a response is in flight.
            # That is a normal client disconnect, not an AudioDSP server fault.
            self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_bytes(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(
            strict_json_bytes(payload, indent=2),
            status=status,
            content_type="application/json; charset=utf-8",
        )

    def send_fir(self, path: Path, rear_mode: str, allowed_root: Path = WEB_PROFILE_DIR) -> None:
        resolved = path.resolve(strict=True)
        allowed = allowed_root.resolve(strict=True)
        if os.path.commonpath((str(resolved), str(allowed))) != str(allowed):
            raise RuntimeError("FIR path is outside the allowed AudioDSP directory")
        size = resolved.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-AudioDSP-Rear-Mode", rear_mode)
        self.end_headers()
        with resolved.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def redirect(self, message: str, target: str = "/") -> None:
        if target not in ("/", "/measure", "/settings"):
            target = "/"
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", target + "?" + urlencode({"message": message}))
        self.end_headers()

    def read_urlencoded(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("Invalid form length")
        values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {key: items[-1] for key, items in values.items()}

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if length <= 0 or length > 4096 or content_type != "application/json":
            raise ValueError("Expected an application/json body up to 4096 bytes")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON body") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def read_multipart(self, expected_name: str = "wav") -> tuple[dict[str, str], str, bytes]:
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        if length <= 0 or length > MAX_REQUEST or not content_type.startswith("multipart/form-data"):
            raise ValueError("Invalid or oversized multipart upload")
        body = self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        fields: dict[str, str] = {}
        filename = ""
        wav = b""
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            part_filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if part_filename is not None and name == expected_name:
                filename = Path(part_filename).name
                wav = payload
            elif name:
                fields[name] = payload.decode("utf-8", errors="strict")
        if not filename or not wav:
            raise ValueError("Upload file was not provided")
        return fields, filename, wav

    def send_measurement_result(self, kind: str) -> None:
        job = measurement_status()
        result = job.get("result") or {}
        name = result.get(kind)
        if kind not in ("front", "rear") or not name:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        directory = Path(job["session_dir"]).resolve(strict=True)
        path = (directory / Path(name).name).resolve(strict=True)
        if os.path.commonpath((str(directory), str(path))) != str(directory):
            raise RuntimeError("Measurement result path escaped its session")
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_measurement_zip(self) -> None:
        job = measurement_status()
        result = job.get("result") or {}
        directory = Path(job["session_dir"]).resolve(strict=True)
        files: list[Path] = []
        names = []
        if result.get("kind") == "mimo_2x4":
            names.extend(item.get("file") for item in result.get("mimo_files", []) if isinstance(item, dict))
            names.extend(result.get(key) for key in ("mimo_manifest", "report_json", "report_md"))
        else:
            names.extend(result.get(kind) for kind in ("front", "rear"))
            names.extend(result.get(key) for key in ("report_json", "report_md"))
        names = [name for name in names if isinstance(name, str) and name]
        if len(names) < 2:
            self.send_error(HTTPStatus.NOT_FOUND, "ZIP requires at least two generated artifacts")
            return
        for name in dict.fromkeys(names):
            path = (directory / Path(name).name).resolve(strict=True)
            if os.path.commonpath((str(directory), str(path))) != str(directory):
                raise RuntimeError("Measurement result path escaped its session")
            files.append(path)
        manifest = {key: value for key, value in result.items() if key != "graphs"}
        manifest.update({"session_id": job.get("session_id"), "note": "Preview before permanent profile overwrite"})
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        body = memory.getvalue()
        safe_session = "".join(character for character in str(job.get("session_id", "result")) if character.isalnum() or character in "-_")
        filename = f"AudioDSP_{safe_session or 'result'}_32768_FIR.zip"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_measurement_report(self, kind: str) -> None:
        job = measurement_status()
        result = job.get("result") or {}
        key = "report_md" if kind == "report-md" else "report_json"
        name = result.get(key)
        if not isinstance(name, str) or not name:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        directory = Path(job["session_dir"]).resolve(strict=True)
        path = (directory / Path(name).name).resolve(strict=True)
        if path.parent != directory:
            raise RuntimeError("Measurement report path escaped its session")
        content_type = "text/markdown; charset=utf-8" if kind == "report-md" else "application/json; charset=utf-8"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if parsed.path == "/api/backup/download":
            try:
                body, filename, _manifest = backup_archive(cached_status(force=True))
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path == "/api/backup/latest":
            path = latest_system_backup()
            if path is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                resolved = path.resolve(strict=True)
                root = SYSTEM_BACKUP_DIR.resolve(strict=True)
                if resolved.parent != root:
                    raise ValueError("backup path mismatch")
                body = resolved.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{resolved.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if len(pieces) == 4 and pieces[0] == "api" and pieces[1] == "profile":
            profile, band = pieces[2], pieces[3]
            try:
                front, rear = baseline_paths(cached_status(), profile)
                path = front if band == "front" else rear
                if path is None or band not in ("front", "rear"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_fir(path, "profile")
            except (KeyError, ValueError, FileNotFoundError):
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if len(pieces) == 5 and pieces[:2] == ["api", "staging"] and pieces[3] == "candidate":
            profile, band = pieces[2], pieces[4]
            try:
                front, rear, _staged = staged_candidates(cached_status(), profile)
                path = front if band == "front" else rear
                if path is None or band not in ("front", "rear"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                allowed = STAGING_DIR if path.parent == STAGING_DIR else WEB_PROFILE_DIR
                self.send_fir(path, "candidate", allowed)
            except (KeyError, ValueError, FileNotFoundError):
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path == "/api/measurement/download/all":
            try:
                self.send_measurement_zip()
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path in ("/api/measurement/download/front", "/api/measurement/download/rear"):
            try:
                self.send_measurement_result(parsed.path.rsplit("/", 1)[-1])
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path in ("/api/measurement/download/report-md", "/api/measurement/download/report-json"):
            try:
                self.send_measurement_report(parsed.path.rsplit("/", 1)[-1])
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path in ("/api/fir/front", "/api/fir/rear"):
            try:
                current = cached_status()
                resolved = current["resolved"]
                if resolved["bypass"]:
                    self.send_error(HTTPStatus.CONFLICT, "The active profile is in DSP bypass mode")
                    return
                path = resolved["front_path"]
                if parsed.path.endswith("/rear") and resolved["rear_path"]:
                    path = resolved["rear_path"]
                allowed_root = MEASUREMENT_ROOT if resolved.get("preview_active") else WEB_PROFILE_DIR
                self.send_fir(Path(path), resolved["effective_rear_mode"], allowed_root)
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path == "/api/status":
            self.send_json(cached_status())
            return
        if parsed.path == "/api/volume":
            self.send_json(read_output_volume())
            return
        if parsed.path == "/api/measurement/status":
            self.send_json(measurement_status())
            return
        if parsed.path == "/api/targets":
            self.send_json(measurement("targets"))
            return
        if parsed.path == "/api/health":
            self.send_json(system_health())
            return
        views = {"/": "status", "/measure": "measure", "/settings": "settings"}
        if parsed.path not in views:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query)
        try:
            status = cached_status()
            body = render_page(status, message=query.get("message", [""])[-1], show_woofer=query.get("woofer", ["0"])[-1] == "1", view=views[parsed.path])
            self.send_bytes(body)
        except Exception as exc:
            self.send_bytes(f"AudioDSP UI error: {html.escape(str(exc))}".encode(), status=500)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/volume":
            self.send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json()
            volume_db = payload.get("db")
            if not isinstance(volume_db, int) or isinstance(volume_db, bool) or not VOLUME_MIN_DB <= volume_db <= VOLUME_MAX_DB:
                raise ValueError("db must be an integer from -60 to 0")
            result = manager("set-output-volume", str(volume_db))
            with STATUS_LOCK:
                STATUS_CACHE.update(signature=None, value=None)
            invalidate_volume_cache()
            volume = read_output_volume(result, force=True)
            applied = result.get("output_volume", {})
            volume["hardware_applied"] = bool(applied.get("hardware_applied"))
            if applied.get("warning"):
                volume["warning"] = applied["warning"]
            self.send_json(volume)
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/volume":
                fields = self.read_urlencoded()
                volume_db = int(fields["db"])
                result = manager("set-output-volume", str(volume_db))
                invalidate_volume_cache()
                applied = result.get("output_volume", {})
                suffix = f" · 경고: {applied['warning']}" if applied.get("warning") else ""
                self.redirect(f"출력 볼륨 저장·적용: {volume_db} dB{suffix}", "/")
                return
            if parsed.path == "/rear-mode":
                fields = self.read_urlencoded()
                result = manager("set-rear-mode", fields["profile"], fields["mode"])
                self.redirect(f"우퍼 모드 적용: {result['effective_rear_mode']} / {result['convolution_channels']}채널 컨볼루션", "/settings")
                return
            if parsed.path == "/bypass":
                fields = self.read_urlencoded()
                result = manager("set-bypass", fields["profile"], fields["enabled"])
                state = "ON" if fields["enabled"] == "on" else "OFF"
                self.redirect(f"{fields['profile']} DSP Bypass {state}; 실제 적용={result['effective_profile']}", "/settings")
                return
            if parsed.path == "/mimo-enabled":
                fields = self.read_urlencoded()
                result = manager("set-mimo-enabled", fields["profile"], fields["enabled"])
                state = "ON" if fields["enabled"] == "on" else "OFF"
                self.redirect(f"{fields['profile']} MIMO {state}; 처리 경로={result['convolution_channels']}", "/settings")
                return
            if parsed.path == "/chunksize":
                fields = self.read_urlencoded()
                result = manager("set-chunksize", fields["chunksize"])
                self.redirect(f"chunksize 적용: {result['chunksize']}", "/settings")
                return
            if parsed.path == "/woofer-trim":
                fields = self.read_urlencoded()
                result = manager("set-woofer-trim", fields["profile"], fields["trim_db"])
                self.redirect(f"{fields['profile']} 우퍼 트림 적용: {result['woofer_trim_db']} dB", "/settings")
                return
            if parsed.path == "/backup/stage":
                _fields, filename, payload = self.read_multipart("backup")
                result = stage_restore_archive(payload, filename)
                self.redirect(f"백업 검증 완료: schema {result['schema_version']} · FIR {len(result['firs'])}개 · 아직 적용하지 않았습니다.", "/settings")
                return
            if parsed.path == "/backup/apply":
                result = apply_restore_staging()
                path = result["browser_restore"]["automatic_backup"]
                self.redirect(f"전체 복원 완료 · 복원 직전 자동 백업: {path}", "/settings")
                return
            if parsed.path == "/backup/discard":
                discard_restore_staging()
                self.redirect("복원 검토를 취소했습니다. 현재 설정은 변경되지 않았습니다.", "/settings")
                return
            if parsed.path == "/measurement/new":
                fields = self.read_urlencoded()
                measurement(
                    "new", fields["mode"], fields["orientation"], fields["level_dbfs"], fields["sweep_seconds"],
                    fields.get("noise_level_dbfs", fields["level_dbfs"]),
                    fields.get("woofer_measurement_attenuation_db", "-9"),
                    fields.get("position_count", "3"),
                )
                self.redirect("측정 세션 생성 완료 · 먼저 레벨 확인을 진행하세요.", "/measure")
                return
            if parsed.path == "/measurement/session-note":
                fields = self.read_urlencoded()
                result = measurement("set-session-note", fields.get("note", ""))
                self.redirect(f"세션 {result['session_id']} 주석을 저장했습니다. 측정 진행 상태는 그대로 유지했습니다.", "/measure")
                return
            if parsed.path == "/measurement/load-session":
                fields = self.read_urlencoded()
                result = measurement("load-session", fields["session_id"])
                integrity = result.get("integrity", {})
                self.redirect(
                    f"세션 {result['session_id']} 불러오기 완료 · 위치 {integrity.get('positions_completed', 0)}/{integrity.get('positions_total', 3)} · "
                    f"FIR {'있음' if integrity.get('has_result') else '없음'} · 저장된 완료 단계에서 이어갑니다.",
                    "/measure",
                )
                return
            if parsed.path == "/measurement/delete-session":
                fields = self.read_urlencoded()
                result = measurement("delete-session", fields["session_id"])
                size_mib = float(result.get("bytes_deleted", 0)) / (1024.0 * 1024.0)
                self.redirect(
                    f"세션 {result['session_id']} 삭제 완료 · {result.get('files_deleted', 0)}개 파일 / {size_mib:.1f} MiB · 정식 프로필 FIR은 유지했습니다.",
                    "/measure",
                )
                return
            if parsed.path == "/measurement/configure":
                fields = self.read_urlencoded()
                result = measurement(
                    "configure", fields["mode"], fields["orientation"], fields["level_dbfs"], fields["sweep_seconds"],
                    fields.get("noise_level_dbfs", fields["level_dbfs"]),
                    fields.get("woofer_measurement_attenuation_db", "-9"),
                    fields.get("position_count", "3"),
                )
                reason = (result.get("invalidation") or {}).get("reason", "변경 없음")
                self.redirect(f"측정 설정 적용: {reason}. 영향을 받지 않는 값은 유지했습니다.", "/measure")
                return
            if parsed.path == "/measurement/configure-level":
                fields = self.read_urlencoded()
                measurement(
                    "configure", fields["mode"], fields["orientation"], fields["level_dbfs"], fields["sweep_seconds"],
                    fields.get("noise_level_dbfs", fields["level_dbfs"]),
                    fields.get("woofer_measurement_attenuation_db", "-9"),
                    fields.get("position_count", "3"),
                )
                measurement("start-level")
                self.redirect("출력 설정을 적용하고 레벨 검사를 시작했습니다. 실행 확정 시 이후 위치 측정과 FIR 결과만 초기화했습니다.", "/measure")
                return
            if parsed.path == "/measurement/level":
                measurement("start-level")
                self.redirect("레벨 검사를 시작했습니다. 실행 확정 시 기존 위치 측정과 FIR 결과를 초기화했습니다.", "/measure")
                return
            if parsed.path == "/measurement/reprocess-level":
                measurement("start-level-reprocess")
                self.redirect("빠른 검사 저장 원본의 SNR을 다시 계산합니다. 소리는 재생하지 않습니다.", "/measure")
                return
            if parsed.path == "/measurement/position":
                measurement("start-position")
                self.redirect("연속 측정을 시작했습니다. 측정 중 DSP 바이패스·U7 입력 꺼짐이며, 모든 소리가 끝난 뒤 응답을 일괄 계산합니다.", "/measure")
                return
            if parsed.path == "/measurement/restart-positions":
                measurement("restart-positions")
                self.redirect("기존 위치 측정 이후 결과를 초기화하고 위치 1부터 재측정을 시작했습니다.", "/measure")
                return
            if parsed.path == "/measurement/reprocess-saved":
                measurement("start-reprocess-saved")
                self.redirect("저장 원본 재계산을 시작했습니다. 소리는 재생하지 않습니다.", "/measure")
                return
            if parsed.path == "/measurement/validation":
                measurement("start-validation")
                self.redirect("FIR 전 기준점 물리 합산 진단을 시작했습니다. 이 진단은 사후 FIR 타깃 검증을 대신하지 않습니다.", "/measure")
                return
            if parsed.path == "/measurement/post-validation":
                fields = self.read_urlencoded()
                measurement("start-post-validation", fields["level_dbfs"])
                self.redirect("현재 미리듣기 FIR을 통과하는 L+우퍼/R+우퍼 사후 합산 측정을 시작했습니다. 원측정과 FIR은 유지됩니다.", "/measure")
                return
            if parsed.path == "/measurement/reset-post-validation":
                measurement("reset-post-validation")
                self.redirect("사후 합산 검증 진행값만 초기화했습니다. 원측정과 생성 FIR은 유지됩니다.", "/measure")
                return
            if parsed.path == "/measurement/build":
                fields = self.read_urlencoded()
                measurement("start-build", fields["target"], fields["preset"], fields["woofer_trim_db"], fields["phase_mode"], fields["phase_cutoff"], fields["spatial_mode"], fields["bass_tilt_db"], fields["treble_tilt_db"], fields["correction_low_hz"], fields["correction_high_hz"], fields["max_boost_db"], fields["max_cut_db"], fields["mimo_high_hz"], fields["mimo_strength"], fields["mimo_support_penalty_db"], fields["crossover_enabled"], fields["crossover_frequency_hz"])
                self.redirect("32768탭 FIR 계산을 시작했습니다.", "/measure")
                return
            if parsed.path == "/measurement/apply":
                fields = self.read_urlencoded()
                measurement("apply", fields["profile"])
                self.redirect(f"생성 FIR을 {fields['profile']} 프로필에 백업 후 정식 덮어쓰기했습니다.", "/measure")
                return
            if parsed.path == "/measurement/preview":
                fields = self.read_urlencoded()
                measurement("preview", fields["profile"])
                self.redirect(f"이번 튜닝을 {fields['profile']} 출력에 임시 테스트 적용했습니다. 프로필 WAV는 변경되지 않았습니다.", "/measure")
                return
            if parsed.path == "/measurement/restore":
                measurement("restore")
                self.redirect("기존 정식 튜닝으로 복귀했습니다.", "/measure")
                return
            if parsed.path == "/measurement/cancel":
                measurement("cancel")
                self.redirect("측정 취소를 요청했습니다.", "/measure")
                return
            if parsed.path == "/measurement/calibration":
                fields, filename, payload = self.read_multipart("file")
                descriptor, temporary_name = tempfile.mkstemp(prefix="audiodsp-cal-", suffix=".txt", dir="/tmp")
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(payload)
                    metadata = measurement("install-cal", fields["orientation"], str(temporary))
                finally:
                    temporary.unlink(missing_ok=True)
                self.redirect(f"UMIK 보정 파일 적용: {metadata['orientation']}° / 일련번호 {metadata['serial']}", "/measure")
                return
            if parsed.path in ("/upload", "/upload-stage"):
                fields, filename, wav = self.read_multipart()
                descriptor, temporary_name = tempfile.mkstemp(prefix="audiodsp-upload-", suffix=".wav", dir="/tmp")
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(wav)
                    preview = manager("status").get("preview", {})
                    if preview.get("active"):
                        manager("restore-profile")
                    result = stage_upload(fields["profile"], fields["band"], temporary, filename)
                finally:
                    temporary.unlink(missing_ok=True)
                staged_band = result["bands"][fields["band"]]
                self.redirect(f"검토용 업로드 완료: {fields['profile']} {fields['band']} / {staged_band['metadata']['frames']} taps · 아직 정식 적용되지 않았습니다.", "/settings")
                return
            if parsed.path == "/staging/preview":
                fields = self.read_urlencoded()
                current = manager("status")
                front, rear, _staged = staged_candidates(current, fields["profile"])
                arguments = ["preview-pair", fields["profile"], str(front)]
                if rear is not None:
                    arguments.append(str(rear))
                manager(*arguments)
                self.redirect(f"{fields['profile']} 업로드값을 임시 테스트 중입니다. 정식 WAV는 변경되지 않았습니다.", "/settings")
                return
            if parsed.path == "/staging/restore":
                manager("restore-profile")
                self.redirect("기존 정식 FIR로 복귀했습니다.", "/settings")
                return
            if parsed.path == "/staging/apply":
                fields = self.read_urlencoded()
                current = manager("status")
                front, rear, _staged = staged_candidates(current, fields["profile"])
                trim = int(current["settings"].get("woofer_trim_db", {}).get(fields["profile"], 0))
                arguments = ["install-pair", fields["profile"], str(front)]
                if rear is not None:
                    arguments.append(str(rear))
                arguments.extend(["--woofer-trim", str(trim)])
                manager(*arguments)
                discard_staging(fields["profile"])
                self.redirect(f"{fields['profile']} FIR을 백업 후 정식 적용했습니다.", "/settings")
                return
            if parsed.path == "/staging/discard":
                fields = self.read_urlencoded()
                preview = manager("status").get("preview", {})
                if preview.get("active") and preview.get("profile") == fields["profile"]:
                    manager("restore-profile")
                discard_staging(fields["profile"])
                self.redirect(f"{fields['profile']} 검토용 업로드를 취소했습니다. 정식 WAV는 그대로입니다.", "/settings")
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            try:
                status = manager("status")
                error_view = "measure" if parsed.path.startswith("/measurement/") else ("settings" if parsed.path in ("/rear-mode", "/bypass", "/mimo-enabled", "/chunksize", "/woofer-trim", "/upload", "/upload-stage") or parsed.path.startswith(("/staging/", "/backup/")) else "status")
                self.send_bytes(render_page(status, error=str(exc), view=error_view), status=400)
            except Exception:
                self.send_bytes(f"AudioDSP UI error: {html.escape(str(exc))}".encode(), status=500)

    def log_message(self, template: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {template % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    server.daemon_threads = True
    print(f"AudioDSP profile UI listening on {WEB_HOST}:{WEB_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
