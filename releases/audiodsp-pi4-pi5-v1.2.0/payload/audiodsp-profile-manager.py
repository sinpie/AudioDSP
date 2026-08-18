#!/usr/bin/env python3
"""Validate FIR WAV files and generate the active AudioDSP CamillaDSP profile."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
from typing import Any


def environment(suffix: str, default: str) -> str:
    """Prefer AudioDSP identifiers; accept legacy GSONIC_* during migration."""
    return os.environ.get(f"AUDIODSP_{suffix}", os.environ.get(f"GSONIC_{suffix}", default))


CAMILLADSP = Path(environment("CAMILLADSP", "/usr/local/bin/camilladsp"))
AMIXER = environment("AMIXER", "/usr/bin/amixer")
U7_MIXER = environment("U7_MIXER", "hw:U7")
CONFIG_DIR = Path(environment("CONFIG_DIR", "/etc/camilladsp"))
CONFIG_PATH = CONFIG_DIR / "camilladsp.yml"
PROFILE_DIR = CONFIG_DIR / "profiles"
MIMO_DIR = PROFILE_DIR / "mimo"
MIMO_MANIFESTS = {
    "speaker": MIMO_DIR / "Speaker_MIMO.json",
    "headphone": MIMO_DIR / "Headphone_MIMO.json",
}
FACTORY_FRONT = PROFILE_DIR / "Factory_Speaker_Front_LR.wav"
STATE_DIR = Path(environment("STATE_DIR", "/var/lib/audiodsp"))
SETTINGS_PATH = STATE_DIR / "profile-settings.json"
LEGACY_STATE_PATH = STATE_DIR / "output-profile"
BACKUP_DIR = STATE_DIR / "profile-backups"
LOCK_PATH = Path(environment("LOCK_PATH", "/run/audiodsp-profile-manager.lock"))
SELECTOR_STATE_PATH = Path(environment("SELECTOR_STATE_PATH", "/var/lib/audiodsp/u7-selector-state.json"))
PREVIEW_STATE_PATH = Path(environment("PREVIEW_STATE_PATH", "/var/lib/audiodsp/fir-preview.json"))
MAX_WAV_BYTES = 32 * 1024 * 1024
MAX_FIR_FRAMES = 262144
DISABLE_SERVICE_RESTART = environment("DISABLE_SERVICE_RESTART", "0") == "1"
ALLOWED_CHUNKSIZES = (512, 1024, 2048, 4096)
ALLOWED_WOOFER_TRIMS = tuple(range(-18, 1))
ALLOWED_OUTPUT_VOLUMES_DB = tuple(range(-60, 1))
U7_VOLUME_RAW_MAX = 127

PROFILE_FILES = {
    "speaker": {
        "front": PROFILE_DIR / "Speaker_Front_LR.wav",
        "rear": PROFILE_DIR / "Speaker_Rear_LR.wav",
    },
    "headphone": {
        "front": PROFILE_DIR / "Headphone_Front_LR.wav",
        "rear": PROFILE_DIR / "Headphone_Rear_LR.wav",
    },
}
SNAPSHOT_FILES = {
    "Factory_Speaker_Front_LR.wav": PROFILE_DIR / "Factory_Speaker_Front_LR.wav",
    "Speaker_Front_LR.wav": PROFILE_FILES["speaker"]["front"],
    "Speaker_Rear_LR.wav": PROFILE_FILES["speaker"]["rear"],
    "Headphone_Front_LR.wav": PROFILE_FILES["headphone"]["front"],
    "Headphone_Rear_LR.wav": PROFILE_FILES["headphone"]["rear"],
}
MIMO_OUTPUT_NAMES = (
    "MIMO_Front_Left_LR_32768.wav",
    "MIMO_Front_Right_LR_32768.wav",
    "MIMO_Rear_Left_LR_32768.wav",
    "MIMO_Rear_Right_LR_32768.wav",
)
MIMO_SNAPSHOT_FILES = {
    **{f"mimo/{name}": MIMO_DIR / name for name in MIMO_OUTPUT_NAMES},
    "mimo/Speaker_MIMO.json": MIMO_MANIFESTS["speaker"],
    "mimo/Headphone_MIMO.json": MIMO_MANIFESTS["headphone"],
}

DEFAULT_SETTINGS = {
    "requested_profile": "speaker",
    "chunksize": 2048,
    "output_volume_db": -10,
    "bypass": {
        "speaker": False,
        "headphone": False,
    },
    "mimo_enabled": {
        "speaker": False,
        "headphone": False,
    },
    "rear_mode": {
        "speaker": "copy_front",
        "headphone": "copy_front",
    },
    "woofer_trim_db": {
        "speaker": 0,
        "headphone": 0,
    },
}


class ProfileError(RuntimeError):
    pass


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def normalize_settings(saved: dict[str, Any], strict: bool = False) -> dict[str, Any]:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if not isinstance(saved, dict):
        raise ProfileError("Settings must be a JSON object.")
    requested = saved.get("requested_profile")
    if requested in PROFILE_FILES:
        settings["requested_profile"] = requested
    elif strict and "requested_profile" in saved:
        raise ProfileError("Invalid requested_profile in backup.")
    saved_chunksize = saved.get("chunksize")
    if saved_chunksize in ALLOWED_CHUNKSIZES:
        settings["chunksize"] = saved_chunksize
    elif strict and "chunksize" in saved:
        raise ProfileError("Invalid chunksize in backup.")
    saved_volume = saved.get("output_volume_db")
    if isinstance(saved_volume, int) and not isinstance(saved_volume, bool) and saved_volume in ALLOWED_OUTPUT_VOLUMES_DB:
        settings["output_volume_db"] = saved_volume
    elif strict and "output_volume_db" in saved:
        raise ProfileError("Invalid output_volume_db in backup; expected an integer from -60 to 0 dB.")
    for key, allowed in (("rear_mode", ("copy_front", "separate")), ("bypass", (False, True)), ("mimo_enabled", (False, True)), ("woofer_trim_db", ALLOWED_WOOFER_TRIMS)):
        values = saved.get(key, {})
        if strict and not isinstance(values, dict):
            raise ProfileError(f"Invalid {key} object in backup.")
        if not isinstance(values, dict):
            continue
        for profile in PROFILE_FILES:
            value = values.get(profile)
            if key in ("bypass", "mimo_enabled"):
                valid = isinstance(value, bool)
            elif key == "woofer_trim_db":
                valid = isinstance(value, int) and not isinstance(value, bool) and value in allowed
            else:
                valid = value in allowed
            if valid:
                settings[key][profile] = value
            elif strict and profile in values:
                raise ProfileError(f"Invalid {key}.{profile} in backup.")
    return settings


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.is_file():
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Invalid settings file: {exc}") from exc
    return normalize_settings(saved)


def validate_settings_file(path: Path) -> dict[str, Any]:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Invalid settings JSON: {exc}") from exc
    normalized = normalize_settings(saved, strict=True)
    return {
        "normalized": normalized,
        "ignored_unknown_keys": sorted(set(saved) - set(DEFAULT_SETTINGS)),
    }


def save_settings(settings: dict[str, Any]) -> None:
    payload = (json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    atomic_write(SETTINGS_PATH, payload)
    atomic_write(LEGACY_STATE_PATH, (settings["requested_profile"] + "\n").encode())


def validate_wav(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProfileError(f"WAV file is missing: {path}") from exc
    if size < 44 or size > MAX_WAV_BYTES:
        raise ProfileError(f"WAV size must be 44 bytes to {MAX_WAV_BYTES // (1024 * 1024)} MiB: {size}")

    fmt_data: bytes | None = None
    data_offset: int | None = None
    data_size: int | None = None
    with path.open("rb") as handle:
        if handle.read(4) != b"RIFF":
            raise ProfileError("Only little-endian RIFF/WAVE files are supported.")
        handle.read(4)
        if handle.read(4) != b"WAVE":
            raise ProfileError("The uploaded file is not WAVE audio.")
        while handle.tell() + 8 <= size:
            chunk_id = handle.read(4)
            chunk_size_bytes = handle.read(4)
            if len(chunk_size_bytes) != 4:
                break
            chunk_size = struct.unpack("<I", chunk_size_bytes)[0]
            chunk_start = handle.tell()
            if chunk_start + chunk_size > size:
                raise ProfileError("WAV chunk extends past the end of the file.")
            if chunk_id == b"fmt ":
                fmt_data = handle.read(chunk_size)
            elif chunk_id == b"data":
                data_offset = chunk_start
                data_size = chunk_size
            handle.seek(chunk_start + chunk_size + (chunk_size & 1))

    if fmt_data is None or len(fmt_data) < 16 or data_offset is None or not data_size:
        raise ProfileError("WAV must contain valid fmt and non-empty data chunks.")
    format_code, channels, rate, _byte_rate, block_align, bits = struct.unpack("<HHIIHH", fmt_data[:16])
    if format_code == 0xFFFE:
        if len(fmt_data) < 40:
            raise ProfileError("Invalid WAVE_FORMAT_EXTENSIBLE header.")
        format_code = struct.unpack("<H", fmt_data[24:26])[0]
    if format_code not in (1, 3):
        raise ProfileError(f"Only PCM or IEEE-float WAV is supported; format code={format_code}.")
    if channels != 2:
        raise ProfileError(f"FIR WAV must be stereo L/R; channels={channels}.")
    if rate != 48000:
        raise ProfileError(f"FIR WAV must be 48 kHz; rate={rate}.")
    valid_bits = (16, 24, 32) if format_code == 1 else (32, 64)
    if bits not in valid_bits:
        raise ProfileError(f"Unsupported WAV bit depth for format {format_code}: {bits}.")
    expected_align = channels * ((bits + 7) // 8)
    if block_align != expected_align or data_size % block_align:
        raise ProfileError("WAV block alignment or data length is invalid.")

    if format_code == 3:
        unpack_code = "f" if bits == 32 else "d"
        sample_size = bits // 8
        with path.open("rb") as handle:
            handle.seek(data_offset)
            remaining = data_size
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if len(chunk) % sample_size:
                    raise ProfileError("Floating-point WAV data is misaligned.")
                for (value,) in struct.iter_unpack("<" + unpack_code, chunk):
                    if not math.isfinite(value):
                        raise ProfileError("Floating-point FIR contains NaN or infinity.")
                remaining -= len(chunk)

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    frames = data_size // block_align
    if frames > MAX_FIR_FRAMES:
        raise ProfileError(f"FIR is too long; maximum frames={MAX_FIR_FRAMES}, uploaded={frames}.")
    return {
        "path": str(path),
        "bytes": size,
        "frames": frames,
        "duration_ms": round(frames * 1000 / rate, 3),
        "sample_rate": rate,
        "channels": channels,
        "format": "float" if format_code == 3 else "pcm",
        "bits": bits,
        "sha256": digest.hexdigest(),
    }


def platform_capability() -> dict[str, Any]:
    """MIMO needs eight 32768-tap convolution paths and is intentionally Pi 4/5 only."""
    override = environment("PLATFORM_CLASS", "").strip().lower()
    model = ""
    try:
        model = Path("/proc/device-tree/model").read_text(encoding="utf-8", errors="replace").rstrip("\x00\n")
    except OSError:
        model = platform.machine()
    lowered = (override or model).lower()
    supported = override in ("test", "pi4", "pi5") or "raspberry pi 4" in lowered or "raspberry pi 5" in lowered
    pi2 = override == "pi2" or "raspberry pi 2" in lowered
    reason = None
    if not supported:
        reason = "Pi 4/5 전용: 2입력×4출력의 32768탭 convolution 8개는 Pi 2 실시간 예산을 초과합니다."
    return {
        "platform": override or model,
        "mimo_supported": supported,
        "pi2": pi2,
        "required_chunksize_min": 1024,
        "convolution_paths": 8,
        "reason": reason,
    }


def validate_mimo_bank(manifest_path: Path) -> dict[str, Any]:
    try:
        manifest_path = manifest_path.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"MIMO manifest 오류: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "AudioDSP MIMO Bank" or manifest.get("schema_version") != 1:
        raise ProfileError("지원하지 않는 MIMO manifest 형식입니다.")
    expected = {"sample_rate": 48000, "taps": 32768, "inputs": 2, "outputs": 4}
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ProfileError(f"MIMO manifest {key}는 {value}이어야 합니다.")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 4:
        raise ProfileError("MIMO bank에는 정확히 네 출력 WAV가 필요합니다.")
    root = manifest_path.parent
    validated: list[dict[str, Any]] = []
    seen_outputs: set[int] = set()
    for item in files:
        if not isinstance(item, dict) or item.get("output") not in range(4):
            raise ProfileError("MIMO 출력 번호는 0..3이어야 합니다.")
        output = int(item["output"])
        if output in seen_outputs:
            raise ProfileError("MIMO 출력 번호가 중복되었습니다.")
        seen_outputs.add(output)
        filename = item.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ProfileError("MIMO WAV는 manifest와 같은 디렉터리의 안전한 파일명이어야 합니다.")
        path = (root / filename).resolve(strict=True)
        if path.parent != root:
            raise ProfileError("MIMO WAV 경로가 manifest 디렉터리를 벗어났습니다.")
        metadata = validate_wav(path)
        if metadata["frames"] != 32768 or metadata["sample_rate"] != 48000 or metadata["channels"] != 2 or metadata["format"] != "float" or metadata["bits"] != 32:
            raise ProfileError(f"{filename}: stereo 48 kHz float32, 정확히 32768탭이어야 합니다.")
        if item.get("sha256") != metadata["sha256"]:
            raise ProfileError(f"{filename}: SHA-256가 manifest와 다릅니다.")
        validated.append({"output": output, "label": item.get("label"), "file": filename, "path": path, "metadata": metadata})
    validated.sort(key=lambda value: value["output"])
    if seen_outputs != set(range(4)):
        raise ProfileError("MIMO bank 출력 0..3이 모두 필요합니다.")
    validation = manifest.get("self_validation", {})
    if not isinstance(validation, dict) or not validation.get("overall_pass"):
        raise ProfileError("MIMO 자체 검증을 통과하지 않은 bank는 적용할 수 없습니다.")
    return {"manifest": manifest, "manifest_path": manifest_path, "files": validated}


def managed_mimo(profile: str) -> dict[str, Any] | None:
    path = MIMO_MANIFESTS[profile]
    return validate_mimo_bank(path) if path.is_file() else None


def resolve_profile(settings: dict[str, Any]) -> dict[str, Any]:
    requested = settings["requested_profile"]
    other = "headphone" if requested == "speaker" else "speaker"
    if settings["bypass"][requested] or PROFILE_FILES[requested]["front"].is_file():
        effective = requested
    elif settings["bypass"][other] or PROFILE_FILES[other]["front"].is_file():
        effective = other
    elif FACTORY_FRONT.is_file():
        effective = "factory"
    else:
        raise ProfileError("No Front L/R FIR exists in either profile and the factory FIR is missing.")

    if effective == "factory":
        front = FACTORY_FRONT
        rear = None
        configured_mode = "copy_front"
        bypass = False
        woofer_trim_db = 0
    else:
        bypass = settings["bypass"][effective]
        front = None if bypass else PROFILE_FILES[effective]["front"]
        rear_candidate = PROFILE_FILES[effective]["rear"]
        configured_mode = settings["rear_mode"][effective]
        rear = rear_candidate if not bypass and configured_mode == "separate" and rear_candidate.is_file() else None
        woofer_trim_db = settings["woofer_trim_db"][effective]

    mode = "bypass" if bypass else ("separate" if rear is not None else "copy_front")
    resolved = {
        "requested_profile": requested,
        "chunksize": settings["chunksize"],
        "effective_profile": effective,
        "front_path": front,
        "rear_path": rear,
        "configured_rear_mode": configured_mode,
        "effective_rear_mode": mode,
        "bypass": bypass,
        "convolution_channels": 0 if bypass else (4 if mode == "separate" else 2),
        "woofer_trim_db": woofer_trim_db,
        "mimo_paths": None,
        "mimo_manifest_path": None,
        "mimo_unavailable_reason": None,
    }
    if effective in MIMO_MANIFESTS and not bypass and settings.get("mimo_enabled", {}).get(effective, False):
        capability = platform_capability()
        if capability["mimo_supported"]:
            bank = managed_mimo(effective)
            if bank is None:
                raise ProfileError(f"{effective} MIMO가 켜졌지만 설치된 bank가 없습니다.")
            resolved.update({
                "chunksize": max(1024, resolved["chunksize"]),
                "effective_rear_mode": "mimo_2x4",
                "convolution_channels": 8,
                "woofer_trim_db": 0,
                "mimo_paths": [item["path"] for item in bank["files"]],
                "mimo_manifest_path": bank["manifest_path"],
                "mimo_topology": bank["manifest"].get("topology"),
            })
        else:
            resolved["mimo_unavailable_reason"] = capability["reason"]
    return resolved


def mixer_yaml(woofer_trim_db: int) -> str:
    return f"""mixers:
  stereo_to_front_and_rear:
    description: "Duplicate stereo to U7 Front L/R and Rear L/R"
    labels: ["Front Left", "Front Right", "Rear Left", "Rear Right"]
    channels:
      in: 2
      out: 4
    mapping:
      - dest: 0
        sources:
          - channel: 0
            gain: 0
            inverted: false
            mute: false
      - dest: 1
        sources:
          - channel: 1
            gain: 0
            inverted: false
            mute: false
      - dest: 2
        sources:
          - channel: 0
            gain: {woofer_trim_db}
            inverted: false
            mute: false
      - dest: 3
        sources:
          - channel: 1
            gain: {woofer_trim_db}
            inverted: false
            mute: false
"""


def build_config(resolved: dict[str, Any]) -> bytes:
    chunksize = resolved["chunksize"]
    woofer_trim_db = resolved["woofer_trim_db"]
    mimo_paths = resolved.get("mimo_paths")
    if mimo_paths:
        if len(mimo_paths) != 4:
            raise ProfileError("MIMO config에는 네 출력 경로가 필요합니다.")
        names = ("front_left", "front_right", "rear_left", "rear_right")
        filter_lines = []
        pipeline_lines = []
        for output, (name, path) in enumerate(zip(names, mimo_paths)):
            for source in range(2):
                filter_name = f"mimo_{name}_from_{'left' if source == 0 else 'right'}"
                filter_lines.extend([
                    f"  {filter_name}:",
                    "    type: Conv",
                    "    parameters:",
                    "      type: Wav",
                    f"      filename: {json.dumps(str(path))}",
                    f"      channel: {source}",
                ])
                pipeline_lines.extend([
                    "  - type: Filter",
                    f"    channels: [{output * 2 + source}]",
                    f"    names: [{json.dumps(filter_name)}]",
                ])
        expand_mapping = []
        for output in range(4):
            for source in range(2):
                expand_mapping.extend([
                    f"      - dest: {output * 2 + source}",
                    "        sources:",
                    f"          - channel: {source}",
                    "            gain: 0",
                    "            inverted: false",
                    "            mute: false",
                ])
        sum_mapping = []
        for output in range(4):
            sum_mapping.extend([
                f"      - dest: {output}",
                "        sources:",
                f"          - channel: {output * 2}",
                "            gain: 0",
                "            inverted: false",
                "            mute: false",
                f"          - channel: {output * 2 + 1}",
                "            gain: 0",
                "            inverted: false",
                "            mute: false",
            ])
        title = f"AudioDSP {resolved['effective_profile']} / MIMO 2x4 / Xonar U7"
        config = f"""---
title: {json.dumps(title)}
description: "Robust 2-input x 4-output MIMO FIR bank; eight 32768-tap convolution paths"

devices:
  samplerate: 48000
  chunksize: {chunksize}
  queuelimit: 4
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "__CAPTURE_DEVICE__"
    format: S32_LE
    labels: ["Left", "Right"]
  playback:
    type: Alsa
    channels: 4
    device: "__PLAYBACK_DEVICE__"
    format: S32_LE

filters:
{chr(10).join(filter_lines)}

mixers:
  mimo_expand_2_to_8:
    channels:
      in: 2
      out: 8
    mapping:
{chr(10).join(expand_mapping)}
  mimo_sum_8_to_4:
    channels:
      in: 8
      out: 4
    mapping:
{chr(10).join(sum_mapping)}

pipeline:
  - type: Mixer
    name: "mimo_expand_2_to_8"
{chr(10).join(pipeline_lines)}
  - type: Mixer
    name: "mimo_sum_8_to_4"
"""
        return config.encode()
    if resolved["bypass"]:
        title = f"AudioDSP {resolved['effective_profile']} / bypass / Xonar U7"
        config = f"""---
title: {json.dumps(title)}
description: "DSP bypass: raw stereo copied to Xonar U7 Front L/R and Rear L/R"

devices:
  samplerate: 48000
  chunksize: {chunksize}
  queuelimit: 4
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "__CAPTURE_DEVICE__"
    format: S32_LE
    labels: ["Left", "Right"]
  playback:
    type: Alsa
    channels: 4
    device: "__PLAYBACK_DEVICE__"
    format: S32_LE

filters: {{}}

{mixer_yaml(woofer_trim_db)}
pipeline:
  - type: Mixer
    name: "stereo_to_front_and_rear"
"""
        return config.encode()
    front = json.dumps(str(resolved["front_path"]))
    rear = json.dumps(str(resolved["rear_path"])) if resolved["rear_path"] else None
    title = f"AudioDSP {resolved['effective_profile']} / {resolved['effective_rear_mode']} / Xonar U7"
    header = f"""---
title: {json.dumps(title)}
description: "Selectable Front/Rear FIR profile with Xonar U7 HID switching"

devices:
  samplerate: 48000
  chunksize: {chunksize}
  queuelimit: 4
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "__CAPTURE_DEVICE__"
    format: S32_LE
    labels: ["Left", "Right"]
  playback:
    type: Alsa
    channels: 4
    device: "__PLAYBACK_DEVICE__"
    format: S32_LE

filters:
  front_left:
    type: Conv
    parameters:
      type: Wav
      filename: {front}
      channel: 0
  front_right:
    type: Conv
    parameters:
      type: Wav
      filename: {front}
      channel: 1
"""
    if rear is None:
        pipeline = """
pipeline:
  - type: Filter
    channels: [0]
    names: ["front_left"]
  - type: Filter
    channels: [1]
    names: ["front_right"]
  - type: Mixer
    name: "stereo_to_front_and_rear"
"""
        return (header + "\n" + mixer_yaml(woofer_trim_db) + pipeline).encode()

    extra_filters = f"""  rear_left:
    type: Conv
    parameters:
      type: Wav
      filename: {rear}
      channel: 0
  rear_right:
    type: Conv
    parameters:
      type: Wav
      filename: {rear}
      channel: 1

"""
    pipeline = """pipeline:
  - type: Mixer
    name: "stereo_to_front_and_rear"
  - type: Filter
    channels: [0]
    names: ["front_left"]
  - type: Filter
    channels: [1]
    names: ["front_right"]
  - type: Filter
    channels: [2]
    names: ["rear_left"]
  - type: Filter
    channels: [3]
    names: ["rear_right"]
"""
    return (header + extra_filters + mixer_yaml(woofer_trim_db) + "\n" + pipeline).encode()


def service_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "camilladsp.service"], check=False, timeout=3
    )
    return result.returncode == 0


def restart_camilladsp() -> None:
    result = subprocess.run(["systemctl", "restart", "camilladsp.service"], check=False, timeout=20)
    if result.returncode != 0:
        raise ProfileError("systemctl restart camilladsp failed.")
    stable_since: float | None = None
    for _ in range(80):
        active = service_active()
        running = subprocess.run(
            ["pgrep", "-x", "camilladsp"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).returncode == 0
        now = time.monotonic()
        if active and running:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= 1.0:
                return
        else:
            stable_since = None
        time.sleep(0.1)
    raise ProfileError("CamillaDSP did not remain active for one second after profile update.")


def check_config(config: bytes) -> None:
    descriptor, check_name = tempfile.mkstemp(prefix="camilladsp-profile-check-", suffix=".yml", dir="/tmp")
    os.close(descriptor)
    check_path = Path(check_name)
    try:
        check_path.write_bytes(config)
        checked = subprocess.run(
            [str(CAMILLADSP), "--check", str(check_path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        if checked.returncode != 0:
            raise ProfileError(f"CamillaDSP rejected the generated config: {checked.stdout.strip()}")
    finally:
        check_path.unlink(missing_ok=True)


def apply_settings(settings: dict[str, Any], restart: bool = True) -> dict[str, Any]:
    resolved = resolve_profile(settings)
    front_meta = validate_wav(resolved["front_path"]) if resolved["front_path"] else None
    rear_meta = validate_wav(resolved["rear_path"]) if resolved["rear_path"] else None
    mimo_meta = validate_mimo_bank(resolved["mimo_manifest_path"]) if resolved.get("mimo_manifest_path") else None
    config = build_config(resolved)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    check_config(config)

    old_config = CONFIG_PATH.read_bytes() if CONFIG_PATH.is_file() else None
    old_settings = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.is_file() else None
    changed = old_config != config
    try:
        atomic_write(CONFIG_PATH, config)
        save_settings(settings)
        if restart and changed and not DISABLE_SERVICE_RESTART:
            restart_camilladsp()
        PREVIEW_STATE_PATH.unlink(missing_ok=True)
    except Exception:
        if old_config is None:
            CONFIG_PATH.unlink(missing_ok=True)
        else:
            atomic_write(CONFIG_PATH, old_config)
        if old_settings is None:
            SETTINGS_PATH.unlink(missing_ok=True)
        else:
            atomic_write(SETTINGS_PATH, old_settings)
        if restart and old_config is not None and not DISABLE_SERVICE_RESTART:
            subprocess.run(["systemctl", "restart", "camilladsp.service"], check=False)
        raise

    resolved["front"] = front_meta
    resolved["rear"] = rear_meta
    resolved["mimo"] = mimo_meta
    resolved["config_changed"] = changed
    return serializable(resolved)


def serializable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serializable(item) for item in value]
    return value


def selector_status() -> dict[str, Any]:
    result: dict[str, Any] = {"profile": None, "state_byte": None, "source": "not_detected", "stale": True}
    if not SELECTOR_STATE_PATH.is_file():
        return result
    try:
        saved = json.loads(SELECTOR_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["source"] = "invalid_state_file"
        return result
    profile = saved.get("profile")
    if profile not in PROFILE_FILES:
        result["source"] = "invalid_profile"
        return result
    current_boot = None
    try:
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        pass
    result.update(saved)
    result["stale"] = not current_boot or saved.get("boot_id") != current_boot
    return result


def current_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None


def preview_status() -> dict[str, Any]:
    result: dict[str, Any] = {"active": False, "profile": None, "stale": False}
    if not PREVIEW_STATE_PATH.is_file():
        return result
    try:
        saved = json.loads(PREVIEW_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["error"] = "invalid preview state"
        return result
    result.update(saved)
    result["active"] = bool(saved.get("active"))
    boot_id = current_boot_id()
    result["stale"] = not boot_id or saved.get("boot_id") != boot_id
    return result


def resolve_preview(settings: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
    """Describe the temporary config that is actually running, not the saved profile."""
    profile = str(preview.get("profile"))
    mode = str(preview.get("mode"))
    if profile not in PROFILE_FILES:
        raise ProfileError("Invalid active preview profile.")
    if mode == "mimo_2x4":
        manifest = preview.get("manifest")
        if not manifest:
            raise ProfileError("Active MIMO preview manifest is missing.")
        bank = validate_mimo_bank(Path(str(manifest)))
        return {
            "requested_profile": settings["requested_profile"],
            "chunksize": max(1024, settings["chunksize"]),
            "effective_profile": profile,
            "front_path": None,
            "rear_path": None,
            "configured_rear_mode": mode,
            "effective_rear_mode": mode,
            "bypass": False,
            "convolution_channels": 8,
            "woofer_trim_db": 0,
            "mimo_paths": [item["path"] for item in bank["files"]],
            "mimo_manifest_path": bank["manifest_path"],
            "mimo_topology": bank["manifest"].get("topology"),
            "preview_active": True,
        }
    if mode not in ("copy_front", "separate"):
        raise ProfileError("Invalid active preview mode.")
    front = preview.get("front") or {}
    rear = preview.get("rear") or {}
    front_path = Path(str(front.get("path"))) if front.get("path") else None
    rear_path = Path(str(rear.get("path"))) if mode == "separate" and rear.get("path") else None
    if front_path is None or not front_path.is_file():
        raise ProfileError("Active preview Front FIR is missing.")
    if mode == "separate" and (rear_path is None or not rear_path.is_file()):
        raise ProfileError("Active preview Rear FIR is missing.")
    return {
        "requested_profile": settings["requested_profile"],
        "chunksize": settings["chunksize"],
        "effective_profile": profile,
        "front_path": front_path,
        "rear_path": rear_path,
        "configured_rear_mode": mode,
        "effective_rear_mode": mode,
        "bypass": False,
        "convolution_channels": 4 if rear_path is not None else 2,
        "woofer_trim_db": int(preview.get("woofer_trim_db", 0)),
        "mimo_paths": None,
        "mimo_manifest_path": None,
        "mimo_unavailable_reason": None,
        "preview_active": True,
    }


def status() -> dict[str, Any]:
    settings = load_settings()
    resolved = resolve_profile(settings)
    preview = preview_status()
    if preview.get("active") and not preview.get("stale"):
        resolved = resolve_preview(settings, preview)
    files: dict[str, Any] = {}
    for profile, bands in PROFILE_FILES.items():
        files[profile] = {}
        for band, path in bands.items():
            if path.is_file():
                try:
                    files[profile][band] = validate_wav(path)
                except ProfileError as exc:
                    files[profile][band] = {"path": str(path), "error": str(exc)}
            else:
                files[profile][band] = None
    mimo = {}
    for profile, path in MIMO_MANIFESTS.items():
        if path.is_file():
            try:
                bank = validate_mimo_bank(path)
                mimo[profile] = {
                    "manifest": str(path),
                    "topology": bank["manifest"].get("topology"),
                    "files": [item["metadata"] for item in bank["files"]],
                    "valid": True,
                }
            except ProfileError as exc:
                mimo[profile] = {"manifest": str(path), "valid": False, "error": str(exc)}
        else:
            mimo[profile] = None
    return serializable({"settings": settings, "resolved": resolved, "files": files, "mimo": mimo, "capabilities": platform_capability(), "u7_selector": selector_status(), "preview": preview})


def activate(profile: str, restart: bool = True) -> dict[str, Any]:
    if profile not in PROFILE_FILES:
        raise ProfileError(f"Unknown profile: {profile}")
    settings = load_settings()
    settings["requested_profile"] = profile
    return apply_settings(settings, restart=restart)


def set_rear_mode(profile: str, mode: str, restart: bool = True) -> dict[str, Any]:
    if profile not in PROFILE_FILES or mode not in ("copy_front", "separate"):
        raise ProfileError("Invalid profile or rear mode.")
    settings = load_settings()
    settings["rear_mode"][profile] = mode
    return apply_settings(settings, restart=restart)


def set_bypass(profile: str, enabled: bool, restart: bool = True) -> dict[str, Any]:
    if profile not in PROFILE_FILES:
        raise ProfileError("Invalid profile for bypass.")
    settings = load_settings()
    settings["bypass"][profile] = enabled
    return apply_settings(settings, restart=restart)


def set_mimo_enabled(profile: str, enabled: bool, restart: bool = True) -> dict[str, Any]:
    if profile not in PROFILE_FILES:
        raise ProfileError("Invalid profile for MIMO.")
    if enabled:
        capability = platform_capability()
        if not capability["mimo_supported"]:
            raise ProfileError(capability["reason"] or "MIMO is unavailable on this platform.")
        if managed_mimo(profile) is None:
            raise ProfileError(f"{profile}에 설치된 MIMO bank가 없습니다.")
    settings = load_settings()
    settings["mimo_enabled"][profile] = enabled
    if enabled:
        settings["bypass"][profile] = False
        settings["chunksize"] = max(1024, settings["chunksize"])
    return apply_settings(settings, restart=restart)


def set_woofer_trim(profile: str, trim_db: int, restart: bool = True) -> dict[str, Any]:
    if profile not in PROFILE_FILES or trim_db not in ALLOWED_WOOFER_TRIMS:
        raise ProfileError(f"Woofer trim must be an integer from {ALLOWED_WOOFER_TRIMS[0]} to 0 dB.")
    settings = load_settings()
    settings["woofer_trim_db"][profile] = trim_db
    return apply_settings(settings, restart=restart)


def set_chunksize(chunksize: int, restart: bool = True) -> dict[str, Any]:
    if chunksize not in ALLOWED_CHUNKSIZES:
        raise ProfileError(f"Invalid chunksize; choose one of {ALLOWED_CHUNKSIZES}.")
    settings = load_settings()
    settings["chunksize"] = chunksize
    return apply_settings(settings, restart=restart)


def apply_output_volume(volume_db: int) -> dict[str, Any]:
    """Apply one global Xonar U7 hardware volume to every playback channel."""
    if volume_db not in ALLOWED_OUTPUT_VOLUMES_DB:
        raise ProfileError("Output volume must be an integer from -60 to 0 dB.")
    raw = U7_VOLUME_RAW_MAX + volume_db
    result = subprocess.run(
        [AMIXER, "-D", U7_MIXER, "set", "PCM,0", str(raw)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )
    if result.returncode != 0:
        raise ProfileError(result.stdout.strip() or "Xonar U7 output volume write failed.")
    return {"hardware_applied": True, "raw": raw, "db": volume_db, "mixer": U7_MIXER}


def set_output_volume(volume_db: int, apply_hardware: bool = True) -> dict[str, Any]:
    """Persist the global output level without restarting CamillaDSP."""
    if volume_db not in ALLOWED_OUTPUT_VOLUMES_DB:
        raise ProfileError("Output volume must be an integer from -60 to 0 dB.")
    settings = load_settings()
    settings["output_volume_db"] = volume_db
    save_settings(settings)
    volume = {"hardware_applied": False, "raw": U7_VOLUME_RAW_MAX + volume_db, "db": volume_db, "mixer": U7_MIXER}
    if apply_hardware:
        try:
            volume = apply_output_volume(volume_db)
        except (OSError, subprocess.SubprocessError, ProfileError) as exc:
            volume["warning"] = str(exc)
    result = status()
    result["output_volume"] = volume
    return serializable(result)


def upload(profile: str, band: str, source: Path, original_name: str) -> dict[str, Any]:
    if profile not in PROFILE_FILES or band not in ("front", "rear"):
        raise ProfileError("Invalid upload profile or band.")
    metadata = validate_wav(source)
    target = PROFILE_FILES[profile][band]
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    previous = target.read_bytes() if target.is_file() else None
    if previous is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"{profile}-{band}-{stamp}-{hashlib.sha256(previous).hexdigest()[:12]}.wav"
        atomic_write(backup, previous)
    temporary = target.with_name(f".{target.name}.upload-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        settings = load_settings()
        settings["mimo_enabled"][profile] = False
        applied = apply_settings(settings, restart=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, previous)
        try:
            apply_settings(load_settings(), restart=True)
        except Exception:
            pass
        raise
    applied["uploaded"] = {
        "profile": profile,
        "band": band,
        "original_name": original_name,
        "metadata": metadata,
    }
    return applied


def preview_pair(profile: str, front_source: Path, rear_source: Path | None, woofer_trim_db: int = 0) -> dict[str, Any]:
    """Temporarily audition generated FIRs without modifying settings or profile WAVs."""
    if profile not in PROFILE_FILES:
        raise ProfileError("Invalid preview profile.")
    if woofer_trim_db not in ALLOWED_WOOFER_TRIMS:
        raise ProfileError("Invalid preview woofer trim.")
    front_meta = validate_wav(front_source)
    rear_meta = validate_wav(rear_source) if rear_source is not None else None
    settings = load_settings()
    mode = "separate" if rear_source is not None else "copy_front"
    resolved = {
        "requested_profile": settings["requested_profile"],
        "chunksize": settings["chunksize"],
        "effective_profile": profile,
        "front_path": front_source,
        "rear_path": rear_source,
        "configured_rear_mode": mode,
        "effective_rear_mode": mode,
        "bypass": False,
        "convolution_channels": 4 if rear_source is not None else 2,
        # Generated Rear FIRs already contain trim; copy-front previews apply it
        # in the runtime mixer without altering saved profile settings.
        "woofer_trim_db": 0 if rear_source is not None else woofer_trim_db,
        "mimo_paths": None,
        "mimo_manifest_path": None,
    }
    config = build_config(resolved)
    check_config(config)
    old_config = CONFIG_PATH.read_bytes() if CONFIG_PATH.is_file() else None
    old_preview = PREVIEW_STATE_PATH.read_bytes() if PREVIEW_STATE_PATH.is_file() else None
    preview = {
        "active": True,
        "profile": profile,
        "mode": mode,
        "boot_id": current_boot_id(),
        "started_unix": time.time(),
        "front": front_meta,
        "rear": rear_meta,
        "woofer_trim_db": resolved["woofer_trim_db"],
        "note": "temporary audition; managed profile WAVs and settings are unchanged",
    }
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(CONFIG_PATH, config)
        atomic_write(PREVIEW_STATE_PATH, (json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        if old_config != config and not DISABLE_SERVICE_RESTART:
            restart_camilladsp()
    except Exception:
        if old_config is None:
            CONFIG_PATH.unlink(missing_ok=True)
        else:
            atomic_write(CONFIG_PATH, old_config)
        if old_preview is None:
            PREVIEW_STATE_PATH.unlink(missing_ok=True)
        else:
            atomic_write(PREVIEW_STATE_PATH, old_preview)
        if old_config is not None and not DISABLE_SERVICE_RESTART:
            subprocess.run(["systemctl", "restart", "camilladsp.service"], check=False)
        raise
    result = serializable(resolved)
    result.update({"front": front_meta, "rear": rear_meta, "preview": preview_status()})
    return result


def preview_mimo(profile: str, manifest_source: Path) -> dict[str, Any]:
    """Audition a validated session MIMO bank without modifying managed files/settings."""
    if profile != "speaker":
        raise ProfileError("MIMO 2×4는 실제 4채널 speaker 출력에만 적용할 수 있습니다.")
    capability = platform_capability()
    if not capability["mimo_supported"]:
        raise ProfileError(capability["reason"] or "MIMO is unavailable.")
    bank = validate_mimo_bank(manifest_source)
    settings = load_settings()
    resolved = {
        "requested_profile": settings["requested_profile"],
        "chunksize": max(1024, settings["chunksize"]),
        "effective_profile": profile,
        "front_path": None,
        "rear_path": None,
        "configured_rear_mode": "mimo_2x4",
        "effective_rear_mode": "mimo_2x4",
        "bypass": False,
        "convolution_channels": 8,
        "woofer_trim_db": 0,
        "mimo_paths": [item["path"] for item in bank["files"]],
        "mimo_manifest_path": bank["manifest_path"],
        "mimo_topology": bank["manifest"].get("topology"),
    }
    config = build_config(resolved)
    check_config(config)
    old_config = CONFIG_PATH.read_bytes() if CONFIG_PATH.is_file() else None
    old_preview = PREVIEW_STATE_PATH.read_bytes() if PREVIEW_STATE_PATH.is_file() else None
    preview = {
        "active": True,
        "profile": profile,
        "mode": "mimo_2x4",
        "topology": bank["manifest"].get("topology"),
        "boot_id": current_boot_id(),
        "started_unix": time.time(),
        "manifest": str(bank["manifest_path"]),
        "note": "temporary MIMO audition; managed bank and settings are unchanged",
    }
    try:
        atomic_write(CONFIG_PATH, config)
        atomic_write(PREVIEW_STATE_PATH, (json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        if old_config != config and not DISABLE_SERVICE_RESTART:
            restart_camilladsp()
    except Exception:
        if old_config is None:
            CONFIG_PATH.unlink(missing_ok=True)
        else:
            atomic_write(CONFIG_PATH, old_config)
        if old_preview is None:
            PREVIEW_STATE_PATH.unlink(missing_ok=True)
        else:
            atomic_write(PREVIEW_STATE_PATH, old_preview)
        if old_config is not None and not DISABLE_SERVICE_RESTART:
            subprocess.run(["systemctl", "restart", "camilladsp.service"], check=False)
        raise
    result = serializable(resolved)
    result.update({"bank": serializable(bank), "preview": preview_status()})
    return result


def install_mimo(profile: str, manifest_source: Path) -> dict[str, Any]:
    """Atomically install a self-validated 2×4 MIMO bank with rollback."""
    if profile != "speaker":
        raise ProfileError("MIMO 2×4는 실제 4채널 speaker 출력에만 적용할 수 있습니다.")
    capability = platform_capability()
    if not capability["mimo_supported"]:
        raise ProfileError(capability["reason"] or "MIMO is unavailable.")
    bank = validate_mimo_bank(manifest_source)
    MIMO_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    targets = [MIMO_DIR / name for name in MIMO_OUTPUT_NAMES]
    manifest_target = MIMO_MANIFESTS[profile]
    prior = {path: path.read_bytes() if path.is_file() else None for path in [*targets, manifest_target]}
    previous_settings = load_settings()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"{profile}-mimo-{stamp}-{os.getpid()}"
    backup.mkdir(parents=True, exist_ok=False)
    for path, data in prior.items():
        if data is not None:
            atomic_write(backup / path.name, data)
    temporary: list[tuple[Path, Path]] = []
    try:
        managed_files = []
        for item, target in zip(bank["files"], targets):
            temp = target.with_name(f".{target.name}.mimo-{os.getpid()}")
            shutil.copyfile(item["path"], temp)
            os.chmod(temp, 0o644)
            temporary.append((temp, target))
            metadata = validate_wav(temp)
            managed_files.append({
                "output": item["output"], "label": item.get("label"), "file": target.name,
                "sha256": metadata["sha256"], "channels": 2, "frames": 32768, "format": "float32",
            })
        for temp, target in temporary:
            os.replace(temp, target)
        managed_manifest = dict(bank["manifest"])
        managed_manifest["files"] = managed_files
        managed_manifest["installed_unix"] = time.time()
        managed_manifest["source_manifest_sha256"] = hashlib.sha256(bank["manifest_path"].read_bytes()).hexdigest()
        atomic_write(manifest_target, (json.dumps(managed_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        validate_mimo_bank(manifest_target)
        settings = load_settings()
        settings["requested_profile"] = profile
        settings["bypass"][profile] = False
        settings["mimo_enabled"][profile] = True
        settings["chunksize"] = max(1024, settings["chunksize"])
        applied = apply_settings(settings, restart=True)
    except Exception:
        for temp, _target in temporary:
            temp.unlink(missing_ok=True)
        for path, data in prior.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        try:
            apply_settings(previous_settings, restart=True)
        except Exception:
            pass
        raise
    applied["installed_mimo"] = {
        "profile": profile,
        "topology": bank["manifest"].get("topology"),
        "manifest": str(manifest_target),
        "rollback_backup": str(backup),
        "paths": 8,
        "taps": 32768,
    }
    return applied


def restore_profile(restart: bool = True) -> dict[str, Any]:
    result = apply_settings(load_settings(), restart=restart)
    result["preview_restored"] = True
    return result


def clear_stale_preview() -> dict[str, Any]:
    preview = preview_status()
    if not preview["active"]:
        return {"cleared": False, "reason": "no preview"}
    if not preview["stale"]:
        return {"cleared": False, "reason": "same boot preview remains active", "preview": preview}
    restored = restore_profile(restart=False)
    return {"cleared": True, "reason": "preview belonged to an earlier boot", "resolved": restored}


def install_pair(profile: str, front_source: Path, rear_source: Path | None, woofer_trim_db: int) -> dict[str, Any]:
    """Install a generated Front/optional Rear pair with one DSP restart."""
    if profile not in PROFILE_FILES:
        raise ProfileError("Invalid generated-filter profile.")
    if woofer_trim_db not in ALLOWED_WOOFER_TRIMS:
        raise ProfileError("Invalid generated-filter woofer trim.")
    metadata = {"front": validate_wav(front_source), "rear": None}
    if rear_source is not None:
        metadata["rear"] = validate_wav(rear_source)
    sources = {"front": front_source}
    if rear_source is not None:
        sources["rear"] = rear_source
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    previous_settings = load_settings()
    previous_files: dict[str, bytes | None] = {}
    temporary_files: dict[str, Path] = {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        for band, source in sources.items():
            target = PROFILE_FILES[profile][band]
            previous = target.read_bytes() if target.is_file() else None
            previous_files[band] = previous
            if previous is not None:
                backup = BACKUP_DIR / f"{profile}-{band}-{stamp}-{hashlib.sha256(previous).hexdigest()[:12]}.wav"
                atomic_write(backup, previous)
            temporary = target.with_name(f".{target.name}.generated-{os.getpid()}")
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o644)
            temporary_files[band] = temporary
        for band, temporary in temporary_files.items():
            os.replace(temporary, PROFILE_FILES[profile][band])
        settings = load_settings()
        settings["bypass"][profile] = False
        settings["mimo_enabled"][profile] = False
        settings["rear_mode"][profile] = "separate" if rear_source is not None else "copy_front"
        settings["woofer_trim_db"][profile] = woofer_trim_db
        applied = apply_settings(settings, restart=True)
    except Exception:
        for temporary in temporary_files.values():
            temporary.unlink(missing_ok=True)
        for band in sources:
            target = PROFILE_FILES[profile][band]
            previous = previous_files.get(band)
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, previous)
        try:
            apply_settings(previous_settings, restart=True)
        except Exception:
            pass
        raise
    applied["installed_pair"] = {
        "profile": profile,
        "rear_mode": settings["rear_mode"][profile],
        "woofer_trim_db": woofer_trim_db,
        "metadata": metadata,
    }
    return applied


def restore_snapshot(source_dir: Path) -> dict[str, Any]:
    """Restore normalized settings and FIRs as one rollback-protected transaction."""
    source_dir = source_dir.resolve(strict=True)
    settings_source = source_dir / "profile-settings.json"
    profiles_source = source_dir / "profiles"
    if not settings_source.is_file() or not profiles_source.is_dir():
        raise ProfileError("Restore snapshot is missing settings or profiles.")
    try:
        raw_settings = json.loads(settings_source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Invalid restore settings: {exc}") from exc
    settings = normalize_settings(raw_settings, strict=True)
    if not (profiles_source / "Factory_Speaker_Front_LR.wav").is_file():
        raise ProfileError("Restore snapshot must contain Factory_Speaker_Front_LR.wav.")
    metadata = {}
    for name in SNAPSHOT_FILES:
        source = profiles_source / name
        if source.is_file():
            metadata[name] = validate_wav(source)
    for profile, manifest in MIMO_MANIFESTS.items():
        source = profiles_source / "mimo" / manifest.name
        if source.is_file():
            metadata[f"mimo/{manifest.name}"] = serializable(validate_mimo_bank(source))

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_DIR / f"full-restore-{timestamp}-{os.getpid()}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    all_snapshot_files = {**SNAPSHOT_FILES, **MIMO_SNAPSHOT_FILES}
    previous_files = {name: target.read_bytes() if target.is_file() else None for name, target in all_snapshot_files.items()}
    previous_settings = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.is_file() else None
    previous_legacy = LEGACY_STATE_PATH.read_bytes() if LEGACY_STATE_PATH.is_file() else None
    previous_config = CONFIG_PATH.read_bytes() if CONFIG_PATH.is_file() else None
    for name, data in previous_files.items():
        if data is not None:
            atomic_write(backup_dir / "profiles" / name, data)
    if previous_settings is not None:
        atomic_write(backup_dir / "profile-settings.json", previous_settings)
    atomic_write(
        backup_dir / "restore-manifest.json",
        (json.dumps({
            "created_unix": time.time(),
            "reason": "automatic pre-restore rollback",
            "files": sorted(name for name, data in previous_files.items() if data is not None),
        }, indent=2) + "\n").encode(),
    )

    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for name, target in all_snapshot_files.items():
            source = profiles_source / name
            if source.is_file():
                atomic_write(target, source.read_bytes())
            elif target.is_file():
                target.unlink()
        applied = apply_settings(settings, restart=True)
    except Exception:
        for name, target in all_snapshot_files.items():
            previous = previous_files[name]
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, previous)
        if previous_settings is None:
            SETTINGS_PATH.unlink(missing_ok=True)
        else:
            atomic_write(SETTINGS_PATH, previous_settings)
        if previous_legacy is None:
            LEGACY_STATE_PATH.unlink(missing_ok=True)
        else:
            atomic_write(LEGACY_STATE_PATH, previous_legacy)
        if previous_config is None:
            CONFIG_PATH.unlink(missing_ok=True)
        else:
            atomic_write(CONFIG_PATH, previous_config)
        if service_running():
            subprocess.run(["systemctl", "restart", "camilladsp.service"], check=False)
        raise
    applied["restored_snapshot"] = {
        "settings": settings,
        "files": metadata,
        "automatic_backup": str(backup_dir),
    }
    return applied


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("profile", choices=PROFILE_FILES)
    activate_parser.add_argument("--no-restart", action="store_true")
    mode_parser = subparsers.add_parser("set-rear-mode")
    mode_parser.add_argument("profile", choices=PROFILE_FILES)
    mode_parser.add_argument("mode", choices=("copy_front", "separate"))
    mode_parser.add_argument("--no-restart", action="store_true")
    bypass_parser = subparsers.add_parser("set-bypass")
    bypass_parser.add_argument("profile", choices=PROFILE_FILES)
    bypass_parser.add_argument("enabled", choices=("on", "off"))
    bypass_parser.add_argument("--no-restart", action="store_true")
    mimo_enabled_parser = subparsers.add_parser("set-mimo-enabled")
    mimo_enabled_parser.add_argument("profile", choices=PROFILE_FILES)
    mimo_enabled_parser.add_argument("enabled", choices=("on", "off"))
    mimo_enabled_parser.add_argument("--no-restart", action="store_true")
    trim_parser = subparsers.add_parser("set-woofer-trim")
    trim_parser.add_argument("profile", choices=PROFILE_FILES)
    trim_parser.add_argument("trim_db", type=int, choices=ALLOWED_WOOFER_TRIMS)
    trim_parser.add_argument("--no-restart", action="store_true")
    chunksize_parser = subparsers.add_parser("set-chunksize")
    chunksize_parser.add_argument("chunksize", type=int, choices=ALLOWED_CHUNKSIZES)
    chunksize_parser.add_argument("--no-restart", action="store_true")
    volume_parser = subparsers.add_parser("set-output-volume")
    volume_parser.add_argument("volume_db", type=int, choices=ALLOWED_OUTPUT_VOLUMES_DB)
    upload_parser = subparsers.add_parser("upload")
    upload_parser.add_argument("profile", choices=PROFILE_FILES)
    upload_parser.add_argument("band", choices=("front", "rear"))
    upload_parser.add_argument("source", type=Path)
    upload_parser.add_argument("original_name")
    validate_parser = subparsers.add_parser("validate-wav")
    validate_parser.add_argument("source", type=Path)
    validate_mimo_parser = subparsers.add_parser("validate-mimo")
    validate_mimo_parser.add_argument("manifest", type=Path)
    validate_settings_parser = subparsers.add_parser("validate-settings")
    validate_settings_parser.add_argument("source", type=Path)
    pair_parser = subparsers.add_parser("install-pair")
    pair_parser.add_argument("profile", choices=PROFILE_FILES)
    pair_parser.add_argument("front_source", type=Path)
    pair_parser.add_argument("rear_source", type=Path, nargs="?")
    pair_parser.add_argument("--woofer-trim", type=int, choices=ALLOWED_WOOFER_TRIMS, default=0)
    preview_parser = subparsers.add_parser("preview-pair")
    preview_parser.add_argument("profile", choices=PROFILE_FILES)
    preview_parser.add_argument("front_source", type=Path)
    preview_parser.add_argument("rear_source", type=Path, nargs="?")
    preview_parser.add_argument("--woofer-trim", type=int, choices=ALLOWED_WOOFER_TRIMS, default=0)
    install_mimo_parser = subparsers.add_parser("install-mimo")
    install_mimo_parser.add_argument("profile", choices=PROFILE_FILES)
    install_mimo_parser.add_argument("manifest", type=Path)
    preview_mimo_parser = subparsers.add_parser("preview-mimo")
    preview_mimo_parser.add_argument("profile", choices=PROFILE_FILES)
    preview_mimo_parser.add_argument("manifest", type=Path)
    restore_parser = subparsers.add_parser("restore-profile")
    restore_parser.add_argument("--no-restart", action="store_true")
    snapshot_parser = subparsers.add_parser("restore-snapshot")
    snapshot_parser.add_argument("source_dir", type=Path)
    subparsers.add_parser("clear-stale-preview")
    args = parser.parse_args()

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == "status":
            result = status()
        elif args.command == "activate":
            result = activate(args.profile, restart=not args.no_restart)
        elif args.command == "set-rear-mode":
            result = set_rear_mode(args.profile, args.mode, restart=not args.no_restart)
        elif args.command == "set-bypass":
            result = set_bypass(args.profile, args.enabled == "on", restart=not args.no_restart)
        elif args.command == "set-mimo-enabled":
            result = set_mimo_enabled(args.profile, args.enabled == "on", restart=not args.no_restart)
        elif args.command == "set-woofer-trim":
            result = set_woofer_trim(args.profile, args.trim_db, restart=not args.no_restart)
        elif args.command == "set-chunksize":
            result = set_chunksize(args.chunksize, restart=not args.no_restart)
        elif args.command == "set-output-volume":
            result = set_output_volume(args.volume_db)
        elif args.command == "validate-wav":
            result = validate_wav(args.source)
        elif args.command == "validate-mimo":
            result = serializable(validate_mimo_bank(args.manifest))
        elif args.command == "validate-settings":
            result = validate_settings_file(args.source)
        elif args.command == "install-pair":
            result = install_pair(args.profile, args.front_source, args.rear_source, args.woofer_trim)
        elif args.command == "preview-pair":
            result = preview_pair(args.profile, args.front_source, args.rear_source, args.woofer_trim)
        elif args.command == "install-mimo":
            result = install_mimo(args.profile, args.manifest)
        elif args.command == "preview-mimo":
            result = preview_mimo(args.profile, args.manifest)
        elif args.command == "restore-profile":
            result = restore_profile(restart=not args.no_restart)
        elif args.command == "restore-snapshot":
            result = restore_snapshot(args.source_dir)
        elif args.command == "clear-stale-preview":
            result = clear_stale_preview()
        else:
            result = upload(args.profile, args.band, args.source, args.original_name)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProfileError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
