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
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
from urllib.parse import parse_qs, urlencode, urlparse


def environment(suffix: str, default: str) -> str:
    """Prefer new AudioDSP names while accepting legacy GSonic overrides."""
    return os.environ.get(f"AUDIODSP_{suffix}", os.environ.get(f"GSONIC_{suffix}", default))


MANAGER = environment("PROFILE_MANAGER", "/usr/local/bin/audiodsp-profile-manager.py")
MEASUREMENT = environment("MEASUREMENT", "/usr/local/bin/audiodsp-measurement.py")
WEB_HOST = environment("WEB_HOST", "0.0.0.0")
WEB_PORT = int(environment("WEB_PORT", "8080"))
WEB_PROFILE_DIR = Path(environment("CONFIG_DIR", "/etc/camilladsp")) / "profiles"
STATE_DIR = Path(environment("STATE_DIR", "/var/lib/audiodsp"))
STAGING_DIR = Path(environment("STAGING_DIR", "/var/lib/audiodsp/upload-staging"))
MEASUREMENT_STATUS_PATH = Path(environment("MEASUREMENT_DIR", "/var/lib/audiodsp/measurements")) / "current.json"
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
DEFAULT_CORRECTION_PREFERENCES = {
    "target": "harman", "preset": "strong", "woofer_trim_db": -9,
    "phase_mode": "bass", "phase_cutoff": 200, "spatial_mode": "equal",
    "bass_tilt_db": 0, "treble_tilt_db": 0, "correction_low_hz": 20,
    "correction_high_hz": 20_000, "max_boost_db": 6, "max_cut_db": 18,
    "mimo_high_hz": 150, "mimo_strength": "balanced", "mimo_support_penalty_db": 6,
}
MAX_REQUEST = 33 * 1024 * 1024
GRAPH_CACHE: dict[tuple[str, str, bool], str] = {}
STAGING_LOCK = threading.Lock()
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


def measurement_status() -> dict:
    """Read the atomically-written job JSON directly; spawning the FFT engine each second is costly on Pi 2."""
    global MEASUREMENT_DEFAULT
    try:
        value = json.loads(MEASUREMENT_STATUS_PATH.read_text(encoding="utf-8"))
        if "correction_preferences" not in value:
            try:
                value["correction_preferences"] = json.loads(CORRECTION_PREFERENCES_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value["correction_preferences"] = dict(DEFAULT_CORRECTION_PREFERENCES)
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
        raise RuntimeError("Front FIR이 없습니다. 먼저 Front WAV를 올려주세요.")
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
    stamp = time.strftime("%Y%m%d-%H%M%S")
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


def stage_restore_archive(payload: bytes, original_name: str) -> dict:
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

    token = f"{time.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha256(payload).hexdigest()[:10]}"
    directory = RESTORE_STAGING_ROOT / token
    directory.mkdir(parents=True, exist_ok=False)
    for name in data_names:
        atomic_bytes(directory / name, contents[name])
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
    report = {
        "active": True,
        "directory": str(directory),
        "original_name": Path(original_name).name,
        "schema_version": version,
        "app_version": manifest.get("app_version", "unknown"),
        "settings": settings_report["normalized"],
        "correction_preferences": preference_report,
        "unknown_settings": settings_report.get("ignored_unknown_keys", []),
        "firs": fir_report,
        "mimo": mimo_report,
        "calibrations": calibration_report,
        "staged_unix": time.time(),
    }
    atomic_bytes(RESTORE_STATE_PATH, (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    return report


def discard_restore_staging() -> None:
    RESTORE_STATE_PATH.unlink(missing_ok=True)


def apply_restore_staging() -> dict:
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
        if "90" in staged.get("calibrations", {}):
            measurement("calibration-changed", "90")
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
    discard_restore_staging()
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
        "profile_monitor": service_active_any("audiodsp-profile-monitor.service", "gsonic-u7-profile.service"),
        "umik1": "UMIK-1" in cards,
        "xonar_u7": "Xonar U7" in cards,
    }


def service_active(name: str) -> bool:
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", name], check=False
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
                raise ValueError("Rear FIR rate differs from Front FIR")
            rear_l_mag = magnitudes(fft(rear_left))
            rear_r_mag = magnitudes(fft(rear_right))
        curves_mag["Woofer"] = [
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
    colors = {"L": "#38bdf8", "R": "#fb7185", "Woofer": "#fbbf24"}
    for name, values in curves.items():
        points = " ".join(f"{x_of(f):.2f},{y_of(v):.2f}" for f, v in zip(frequencies, values))
        dash = ' stroke-dasharray="8 6"' if name == "Woofer" and copied else ""
        parts.append(f'<polyline points="{points}" fill="none" stroke="{colors[name]}" stroke-width="2.2" vector-effect="non-scaling-stroke"{dash}/>')
    legend_x = left + 16
    for index, name in enumerate(curves):
        x = legend_x + index * 130
        dash = ' stroke-dasharray="8 6"' if name == "Woofer" and copied else ""
        label = "Woofer (copy)" if name == "Woofer" and copied else name
        parts.append(f'<line x1="{x}" y1="{top + 18}" x2="{x + 28}" y2="{top + 18}" stroke="{colors[name]}" stroke-width="3"{dash}/>')
        parts.append(f'<text x="{x + 36}" y="{top + 22}" fill="#e2e8f0" font-size="13">{label}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    GRAPH_CACHE[cache_key] = svg
    return svg


def client_svg_graph(show_woofer: bool, rear_mode: str, bypass: bool) -> str:
    markup = r'''<div class="graph-scroll"><svg id="fir-response" class="response" viewBox="0 0 980 430" role="img" aria-label="FIR frequency response"></svg></div>
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
        if(SHOW_WOOFER)curve("Woofer (copy)",flat,"var(--curve-w)",true,2);
        status.textContent="DSP Bypass · FIR 연산 없음 · 원본 L/R을 Front/Rear로 복사";
        return;
      }
      const requests=[fetch("/api/fir/front").then(r=>{if(!r.ok)throw Error("Front FIR 읽기 실패");return r.arrayBuffer();})];
      if(SHOW_WOOFER&&REAR_MODE==="separate")requests.push(fetch("/api/fir/rear").then(r=>{if(!r.ok)throw Error("Rear FIR 읽기 실패");return r.arrayBuffer();}));
      Promise.all(requests).then(buffers=>{const front=wave(buffers[0]),lm=magnitude(front.left,front.rate),rm=magnitude(front.right,front.rate);curve("L",db(lm),"var(--curve-l)",false,0);curve("R",db(rm),"var(--curve-r)",false,1);if(SHOW_WOOFER){let wl=lm,wr=rm;if(REAR_MODE==="separate"){const rear=wave(buffers[1]);wl=magnitude(rear.left,rear.rate);wr=magnitude(rear.right,rear.rate);}const woofer=wl.map((v,i)=>Math.sqrt((v*v+wr[i]*wr[i])/2));curve(REAR_MODE==="separate"?"Woofer":"Woofer (copy)",db(woofer),"var(--curve-w)",REAR_MODE!=="separate",2);}status.textContent=`SVG 벡터 그래프 · ${front.left.length.toLocaleString()} taps · 계산은 이 브라우저에서 수행됨`;}).catch(e=>{status.textContent="그래프 오류: "+e.message;status.classList.add("bad");});
    })();</script>'''
    return (markup
            .replace("__SHOW_WOOFER__", "true" if show_woofer else "false")
            .replace("__REAR_MODE__", rear_mode)
            .replace("__BYPASS__", "true" if bypass else "false"))


def staged_compare_graph(profile: str, candidate_rear: bool) -> str:
    """Client-side vector response comparison; keeps FFT work off the Pi 2."""
    safe = html.escape(profile)
    markup = r'''<div class="staged-compare"><h3>기존 / 업로드 FIR 응답 비교</h3><div class="graph-scroll"><svg id="stage-graph-__PROFILE__" class="response" viewBox="0 0 980 430" role="img" aria-label="Existing and staged FIR response comparison"></svg></div><p id="stage-graph-status-__PROFILE__" class="muted">브라우저에서 기존/업로드 FIR 응답을 계산하는 중…</p></div>
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
      Promise.all([get(`/api/profile/${PROFILE}/front`),get(`/api/staging/${PROFILE}/candidate/front`),...(HAS_REAR?[get(`/api/profile/${PROFILE}/rear`).catch(()=>null),get(`/api/staging/${PROFILE}/candidate/rear`)]:[])]).then(b=>{const old=wave(b[0]),next=wave(b[1]),ol=mag(old.l,old.rate),or=mag(old.r,old.rate),nl=mag(next.l,next.rate),nr=mag(next.r,next.rate);curve("기존 L",db(ol),"var(--curve-l)",true);curve("업로드 L",db(nl),"var(--curve-l)",false);curve("기존 R",db(or),"var(--curve-r)",true);curve("업로드 R",db(nr),"var(--curve-r)",false);if(HAS_REAR){const oldRear=b[2]?wave(b[2]):old,newRear=wave(b[3]),owl=mag(oldRear.l,oldRear.rate),owr=mag(oldRear.r,oldRear.rate),nwl=mag(newRear.l,newRear.rate),nwr=mag(newRear.r,newRear.rate),ow=owl.map((v,i)=>Math.hypot(v,owr[i])/Math.SQRT2),nw=nwl.map((v,i)=>Math.hypot(v,nwr[i])/Math.SQRT2);curve("기존 Woofer",db(ow),"var(--curve-w)",true);curve("업로드 Woofer",db(nw),"var(--curve-w)",false);}status.textContent=`SVG 벡터 그래프 · ${next.frames.toLocaleString()} taps · FFT는 이 브라우저에서 계산`;}).catch(e=>{status.textContent="그래프 오류: "+e.message;status.classList.add("bad");});
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


def measurement_panel(job: dict, preview: dict) -> str:
    state = str(job.get("state", "idle"))
    busy = state in ("running", "processing", "cancelling")
    positions = int(job.get("positions_completed", 0))
    total = int(job.get("positions_total", 3))
    progress = max(0.0, min(100.0, float(job.get("progress", 0.0))))
    eta = job.get("eta_seconds")
    eta_text = f" · 예상 {int(eta)}초" if isinstance(eta, (int, float)) else ""
    calibration = job.get("calibration") or {}
    result = job.get("result") or {}
    level = job.get("level_check") or {}
    preferences = job.get("correction_preferences") or {}
    installed = job.get("installed_calibrations") or {}
    capabilities = job.get("capabilities") or {}
    mimo_supported = bool(capabilities.get("mimo_supported"))
    cal90 = installed.get("90") or {}
    cal0 = installed.get("0") or {}
    if job.get("applied_profile"):
        current_step = 6
    elif result:
        current_step = 5
    elif positions == total:
        current_step = 4
    elif state != "idle" and level.get("ok"):
        current_step = 3
    elif state != "idle":
        current_step = 2
    else:
        current_step = 1
    workflow_items = []
    for number, label in ((1, "연결·Cal"), (2, "레벨"), (3, "3위치 측정"), (4, "FIR 계산"), (5, "검토·A/B"), (6, "정식 적용")):
        classes = "current" if number == current_step else "done" if number < current_step else "future"
        content = f'<span>{number}</span><b>{label}</b>'
        if number <= current_step:
            workflow_items.append(f'<a class="flow-step {classes}" href="#measurement-step-{number}" title="측정값은 유지하고 이 단계로 이동">{content}</a>')
        else:
            workflow_items.append(f'<span class="flow-step {classes}" aria-disabled="true">{content}</span>')
    workflow = "".join(workflow_items)
    cal90_summary = (
        f"serial {html.escape(str(cal90.get('serial')))} · {cal90.get('points')} points · Sens {cal90.get('sensitivity_db')} dB"
        if cal90.get("available") else "90° calibration 파일 없음"
    )
    cal0_summary = (
        f"serial {html.escape(str(cal0.get('serial')))} · {cal0.get('points')} points · Sens {cal0.get('sensitivity_db')} dB"
        if cal0.get("available") else "0° calibration 파일 없음"
    )
    controls = ""
    if state == "idle":
        mode_options = ''.join(
            f'<option value="{value}" {"disabled" if value.startswith("mimo_") and not mimo_supported else ""}>{label}</option>'
            for value, label in (
                ("lrw", "L / R / Woofer · SISO · 3위치"), ("lr", "L / R · SISO · 3위치"),
                ("mimo_stereo", "MIMO Stereo · L/R 2제어원 · Pi4/5"),
                ("mimo_one_sub", "MIMO 2.1 · L/R+T5S 3제어원 · Pi4/5"),
                ("mimo_dual_sub", "MIMO 2.2 · L/R+독립 우퍼2대 · Pi4/5"),
            )
        )
        controls = f"""
        <form method="post" action="/measurement/new" class="measure-form">
          <label>측정 구성<select name="mode">{mode_options}</select></label>
          <label>UMIK 방향<select name="orientation"><option value="90" selected>90° · 천장 방향 · 권장</option></select></label>
          <label>측정 출력<select name="level_dbfs"><option value="-48">-48 dBFS · 야간 매우 작게</option><option value="-42" selected>-42 dBFS · 야간 기본</option><option value="-36">-36 dBFS · 작게</option><option value="-30">-30 dBFS · 일반</option><option value="-24">-24 dBFS · 크게</option></select></label>
          <label>Sweep 길이<select name="sweep_seconds"><option value="4">4초 · 시험</option><option value="8" selected>8초 · 권장</option><option value="12">12초 · 정밀</option><option value="14">14초 · 저레벨 정밀</option></select></label>
          <button>새 측정 Session</button>
        </form>"""
    else:
        disabled = " disabled" if busy else ""
        level_ok = bool(level.get("ok"))
        position_disabled = " disabled" if busy or not level_ok else ""
        mode = str(job.get("mode", "lrw"))
        mode_options = ''.join(
            f'<option value="{value}" {"selected" if mode == value else ""} {"disabled" if value.startswith("mimo_") and not mimo_supported else ""}>{label}</option>'
            for value, label in (
                ("lrw", "L / R / Woofer · SISO · 3위치"), ("lr", "L / R · SISO · 3위치"),
                ("mimo_stereo", "MIMO Stereo · L/R 2제어원 · Pi4/5"),
                ("mimo_one_sub", "MIMO 2.1 · L/R+T5S 3제어원 · Pi4/5"),
                ("mimo_dual_sub", "MIMO 2.2 · L/R+독립 우퍼2대 · Pi4/5"),
            )
        )
        level_dbfs = int(job.get("level_dbfs", -42))
        sweep_seconds = int(job.get("sweep_seconds", 8))
        session_settings = f"""
        <form method="post" action="/measurement/configure" class="measure-form session-settings" onsubmit="return confirm('변경 적용 시 영향을 받는 단계만 초기화합니다. 단순 단계 이동은 측정값을 지우지 않습니다. 적용할까요?')">
          <label>측정 구성<select name="mode">{mode_options}</select></label>
          <label>UMIK 방향<select name="orientation"><option value="90" selected>90° · 천장 방향 · 권장</option></select></label>
          <label>측정 출력<select name="level_dbfs">{''.join(f'<option value="{value}" {"selected" if value == level_dbfs else ""}>{value} dBFS</option>' for value in (-48, -42, -36, -30, -24))}</select></label>
          <label>Sweep 길이<select name="sweep_seconds">{''.join(f'<option value="{value}" {"selected" if value == sweep_seconds else ""}>{value}초</option>' for value in (4, 8, 12, 14))}</select></label>
          <button>변경 적용</button>
          <p class="form-note">구성·sweep 변경은 3위치 측정 이후만, 출력 레벨 변경은 레벨 검사 이후를 초기화합니다. 값이 같으면 모두 유지됩니다.</p>
        </form>
        <details class="session-tools"><summary>Session 관리</summary><p class="muted">현재 session 폴더는 보존한 채 새 session을 만들어 처음부터 시작할 수 있습니다.</p><form method="post" action="/measurement/new" onsubmit="return confirm('현재 session 기록은 디스크에 보존하고 새 측정을 시작합니다. 현재 진행 화면을 새 session으로 바꿀까요?')"><input type="hidden" name="mode" value="{mode}"><input type="hidden" name="orientation" value="90"><input type="hidden" name="level_dbfs" value="{level_dbfs}"><input type="hidden" name="sweep_seconds" value="{sweep_seconds}"><button class="secondary">새 Session으로 처음부터</button></form></details>"""
        if positions >= total:
            position_control = f'<form method="post" action="/measurement/restart-positions" id="measurement-step-3" onsubmit="return confirm(\'3위치 측정을 처음부터 다시 시작합니다. 기존 측정·검증·생성 FIR 결과를 초기화할까요?\')"><button{position_disabled}>3위치 처음부터 재측정</button></form>'
        else:
            position_control = f'<form method="post" action="/measurement/position" id="measurement-step-3"><button{position_disabled}>위치 {positions + 1}/{total} 개별 측정 시작</button></form>'
            if positions > 0:
                position_control += f'<form method="post" action="/measurement/restart-positions" onsubmit="return confirm(\'완료한 위치 측정을 버리고 위치 1부터 다시 시작할까요?\')"><button{disabled} class="secondary">3위치 처음부터 다시</button></form>'
        controls = f"""
        {session_settings}
        <div class="measure-actions">
          <form method="post" action="/measurement/level" id="measurement-step-2" onsubmit="return confirm('레벨 검사를 다시 실행하면 기기 볼륨이 달라졌을 수 있으므로 기존 3위치 측정과 FIR 결과를 초기화합니다. 계속할까요?')"><button{disabled}>5초 무음 + 5초 백색소음 레벨 검사</button></form>
          {position_control}
          {f'<form method="post" action="/measurement/validation"><button{disabled}>중앙에서 L+Woofer / R+Woofer 합산 검증</button></form>' if job.get('mode') == 'lrw' and positions == total and not job.get('validation') else ''}
          {f'<form method="post" action="/measurement/cancel"><button class="danger">작업 취소</button></form>' if busy else ''}
        </div>{'<p class="muted">위치 측정은 레벨 검사가 OK일 때 활성화됩니다. NOT OK면 기기 볼륨을 수동 조절한 뒤 같은 버튼으로 다시 검사하세요.</p>' if not level_ok else ''}"""
        if positions == total and not busy:
            target_labels = (("harman", "Harman Kardon"), ("rtings", "RTINGS"), ("acoustix", "AcoustiX Default"), ("toole", "Not Dr. Toole"), ("bk", "Brüel & Kjær"), ("flat", "Flat"))
            target_options = ''.join(f'<option value="{value}" {"selected" if preferences.get("target", "harman") == value else ""}>{label}</option>' for value, label in target_labels)
            preset_options = ''.join(f'<option value="{value}" {"selected" if preferences.get("preset", "strong") == value else ""}>{label}</option>' for value, label in (("strong", "T5S 강한 억제 · 현재 선호"), ("primus360", "Primus 360 수준"), ("none", "추가 억제 없음")))
            phase_options = ''.join(f'<option value="{value}" {"selected" if preferences.get("phase_mode", "bass") == value else ""}>{label}</option>' for value, label in (("bass", "저역 음량 + excess phase"), ("magnitude", "음량만 · 최소위상")))
            controls += """
            <form method="post" action="/measurement/build" id="measurement-step-4" class="measure-form build-options" onsubmit="return confirm('측정 원본은 유지하고 기존 생성 FIR/A-B 임시 결과만 초기화한 뒤 다시 계산합니다. 계속할까요?')">
              <label>기준 음색 Target<select name="target" id="target-choice">""" + target_options + """</select></label>
              <label>우퍼 과잉 억제<select name="preset">""" + preset_options + """</select></label>
              <label>Woofer 최종 trim<select name="woofer_trim_db">""" + "".join(f'<option value="{value}" {"selected" if value == preferences.get("woofer_trim_db", -9) else ""}>{value} dB</option>' for value in range(0, -19, -1)) + """</select></label>
              <label>Phase 방식<select name="phase_mode">""" + phase_options + """</select></label>
              <button>설정으로 32768탭 FIR 생성</button>
              <details class="advanced"><summary>고급 보정 설정 · 기본값은 안전 권장값</summary><div class="advanced-grid">
                <label>공간 대표 응답<select name="spatial_mode">""" + ''.join(f'<option value="{value}" {"selected" if preferences.get("spatial_mode", "equal") == value else ""}>{label}</option>' for value, label in (("equal", "세 위치 균등 · 넓은 청취영역"), ("center", "중앙 우선 · 고역 중심 가중"))) + """</select></label>
                <label>추가 저음 취향<select name="bass_tilt_db">""" + "".join(f'<option value="{value}" {"selected" if value == preferences.get("bass_tilt_db", 0) else ""}>{value:+d} dB @ 20 Hz</option>' for value in range(-6, 7)) + """</select></label>
                <label>추가 고음 경사<select name="treble_tilt_db">""" + "".join(f'<option value="{value}" {"selected" if value == preferences.get("treble_tilt_db", 0) else ""}>{value:+d} dB @ 20 kHz</option>' for value in range(-6, 3)) + """</select></label>
                <label>룸보정 하한<select name="correction_low_hz">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("correction_low_hz", 20) else ""}>{value} Hz</option>' for value in (20, 30, 40, 60, 80)) + """</select></label>
                <label>룸보정 상한<select name="correction_high_hz">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("correction_high_hz", 20000) else ""}>{value // 1000 if value >= 1000 else value}{" kHz" if value >= 1000 else " Hz"}</option>' for value in (300, 500, 1000, 5000, 20000)) + """</select></label>
                <label>최대 room boost<select name="max_boost_db">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("max_boost_db", 6) else ""}>+{value} dB</option>' for value in (0, 3, 6, 9)) + """</select></label>
                <label>최대 room cut<select name="max_cut_db">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("max_cut_db", 18) else ""}>−{value} dB</option>' for value in (6, 9, 12, 18, 24)) + """</select></label>
                <label>저역 phase 상한<select name="phase_cutoff">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("phase_cutoff", 200) else ""}>{value} Hz</option>' for value in (80, 120, 160, 200, 250)) + """</select></label>
                <label>MIMO 공동제어 상한<select name="mimo_high_hz">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("mimo_high_hz", 150) else ""}>{value} Hz</option>' for value in (80, 120, 150)) + """</select></label>
                <label>MIMO 강도<select name="mimo_strength">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("mimo_strength", "balanced") else ""}>{label}</option>' for value, label in (("safe", "Safe · 높은 안정성"), ("balanced", "Balanced · 권장"), ("maximum", "Maximum · 측정영역 우선"))) + """</select></label>
                <label>지원 제어원 제한<select name="mimo_support_penalty_db">""" + ''.join(f'<option value="{value}" {"selected" if value == preferences.get("mimo_support_penalty_db", 6) else ""}>{value} dB</option>' for value in (3, 6, 9, 12)) + """</select></label>
              </div><p class="muted">자연 roll-off 밖과 위치별 편차가 큰 null은 최대 boost보다 우선하여 보호됩니다. MIMO 항목은 MIMO 측정 구성에만 쓰이며 Pi4/5에서 chunksize 1024 이상으로 동작합니다.</p></details>
            </form>
            <div class="target-preview"><b>선택 Target 곡선 · 1kHz 기준</b><svg id="target-graph" viewBox="0 0 760 230" role="img" aria-label="Target frequency response"></svg></div>"""
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
            if item:
                fit_items.append(f'<span class="pill {"" if item.get("pass") else "warn"}">{channel.title()} MAE {item.get("mae_db", "?")} dB · P90 {item.get("p90_abs_error_db", "?")} dB</span>')
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
                decay_cards.append(f'<div><small>{channel.title()} T20→RT60</small>{rows}</div>')
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
        result_html = f"""
        <div class="result-box" id="measurement-step-5"><h3>적용 전 검토 · 생성 결과</h3>
          <p><b>{html.escape(str(result.get('target')))}</b> · {html.escape(str(result.get('preset')))} · {result.get('taps')} taps · Front peak tap {left.get('peak_tap', '?')} ({left.get('peak_delay_ms', '?')} ms)</p>
          <p><code>{html.escape(str(result.get('front_sha256', '')))}</code></p>
          <div class="diagnostic-grid"><div><small>공간 평균</small><b>{html.escape(str(result.get('spatial_mode', 'equal')))}</b></div><div><small>룸보정 범위</small><b>{limits.get('low_hz', '?')}–{limits.get('high_hz', '?')} Hz</b></div><div><small>추가 취향</small><b>Bass {preference.get('bass_db_at_20_hz', 0):+} / Treble {preference.get('treble_db_at_20_khz', 0):+} dB</b></div><div><small>L/R 중앙값 차이</small><b>{diagnostics.get('lr_median_difference_db', '?')} dB</b></div><div><small>공간편차 중앙값</small><b>{diagnostics.get('spatial_std_median_db', '?')} dB</b></div><div><small>측정 SNR 최소/중앙</small><b>{diagnostics.get('measurement_snr_min_db', '?')} / {diagnostics.get('measurement_snr_median_db', '?')} dB</b></div><div><small>FIR 셀프검증</small><b>{'PASS' if self_validation.get('overall_pass') else '확인 필요'}</b></div><div><small>우퍼 측정 보호</small><b>{job.get('woofer_measurement_attenuation_db', -12)} dB</b></div></div>
          <div class="measure-actions target-fit">{fit_html}</div>
          {f'<details class="decay-report"><summary>잔향/공진 T20→RT60 보기</summary><div class="decay-grid">{decay_html}</div><p class="muted">late reverb는 불안정한 역보정을 하지 않습니다. 신뢰 가능한 300 Hz 이하 장시간 공진만 최대 3 dB 추가 감쇄합니다.</p></details>' if decay_html else ''}
          <div class="diagnostic-note"><b>자동 진단</b><ul>{warning_html}</ul></div>
          {audit_html}
          <p class="muted">WAV 다운로드와 그래프 확인은 현재 재생 설정을 바꾸지 않습니다. 점선은 튜닝 전 측정, 실선은 32768탭 FIR 적용 후 예상 응답입니다.</p>
          <div class="measure-actions"><a class="button" download href="/api/measurement/download/front">Front WAV 받기</a>
          {('<a class="button" download href="/api/measurement/download/rear">Rear WAV 받기</a>' if result.get('rear') else '')}
          {('<a class="button" download href="/api/measurement/download/all">WAV + 보고서 ZIP 받기</a>' if result.get('rear') else '')}
          {('<a class="button secondary" download href="/api/measurement/download/report-md">한계 포함 보고서 MD</a>' if result.get('report_md') else '')}
          {('<a class="button secondary" download href="/api/measurement/download/report-json">전체 결과 JSON</a>' if result.get('report_json') else '')}</div>
          <div class="result-box"><b>A/B 청취 비교</b><p class="muted">현재 상태: <span class="pill">{preview_label}</span> · 테스트 적용은 프로필 WAV와 설정을 덮어쓰지 않습니다.</p><div class="measure-actions">
          <form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="speaker"><button>이번 튜닝 · Speaker 테스트</button></form>
          <form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="headphone"><button>이번 튜닝 · Headphones 테스트</button></form>
          <form method="post" action="/measurement/restore"><button>기존 튜닝 듣기</button></form></div></div>
          <div class="measure-actions" id="measurement-step-6"><form method="post" action="/measurement/apply" onsubmit="return confirm('Speaker의 기존 FIR WAV를 새 결과로 덮어씁니다. 기존 파일은 자동 백업됩니다. 정식 적용할까요?')"><input type="hidden" name="profile" value="speaker"><button>Speaker 정식 적용 · 덮어쓰기</button></form>
          <form method="post" action="/measurement/apply" onsubmit="return confirm('Headphones의 기존 FIR WAV를 새 결과로 덮어씁니다. 기존 파일은 자동 백업됩니다. 정식 적용할까요?')"><input type="hidden" name="profile" value="headphone"><button>Headphones 정식 적용 · 덮어쓰기</button></form></div>
          <svg id="measurement-result-graph" data-result-target="{html.escape(str(result.get('target', 'harman')))}" viewBox="0 0 760 250" role="img" aria-label="Tuning before and predicted after frequency response"></svg>
        </div>"""
        if result.get("kind") == "mimo_2x4":
            mimo = result.get("mimo", {})
            prediction = mimo.get("prediction", {})
            metric_cards = "".join(
                f'<div><small>{channel.title()} target MAE</small><b>{values.get("before_target_mae_db", "?")} → {values.get("after_target_mae_db", "?")} dB</b><small>좌석 편차 {values.get("before_spatial_std_db", "?")} → {values.get("after_spatial_std_db", "?")} dB</small></div>'
                for channel, values in prediction.items()
            )
            topology = html.escape(str(mimo.get("topology", "mimo")))
            headroom = mimo.get("headroom", {})
            diversity = mimo.get("actuator_diversity", {})
            result_html = f"""
            <div class="result-box" id="measurement-step-5"><h3>MIMO 2×4 적용 전 검토</h3>
              <p><b>{topology}</b> · {result.get('taps')} taps × 8 convolution paths · 공동 제어 {mimo.get('frequency_range_hz', ['?', '?'])[0]}–{mimo.get('frequency_range_hz', ['?', '?'])[1]} Hz</p>
              <div class="diagnostic-grid">{metric_cards}<div><small>최악 상관입력 row sum</small><b>{headroom.get('maximum_correlated_input_row_sum', '?')}</b><small>global {headroom.get('global_scale_db', '?')} dB</small></div><div><small>제어원 최대 coherence</small><b>{diversity.get('maximum_coherence', '?')}</b><small>1에 가까우면 독립성 부족</small></div><div><small>Self validation</small><b>{'PASS' if self_validation.get('overall_pass') else 'FAIL · 적용 차단'}</b></div></div>
              <div class="diagnostic-note"><b>자동 진단</b><ul>{warning_html}</ul></div>{audit_html}
              <p class="muted">예측은 측정한 세 위치의 선형 모델에만 유효합니다. 실제 적용 전 Preview, 이후 별도 위치 재측정과 XRUN/CPU 확인이 필요합니다.</p>
              <div class="measure-actions"><a class="button" download href="/api/measurement/download/all">MIMO WAV 4개 + 보고서 ZIP</a><a class="button secondary" download href="/api/measurement/download/report-md">한계 포함 보고서 MD</a><a class="button secondary" download href="/api/measurement/download/report-json">전체 결과 JSON</a></div>
              <div class="result-box"><b>A/B 청취 비교</b><p class="muted">현재 상태: <span class="pill">{preview_label}</span> · MIMO는 실제 4채널 Speaker 출력 전용입니다.</p><div class="measure-actions"><form method="post" action="/measurement/preview"><input type="hidden" name="profile" value="speaker"><button>이번 MIMO · Speaker 테스트</button></form><form method="post" action="/measurement/restore"><button>기존 튜닝 듣기</button></form></div></div>
              <div class="measure-actions" id="measurement-step-6"><form method="post" action="/measurement/apply" onsubmit="return confirm('검증된 MIMO bank를 Speaker에 설치합니다. 기존 bank와 설정은 자동 백업됩니다. 정식 적용할까요?')"><input type="hidden" name="profile" value="speaker"><button{' disabled' if not self_validation.get('overall_pass') else ''}>Speaker MIMO 정식 적용</button></form></div>
              <svg id="measurement-result-graph" data-result-target="{html.escape(str(result.get('target', 'harman')))}" viewBox="0 0 760 250" role="img" aria-label="MIMO predicted response"></svg>
            </div>"""
    error = f'<div class="failure">{html.escape(str(job.get("error")))}</div>' if job.get("error") else ""
    level_html = ""
    if level:
        level_html = f'''<div class="level-result {'ok' if level.get('ok') else 'not-ok'}"><div class="level-verdict"><b>{'OK' if level.get('ok') else 'NOT OK'}</b><span>{html.escape(str(level.get('verdict', '')))}</span></div><div class="metric-grid"><div><small>무음 배경 RMS</small><b>{level.get('background_rms_dbfs', '?')} dBFS</b></div><div><small>백색소음 RMS</small><b>{level.get('white_noise_rms_dbfs', '?')} dBFS</b></div><div><small>추정 신호 RMS</small><b>{level.get('estimated_signal_rms_dbfs', '?')} dBFS</b></div><div><small>신호/배경 SNR</small><b>{level.get('snr_db', '?')} dB</b></div><div><small>입력 peak</small><b>{level.get('peak_dbfs', '?')} dBFS</b></div></div></div>'''
    return f"""
    <section class="measurement card-wide" data-job-state="{html.escape(state)}" data-job-position="{positions}" data-job-updated="{job.get('updated_unix', 0)}">
      <div class="section-head"><div><h2>UMIK-1 측정 · 32768탭 자동 보정</h2><p class="muted">실제 측정은 청취 위치 3곳, UMIK 천장 방향 90°. 재생 중 CamillaDSP direct bypass 및 U7 입력 OFF.</p></div><span class="pill">{'UMIK 연결' if job.get('umik_connected') else 'UMIK 없음'}</span></div>
      <div class="workflow" aria-label="Calibration workflow">{workflow}</div>
      {error}<div class="job-status"><div><b id="job-stage">{html.escape(str(job.get('stage', '대기')))}</b><span id="job-eta">{eta_text}</span></div><progress id="job-progress" max="100" value="{progress:.2f}"></progress><small id="job-percent">{progress:.0f}%</small></div>
      <p class="muted">Session: {html.escape(str(job.get('session_id', '없음')))} · 위치 {positions}/{total} · Calibration {html.escape(str(calibration.get('orientation', '90')))}° / {html.escape(str(calibration.get('serial', '7200660')))}</p>
      {'' if mimo_supported else '<p class="diagnostic-note"><b>MIMO 비활성</b> · Pi 2는 측정/UI 코드를 공유하지만 실시간 8경로 적용을 차단합니다. SISO L/R/W 보정은 그대로 사용할 수 있습니다.</p>'}
      <details class="cal-card" id="measurement-step-1" {'open' if current_step == 1 else ''}><summary class="cal-head"><span class="state-icon">μ</span><div><b>1 · UMIK calibration</b><p class="muted">0°/90° 파일 상태 · 클릭해서 펼치기 · 단계 이동만으로는 값이 지워지지 않습니다.</p></div></summary><div class="cal-slots">
        <form method="post" action="/measurement/calibration" enctype="multipart/form-data" class="cal-slot" onsubmit="return confirm('90° calibration 교체를 적용하면 이 파일로 측정한 레벨·3위치 응답·생성 FIR 결과가 초기화됩니다. 계속할까요?')"><input type="hidden" name="orientation" value="90"><div><b>90° · 천장 방향</b><span class="pill">룸 측정용</span></div><p>{cal90_summary}</p><label>miniDSP 90° TXT<input required type="file" name="file" accept="text/plain,.txt"></label><button>90° 파일 교체 적용</button></form>
        <form method="post" action="/measurement/calibration" enctype="multipart/form-data" class="cal-slot"><input type="hidden" name="orientation" value="0"><div><b>0° · 마이크 정면</b><span class="pill neutral">근접 진단용</span></div><p>{cal0_summary}</p><label>miniDSP 0° TXT<input required type="file" name="file" accept="text/plain,.txt"></label><button>0° 파일 교체 적용</button></form>
      </div></details>
      {level_html}{controls}{result_html}
      <details><summary>알고리즘과 안전 제한</summary><p>세 위치 대표 응답에는 저역 1/12-oct, 중역 1/6-oct, 고역 1/3-oct 가변 smoothing을 사용합니다. 위치 편차가 큰 null은 주파수별 regularization으로 boost를 축소하고, 반 옥타브 중앙값으로 추정한 스피커 자연 roll-off 밖은 boost하지 않습니다. 옥타브별 noise-compensated Schroeder EDT/T20으로 잔향을 진단하고 신뢰 가능한 300 Hz 이하 장시간 공진만 cut-only로 최대 3 dB 더 감쇄합니다. late reverb는 역보정하지 않습니다. 고역은 magnitude 위주, 저역은 선택적으로 중앙 위치 excess phase와 시간 정렬을 보정하며 causality delay를 제한합니다. 최종 FIR 최대 전달 이득은 0 dB 이하입니다.</p></details>
    </section>"""


def render_page(status: dict, message: str = "", error: str = "", show_woofer: bool = False, view: str = "status") -> bytes:
    settings = status["settings"]
    resolved = status["resolved"]
    selected = settings["requested_profile"]
    effective = resolved["effective_profile"]
    selector = status.get("u7_selector", {})
    physical = selector.get("profile") if not selector.get("stale", True) else None
    chunksize = settings["chunksize"]
    saved_volume_db = int(settings.get("output_volume_db", -10))
    graph = client_svg_graph(show_woofer, resolved["effective_rear_mode"], resolved["bypass"]) if view == "status" else ""
    measurement_html = measurement_panel(measurement_status(), status.get("preview", {})) if view == "measure" else ""
    cards = []
    for profile, korean in (("speaker", "스피커"), ("headphone", "헤드폰")):
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
                mimo_status_text = "켜짐 · 8 convolution paths"
            elif mimo_info and mimo_info.get("valid"):
                mimo_status_text = "설치됨 · 현재 SISO"
            elif capability.get("mimo_supported"):
                mimo_status_text = "Pi4/5 사용 가능 · bank 없음"
            else:
                mimo_status_text = html.escape(str(capability.get("reason", "Pi4/5 전용")))
            mimo_disabled = " disabled" if not mimo_enabled and not (mimo_info and mimo_info.get("valid") and capability.get("mimo_supported")) else ""
            mimo_control = f'''<form method="post" action="/mimo-enabled" class="bypass {'enabled' if mimo_enabled else ''}"><input type="hidden" name="profile" value="speaker"><input type="hidden" name="enabled" value="{'off' if mimo_enabled else 'on'}"><div><b>MIMO 2×4 bank</b><small>{mimo_status_text}</small></div><button{mimo_disabled}>{'MIMO 끄기' if mimo_enabled else 'MIMO 켜기'}</button></form>'''
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
            <div class="stage-summary"><span class="state-icon">∿</span><div><b>적용 대기 중</b><p>{('Front · ' + staged_front_name) if staged_front['present'] else 'Front · 기존값 유지'}<br>{('Rear · ' + staged_rear_name) if staged_rear['present'] else ('Rear · 기존값 유지' if candidate_rear else 'Rear · Front 복사')}</p></div><span class="pill">{'업로드값 테스트 중' if previewing else '기존값 재생 중'}</span></div>
            {staged_compare_graph(profile, candidate_rear)}
            <div class="stage-actions"><div><b>3 · 소리로 확인</b><p class="muted">업로드값 테스트는 설정과 정식 WAV를 바꾸지 않습니다.</p></div><div class="measure-actions">
              <form method="post" action="/staging/preview"><input type="hidden" name="profile" value="{profile}"><button>업로드값 테스트</button></form>
              <form method="post" action="/staging/restore"><button class="secondary">기존값 듣기</button></form>
            </div></div>
            <div class="stage-actions final"><div><b>4 · 확인 후 정식 적용</b><p class="muted">기존 WAV는 자동 백업됩니다. 이 버튼을 누르기 전에는 덮어쓰지 않습니다.</p></div><div class="measure-actions">
              <form method="post" action="/staging/apply" onsubmit="return confirm('{korean}의 기존 FIR을 업로드한 값으로 교체합니다. 정식 적용할까요?')"><input type="hidden" name="profile" value="{profile}"><button>검토 완료 · 정식 적용</button></form>
              <form method="post" action="/staging/discard"><input type="hidden" name="profile" value="{profile}"><button class="secondary">업로드 취소</button></form>
            </div></div>"""
        cards.append(f"""
        <section class="card {'active-profile' if is_active else ''}" data-profile="{profile}">
          <div class="profile-title"><h2><span class="profile-icon">{'◖))' if profile == 'speaker' else '∩'}</span>{korean} 프로필</h2>{'<span class="active-badge">U7 현재 출력</span>' if is_active else ''}</div>
          <form method="post" action="/bypass" class="bypass {'enabled' if bypass else ''}">
            <input type="hidden" name="profile" value="{profile}">
            <input type="hidden" name="enabled" value="{'off' if bypass else 'on'}">
            <div><b>DSP Bypass</b><small>{'켜짐 · FIR 0채널, 원본 L/R 복사' if bypass else '꺼짐 · FIR 프로필 사용'}</small></div>
            <button>{'Bypass 끄기' if bypass else 'Bypass 켜기'}</button>
          </form>
          {mimo_control}
          <div class="file"><b>Front L/R FIR</b><p>{file_summary(files['front'])}</p>
            <form method="post" action="/upload-stage" enctype="multipart/form-data">
              <input type="hidden" name="profile" value="{profile}"><input type="hidden" name="band" value="front">
              <input required type="file" name="wav" accept="audio/wav,.wav"><button>Front WAV 검토하기</button>
            </form>
          </div>
          <div class="file"><b>Rear L/R / Woofer FIR</b><p>{file_summary(files['rear'])}</p>
            <form method="post" action="/upload-stage" enctype="multipart/form-data">
              <input type="hidden" name="profile" value="{profile}"><input type="hidden" name="band" value="rear">
              <input required type="file" name="wav" accept="audio/wav,.wav"><button>Rear WAV 검토하기</button>
            </form>
          </div>
          {stage_html}
          <form method="post" action="/rear-mode" class="mode">
            <input type="hidden" name="profile" value="{profile}">
            <label><input type="radio" name="mode" value="copy_front" {'checked' if mode == 'copy_front' else ''}> Front 처리 후 Rear로 복사 (2채널 컨볼루션)</label>
            <label><input type="radio" name="mode" value="separate" {'checked' if mode == 'separate' else ''}> 별도 Rear FIR 사용 (WAV가 있을 때 4채널 컨볼루션)</label>
            <button>Rear 모드 적용</button>
          </form>
          <form method="post" action="/woofer-trim" class="mode">
            <input type="hidden" name="profile" value="{profile}">
            <label>실시간 Woofer trim <select name="trim_db">{''.join(f'<option value="{value}" {"selected" if value == woofer_trim else ""}>{value} dB</option>' for value in range(0, -19, -1))}</select></label>
            <button>Woofer trim 적용</button>
          </form>
        </section>""")
    camilla = "정상" if service_active("camilladsp.service") else "중지/오류"
    monitor = "정상" if service_active_any("audiodsp-profile-monitor.service", "gsonic-u7-profile.service") else "중지/오류"
    notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    failure = f'<div class="failure">{html.escape(error)}</div>' if error else ""
    woofer_query = "0" if show_woofer else "1"
    nav = "".join(
        f'<a class="{"active" if view == key else ""}" href="{path}">{label}</a>'
        for key, path, label in (("status", "/", "현황"), ("measure", "/measure", "측정 · 보정"), ("settings", "/settings", "프로필 · 설정"))
    )
    status_html = ""
    if view == "status":
        job = measurement_status()
        job_state = str(job.get("state", "idle"))
        job_positions = int(job.get("positions_completed", 0))
        if job_state in ("running", "processing", "cancelling"):
            next_label, next_href, next_note = "진행 상황 보기", "/measure", str(job.get("stage", "측정 작업 진행 중"))
        elif job_state == "idle":
            next_label, next_href, next_note = "룸 보정 시작", "/measure#measurement-step-1", "UMIK calibration 확인 후 새 session을 만듭니다."
        elif not (job.get("level_check") or {}).get("ok"):
            next_label, next_href, next_note = "레벨 검사로 이동", "/measure#measurement-step-2", "5초 무음과 5초 백색소음으로 안전한 측정 레벨을 확인합니다."
        elif job_positions < int(job.get("positions_total", 3)):
            next_label, next_href, next_note = f"위치 {job_positions + 1} 측정으로 이동", "/measure#measurement-step-3", "완료한 위치는 유지됩니다. 실행 버튼을 누를 때만 측정합니다."
        elif not job.get("result"):
            next_label, next_href, next_note = "FIR 설정·계산으로 이동", "/measure#measurement-step-4", "원본 측정값은 유지하고 보정 설정을 선택합니다."
        elif not job.get("applied_profile"):
            next_label, next_href, next_note = "결과 검토·A/B로 이동", "/measure#measurement-step-5", "다운로드와 A/B 테스트 후에만 정식 적용합니다."
        else:
            next_label, next_href, next_note = "적용 결과 확인", "/measure#measurement-step-6", f"{job.get('applied_profile')} 프로필에 정식 적용된 상태입니다."
        graph_note = (
            "MIMO 활성 상태입니다. 아래 곡선은 전이대역 위에서 사용하는 base SISO FIR만 보여줍니다. 저역의 실제 합산 예상은 측정·보정 결과 그래프와 MIMO 보고서에서 확인하세요."
            if resolved.get("mimo_paths") else
            "Front L/R은 개별 곡선입니다. Woofer는 Rear L/R 크기의 에너지 평균이며, Front 복사 모드에서는 점선입니다."
        )
        status_html = f"""
        <section class="next-action"><div><small>지금 할 일</small><h2>{html.escape(next_label)}</h2><p>{html.escape(next_note)}</p></div><div class="measure-actions"><a class="button" href="{next_href}">{html.escape(next_label)}</a><a class="button secondary" href="/settings">프로필 · 백업 설정</a></div></section>
        <section class="card-wide output-volume" id="output-volume-control" data-saved-volume="{saved_volume_db}">
          <div class="section-head"><div><h2>출력 볼륨</h2><p class="muted">Xonar U7의 Front/Rear 전체 PCM 출력에 즉시 적용됩니다. FIR과 CamillaDSP는 재시작하지 않습니다.</p></div><output id="output-volume-value" for="output-volume-slider">{saved_volume_db} dB</output></div>
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
          <p class="muted volume-note" id="output-volume-status">저장값 {saved_volume_db} dB · U7 실제 볼륨 확인 중…</p>
          <p class="muted">U7 물리 노브로 바꾼 값도 약 3초 안에 표시됩니다. 물리 노브 변경은 저장값을 바꾸지 않으므로 재부팅하면 마지막 웹/API 저장값으로 돌아옵니다.</p>
        </section>
        <section class="status"><h2>현재 설정</h2><table>
          <tr><td>U7 실제 출력</td><td><span class="pill" id="u7-physical">{html.escape(physical or '감지 대기')}</span> <span class="muted">(표시 전용 · U7 상단 버튼으로 변경)</span></td></tr>
          <tr><td>DSP 요청 프로필</td><td id="dsp-requested">{html.escape(selected)}</td></tr>
          <tr><td>실제 적용 프로필</td><td id="dsp-effective">{html.escape(effective)}{' (fallback)' if selected != effective else ''}</td></tr>
          <tr><td>DSP Bypass</td><td>{'켜짐 · 원본 L/R 복사' if resolved['bypass'] else '꺼짐'}</td></tr>
          <tr><td>A/B 청취 상태</td><td>{('이번 튜닝 테스트 중 · ' + html.escape(str(status.get('preview', {}).get('profile')))) if status.get('preview', {}).get('active') and not status.get('preview', {}).get('stale') else '기존 정식 튜닝'}</td></tr>
          <tr><td>Rear 처리</td><td>{html.escape(resolved['effective_rear_mode'])}</td></tr>
          <tr><td>컨볼루션</td><td>{resolved['convolution_channels']}채널</td></tr>
          <tr><td>MIMO bank</td><td>{'활성 · ' + html.escape(str(resolved.get('mimo_topology'))) if resolved.get('mimo_paths') else ('설정됨, 현재 플랫폼에서 비활성: ' + html.escape(str(resolved.get('mimo_unavailable_reason'))) if resolved.get('mimo_unavailable_reason') else '비활성')}</td></tr>
          <tr><td>CamillaDSP / HID 감시</td><td>{camilla} / {monitor}</td></tr>
          <tr><td>시스템 상태</td><td id="system-health">확인 중…</td></tr>
          <tr><td>오디오</td><td>48 kHz · 입력 2ch · 출력 4ch · chunksize {chunksize}</td></tr>
        </table></section>
        <section class="graphbox"><h2>현재 FIR 주파수 응답</h2>
          <a class="button" href="/?woofer={woofer_query}">{'Woofer 숨기기' if show_woofer else 'Woofer 표시'}</a>
          <p class="muted">{graph_note}</p>{graph}</section>"""
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
              <div class="measure-actions"><form method="post" action="/backup/apply" onsubmit="return confirm('현재 전체 설정을 자동 백업한 뒤 검증된 ZIP을 복원합니다. 오디오가 잠시 재시작됩니다. 계속할까요?')"><button>검증 완료 · 전체 복원</button></form><form method="post" action="/backup/discard"><button class="secondary">복원 취소 · 현재 상태 유지</button></form></div>
            </div>"""
        else:
            restore_detail = '<p class="muted">복원 ZIP을 선택하면 먼저 임시 검토만 합니다. 검증 완료 후 별도 확정 버튼을 눌러야 실제 설정이 바뀝니다.</p>'
        settings_html = f"""
        <section class="card-wide backup-panel"><div class="section-head"><div><h2>전체 백업 · 안전 복원</h2><p class="muted">프로필 설정, Speaker/Headphones/Factory FIR, 선택적 MIMO bank, 0°/90° UMIK calibration을 버전형 ZIP 하나로 관리합니다.</p></div><span class="pill neutral">schema v{BACKUP_SCHEMA_VERSION}</span></div>
          <div class="backup-actions"><div><b>현재 상태 보관</b><p>다운로드는 오디오를 바꾸지 않습니다.</p><a class="button" download href="/api/backup/download">전체 백업 ZIP 받기</a>{automatic_backup_html}</div><form method="post" action="/backup/stage" enctype="multipart/form-data"><b>백업에서 복원</b><p>업로드 → 검사 → 확인 → 복원</p><input required type="file" name="backup" accept="application/zip,.zip"><button>ZIP 검토하기</button></form></div>
          {restore_detail}
        </section>
        <section class="status"><h2>엔진 설정</h2><table>
          <tr><td>오디오 형식</td><td>48 kHz · 입력 2ch · 출력 4ch</td></tr>
          <tr><td>처리 블록</td><td><form method="post" action="/chunksize"><select name="chunksize" aria-label="CamillaDSP chunksize">{chunk_options}</select> <button>적용</button></form><span class="muted">변경 시 오디오가 잠시 재시작됩니다. Pi 2는 2048, Pi 4/5는 1024를 권장합니다.</span></td></tr>
        </table></section>
        <div class="grid">{''.join(cards)}</div>
        <p class="muted">업로드 조건: stereo, 48 kHz, PCM/IEEE-float WAV, 최대 262,144 taps. LAN 내부 포트 8080에서만 사용하세요.</p>"""
    body = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>AudioDSP</title>
    <script>(()=>{{const t=localStorage.getItem('audiodsp-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}})();</script><style>
    :root{{--bg:#f3f6fb;--bg-glow:#dbeafe;--surface:rgba(255,255,255,.88);--surface-strong:#fff;--text:#13213a;--muted:#61708a;--border:#d8e0eb;--accent:#2563eb;--accent-hover:#1d4ed8;--accent-soft:#dbeafe;--success:#047857;--success-bg:#d1fae5;--danger:#b42318;--danger-bg:#fee4e2;--warning:#a15c00;--shadow:0 18px 45px rgba(32,55,92,.10);--graph-bg:#111827;--graph-grid:#334155;--graph-text:#94a3b8;--curve-l:#0284c7;--curve-r:#e11d48;--curve-w:#d97706;color-scheme:light}}
    :root[data-theme="dark"]{{--bg:#080d18;--bg-glow:#172554;--surface:rgba(19,29,48,.90);--surface-strong:#151f33;--text:#e8eef8;--muted:#9babc2;--border:#2b3a54;--accent:#38bdf8;--accent-hover:#7dd3fc;--accent-soft:#0c4a6e;--success:#5eead4;--success-bg:#123f3a;--danger:#fda4af;--danger-bg:#4c1720;--warning:#fbbf24;--shadow:0 22px 55px rgba(0,0,0,.30);--graph-bg:#090f1c;--graph-grid:#2b3a54;--graph-text:#9babc2;--curve-l:#38bdf8;--curve-r:#fb7185;--curve-w:#fbbf24;color-scheme:dark}}
    @media(prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#080d18;--bg-glow:#172554;--surface:rgba(19,29,48,.90);--surface-strong:#151f33;--text:#e8eef8;--muted:#9babc2;--border:#2b3a54;--accent:#38bdf8;--accent-hover:#7dd3fc;--accent-soft:#0c4a6e;--success:#5eead4;--success-bg:#123f3a;--danger:#fda4af;--danger-bg:#4c1720;--warning:#fbbf24;--shadow:0 22px 55px rgba(0,0,0,.30);--graph-bg:#090f1c;--graph-grid:#2b3a54;--graph-text:#9babc2;--curve-l:#38bdf8;--curve-r:#fb7185;--curve-w:#fbbf24;color-scheme:dark}}}}
    *{{box-sizing:border-box}}html{{min-height:100%;overflow-x:hidden}}body{{margin:0;min-height:100%;overflow-x:hidden;background:radial-gradient(circle at 12% -10%,var(--bg-glow),transparent 34rem),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI','Malgun Gothic',sans-serif;transition:background .25s,color .25s}}
    main{{width:100%;max-width:1160px;min-width:0;margin:auto;padding:clamp(16px,3vw,36px);overflow-x:clip}}h1{{margin:0;font-size:clamp(1.65rem,4vw,2.35rem);letter-spacing:-.04em}}h2{{margin:0 0 14px;font-size:1.15rem;letter-spacing:-.02em;overflow-wrap:anywhere}}p,code{{overflow-wrap:anywhere}}code{{color:var(--accent);font-size:.82rem}}
    .topbar{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}}.subtitle{{color:var(--muted);margin:5px 0 0}}
    .app-nav{{display:flex;gap:7px;overflow-x:auto;padding:5px;margin:0 0 16px;background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow)}}.app-nav a{{flex:1;min-width:max-content;padding:11px 16px;border-radius:10px;text-decoration:none;color:var(--muted);font-weight:800;text-align:center}}.app-nav a.active{{background:var(--accent-soft);color:var(--accent)}}.app-nav a:hover{{color:var(--text)}}
    .theme-switch{{display:flex;padding:4px;background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}}.theme-switch button{{padding:7px 10px;background:transparent;color:var(--muted);box-shadow:none}}.theme-switch button[aria-pressed="true"]{{background:var(--accent-soft);color:var(--text)}}
    .status,.card,.graphbox{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:clamp(16px,2.5vw,24px);margin:16px 0;box-shadow:var(--shadow);backdrop-filter:blur(16px)}}
    .grid{{display:grid;grid-template-columns:1fr;gap:16px}}.grid .card{{margin:0}}.card.active-profile{{border:2px solid var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 13%,transparent),var(--shadow)}}.profile-title{{display:flex;align-items:center;justify-content:space-between;gap:10px}}.profile-title h2{{display:flex;align-items:center;gap:9px;margin:0}}.profile-icon,.state-icon{{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:10px;background:var(--accent-soft);color:var(--accent);font-weight:900;font-size:.9rem}}.active-badge{{display:inline-flex;padding:5px 9px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:.78rem;font-weight:800;white-space:nowrap}}
    .pill{{display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;background:var(--success-bg);color:var(--success);font-weight:700;text-transform:capitalize}}.pill.warn{{background:color-mix(in srgb,var(--warning) 14%,var(--surface-strong));color:var(--warning)}}.muted{{color:var(--muted);overflow-wrap:anywhere}}
    .file{{border-top:1px solid var(--border);padding:15px 0}}.file p{{min-height:42px;color:var(--muted)}}form{{margin:9px 0;min-width:0}}input[type=file]{{max-width:100%;margin:7px 0;color:var(--muted)}}select{{max-width:100%;min-width:0;font:inherit;background:var(--surface-strong);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:9px 12px}}
    button,.button{{font:inherit;font-weight:700;background:var(--accent);color:white;border:0;border-radius:10px;padding:9px 14px;cursor:pointer;text-decoration:none;display:inline-block;box-shadow:0 6px 14px color-mix(in srgb,var(--accent) 25%,transparent);transition:transform .15s,background .15s}}
    button:hover,.button:hover{{background:var(--accent-hover);transform:translateY(-1px)}}button.secondary,.button.secondary{{background:transparent;color:var(--text);border:1px solid var(--border);box-shadow:none}}button.secondary:hover,.button.secondary:hover{{background:var(--accent-soft);color:var(--accent)}}button:focus-visible,.button:focus-visible,input:focus-visible{{outline:3px solid color-mix(in srgb,var(--accent) 38%,transparent);outline-offset:2px}}
    .mode{{background:color-mix(in srgb,var(--surface-strong) 72%,transparent);border:1px solid var(--border);border-radius:12px;padding:12px}}.mode label{{display:block;margin:9px 0;line-height:1.45}}.bypass{{display:flex;align-items:center;justify-content:space-between;gap:12px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent);border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:14px}}.bypass.enabled{{border-color:var(--warning);background:color-mix(in srgb,var(--warning) 9%,var(--surface-strong))}}.bypass small{{display:block;color:var(--muted);margin-top:3px}}.missing{{color:var(--warning)}}.bad,.failure{{color:var(--danger)}}
    .notice{{background:var(--success-bg);color:var(--success);padding:13px 15px;border-radius:11px;margin:12px 0}}.failure{{background:var(--danger-bg);padding:13px 15px;border-radius:11px;margin:12px 0}}.graphbox>.muted{{word-break:keep-all}}.graph-scroll{{overflow-x:auto;overscroll-behavior-inline:contain}}.response{{display:block;width:100%;height:auto;margin-top:10px;border-radius:12px}}
    .card-wide{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:clamp(16px,2.5vw,24px);margin:16px 0;box-shadow:var(--shadow)}}.next-action{{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 20px;margin:16px 0;border:1px solid color-mix(in srgb,var(--accent) 48%,var(--border));border-radius:18px;background:linear-gradient(135deg,var(--accent-soft),var(--surface));box-shadow:var(--shadow)}}.next-action small{{color:var(--accent);font-weight:900;text-transform:uppercase;letter-spacing:.08em}}.next-action h2{{margin:4px 0}}.next-action p{{margin:0;color:var(--muted)}}.section-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}}.section-head h2{{margin-bottom:6px}}.output-volume output{{min-width:6ch;color:var(--accent);font-size:clamp(1.7rem,5vw,2.5rem);font-weight:900;text-align:right;font-variant-numeric:tabular-nums}}.output-volume form{{display:grid;gap:12px;margin-top:14px}}.output-volume input[type=range]{{width:100%;height:28px;accent-color:var(--accent);cursor:pointer}}.volume-actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}.volume-presets{{display:flex;gap:6px;flex:1;flex-wrap:wrap}}.volume-note{{margin-bottom:4px;font-weight:700}}.job-status{{padding:14px;border:1px solid var(--border);border-radius:12px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent);margin:14px 0}}progress{{width:100%;height:13px;accent-color:var(--accent);margin:10px 0 4px}}.measure-form{{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;align-items:end;padding:14px;border:1px solid var(--border);border-radius:12px;margin:14px 0}}.measure-form label{{display:grid;gap:6px;color:var(--muted);font-size:.86rem}}.build-options>.advanced{{grid-column:1/-1;margin:2px 0 0;padding:12px;border:1px solid var(--border);border-radius:12px;background:var(--surface-strong)}}.advanced-grid{{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:10px;margin-top:12px}}.measure-actions{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}.measure-actions form{{margin:0}}button.danger{{background:var(--danger)}}.target-preview,.result-box{{border-top:1px solid var(--border);margin-top:16px;padding-top:16px}}#target-graph,#measurement-result-graph{{display:block;width:100%;height:auto;background:var(--graph-bg);border-radius:12px;margin-top:10px}}details{{margin-top:14px;border-top:1px solid var(--border);padding-top:12px}}summary{{cursor:pointer;font-weight:700}}
    .workflow{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:7px;margin:18px 0}}.flow-step{{display:flex;align-items:center;gap:7px;padding:10px 8px;border:1px solid var(--border);border-radius:12px;color:var(--muted);background:color-mix(in srgb,var(--surface-strong) 64%,transparent);font-size:.78rem;text-decoration:none}}a.flow-step{{cursor:pointer}}a.flow-step:hover{{border-color:var(--accent);transform:translateY(-1px)}}.flow-step>span{{display:grid;place-items:center;min-width:24px;height:24px;border-radius:50%;background:var(--border);color:var(--text);font-weight:800}}.flow-step.done{{color:var(--success)}}.flow-step.done>span{{background:var(--success-bg);color:var(--success)}}.flow-step.current{{border-color:var(--accent);color:var(--text);background:var(--accent-soft);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 14%,transparent)}}.flow-step.current>span{{background:var(--accent);color:white}}.flow-step.future{{opacity:.56}}[id^="measurement-step-"]{{scroll-margin-top:18px}}.form-note{{grid-column:1/-1;margin:0;color:var(--muted);font-size:.8rem;line-height:1.45}}.cal-card{{display:grid;gap:14px;padding:14px;border:1px solid var(--border);border-radius:14px;background:color-mix(in srgb,var(--surface-strong) 72%,transparent)}}.cal-head{{display:flex;gap:10px;align-items:center}}.cal-head p,.cal-card p{{margin:5px 0 0}}.cal-slots{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.cal-slot{{display:grid;grid-template-columns:1fr auto;gap:8px 12px;padding:12px;margin:0;border:1px solid var(--border);border-radius:12px;background:var(--surface-strong)}}.cal-slot>div{{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:8px}}.cal-slot>p{{grid-column:1/-1;color:var(--muted);font-size:.82rem}}.cal-slot label{{display:grid;gap:5px;color:var(--muted);font-size:.82rem}}.cal-slot input[type=file]{{margin:0}}.pill.neutral{{background:var(--border);color:var(--muted)}}
    .level-result{{margin:14px 0;padding:14px;border:1px solid var(--border);border-radius:14px}}.level-result.ok{{border-color:var(--success);background:color-mix(in srgb,var(--success-bg) 55%,transparent)}}.level-result.not-ok{{border-color:var(--warning);background:color-mix(in srgb,var(--warning) 8%,var(--surface-strong))}}.level-verdict{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}.level-verdict>b{{font-size:1.05rem}}.metric-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-top:12px}}.metric-grid>div{{display:grid;gap:3px;padding:9px;border-radius:10px;background:var(--surface-strong)}}.metric-grid small{{color:var(--muted)}}button:disabled{{cursor:not-allowed;opacity:.45;transform:none;box-shadow:none}}
    .diagnostic-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:12px 0}}.diagnostic-grid>div{{display:grid;gap:4px;padding:10px;border:1px solid var(--border);border-radius:10px;background:var(--surface-strong)}}.diagnostic-grid small{{color:var(--muted)}}.diagnostic-note{{padding:12px;border-left:4px solid var(--accent);border-radius:8px;background:var(--accent-soft)}}.diagnostic-note ul{{margin:6px 0 0;padding-left:20px}}.target-fit{{margin:10px 0}}.decay-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}}.decay-grid>div{{display:grid;gap:5px;padding:10px;border:1px solid var(--border);border-radius:10px;background:var(--surface-strong)}}.decay-grid small{{color:var(--muted);font-weight:800}}.decay-grid span{{display:flex;justify-content:space-between;gap:8px;font-size:.82rem}}
    .stage-workflow{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding-top:16px;border-top:1px solid var(--border)}}.stage-step{{display:grid;grid-template-columns:30px 1fr;column-gap:8px;align-items:center;padding:10px;border:1px solid var(--border);border-radius:12px;color:var(--muted)}}.stage-step span{{grid-row:1/3;display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--border);font-weight:900;color:var(--text)}}.stage-step b{{font-size:.88rem;color:var(--text)}}.stage-step small{{font-size:.72rem}}.stage-step.done span{{background:var(--success-bg);color:var(--success)}}.stage-step.current{{border-color:var(--accent);background:var(--accent-soft)}}.stage-step.current span{{background:var(--accent);color:white}}.stage-summary{{display:flex;align-items:center;gap:12px;padding:14px;margin:12px 0;border-radius:13px;background:color-mix(in srgb,var(--accent-soft) 56%,var(--surface-strong));border:1px solid color-mix(in srgb,var(--accent) 45%,var(--border))}}.stage-summary>div{{flex:1}}.stage-summary p{{margin:4px 0 0;color:var(--muted);font-size:.84rem;line-height:1.45}}.staged-compare h3{{margin:18px 0 4px;font-size:1rem}}.stage-actions{{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px;margin-top:12px;border:1px solid var(--border);border-radius:13px;background:color-mix(in srgb,var(--surface-strong) 70%,transparent)}}.stage-actions p{{margin:4px 0 0}}.stage-actions.final{{border-color:color-mix(in srgb,var(--success) 52%,var(--border));background:color-mix(in srgb,var(--success-bg) 35%,var(--surface-strong))}}
    .backup-actions{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}}.backup-actions>div,.backup-actions>form{{display:grid;align-content:start;gap:8px;padding:14px;margin:0;border:1px solid var(--border);border-radius:13px;background:var(--surface-strong)}}.backup-actions p{{margin:0;color:var(--muted);font-size:.84rem}}.backup-actions .button,.backup-actions button{{justify-self:start}}.restore-review{{margin-top:14px;padding-top:14px;border-top:1px solid var(--border)}}
    table{{border-collapse:collapse;width:100%}}td{{padding:9px 6px;border-bottom:1px solid var(--border)}}td:first-child{{color:var(--muted);width:38%}}
    @media(max-width:760px){{body{{padding-bottom:74px}}.grid{{grid-template-columns:1fr}}.topbar,.section-head,.stage-actions{{align-items:flex-start;flex-direction:column}}.app-nav{{position:fixed;z-index:20;left:10px;right:10px;bottom:max(8px,env(safe-area-inset-bottom));margin:0;padding:6px;background:color-mix(in srgb,var(--surface) 92%,transparent);box-shadow:0 8px 28px #0005;backdrop-filter:blur(14px)}}.app-nav a{{flex:1;text-align:center;padding:10px 5px;font-size:.78rem}}.theme-switch{{align-self:stretch;justify-content:center}}.theme-switch button{{flex:1}}td{{display:block;width:100%!important;padding:6px 2px}}td:first-child{{border-bottom:0;padding-top:11px}}.status,.card,.graphbox,.card-wide{{border-radius:15px}}.bypass{{align-items:stretch;flex-direction:column}}.bypass button{{width:100%}}.graph-scroll .response{{width:700px;max-width:none}}.measure-form,.advanced-grid,.backup-actions,.decay-grid{{grid-template-columns:1fr}}.measure-form button,.cal-slot button,.backup-actions .button,.backup-actions button{{width:100%}}.workflow{{position:sticky;z-index:12;top:6px;grid-template-columns:repeat(3,1fr);padding:6px;margin-inline:-6px;border-radius:14px;background:color-mix(in srgb,var(--surface) 94%,transparent);box-shadow:var(--shadow);backdrop-filter:blur(12px)}}.stage-workflow,.cal-slots{{grid-template-columns:1fr}}.cal-slot{{grid-template-columns:1fr}}.metric-grid,.diagnostic-grid{{grid-template-columns:1fr 1fr}}}}
    .topbar,.app-nav,.status,.card,.graphbox,.card-wide,.measurement,.backup-panel{{min-width:0;max-width:100%}}.flow-step{{min-width:0;overflow:hidden}}.flow-step b{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.cal-card>summary{{margin:0;padding:0;border:0;list-style:none}}.cal-card>summary::-webkit-details-marker{{display:none}}
    @media(max-width:760px){{.next-action{{align-items:flex-start;flex-direction:column}}.app-nav a{{min-width:0}}.theme-switch{{width:100%}}}}
    @media(max-width:600px){{.workflow{{grid-template-columns:1fr 1fr;width:100%}}.topbar>*,.section-head>*,.backup-actions>*,.measure-form>*,.cal-slot>*{{min-width:0;max-width:100%}}.theme-switch{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%}}.theme-switch button{{min-width:0;padding-inline:4px}}input[type=file]{{width:100%}}button,.button{{text-align:center}}.measure-actions form,.measure-actions a{{width:100%}}.volume-actions>button[type=submit]{{width:100%}}.volume-presets{{order:3;flex-basis:100%}}.volume-presets button{{flex:1}}}}
    @media(prefers-reduced-motion:reduce){{*{{transition:none!important;scroll-behavior:auto!important}}}}
    </style></head><body data-physical="{physical or ''}" data-requested="{selected}" data-effective="{effective}"><main>
    <header class="topbar"><div><h1>AudioDSP</h1><p class="subtitle">Xonar U7 · Room correction · FIR profiles</p></div><div class="theme-switch" role="group" aria-label="색상 테마"><button type="button" data-theme-choice="auto">Auto</button><button type="button" data-theme-choice="light">Light</button><button type="button" data-theme-choice="dark">Dark</button></div></header>
    <nav class="app-nav" aria-label="주요 화면">{nav}</nav>{notice}{failure}{status_html}{measurement_html}{settings_html}
    <script>(()=>{{const buttons=[...document.querySelectorAll('[data-theme-choice]')];const current=()=>localStorage.getItem('audiodsp-theme')||'auto';const paint=()=>buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.themeChoice===current())));buttons.forEach(b=>b.addEventListener('click',()=>{{const t=b.dataset.themeChoice;if(t==='auto'){{localStorage.removeItem('audiodsp-theme');delete document.documentElement.dataset.theme;}}else{{localStorage.setItem('audiodsp-theme',t);document.documentElement.dataset.theme=t;}}paint();}}));paint();}})();</script>
    <script>(()=>{{/* output_volume_control */const root=document.getElementById('output-volume-control');if(!root)return;const slider=document.getElementById('output-volume-slider'),value=document.getElementById('output-volume-value'),note=document.getElementById('output-volume-status'),form=document.getElementById('output-volume-form');let timer=0,writing=false;const clamp=db=>Math.max(-60,Math.min(0,Math.round(db)));const label=db=>`${{Number(db).toFixed(Number.isInteger(Number(db))?0:1)}} dB`;const paint=v=>{{const saved=Number(v.saved_db??root.dataset.savedVolume);root.dataset.savedVolume=String(saved);const actual=v.available?Number(v.actual_db):null;if(document.activeElement!==slider)slider.value=String(actual??saved);value.textContent=label(actual??saved);if(!v.available){{note.textContent=`U7 실제 볼륨을 읽을 수 없음 · 재부팅 저장값 ${{label(saved)}}${{v.error?' · '+v.error:''}}`;note.classList.add('bad');}}else if(Math.abs(actual-saved)>.05){{note.textContent=`U7 실제 ${{label(actual)}} · 재부팅 저장값 ${{label(saved)}} · 물리 노브 변경 감지`;note.classList.remove('bad');}}else{{note.textContent=`U7 실제·저장값 ${{label(actual)}} · ${{v.channels}}채널 동일 적용`;note.classList.remove('bad');}}}};const load=async()=>{{clearTimeout(timer);if(document.hidden){{timer=setTimeout(load,3000);return;}}try{{const r=await fetch('/api/volume',{{cache:'no-store'}});if(r.ok)paint(await r.json());}}catch(_e){{}}finally{{timer=setTimeout(load,3000);}}}};const write=async db=>{{if(writing)return;writing=true;const target=clamp(db);slider.value=String(target);value.textContent=label(target);note.textContent='U7에 적용 중…';try{{const r=await fetch('/api/volume',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{db:target}})}});const body=await r.json().catch(()=>({{}}));if(!r.ok)throw Error(body.error||`HTTP ${{r.status}}`);paint(body);}}catch(e){{note.textContent='볼륨 적용 실패 · '+e.message;note.classList.add('bad');}}finally{{writing=false;}}}};slider.addEventListener('input',()=>{{value.textContent=label(Number(slider.value));}});slider.addEventListener('change',()=>write(Number(slider.value)));form.addEventListener('submit',e=>{{e.preventDefault();write(Number(slider.value));}});root.querySelectorAll('[data-volume-step]').forEach(b=>b.addEventListener('click',()=>write(Number(slider.value)+Number(b.dataset.volumeStep))));root.querySelectorAll('[data-volume]').forEach(b=>b.addEventListener('click',()=>write(Number(b.dataset.volume))));document.addEventListener('visibilitychange',()=>{{if(!document.hidden)load();}});load();}})();</script>
    <script>(()=>{{/* live_u7_status_poll */const initial={{physical:document.body.dataset.physical,requested:document.body.dataset.requested,effective:document.body.dataset.effective}};let timer=0,busy=false,reloading=false;const badge=(card,on)=>{{card.classList.toggle('active-profile',on);let b=card.querySelector('.active-badge');if(on&&!b){{b=document.createElement('span');b.className='active-badge';b.textContent='U7 현재 출력';card.querySelector('.profile-title').append(b);}}else if(!on&&b)b.remove();}};const paint=s=>{{const q=s.u7_selector||{{}};const physical=q.stale?'':(q.profile||'');const physicalNode=document.getElementById('u7-physical');if(physicalNode)physicalNode.textContent=physical||'감지 대기';document.querySelectorAll('.card[data-profile]').forEach(c=>badge(c,c.dataset.profile===physical));const requested=s.settings.requested_profile;const effective=s.resolved.effective_profile;const requestedNode=document.getElementById('dsp-requested');const effectiveNode=document.getElementById('dsp-effective');if(requestedNode)requestedNode.textContent=requested;if(effectiveNode)effectiveNode.textContent=effective+(requested!==effective?' (fallback)':'');if(!reloading&&(requested!==initial.requested||effective!==initial.effective)){{reloading=true;setTimeout(()=>location.reload(),180);}}}};const poll=async()=>{{clearTimeout(timer);if(document.hidden){{timer=setTimeout(poll,1000);return;}}if(busy)return;busy=true;try{{const r=await fetch('/api/status',{{cache:'no-store'}});if(r.ok)paint(await r.json());}}catch(_e){{}}finally{{busy=false;timer=setTimeout(poll,1000);}}}};document.addEventListener('visibilitychange',()=>{{if(!document.hidden)poll();}});timer=setTimeout(poll,500);}})();</script>
    <script>(()=>{{/* measurement_ui */const panel=document.querySelector('.measurement');if(!panel)return;const initial={{state:panel.dataset.jobState,position:panel.dataset.jobPosition,updated:panel.dataset.jobUpdated}};const draw=(svg,curves,minY,maxY)=>{{if(!svg)return;svg.replaceChildren();const W=760,H=250,L=48,R=12,T=12,B=28;const x=f=>L+(Math.log10(f)-Math.log10(20))/3*(W-L-R);const y=d=>T+(maxY-d)/(maxY-minY)*(H-T-B);let markup='';for(const f of [20,50,100,200,500,1000,2000,5000,10000,20000])markup+=`<line x1="${{x(f)}}" y1="${{T}}" x2="${{x(f)}}" y2="${{H-B}}" stroke="var(--graph-grid)"/><text x="${{x(f)}}" y="${{H-8}}" text-anchor="middle" fill="var(--graph-text)" font-size="10">${{f>=1000?(f/1000)+'k':f}}</text>`;for(let d=Math.ceil(minY/5)*5;d<=maxY;d+=5)markup+=`<line x1="${{L}}" y1="${{y(d)}}" x2="${{W-R}}" y2="${{y(d)}}" stroke="var(--graph-grid)"/><text x="${{L-6}}" y="${{y(d)+3}}" text-anchor="end" fill="var(--graph-text)" font-size="10">${{d}}</text>`;const colors=['var(--curve-l)','var(--curve-r)','var(--curve-w)'];curves.forEach((curve,i)=>{{const color=curve.color||colors[i%colors.length];if(curve.band){{const upper=curve.f.map((f,n)=>`${{x(f).toFixed(1)}},${{y(curve.d[n]+curve.band[n]).toFixed(1)}}`);const lower=curve.f.map((f,n)=>`${{x(f).toFixed(1)}},${{y(curve.d[n]-curve.band[n]).toFixed(1)}}`).reverse();markup+=`<polygon points="${{upper.concat(lower).join(' ')}}" fill="${{color}}" opacity=".10"/>`;}}const points=curve.f.map((f,n)=>`${{x(f).toFixed(1)}},${{y(curve.d[n]).toFixed(1)}}`).join(' ');markup+=`<polyline points="${{points}}" fill="none" stroke="${{color}}" stroke-width="${{curve.width||2}}" stroke-dasharray="${{curve.dash||''}}"/><text x="${{L+8}}" y="${{T+15+i*15}}" fill="${{color}}" font-size="10">${{curve.name}}</text>`;}});svg.innerHTML=markup;}};const targetSelect=document.getElementById('target-choice'),bassSelect=document.querySelector('[name=bass_tilt_db]'),trebleSelect=document.querySelector('[name=treble_tilt_db]');let catalog=null;const pref=(f,b,t)=>{{let x=0;if(f<=20)x+=b;else if(f<250){{const p=Math.log(f/20)/Math.log(12.5);x+=b*(.5+.5*Math.cos(Math.PI*p));}}if(f>=20000)x+=t;else if(f>1000){{const p=Math.log(f/1000)/Math.log(20);x+=t*(.5-.5*Math.cos(Math.PI*p));}}return x;}};const paintTarget=()=>{{if(!catalog||!targetSelect)return;const t=catalog.targets[targetSelect.value],b=Number(bassSelect?.value||0),h=Number(trebleSelect?.value||0),values=t.db.map((v,i)=>v+pref(t.frequency[i],b,h));draw(document.getElementById('target-graph'),[{{name:t.label+` · Bass ${{b>=0?'+':''}}${{b}} / Treble ${{h>=0?'+':''}}${{h}} dB`,f:t.frequency,d:values}}],-12,12);}};fetch('/api/targets',{{cache:'no-store'}}).then(r=>r.json()).then(j=>{{catalog=j;paintTarget();}}).catch(()=>{{}});[targetSelect,bassSelect,trebleSelect].forEach(e=>e?.addEventListener('change',paintTarget));let timer=0;const poll=async()=>{{clearTimeout(timer);if(document.hidden){{timer=setTimeout(poll,1000);return;}}try{{const r=await fetch('/api/measurement/status',{{cache:'no-store'}});if(r.ok){{const j=await r.json();const p=document.getElementById('job-progress');if(p)p.value=j.progress||0;const pct=document.getElementById('job-percent');if(pct)pct.textContent=Math.round(j.progress||0)+'%';const stage=document.getElementById('job-stage');if(stage)stage.textContent=j.stage||'';const eta=document.getElementById('job-eta');if(eta)eta.textContent=Number.isFinite(j.eta_seconds)?' · 예상 '+j.eta_seconds+'초':'';if(j.state!==initial.state||String(j.positions_completed||0)!==initial.position){{if(!['running','processing','cancelling'].includes(j.state)||!['running','processing','cancelling'].includes(initial.state))setTimeout(()=>location.reload(),250);}}if(j.result?.graphs){{const g=j.result.graphs;const curves=[];for(const [name,key,color] of [['Left','left','var(--curve-l)'],['Right','right','var(--curve-r)'],['Woofer','woofer','var(--curve-w)']])if(g[key]?.frequency){{curves.push({{name:name+' · 전 ±공간편차',f:g[key].frequency,d:g[key].before_db,band:g[key].spatial_std_db,color,dash:'6 5',width:1.3}});curves.push({{name:name+' · 후(예상)',f:g[key].frequency,d:g[key].predicted_db,color,width:2.5}});}}const first=g.left||g.right||g.woofer;if(first?.target_db)curves.push({{name:'적용 Target',f:first.frequency,d:first.target_db,color:'var(--graph-text)',dash:'2 4',width:1.4}});const values=curves.flatMap(c=>c.d).filter(Number.isFinite);const minY=Math.floor((Math.min(-10,...values)-2)/5)*5;const maxY=Math.ceil((Math.max(10,...values)+2)/5)*5;draw(document.getElementById('measurement-result-graph'),curves,minY,maxY);}}}}}}catch(_e){{}}finally{{timer=setTimeout(poll,1000);}}}};document.addEventListener('visibilitychange',()=>{{if(!document.hidden)poll();}});poll();}})();</script>
    <script>(()=>{{const paint=async()=>{{try{{const h=await fetch('/api/health',{{cache:'no-store'}}).then(r=>r.json());const e=document.getElementById('system-health');if(e)e.textContent=`CPU ${{h.load[0].toFixed(2)}} · ${{h.temperature_c??'?'}}°C · 메모리 ${{h.memory_used_percent}}% · U7 ${{h.xonar_u7?'연결':'없음'}} · UMIK ${{h.umik1?'연결':'없음'}}`;}}catch(_e){{}}}};paint();setInterval(()=>{{if(!document.hidden)paint();}},5000);}})();</script>
    <script>(()=>{{/* non_destructive_step_navigation */document.querySelectorAll('a.flow-step[href^="#measurement-step-"]').forEach(a=>a.addEventListener('click',()=>{{const target=document.querySelector(a.getAttribute('href'));if(target?.tagName==='DETAILS')target.open=true;}}));}})();</script>
    <script>(()=>{{/* prevent_accidental_double_submit */document.addEventListener('submit',e=>{{if(e.defaultPrevented)return;const f=e.target;if(f.dataset.submitting==='1'){{e.preventDefault();return;}}f.dataset.submitting='1';queueMicrotask(()=>{{if(e.defaultPrevented){{delete f.dataset.submitting;return;}}const b=e.submitter||f.querySelector('button[type=submit],button:not([type])');if(b){{b.disabled=true;b.dataset.originalText=b.textContent;b.textContent='처리 중…';}}}});}});}})();</script>
    </main></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "AudioDSP/1.2"

    def send_bytes(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            status=status,
            content_type="application/json; charset=utf-8",
        )

    def send_fir(self, path: Path, rear_mode: str, allowed_root: Path = WEB_PROFILE_DIR) -> None:
        resolved = path.resolve(strict=True)
        allowed = allowed_root.resolve(strict=True)
        if os.path.commonpath((str(resolved), str(allowed))) != str(allowed):
            raise RuntimeError("FIR path is outside the managed profile directory")
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
                self.send_fir(Path(path), resolved["effective_rear_mode"])
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if parsed.path == "/api/status":
            self.send_bytes((json.dumps(cached_status(), ensure_ascii=False, indent=2) + "\n").encode(), content_type="application/json; charset=utf-8")
            return
        if parsed.path == "/api/volume":
            self.send_json(read_output_volume())
            return
        if parsed.path == "/api/measurement/status":
            self.send_bytes((json.dumps(measurement_status(), ensure_ascii=False, indent=2) + "\n").encode(), content_type="application/json; charset=utf-8")
            return
        if parsed.path == "/api/targets":
            self.send_bytes((json.dumps(measurement("targets"), ensure_ascii=False) + "\n").encode(), content_type="application/json; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self.send_bytes((json.dumps(system_health(), ensure_ascii=False) + "\n").encode(), content_type="application/json; charset=utf-8")
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
                self.redirect(f"Rear 모드 적용: {result['effective_rear_mode']} / {result['convolution_channels']}ch convolution", "/settings")
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
                self.redirect(f"{fields['profile']} Woofer trim 적용: {result['woofer_trim_db']} dB", "/settings")
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
                measurement("new", fields["mode"], fields["orientation"], fields["level_dbfs"], fields["sweep_seconds"])
                self.redirect("측정 Session 생성 완료 · 먼저 레벨 검사를 진행하세요.", "/measure")
                return
            if parsed.path == "/measurement/configure":
                fields = self.read_urlencoded()
                result = measurement("configure", fields["mode"], fields["orientation"], fields["level_dbfs"], fields["sweep_seconds"])
                reason = (result.get("invalidation") or {}).get("reason", "변경 없음")
                self.redirect(f"측정 설정 적용: {reason}. 영향을 받지 않는 값은 유지했습니다.", "/measure")
                return
            if parsed.path == "/measurement/level":
                measurement("start-level")
                self.redirect("레벨 검사를 시작했습니다. 실행 확정 시 기존 위치 측정과 FIR 결과를 초기화했습니다.", "/measure")
                return
            if parsed.path == "/measurement/position":
                measurement("start-position")
                self.redirect("개별 측정을 시작했습니다. 측정 중 DSP bypass / U7 입력 OFF입니다.", "/measure")
                return
            if parsed.path == "/measurement/restart-positions":
                measurement("restart-positions")
                self.redirect("기존 위치 측정 이후 결과를 초기화하고 위치 1부터 재측정을 시작했습니다.", "/measure")
                return
            if parsed.path == "/measurement/validation":
                measurement("start-validation")
                self.redirect("중앙 위치 합산 검증을 시작했습니다.", "/measure")
                return
            if parsed.path == "/measurement/build":
                fields = self.read_urlencoded()
                measurement("start-build", fields["target"], fields["preset"], fields["woofer_trim_db"], fields["phase_mode"], fields["phase_cutoff"], fields["spatial_mode"], fields["bass_tilt_db"], fields["treble_tilt_db"], fields["correction_low_hz"], fields["correction_high_hz"], fields["max_boost_db"], fields["max_cut_db"], fields["mimo_high_hz"], fields["mimo_strength"], fields["mimo_support_penalty_db"])
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
                self.redirect(f"UMIK calibration 적용: {metadata['orientation']}° / serial {metadata['serial']}", "/measure")
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
