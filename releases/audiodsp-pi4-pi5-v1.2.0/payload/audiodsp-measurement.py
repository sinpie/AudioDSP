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
import shutil
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


def environment(suffix: str, default: str) -> str:
    """Prefer AudioDSP identifiers; accept legacy GSONIC_* during migration."""
    return os.environ.get(f"AUDIODSP_{suffix}", os.environ.get(f"GSONIC_{suffix}", default))


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
CAPTURE_DEVICE = environment("UMIK_DEVICE", "hw:CARD=UMIK1,DEV=0")
PLAYBACK_DEVICE = environment("U7_DEVICE", "audiodsp_announce")
DEFAULT_NOISE_LEVEL_DBFS = -42
DEFAULT_SWEEP_LEVEL_DBFS = -42
DEFAULT_WOOFER_MEASUREMENT_ATTENUATION_DB = -9
WOOFER_MEASUREMENT_ATTENUATION_DB = float(environment(
    "WOOFER_MEASUREMENT_ATTENUATION_DB",
    str(DEFAULT_WOOFER_MEASUREMENT_ATTENUATION_DB),
))
if not -18.0 <= WOOFER_MEASUREMENT_ATTENUATION_DB <= 0.0:
    raise RuntimeError("WOOFER_MEASUREMENT_ATTENUATION_DB must be between -18 and 0 dB")
WOOFER_MEASUREMENT_SCALE = 10.0 ** (WOOFER_MEASUREMENT_ATTENUATION_DB / 20.0)
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
    "mimo_stereo": ("front_left", "front_right"),
    "mimo_one_sub": ("front_left", "front_right", "sub_pair"),
    "mimo_dual_sub": ("front_left", "front_right", "sub_left", "sub_right"),
}
SOURCE_LABELS = {
    "left": "Front L (Woofer muted)",
    "right": "Front R (Woofer muted)",
    "woofer": "Woofer only",
    "left_woofer": "L + Woofer",
    "right_woofer": "R + Woofer",
    "front_left": "Front L actuator",
    "front_right": "Front R actuator",
    "sub_pair": "T5S single-sub actuator",
    "sub_left": "Independent Sub 1",
    "sub_right": "Independent Sub 2",
}
SUBWOOFER_ONLY_SOURCES = frozenset(("woofer", "sub_pair", "sub_left", "sub_right"))
MIMO_MODES = tuple(mode for mode in SOURCES if mode.startswith("mimo_"))
TARGET_FILES = {
    "flat": None,
    "harman": "target_Harman_Kardon.txt",
    "rtings": "target_RTings.txt",
    "acoustix": "target_AcoustiX.txt",
    "toole": "target_Not_Dr_Toole.txt",
    "bk": "target_Bruel_Kjaer.txt",
}
PHASE_CUTOFFS = (80, 120, 160, 200, 250)
MAX_PHASE_SHIFT = 2048
DEFAULT_CORRECTION_PREFERENCES = {
    "target": "harman",
    "preset": "strong",
    "woofer_trim_db": -9,
    "phase_mode": "bass",
    "phase_cutoff": 200,
    "spatial_mode": "equal",
    "bass_tilt_db": 0,
    "treble_tilt_db": 0,
    "correction_low_hz": 20,
    "correction_high_hz": 20_000,
    "max_boost_db": 6,
    "max_cut_db": 18,
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


def measurement_worker_alive(value: dict[str, Any]) -> bool:
    """Verify that a stored PID still belongs to this engine's worker."""
    pid = value.get("worker_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return time.time() < float(value.get("worker_launch_pending_until", 0.0))
    try:
        os.kill(pid, 0)
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


def load_current() -> dict[str, Any]:
    if not CURRENT.is_file():
        return {
            "state": "idle",
            "stage": "새 측정을 시작하세요.",
            "progress": 0.0,
            "eta_seconds": None,
            "umik_connected": umik_connected(),
            "installed_calibrations": calibration_inventory(),
            "correction_preferences": load_correction_preferences(),
            "capabilities": platform_capabilities(),
        }
    try:
        value = json.loads(CURRENT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasurementError(f"측정 상태 파일 오류: {exc}") from exc
    # Schema-1 sessions used one level for both signals and a process-wide
    # Woofer attenuation. Keep them readable without rewriting their files.
    value.setdefault("noise_level_dbfs", value.get("level_dbfs", DEFAULT_NOISE_LEVEL_DBFS))
    value.setdefault("woofer_measurement_attenuation_db", int(WOOFER_MEASUREMENT_ATTENUATION_DB))
    value = recover_interrupted_worker(value)
    value["umik_connected"] = umik_connected()
    value["installed_calibrations"] = calibration_inventory()
    value["correction_preferences"] = load_correction_preferences()
    value["capabilities"] = platform_capabilities()
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
        "max_boost_db": (0, 3, 6, 9), "max_cut_db": (6, 9, 12, 18, 24),
        "mimo_high_hz": (80, 120, 150),
        "mimo_strength": ("safe", "balanced", "maximum"),
        "mimo_support_penalty_db": (3, 6, 9, 12),
    }
    for key, allowed in checks.items():
        if key in value:
            candidate = value[key]
            if candidate not in allowed or (isinstance(candidate, bool) and key not in ("phase_mode", "target", "preset", "spatial_mode")):
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
    return {
        "platform_class": kind,
        "mimo_supported": kind in ("pi4plus", "development", "test"),
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
        raise MeasurementError("측정 작업 중에는 현재 session이 사용하는 calibration을 바꿀 수 없습니다.")
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


def write_sweep(
    path: Path,
    source: str,
    level_dbfs: int,
    seconds: int,
    *,
    level_check: bool = False,
    woofer_attenuation_db: float | None = None,
) -> list[float]:
    lead_seconds = 0.35
    tail_seconds = 1.0 if level_check else 2.0
    sweep_seconds = float(seconds)
    frames = round((lead_seconds + sweep_seconds + tail_seconds) * RATE)
    amplitude = 10.0 ** (level_dbfs / 20.0)
    woofer_db = WOOFER_MEASUREMENT_ATTENUATION_DB if woofer_attenuation_db is None else float(woofer_attenuation_db)
    if not -18.0 <= woofer_db <= 0.0:
        raise MeasurementError("우퍼 측정 상대레벨은 -18~0 dB여야 합니다.")
    woofer_scale = 10.0 ** (woofer_db / 20.0)
    f1, f2 = (40.0, 2_000.0) if level_check else (15.0, 22_000.0)
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
                # The T5S receives both Rear channels through a stereo cable. Each
                # side carries half the mono sweep, and the pair is attenuated for
                # night-safe measurement. The returned reference is scaled by the
                # same amount so deconvolution remains level-correct.
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


def write_white_noise(path: Path, level_dbfs: int, seconds: int = 5) -> None:
    """Write deterministic full-band white noise to Front L/R in 4ch S24_3LE."""
    frames = seconds * RATE
    amplitude = 10.0 ** (level_dbfs / 20.0) * 0.70710678
    fade = round(0.05 * RATE)
    state = 0x7200660
    data_bytes = frames * 4 * 3
    with path.open("wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE")
        handle.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 4, RATE, RATE * 12, 12, 24))
        handle.write(b"data" + struct.pack("<I", data_bytes))
        buffer = bytearray()
        zero = b"\x00\x00\x00"
        for index in range(frames):
            state ^= (state << 13) & 0xFFFFFFFF
            state ^= state >> 17
            state ^= (state << 5) & 0xFFFFFFFF
            value = amplitude * (2.0 * (state & 0xFFFFFFFF) / 0xFFFFFFFF - 1.0)
            if index < fade:
                value *= 0.5 - 0.5 * math.cos(math.pi * index / fade)
            elif index >= frames - fade:
                value *= 0.5 - 0.5 * math.cos(math.pi * (frames - 1 - index) / fade)
            payload = pack_pcm24(value)
            buffer.extend(payload + payload + zero + zero)
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)


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
    camilla_was_active = subprocess.run([SYSTEMCTL, "is-active", "--quiet", "camilladsp.service"], check=False).returncode == 0
    with AUDIO_LOCK.open("w") as audio_handle:
        fcntl.flock(audio_handle, fcntl.LOCK_EX)
        active_processes: list[subprocess.Popen] = []
        try:
            if camilla_was_active:
                subprocess.run([SYSTEMCTL, "stop", "camilladsp.service"], check=True, timeout=20)
                # USB/amp switching transients immediately after stopping the
                # live stream must not contaminate the background reference.
                time.sleep(0.75)
            # Measurement must not capture or loop the preamp/U7 input. The
            # CamillaDSP starter restores Line capture when normal DSP resumes.
            subprocess.run([AMIXER, "-D", "hw:U7", "set", "Mic", "nocap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([AMIXER, "-D", "hw:U7", "set", "Line", "nocap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            update_current(dsp_mode="direct_bypass", u7_input="off", stage="DSP bypass · U7 입력 OFF · UMIK 녹음 준비", progress=progress_base)
            item_span = progress_span / len(captures)
            for index, (output, recorded, label) in enumerate(captures):
                if load_current().get("cancel_requested"):
                    raise MeasurementError("사용자가 측정을 취소했습니다.")
                item_base = progress_base + item_span * index
                capture_seconds = max(2, math.ceil(output.stat().st_size / (RATE * 12) + 1.0))
                capture = subprocess.Popen([
                    ARECORD, "-q", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
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
                    elapsed = time.monotonic() - start
                    fraction = min(0.98, elapsed / expected)
                    update_current(progress=item_base + item_span * fraction, eta_seconds=max(0, round(expected - elapsed)))
                    time.sleep(0.5)
                if playback.returncode != 0:
                    raise MeasurementError(f"U7 측정음 재생 실패: {playback.returncode}")
                capture.wait(timeout=capture_seconds + 3)
                if capture.returncode != 0:
                    raise MeasurementError(f"UMIK 녹음 실패: {capture.returncode}")
                active_processes = []
                update_current(progress=item_base + item_span)
        finally:
            update_current(active_pids=[])
            for process in active_processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            if camilla_was_active:
                subprocess.run([SYSTEMCTL, "start", "camilladsp.service"], check=False, timeout=25)
            else:
                subprocess.run([AMIXER, "-D", "hw:U7", "set", "Line", "cap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            update_current(dsp_mode="restored", u7_input="restored")


def run_direct_capture(output: Path, recorded: Path, duration: int, progress_base: float, progress_span: float) -> None:
    # Kept as a compatibility wrapper for one-off validation and tests.
    del duration
    run_direct_capture_batch([(output, recorded, "측정")], progress_base, progress_span)


def run_level_sequence(noise: Path, silence_recorded: Path, noise_recorded: Path) -> None:
    """Capture 5 s background and 5 s white noise under one exclusive bypass window."""
    camilla_was_active = subprocess.run([SYSTEMCTL, "is-active", "--quiet", "camilladsp.service"], check=False).returncode == 0
    with AUDIO_LOCK.open("w") as audio_handle:
        fcntl.flock(audio_handle, fcntl.LOCK_EX)
        processes: list[subprocess.Popen] = []
        try:
            if camilla_was_active:
                subprocess.run([SYSTEMCTL, "stop", "camilladsp.service"], check=True, timeout=20)
                time.sleep(0.75)
            subprocess.run([AMIXER, "-D", "hw:U7", "set", "Mic", "nocap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([AMIXER, "-D", "hw:U7", "set", "Line", "nocap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            update_current(dsp_mode="direct_bypass", u7_input="off", stage="1/2 · 무음 5초 · 배경소음 측정 중", progress=5.0, eta_seconds=11)
            silence = subprocess.Popen([
                ARECORD, "-q", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
                "-r", str(RATE), "-c", "1", "-d", "5", str(silence_recorded),
            ])
            processes = [silence]
            update_current(active_pids=[silence.pid])
            silence.wait(timeout=9)
            if silence.returncode != 0:
                raise MeasurementError(f"UMIK 무음 녹음 실패: {silence.returncode}")

            update_current(stage="2/2 · 백색소음 5초 · 신호 레벨 측정 중", progress=45.0, eta_seconds=6)
            capture = subprocess.Popen([
                ARECORD, "-q", "-D", CAPTURE_DEVICE, "-t", "wav", "-f", "S24_3LE",
                "-r", str(RATE), "-c", "1", "-d", "6", str(noise_recorded),
            ])
            processes = [capture]
            time.sleep(0.4)
            playback = subprocess.Popen([APLAY, "-q", "-D", PLAYBACK_DEVICE, str(noise)])
            processes.append(playback)
            update_current(active_pids=[capture.pid, playback.pid])
            playback.wait(timeout=9)
            if playback.returncode != 0:
                raise MeasurementError(f"U7 백색소음 재생 실패: {playback.returncode}")
            capture.wait(timeout=10)
            if capture.returncode != 0:
                raise MeasurementError(f"UMIK 백색소음 녹음 실패: {capture.returncode}")
            update_current(progress=95.0, eta_seconds=1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            update_current(active_pids=[])
            if camilla_was_active:
                subprocess.run([SYSTEMCTL, "start", "camilladsp.service"], check=False, timeout=25)
            else:
                subprocess.run([AMIXER, "-D", "hw:U7", "set", "Line", "cap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            update_current(dsp_mode="restored", u7_input="restored")


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
    # A bandwidth-limited actuator can produce a plateau of equally energetic
    # sweep-length windows.  Prefer the nominal ALSA arm timing within 0.5% of
    # the maximum, while still allowing a clearly truncated/delayed recording
    # to move away from that anchor.
    nominal_block = round((round(0.4 * RATE) + reference_start) / envelope_block)
    near_best = [
        index for index, power in enumerate(window_powers)
        if power >= best_power * 0.995
    ]
    best_block = min(near_best, key=lambda index: abs(index - nominal_block))
    active_start = min(len(samples), best_block * envelope_block)
    active_end = min(len(samples), active_start + active_length)
    capture_lead = active_start - reference_start

    # Median 200 ms AC-RMS rejects switching clicks.  When both sides of the
    # sweep are available, retain the noisier median as a conservative floor.
    noise_guard = round(0.10 * RATE)
    noise_block = round(0.20 * RATE)
    noise_segments = [
        samples[:max(0, active_start - noise_guard)],
        samples[min(len(samples), active_end + noise_guard):],
    ]
    noise_estimates = []
    for segment in noise_segments:
        blocks = [
            ac_rms(segment[start:start + noise_block])
            for start in range(0, len(segment) - noise_block + 1, noise_block)
        ]
        if blocks:
            noise_estimates.append(statistics.median(blocks))
    if not noise_estimates:
        raise MeasurementError("측정 sweep의 무음 구간을 평가할 수 없습니다.")
    background_rms = max(noise_estimates)
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
    minimum_active_samples = RATE // 5 if source in SUBWOOFER_ONLY_SOURCES else RATE
    if len(active) < minimum_active_samples:
        raise MeasurementError("측정 sweep의 무음/신호 구간을 평가할 수 없습니다.")
    active_rms = ac_rms(active)
    signal_power = max(0.0, active_rms * active_rms - background_rms * background_rms)
    snr_db = 10.0 * math.log10(max(signal_power, 1.0e-30) / max(background_rms * background_rms, 1.0e-30))
    return {
        "background_rms_dbfs": round(20.0 * math.log10(max(background_rms, 1.0e-15)), 2),
        "active_rms_dbfs": round(20.0 * math.log10(max(active_rms, 1.0e-15)), 2),
        "estimated_signal_rms_dbfs": round(10.0 * math.log10(max(signal_power, 1.0e-30)), 2),
        "snr_db": round(snr_db, 2),
        "minimum_usable_snr_db": 6.0,
        "recommended_snr_db": 15.0,
        "usable": snr_db >= 6.0,
        "recommended": snr_db >= 15.0,
        "analysis_band_hz": analysis_band_hz,
        "subwoofer_passband": passband,
        "source": source,
        "active_interval_samples": [active_start, active_end],
        "capture_delay_samples": capture_lead,
        "capture_delay_ms": round(capture_lead * 1000.0 / RATE, 3),
        "timing_method": "maximum-energy sweep-length window on 50 ms AC-RMS envelope",
        "noise_segments_used": len(noise_estimates),
    }


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
        "method": "conservative max(pre-roll, post-roll) short-window noise PSD; local 1/12-octave FFT SNR; 6-15 dB confidence ramp; 100 ms positive-transient detector",
    }


def response_from_recording(
    recorded: Path,
    reference: list[float],
    cal: dict[str, Any],
    source: str | None = None,
) -> dict[str, Any]:
    _, bits, samples = read_pcm_wav(recorded)
    if len(samples) < RATE:
        raise MeasurementError("녹음이 너무 짧습니다.")
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if peak >= 0.988:
        raise MeasurementError("UMIK 입력이 클리핑되었습니다. 볼륨을 낮추세요.")
    quality = sweep_capture_quality(samples, reference, source)
    if not quality["usable"]:
        raise MeasurementError(
            f"측정 sweep SNR이 {quality['snr_db']:.1f} dB로 너무 낮습니다. "
            "측정 레벨 또는 sweep 시간을 올리세요."
        )
    capture_delay = int(quality.get("capture_delay_samples", round(0.4 * RATE)))
    if capture_delay >= 0:
        delayed_reference = [0.0] * capture_delay + reference
    else:
        delayed_reference = reference[min(len(reference), -capture_delay):]
    length = next_power_of_two(max(len(samples), len(delayed_reference)))
    fft = FFTBackend()
    y = fft.rfft(samples, length)
    x = fft.rfft(delayed_reference, length)
    maximum_power = max((value.real * value.real + value.imag * value.imag) for value in x)
    regularization = maximum_power * 1.0e-9
    h: list[complex] = []
    for output_value, input_value in zip(y, x):
        power = input_value.real * input_value.real + input_value.imag * input_value.imag
        h.append(output_value * input_value.conjugate() / (power + regularization))
    impulse = fft.irfft(h, length)
    raw_peak_index = max(range(length), key=lambda index: abs(impulse[index]))
    decay = room_decay_metrics(impulse, raw_peak_index, fft)
    temporal = temporal_room_metrics(impulse, raw_peak_index)
    peak_index = raw_peak_index
    if peak_index > length // 2:
        peak_index -= length
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
    smoothed = variable_smooth(frequencies, levels)
    group_delay = group_delay_metrics(frequencies, phases)
    return {
        "frequencies": [round(value, 3) for value in frequencies],
        "db": [round(value, 4) for value in smoothed],
        "phase_rad": [round(value, 7) for value in phases],
        "bulk_delay_samples": peak_index,
        "bulk_delay_ms": round(peak_index * 1000.0 / RATE, 3),
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1.0e-15)), 2),
        "rms_dbfs": round(20.0 * math.log10(max(rms, 1.0e-15)), 2),
        "capture_bits": bits,
        "fft_backend": fft.kind,
        "fft_size": length,
        "smoothing": "variable 1/12 octave <200 Hz; 1/6 octave 200-2000 Hz; 1/3 octave >2 kHz",
        "measurement_quality": quality,
        "room_decay": decay,
        "temporal": temporal,
        "group_delay": group_delay,
        "frequency_quality": frequency_noise,
    }


def inspect_saved_recording(position: int, source: str, *, reprocess: bool = False) -> dict[str, Any]:
    """Quality-check or rebuild one saved raw capture without playing sound."""
    state = load_current()
    if position not in range(1, POSITIONS + 1) or source not in state.get("sources", []):
        raise MeasurementError("현재 session의 측정 위치/채널이 아닙니다.")
    directory = Path(state["session_dir"])
    recorded = directory / f"p{position}_{source}_recorded.wav"
    if not recorded.is_file():
        raise MeasurementError(f"저장된 원본 녹음이 없습니다: {recorded.name}")
    sweep = directory / f"p{position}_{source}_sweep.wav"
    reference = write_sweep(
        sweep,
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
    for candidate in range(1, POSITIONS + 1):
        if all((candidate, candidate_source) in completed_keys for candidate_source in state["sources"]):
            completed_positions = candidate
        else:
            break
    job_state = "measured" if completed_positions == POSITIONS else "ready"
    stage = (
        "세 위치 측정 완료 · 32768탭 FIR을 생성할 수 있습니다."
        if completed_positions == POSITIONS
        else f"저장 원본 무음 재처리 완료 · 위치 {completed_positions + 1} 준비"
    )
    updated = update_current(
        state=job_state,
        stage=stage,
        error=None,
        worker_pid=None,
        measurements=measurements,
        positions_completed=completed_positions,
        progress=100.0 * completed_positions / POSITIONS,
    )
    atomic_json(directory / "session.json", updated)
    result["reprocessed"] = True
    result["response"] = response_path.name
    result["positions_completed"] = completed_positions
    result["measurement_quality"] = response["measurement_quality"]
    return result


def validate_measurement_output_levels(
    sweep_level_dbfs: int,
    noise_level_dbfs: int,
    woofer_attenuation_db: int,
) -> None:
    if sweep_level_dbfs not in ALLOWED_SWEEP_LEVELS:
        raise MeasurementError("Sweep 출력은 -54~0 dBFS 범위여야 합니다.")
    if noise_level_dbfs not in ALLOWED_NOISE_LEVELS:
        raise MeasurementError("백색소음 출력은 -54~-6 dBFS 범위여야 합니다.")
    if woofer_attenuation_db not in ALLOWED_WOOFER_MEASUREMENT_ATTENUATIONS:
        raise MeasurementError("우퍼 측정 상대레벨은 -18~0 dB 범위여야 합니다.")


def new_session(
    mode: str,
    orientation: str,
    level_dbfs: int,
    sweep_seconds: int,
    noise_level_dbfs: int | None = None,
    woofer_measurement_attenuation_db: int | None = None,
) -> dict[str, Any]:
    if mode not in SOURCES:
        raise MeasurementError("측정 모드가 잘못되었습니다.")
    capability = platform_capabilities()
    if mode in MIMO_MODES and not capability["mimo_supported"]:
        raise MeasurementError("MIMO 측정/보정은 Raspberry Pi 4/5 전용입니다.")
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
        "version": 1,
        "session_id": session_id,
        "session_dir": str(directory),
        "state": "ready",
        "stage": "위치 1: UMIK를 천장 방향으로 세우고 측정을 시작하세요.",
        "progress": 0.0,
        "eta_seconds": None,
        "mode": mode,
        "sources": list(SOURCES[mode]),
        "positions_total": POSITIONS,
        "positions_completed": 0,
        "level_dbfs": level_dbfs,
        "noise_level_dbfs": noise_level_dbfs,
        "woofer_measurement_attenuation_db": woofer_measurement_attenuation_db,
        "sweep_seconds": sweep_seconds,
        "orientation": orientation,
        "calibration": {key: value for key, value in cal.items() if key not in ("frequencies", "corrections")},
        "measurements": [],
        "result": None,
        "validation": None,
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
            "applied_profile": None,
            "preview_active": False,
            "preview_profile": None,
        })
    if step <= 3:
        state.update({
            "measurements": [],
            "positions_completed": 0,
            "validation": None,
        })
    if step <= 2:
        state["level_check"] = None
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
) -> dict[str, Any]:
    if mode not in SOURCES or orientation != "90":
        raise MeasurementError("측정 모드 또는 UMIK 방향이 잘못되었습니다.")
    if mode in MIMO_MODES and not platform_capabilities()["mimo_supported"]:
        raise MeasurementError("MIMO 측정/보정은 Raspberry Pi 4/5 전용입니다.")
    state = load_current()
    if state.get("state") == "idle":
        raise MeasurementError("먼저 새 측정 session을 만드세요.")
    noise_level_dbfs = int(state.get("noise_level_dbfs", state.get("level_dbfs", DEFAULT_NOISE_LEVEL_DBFS))) if noise_level_dbfs is None else int(noise_level_dbfs)
    woofer_measurement_attenuation_db = int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)) if woofer_measurement_attenuation_db is None else int(woofer_measurement_attenuation_db)
    validate_measurement_output_levels(level_dbfs, noise_level_dbfs, woofer_measurement_attenuation_db)
    if sweep_seconds not in ALLOWED_DURATIONS:
        raise MeasurementError("Sweep 시간이 허용 범위를 벗어났습니다.")
    changes = []
    earliest = 7
    if mode != state.get("mode"):
        changes.append("측정 구성")
        earliest = min(earliest, 3)
    if level_dbfs != state.get("level_dbfs"):
        changes.append("sweep 출력")
        checked = state.get("level_check") or {}
        checked_noise_dbfs = int(checked.get(
            "requested_white_noise_level_dbfs",
            state.get("noise_level_dbfs", DEFAULT_NOISE_LEVEL_DBFS),
        ))
        checked_peak_dbfs = float(checked.get("peak_dbfs", 0.0))
        # A sine sweep can peak about 3 dB above the deterministic white-noise
        # generator at the same nominal setting. Preserve the check only with
        # at least 6 dB of projected microphone headroom.
        projected_sweep_peak_dbfs = checked_peak_dbfs + level_dbfs - checked_noise_dbfs + 3.0
        # A successful broadband check at an equal or louder digital level
        # already proves capture headroom. Every real sweep still has its own
        # SNR/clipping gate, so only measured responses need invalidation.
        level_step = 3 if checked.get("ok") and projected_sweep_peak_dbfs <= -6.0 else 2
        earliest = min(earliest, level_step)
    if noise_level_dbfs != int(state.get("noise_level_dbfs", state.get("level_dbfs", DEFAULT_NOISE_LEVEL_DBFS))):
        changes.append("백색소음 출력")
        earliest = min(earliest, 2)
    if woofer_measurement_attenuation_db != int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB)):
        changes.append("우퍼 측정 상대레벨")
        earliest = min(earliest, 3)
    if sweep_seconds != state.get("sweep_seconds"):
        changes.append("sweep 길이")
        earliest = min(earliest, 3)
    if orientation != state.get("orientation"):
        changes.append("UMIK 방향")
        earliest = min(earliest, 1)
    preserve_front_measurements = (
        changes == ["우퍼 측정 상대레벨"] and state.get("mode") == "lrw"
    )
    retained_front = [
        item for item in state.get("measurements", [])
        if item.get("source") in ("left", "right")
        and (Path(state["session_dir"]) / str(item.get("response", ""))).is_file()
    ] if preserve_front_measurements else []
    if earliest <= 6:
        state = invalidate_from_step(state, earliest, ", ".join(changes) + " 변경")
    if preserve_front_measurements:
        state["measurements"] = retained_front
        state["stage"] = f"Woofer 측정 상대레벨 변경 · Front L/R {len(retained_front)}개 보존 · Woofer만 재측정"
    state.update(
        mode=mode,
        sources=list(SOURCES[mode]),
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
        raise MeasurementError("먼저 새 측정 session을 만드세요.")
    state = invalidate_from_step(state, 2, "레벨 검사 다시 실행")
    save_current(state)
    return state


def prepare_position_restart() -> dict[str, Any]:
    state = load_current()
    if not (state.get("level_check") or {}).get("ok"):
        raise MeasurementError("레벨 검사를 OK로 통과한 뒤 재측정하세요.")
    state = invalidate_from_step(state, 3, "3위치 재측정 실행")
    save_current(state)
    return state


def prepare_build() -> dict[str, Any]:
    state = load_current()
    if int(state.get("positions_completed", 0)) != POSITIONS:
        raise MeasurementError("세 위치 측정을 먼저 완료하세요.")
    state = invalidate_from_step(state, 4, "보정 설정 적용")
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


def measure_position_worker() -> None:
    state = load_current()
    if not (state.get("level_check") or {}).get("ok"):
        raise MeasurementError("5초 무음 + 5초 백색소음 레벨 검사를 OK로 통과한 뒤 측정하세요.")
    position = int(state["positions_completed"])
    if position >= POSITIONS:
        raise MeasurementError("세 위치 측정이 이미 완료되었습니다.")
    directory = Path(state["session_dir"])
    cal = calibration_for(state["orientation"])
    sources = list(state["sources"])
    # At the first position Front L and Woofer provide the two useful level
    # checks first. Front R follows without stopping the direct-capture window.
    if position == 0 and all(source in sources for source in ("left", "right", "woofer")):
        sources = ["left", "woofer", "right"] + [
            source for source in sources if source not in ("left", "right", "woofer")
        ]
    total_items = POSITIONS * len(sources)
    completed_items = position * len(sources)
    new_items = list(state.get("measurements", []))
    completed_keys = {
        (int(item.get("position", 0)), str(item.get("source", "")))
        for item in new_items
        if (directory / str(item.get("response", ""))).is_file()
    }
    pending: list[dict[str, Any]] = []
    response_eta = int(platform_capabilities()["offline_estimates_seconds"]["response_per_channel"])
    for source_index, source in enumerate(sources):
        if (position + 1, source) in completed_keys:
            update_current(
                stage=f"위치 {position + 1}/3 · {SOURCE_LABELS.get(source, source)} 기존 측정 보존 · 건너뜀",
                progress=100.0 * (completed_items + source_index + 1) / total_items,
            )
            continue
        if load_current().get("cancel_requested"):
            raise MeasurementError("사용자가 측정을 취소했습니다.")
        item_index = completed_items + source_index
        base = 100.0 * item_index / total_items
        span = 100.0 / total_items
        source_label = SOURCE_LABELS.get(source, source)
        update_current(state="running", stage=f"위치 {position + 1}/3 · {source_label} sweep 준비", progress=base, eta_seconds=round((len(sources) - source_index) * (state["sweep_seconds"] + 5)))
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

    # Sound first, processing second: CamillaDSP is stopped/restored once for
    # the whole position and no FFT delays occur between audible sweeps.
    position_base = 100.0 * position / POSITIONS
    position_span = 100.0 / POSITIONS
    captures = [
        (item["sweep_path"], item["record_path"], item["source_label"])
        for item in pending
    ]
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
            stage=f"위치 {position + 1}/3 · 모든 녹음 완료 · {source_label} 응답 일괄 계산",
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
    positions_completed = position + 1
    if positions_completed < POSITIONS:
        stage = f"위치 {positions_completed + 1}: 마이크를 조금 옮기고 천장 방향을 유지하세요."
        job_state = "ready"
    else:
        stage = "세 위치 측정 완료 · 32768탭 FIR을 생성할 수 있습니다."
        job_state = "measured"
    state = update_current(state=job_state, positions_completed=positions_completed, stage=stage, progress=100.0 * positions_completed / POSITIONS, eta_seconds=None, worker_pid=None)
    atomic_json(directory / "session.json", state)


def evaluate_level_samples(silence_samples: list[float], active_samples: list[float], bits: int) -> dict[str, Any]:
    if len(silence_samples) < 4 * RATE or len(active_samples) < 4 * RATE:
        raise MeasurementError("레벨 검사 녹음 길이가 부족합니다.")

    def robust_ac_rms(samples: list[float]) -> float:
        """Typical 200 ms block RMS, immune to one switching transient."""
        trim = min(round(0.25 * RATE), len(samples) // 8)
        stable = samples[trim:len(samples) - trim] if trim else samples
        block = round(0.20 * RATE)
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
    noise_dbfs = 20.0 * math.log10(max(total_rms, 1e-15))
    signal_dbfs = 20.0 * math.log10(max(signal_rms, 1e-15))
    peak_dbfs = 20.0 * math.log10(max(peak, 1e-15))
    snr_db = 10.0 * math.log10(max(signal_power, 1e-30) / max(background_rms * background_rms, 1e-30))
    if peak_dbfs >= -1.0:
        ok = False
        verdict = "NOT OK · 입력 clipping 위험 · 기기 볼륨을 수동으로 낮추고 다시 검사하세요."
    elif snr_db < 15.0:
        ok = False
        verdict = "NOT OK · 배경음 대비 신호가 작음 · 기기 볼륨을 수동으로 올리고 다시 검사하세요."
    else:
        ok = True
        verdict = "OK · 배경음 대비 레벨과 입력 headroom이 적당합니다."
    return {
        "bits": bits,
        "silence_seconds": 5,
        "white_noise_seconds": 5,
        "background_rms_dbfs": round(background_dbfs, 2),
        "white_noise_rms_dbfs": round(noise_dbfs, 2),
        "estimated_signal_rms_dbfs": round(signal_dbfs, 2),
        "snr_db": round(snr_db, 2),
        "peak_dbfs": round(peak_dbfs, 2),
        "required_snr_db": 15.0,
        "estimator": "median 200 ms AC-RMS blocks after 250 ms edge trim",
        "ok": ok,
        "verdict": verdict,
    }


def level_check_worker() -> None:
    state = load_current()
    directory = Path(state["session_dir"])
    level = int(state.get("noise_level_dbfs", state["level_dbfs"]))
    noise = directory / "level_check_white_noise.wav"
    silence_recorded = directory / "level_check_silence_5s.wav"
    noise_recorded = directory / "level_check_white_noise_5s.wav"
    write_white_noise(noise, level, 5)
    run_level_sequence(noise, silence_recorded, noise_recorded)
    _, bits, silence_samples = read_pcm_wav(silence_recorded)
    _, _, captured_samples = read_pcm_wav(noise_recorded)
    active_start = round(0.4 * RATE)
    active_samples = captured_samples[active_start:active_start + 5 * RATE]
    result = evaluate_level_samples(silence_samples, active_samples, bits)
    result["requested_white_noise_level_dbfs"] = level
    result["sweep_level_dbfs"] = int(state["level_dbfs"])
    result["woofer_measurement_attenuation_db"] = int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB))
    ok = result["ok"]
    snr_db = result["snr_db"]
    update_current(state="ready", stage=f"레벨 검사 {'OK' if ok else 'NOT OK'} · SNR {snr_db:.1f} dB", progress=100.0, eta_seconds=None, worker_pid=None, level_check=result)


def validation_worker() -> None:
    state = load_current()
    if state.get("mode") != "lrw" or int(state.get("positions_completed", 0)) != POSITIONS:
        raise MeasurementError("L/R/W 세 위치 측정을 먼저 완료하세요.")
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
            deficit = combined_db + 3.0103 - coherent_max
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


def build_room_tuning_audit(directory: Path, state: dict[str, Any], *, mimo: bool = False) -> list[dict[str, Any]]:
    """Persist an explicit corrected/limited/not-measured inventory; never imply FIR can fix everything."""
    responses = []
    for position in range(1, POSITIONS + 1):
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
        {"id": "crossover_integration", "label": "메인–우퍼 크로스오버 합산", "classification": "limited_mimo" if mimo else "limited_fir", "status": "evaluated" if state.get("mode") in ("lrw", "mimo_one_sub", "mimo_dual_sub") else "not_applicable", "action": "레벨·지연·극성·저역 phase를 공동 조정; 아날로그 crossover 자체와 비선형은 변경 불가"},
        {"id": "nonlinear_distortion", "label": "고조파 왜곡·압축·기계 잡음", "classification": "not_measured", "status": "not_available", "action": "향후 다중 레벨 Farina harmonic 분리 측정 필요; 선형 convolution으로 보정 불가"},
        {"id": "directivity", "label": "지향성·파워 응답·오프축", "classification": "not_measured", "status": "not_available", "action": "회전/근접 다각도 측정이 필요; 단일 청취영역 UMIK 측정으로 분리 불가"},
        {"id": "binaural_spatial", "label": "IACC·양이간 공간감·이미징", "classification": "not_measured", "status": "not_available", "action": "단일 omnidirectional UMIK-1로 직접 측정 불가; 더미헤드/2마이크와 별도 지표 필요"},
        {"id": "absolute_spl_neighbor", "label": "절대 SPL·청력·층간소음", "classification": "not_certified", "status": "not_available", "action": "UMIK sensitivity/전체 체인 검교정과 수음세대 측정 없이는 보장 불가; 야간 저역 shelf·volume cap은 위험 저감일 뿐"},
        {"id": "latency_clock", "label": "실시간 latency·clock drift·XRUN", "classification": "runtime_validation", "status": "requires_runtime_test", "action": "적용 후 CamillaDSP load, ALSA XRUN, rate drift, end-to-end latency를 무음 상태에서 모니터링"},
        {"id": "post_verification", "label": "적용 후 독립 재측정", "classification": "measurement_gate", "status": "required", "action": "동일 위치 재사용만으로 과적합을 판단하지 말고 별도 검증 위치에서 전/후 측정"},
    ]


def write_room_tuning_report(path: Path, state: dict[str, Any], result: dict[str, Any]) -> None:
    lines = [
        "# AudioDSP 룸 튜닝 보고서", "",
        f"- 모드: `{state.get('mode')}`", f"- 타깃: `{result.get('target')}`", f"- FIR: {result.get('sample_rate')} Hz / {result.get('taps')} taps",
        f"- 자체 검증: {'PASS' if result.get('self_validation', {}).get('overall_pass') else 'FAIL'}", "",
        "## 보정 가능성 분류", "",
    ]
    for item in result.get("room_tuning_audit", []):
        lines.append(f"- **{item['label']}** — `{item['classification']}` / `{item['status']}`: {item['action']}")
    lines += ["", "## 해석 원칙", "", "`fir_correctable`도 측정한 위치와 선형·시간불변 범위에서만 유효하다. `limited_*`는 부분 개선이며, `physical_treatment`, `not_measured`, `not_certified`는 FIR 성공으로 표시하지 않는다.", ""]
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


def variable_smooth(frequencies: list[float], values: list[float]) -> list[float]:
    """Perceptual log-frequency smoothing: 1/12 octave bass, 1/6 mid, 1/3 treble."""
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
                weighted += value * weight
                weight_sum += weight
        result.append(weighted / weight_sum if weight_sum else values[center])
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
    """Estimate the -10 dB usable band using half-octave local medians, not single peaks."""
    normalized = [value - reference_db for value in levels_db]
    low, high = 20.0, 20_000.0
    for frequency in frequencies:
        if not 20.0 <= frequency <= 1000.0:
            continue
        window = [value for f, value in zip(frequencies, normalized) if frequency <= f <= frequency * math.sqrt(2.0)]
        if window and statistics.median(window) >= -10.0:
            low = frequency
            break
    for frequency in reversed(frequencies):
        if not 1000.0 <= frequency <= 20_000.0:
            continue
        window = [value for f, value in zip(frequencies, normalized) if frequency / math.sqrt(2.0) <= f <= frequency]
        if window and statistics.median(window) >= -10.0:
            high = frequency
            break
    return max(20.0, low), min(20_000.0, high)


def correction_window(frequency: float, low_hz: int, high_hz: int) -> float:
    if frequency < low_hz or frequency > high_hz:
        return 0.0
    lower_end = min(high_hz, low_hz * math.sqrt(2.0))
    upper_start = max(low_hz, high_hz / math.sqrt(2.0))
    if frequency < lower_end and lower_end > low_hz:
        position = math.log(frequency / low_hz) / math.log(lower_end / low_hz)
        return 0.5 - 0.5 * math.cos(math.pi * position)
    if frequency > upper_start and high_hz > upper_start:
        position = math.log(frequency / upper_start) / math.log(high_hz / upper_start)
        return 0.5 + 0.5 * math.cos(math.pi * position)
    return 1.0


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
    response = fft.rfft(impulse, fft_length)
    peak = max(abs(value) for value in response)
    if peak > 1.0:
        impulse = [value / peak for value in impulse]
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
            measured = interpolate_log(measure_f, measured_phase, max(measure_f[0], min(measure_f[-1], frequency)))
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
        peak = max(abs(value) for value in response)
        if peak > 1.0:
            result = [value / peak for value in result]
            response = [value / peak for value in response]
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


def load_average_response(directory: Path, source: str, spatial_mode: str = "equal") -> dict[str, Any]:
    if spatial_mode not in ("equal", "center"):
        raise MeasurementError("공간 평균 방식이 잘못되었습니다.")
    responses = []
    for position in range(1, POSITIONS + 1):
        path = directory / f"p{position}_{source}_response.json"
        if not path.is_file():
            raise MeasurementError(f"측정 응답이 없습니다: {path.name}")
        responses.append(json.loads(path.read_text(encoding="utf-8")))
    frequencies = responses[0]["frequencies"]
    smoothing_name = "variable 1/12 octave <200 Hz; 1/6 octave 200-2000 Hz; 1/3 octave >2 kHz"
    smoothed_positions = [
        response["db"] if response.get("smoothing") == smoothing_name else variable_smooth(frequencies, response["db"])
        for response in responses
    ]
    averaged = []
    aggregate_confidence = []
    for index, frequency in enumerate(frequencies):
        if spatial_mode == "equal":
            weights = [1.0 / len(responses)] * len(responses)
        else:
            # Frequency-dependent proxy for distance/directivity weighting: bass is
            # spatially robust, while the on-axis center position matters more in treble.
            if frequency <= 200.0:
                center_weight = 1.0 / 3.0
            elif frequency >= 2000.0:
                center_weight = 0.60
            else:
                blend = math.log(frequency / 200.0) / math.log(10.0)
                center_weight = 1.0 / 3.0 + blend * (0.60 - 1.0 / 3.0)
            weights = [center_weight, (1.0 - center_weight) / 2.0, (1.0 - center_weight) / 2.0]
        noise_confidence = []
        for response in responses:
            values = response.get("frequency_quality", {}).get("confidence")
            noise_confidence.append(float(values[index]) if isinstance(values, list) and index < len(values) else 1.0)
        aggregate_confidence.append(sum(weight * confidence for weight, confidence in zip(weights, noise_confidence)))
        effective_weights = [weight * max(0.05, confidence) for weight, confidence in zip(weights, noise_confidence)]
        weight_sum = sum(effective_weights)
        averaged.append(sum(weight * values[index] for weight, values in zip(effective_weights, smoothed_positions)) / max(weight_sum, 1.0e-12))
    spatial_std_db = [
        statistics.pstdev(values[index] for values in smoothed_positions)
        for index in range(len(frequencies))
    ]
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
        "spatial_mode": spatial_mode,
        "position_weights": ("equal 1/3 each" if spatial_mode == "equal" else "frequency-dependent: center 1/3 below 200 Hz to 0.60 above 2 kHz") + "; multiplied by per-position noise confidence",
        "smoothing": smoothing_name,
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
    implementation_low = max(20.0, float(graph["correction_band_hz"][0]))
    implementation_high = min(20_000.0, float(graph["correction_band_hz"][1]))
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

    effective = [float(value) for value in graph["effective_target_db"]]
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
    graph["fir_implementation"] = {
        "evaluation_band_hz": [round(implementation_low, 1), round(implementation_high, 1)],
        "normalization_offset_db": round(normalization_offset, 3),
        "residual_mae_db": round(sum(implementation_residual) / len(implementation_residual), 4),
        "residual_p95_db": round(percentile(implementation_residual, 0.95), 4),
        "pass": percentile(implementation_residual, 0.95) <= 0.75,
    }
    graph["target_fit"] = {
        "evaluation_band_hz": [round(fit_low, 1), round(fit_high, 1)],
        "mae_db": round(mae, 3),
        "p90_abs_error_db": round(p90, 3),
        "pass": bool(errors) and mae <= 3.5 and p90 <= 7.0,
        "note": "안전한 boost/cut 한계와 자연 roll-off 보호를 적용한 뒤의 달성도",
    }
    return graph


def design_channel(measure_f: list[float], measure_db: list[float], spatial_std_db: list[float], measured_phase: list[float] | None, target_name: str, preset: str, *, woofer: bool, woofer_trim_db: int, phase_mode: str, phase_cutoff: int, spatial_mode: str = "equal", bass_tilt_db: int = 0, treble_tilt_db: int = 0, correction_low_hz: int = 20, correction_high_hz: int = 20_000, max_boost_db: int = 6, max_cut_db: int = 18, decay_frequency_hz: list[float] | None = None, decay_t20_rt60_s: list[float] | None = None, shared_reference_measure_db: float | None = None, shared_reference_target_db: float | None = None, frequency_confidence: list[float] | None = None, fft: FFTBackend) -> tuple[list[float], dict[str, Any]]:
    target_f, target_db = target_curve(target_name)
    reference_band = (50, 120) if woofer else (500, 2000)
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
    natural_low, natural_high = natural_usable_band(measure_f, measure_db, reference_measure)
    fft_length = TAPS * 2
    gains: list[float] = []
    graph_frequency: list[float] = []
    graph_before: list[float] = []
    graph_after: list[float] = []
    graph_variation: list[float] = []
    graph_confidence: list[float] = []
    graph_target: list[float] = []
    graph_effective_target: list[float] = []
    graph_correction: list[float] = []
    graph_decay: list[float | None] = []
    graph_decay_cut: list[float] = []
    woofer_target_cut: list[float] = []
    guarded_boost_bins = 0
    for index in range(fft_length // 2 + 1):
        frequency = index * RATE / fft_length
        safe_frequency = max(3.0, frequency)
        measured = interpolate_log(measure_f, measure_db, max(measure_f[0], min(measure_f[-1], safe_frequency))) - reference_measure
        variation = interpolate_log(measure_f, spatial_std_db, max(measure_f[0], min(measure_f[-1], safe_frequency)))
        noise_confidence = interpolate_log(measure_f, frequency_confidence, max(measure_f[0], min(measure_f[-1], safe_frequency))) if frequency_confidence else 1.0
        noise_confidence = max(0.0, min(1.0, noise_confidence))
        target_value = interpolate_log(target_f, target_db, max(target_f[0], min(target_f[-1], safe_frequency))) + preference_modifier_db(safe_frequency, bass_tilt_db, treble_tilt_db) - reference_target
        window = correction_window(safe_frequency, correction_low_hz, correction_high_hz)
        if woofer:
            correction = max(-float(max_cut_db), min(0.0, target_value - measured)) * window * noise_confidence if 20.0 <= frequency <= 180.0 else 0.0
            if 40.0 <= frequency <= 120.0:
                woofer_target_cut.append(correction)
        else:
            raw_correction = (target_value - measured) * window * noise_confidence
            if raw_correction > 0.0:
                # Deep, position-dependent nulls are not safely invertible. This is the
                # spatial regularization term: a 3 dB position spread halves the boost.
                reliability = 1.0 / (1.0 + (variation / 3.0) ** 2)
                raw_correction *= reliability
                boost_limit = float(max_boost_db if frequency < 500.0 else min(max_boost_db, 3))
                correction = boost_limit * math.tanh(raw_correction / max(boost_limit, 1.0e-9)) if boost_limit else 0.0
                if frequency < natural_low or frequency > natural_high:
                    correction = 0.0
                    guarded_boost_bins += 1
            else:
                correction = max(-float(max_cut_db), raw_correction)
            if not 20.0 <= frequency <= 20_000.0:
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
        if woofer or frequency <= 350.0:
            correction += modifier
        if woofer:
            correction += woofer_trim_db
        gains.append(correction)
        if index > 0 and (not graph_frequency or frequency / graph_frequency[-1] >= 1.025) and frequency <= 20_000:
            graph_frequency.append(round(frequency, 2))
            graph_before.append(round(measured, 3))
            graph_after.append(round(measured + correction, 3))
            graph_variation.append(round(variation, 3))
            graph_confidence.append(round(noise_confidence, 4))
            graph_target.append(round(target_value, 3))
            graph_effective_target.append(round(target_value + (modifier if woofer or frequency <= 350.0 else 0.0) - decay_cut + (woofer_trim_db if woofer else 0), 3))
            graph_decay.append(round(decay_value, 3) if decay_value is not None else None)
            graph_decay_cut.append(round(-decay_cut, 3))
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
        "target_db": graph_target,
        "effective_target_db": graph_effective_target,
        "correction_db": graph_correction,
        "decay_t20_rt60_s": graph_decay,
        "decay_control_db": graph_decay_cut,
        "phase": phase_details,
        "regularization": "3-position weighted dB prototype; pre/post noise confidence; variable perceptual smoothing; variance-weighted soft boost; natural-rolloff boost guard",
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
        "max_room_boost_db": max_boost_db,
        "max_room_cut_db": max_cut_db,
        "preference": {"bass_db_at_20_hz": bass_tilt_db, "treble_db_at_20_khz": treble_tilt_db},
        "woofer": woofer,
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


def build_mimo_worker(state: dict[str, Any], options: dict[str, Any]) -> None:
    if not platform_capabilities()["mimo_supported"]:
        raise MeasurementError("MIMO 계산과 활성화는 Raspberry Pi 4/5 전용입니다.")
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
        "max_room_cut_db": options["max_cut_db"],
    }
    updated = update_current(
        state="built", stage="2×4 MIMO 32768탭 bank 생성 완료 · 보고서와 예측을 확인하세요.",
        progress=100.0, eta_seconds=None, worker_pid=None, result=result,
    )
    atomic_json(session_path, updated)


def build_worker(target_name: str, preset: str, woofer_trim_db: int, phase_mode: str, phase_cutoff: int, spatial_mode: str = "equal", bass_tilt_db: int = 0, treble_tilt_db: int = 0, correction_low_hz: int = 20, correction_high_hz: int = 20_000, max_boost_db: int = 6, max_cut_db: int = 18, mimo_high_hz: int = 150, mimo_strength: str = "balanced", mimo_support_penalty_db: int = 6) -> None:
    state = load_current()
    if int(state.get("positions_completed", 0)) != POSITIONS:
        raise MeasurementError("세 위치 측정을 먼저 완료하세요.")
    if spatial_mode not in ("equal", "center") or not -6 <= bass_tilt_db <= 6 or not -6 <= treble_tilt_db <= 2:
        raise MeasurementError("공간 평균 또는 음색 선호값이 범위를 벗어났습니다.")
    if correction_low_hz not in (20, 30, 40, 60, 80) or correction_high_hz not in (300, 500, 1000, 5000, 20_000) or correction_low_hz >= correction_high_hz:
        raise MeasurementError("보정 주파수 범위가 잘못되었습니다.")
    if max_boost_db not in (0, 3, 6, 9) or max_cut_db not in (6, 9, 12, 18, 24):
        raise MeasurementError("최대 boost/cut 값이 잘못되었습니다.")
    if mimo_high_hz not in (80, 120, 150) or mimo_strength not in ("safe", "balanced", "maximum") or mimo_support_penalty_db not in (3, 6, 9, 12):
        raise MeasurementError("MIMO 보정 범위·강도·지원 제어원 제한값이 잘못되었습니다.")
    if state.get("mode") in MIMO_MODES:
        build_mimo_worker(state, {
            "target": target_name, "preset": preset, "woofer_trim_db": woofer_trim_db,
            "phase_mode": phase_mode, "phase_cutoff": phase_cutoff, "spatial_mode": spatial_mode,
            "bass_tilt_db": bass_tilt_db, "treble_tilt_db": treble_tilt_db,
            "correction_low_hz": correction_low_hz, "correction_high_hz": correction_high_hz,
            "max_boost_db": max_boost_db, "max_cut_db": max_cut_db,
            "mimo_high_hz": mimo_high_hz, "mimo_strength": mimo_strength,
            "mimo_support_penalty_db": mimo_support_penalty_db,
        })
        return
    estimates = platform_capabilities()["offline_estimates_seconds"]
    build_eta = int(estimates["fir_bass_phase"] if phase_mode == "bass" else estimates["fir_magnitude"])
    measured_woofer_attenuation_db = int(state.get("woofer_measurement_attenuation_db", WOOFER_MEASUREMENT_ATTENUATION_DB))
    if state.get("mode") == "lr" and woofer_trim_db != measured_woofer_attenuation_db:
        raise MeasurementError(
            "L+Woofer / R+Woofer 합산 측정에서는 최종 Woofer trim이 "
            f"측정 상대레벨({measured_woofer_attenuation_db} dB)과 같아야 합니다."
        )
    directory = Path(state["session_dir"])
    update_current(state="processing", stage="공간 평균 응답 계산", progress=5.0, eta_seconds=build_eta)
    fft = FFTBackend()
    left_source, right_source = (
        ("left_woofer", "right_woofer") if state.get("mode") == "lr" else ("left", "right")
    )
    left_response = load_average_response(directory, left_source, spatial_mode)
    right_response = load_average_response(directory, right_source, spatial_mode)
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
    update_current(stage="Left 32768탭 최소위상 FIR 계산", progress=22.0, eta_seconds=round(build_eta * 0.80))
    common = {"spatial_mode": spatial_mode, "bass_tilt_db": bass_tilt_db, "treble_tilt_db": treble_tilt_db, "correction_low_hz": correction_low_hz, "correction_high_hz": correction_high_hz, "max_boost_db": max_boost_db, "max_cut_db": max_cut_db, "fft": fft}
    front_design_phase_mode = "magnitude" if phase_mode == "bass" else phase_mode
    left_ir, left_graph = design_channel(left_f, left_db, left_response["spatial_std_db"], left_response["center_phase_rad"], target_name, preset, woofer=False, woofer_trim_db=0, phase_mode=front_design_phase_mode, phase_cutoff=phase_cutoff, decay_frequency_hz=left_response["decay_frequency_hz"], decay_t20_rt60_s=left_response["decay_t20_rt60_s"], frequency_confidence=left_response["frequency_confidence"], **common)
    update_current(stage="Right 32768탭 최소위상 FIR 계산", progress=50.0, eta_seconds=round(build_eta * 0.52))
    right_ir, right_graph = design_channel(right_f, right_db, right_response["spatial_std_db"], right_response["center_phase_rad"], target_name, preset, woofer=False, woofer_trim_db=0, phase_mode=front_design_phase_mode, phase_cutoff=phase_cutoff, decay_frequency_hz=right_response["decay_frequency_hz"], decay_t20_rt60_s=right_response["decay_t20_rt60_s"], frequency_confidence=right_response["frequency_confidence"], **common)
    if phase_mode == "bass":
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
    front = directory / "Generated_Front_LR_32768.wav"
    rear = None
    rear_graph = None
    rear_channels = None
    if state["mode"] == "lrw":
        update_current(stage="Woofer 32768탭 FIR 계산", progress=72.0, eta_seconds=round(build_eta * 0.30))
        woofer_response = load_average_response(directory, "woofer", spatial_mode)
        woofer_f, woofer_db = woofer_response["frequencies"], woofer_response["average_db"]
        woofer_ir, rear_graph = design_channel(
            woofer_f, woofer_db, woofer_response["spatial_std_db"], woofer_response["center_phase_rad"],
            target_name, preset, woofer=True, woofer_trim_db=woofer_trim_db,
            phase_mode=phase_mode, phase_cutoff=phase_cutoff,
            decay_frequency_hz=woofer_response["decay_frequency_hz"],
            decay_t20_rt60_s=woofer_response["decay_t20_rt60_s"],
            frequency_confidence=woofer_response["frequency_confidence"],
            shared_reference_measure_db=shared_front_reference_db,
            shared_reference_target_db=shared_target_reference_db,
            **common,
        )
        rear_channels = [woofer_ir, woofer_ir]
    decay_summary = {
        "left": dict(zip(left_response["decay_frequency_hz"], left_response["decay_t20_rt60_s"])),
        "right": dict(zip(right_response["decay_frequency_hz"], right_response["decay_t20_rt60_s"])),
        "woofer": dict(zip(woofer_response["decay_frequency_hz"], woofer_response["decay_t20_rt60_s"])) if state["mode"] == "lrw" else None,
    }
    time_alignment = {"enabled": False, "front_delay_samples": 0, "rear_delay_samples": 0}
    if state["mode"] == "lrw" and phase_mode == "bass" and rear_channels is not None:
        front_acoustic = round(statistics.median((left_response["center_bulk_delay_samples"], right_response["center_bulk_delay_samples"])))
        rear_acoustic = int(woofer_response["center_bulk_delay_samples"])
        front_filter = round(statistics.median((fir_energy_delay(left_ir), fir_energy_delay(right_ir))))
        rear_filter = round(statistics.median((fir_energy_delay(rear_channels[0]), fir_energy_delay(rear_channels[1]))))
        front_total = front_acoustic + front_filter
        rear_total = rear_acoustic + rear_filter
        target_delay = max(front_total, rear_total)
        # The later FIR path already determines system latency. Delaying only the
        # earlier path up to the full phase+acoustic budget restores crossover
        # coherence without increasing the latest output's latency.
        alignment_limit = MAX_PHASE_SHIFT + 960
        front_delay = min(alignment_limit, max(0, target_delay - front_total))
        rear_delay = min(alignment_limit, max(0, target_delay - rear_total))
        left_ir = delay_fir(left_ir, front_delay)
        right_ir = delay_fir(right_ir, front_delay)
        rear_channels = [delay_fir(rear_channels[0], rear_delay), delay_fir(rear_channels[1], rear_delay)]
        aligned_front_total = front_total + front_delay
        aligned_rear_total = rear_total + rear_delay
        time_alignment = {
            "enabled": True,
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
    for position in range(1, POSITIONS + 1):
        for source in state["sources"]:
            response_path = directory / f"p{position}_{source}_response.json"
            response_value = json.loads(response_path.read_text(encoding="utf-8"))
            quality = response_value.get("measurement_quality", {})
            if isinstance(quality.get("snr_db"), (int, float)):
                measurement_snrs.append(float(quality["snr_db"]))
    target_fit = {
        "left": left_graph["target_fit"],
        "right": right_graph["target_fit"],
        "woofer": rear_graph.get("target_fit") if isinstance(rear_graph, dict) else None,
    }
    implementation = {
        "left": left_graph["fir_implementation"],
        "right": right_graph["fir_implementation"],
        "woofer": rear_graph.get("fir_implementation") if isinstance(rear_graph, dict) else None,
    }
    channel_metrics = [front_metrics["left"], front_metrics["right"]]
    if rear_metrics:
        channel_metrics += [rear_metrics["left"], rear_metrics["right"]]
    core_checks = {
        "exact_32768_taps": all(item["taps"] == TAPS for item in channel_metrics),
        "finite_samples": all(item["finite"] for item in channel_metrics),
        "no_positive_transfer": all(item["transfer_pass"] for item in channel_metrics),
        "early_impulse": all(item["early_impulse_pass"] for item in channel_metrics),
        "fir_matches_design": all(
            item is None or item["pass"] for item in implementation.values()
        ),
    }
    diagnostics = {
        "lr_median_difference_db": round(statistics.median(lr_differences), 2) if lr_differences else None,
        "spatial_std_median_db": round(statistics.median(all_variation), 2) if all_variation else None,
        "spatial_high_variance_percent": round(100.0 * sum(value >= 6.0 for value in all_variation) / len(all_variation), 1) if all_variation else None,
        "measurement_snr_min_db": round(min(measurement_snrs), 2) if measurement_snrs else None,
        "measurement_snr_median_db": round(statistics.median(measurement_snrs), 2) if measurement_snrs else None,
        "warnings": [],
    }
    if diagnostics["lr_median_difference_db"] is not None and diagnostics["lr_median_difference_db"] > 4.0:
        diagnostics["warnings"].append("L/R 차이가 큽니다. 마이크 중심과 스피커 거리·toe-in을 확인하세요.")
    if diagnostics["spatial_high_variance_percent"] is not None and diagnostics["spatial_high_variance_percent"] > 15.0:
        diagnostics["warnings"].append("위치별 편차가 큰 대역이 많아 boost를 강하게 제한했습니다.")
    if diagnostics["measurement_snr_min_db"] is not None and diagnostics["measurement_snr_min_db"] < 15.0:
        diagnostics["warnings"].append("일부 sweep SNR이 권장 15 dB보다 낮습니다. 실제 측정에서는 레벨 또는 sweep 시간을 올리세요.")
    if not all(item is None or item["pass"] for item in target_fit.values()):
        diagnostics["warnings"].append("안전 제한 때문에 일부 채널이 선택 타겟을 허용 오차 안에서 완전히 달성하지 못했습니다.")
    long_bass_decay = [
        value for channels in decay_summary.values() if isinstance(channels, dict)
        for frequency, value in channels.items() if float(frequency) <= 125.0 and value > 0.70
    ]
    if long_bass_decay:
        diagnostics["warnings"].append("125 Hz 이하 잔향이 길어 해당 공진 대역에 최대 3 dB cut-only 감쇄를 적용했습니다.")
    result = {
        "preset": preset,
        "target": target_name,
        "woofer_trim_db": woofer_trim_db,
        "woofer_level_control": {
            "measurement_attenuation_compensated": state["mode"] == "lrw",
            "reference": "Front L/R spatial response median at 500-2000 Hz" if state["mode"] == "lrw" else "measured combined system response",
            "shared_front_reference_db": round(shared_front_reference_db, 3),
            "automatic_target_cut_median_db_40_120": rear_graph.get("automatic_target_cut_median_db_40_120") if state["mode"] == "lrw" and isinstance(rear_graph, dict) else None,
            "automatic_boost_allowed": False,
            "processing_order": ["target-relative automatic cut", "bass-control preset", "user woofer trim"],
        },
        "phase_mode": phase_mode,
        "phase_cutoff_hz": phase_cutoff if phase_mode == "bass" else None,
        "spatial_mode": spatial_mode,
        "position_weights": left_response["position_weights"],
        "smoothing": left_response["smoothing"],
        "preference": {"bass_db_at_20_hz": bass_tilt_db, "treble_db_at_20_khz": treble_tilt_db},
        "correction_limits": {"low_hz": correction_low_hz, "high_hz": correction_high_hz, "max_room_boost_db": max_boost_db, "max_room_cut_db": max_cut_db},
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
        "integration": integration_summary(
            directory,
            state.get("validation"),
            measured_woofer_attenuation_db,
        ),
        "measurement_output": {
            "mode": state.get("mode"),
            "signal_path": "L+Woofer / R+Woofer simultaneous" if state.get("mode") == "lr" else "Front L / Front R / Woofer separately",
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
            "overall_pass": all(core_checks.values()),
            "core_checks": core_checks,
            "target_fit": target_fit,
            "fir_implementation": implementation,
            "measurement_snr_db": {
                "minimum": diagnostics["measurement_snr_min_db"],
                "median": diagnostics["measurement_snr_median_db"],
                "recommended_minimum": 15.0,
            },
        },
        "graphs": {"left": left_graph, "right": right_graph, "woofer": rear_graph},
        "bass_reference": {
            "primus360": "96 Hz -7 dB, Q 3",
            "strong": "Primus360 + 140 Hz low shelf -9 dB + 63 Hz -5 dB",
        },
        "algorithm": {
            "prototype": "multi-position weighted power-response prototype",
            "regularization": "frequency-dependent spatial-variance reliability and soft boost bound",
            "rolloff_guard": "half-octave median -10 dB natural usable-band estimator",
            "phase": "minimum phase; optional low-frequency excess phase with acausality limit",
            "target": "named target plus optional bass/treble house-curve preference",
            "verification": "actual 32768-tap FIR FFT, normalized target-fit error, transfer/causality/SNR invariants",
            "reverberation": "octave-band noise-compensated Schroeder EDT/T20; reliable low-frequency decay controls cut-only damping",
        },
    }
    result["room_tuning_audit"] = build_room_tuning_audit(directory, state, mimo=False)
    result["report_json"] = "Room_Tuning_Report.json"
    result["report_md"] = "Room_Tuning_Report.md"
    atomic_json(directory / result["report_json"], result)
    write_room_tuning_report(directory / result["report_md"], state, result)
    state = update_current(state="built", stage="32768탭 FIR 생성 완료 · 그래프 확인 후 프로필에 적용하세요.", progress=100.0, eta_seconds=None, worker_pid=None, result=result)
    atomic_json(directory / "session.json", state)


def apply_result(profile: str) -> dict[str, Any]:
    state = load_current()
    if state.get("state") != "built" or not state.get("result"):
        raise MeasurementError("먼저 FIR을 생성하세요.")
    if profile not in ("speaker", "headphone"):
        raise MeasurementError("프로필이 잘못되었습니다.")
    directory = Path(state["session_dir"])
    if state["result"].get("kind") == "mimo_2x4":
        if profile != "speaker":
            raise MeasurementError("MIMO 2×4 결과는 Speaker 출력에만 적용할 수 있습니다.")
        if not state["result"].get("self_validation", {}).get("overall_pass"):
            raise MeasurementError("MIMO 자체 검증을 통과하지 않아 적용을 차단했습니다.")
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
    if profile not in ("speaker", "headphone"):
        raise MeasurementError("프로필이 잘못되었습니다.")
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
    try:
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
    """Exercise every target/preset through the real 32768-tap design path."""
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
        for preset in ("none", "primus360", "strong"):
            channels = {}
            for channel_name, woofer in (("front", False), ("woofer", True)):
                impulse, graph = design_channel(
                    frequencies, measured, variation, phase, target_name, preset,
                    woofer=woofer, woofer_trim_db=-12 if woofer else 0,
                    phase_mode="magnitude", phase_cutoff=200, fft=fft,
                )
                metrics = fir_metrics([impulse, impulse], fft)["left"]
                passed = bool(
                    len(impulse) == TAPS
                    and metrics["finite"]
                    and metrics["transfer_pass"]
                    and metrics["early_impulse_pass"]
                    and graph["fir_implementation"]["pass"]
                    and graph["target_fit"]["pass"]
                )
                all_pass = all_pass and passed
                channels[channel_name] = {
                    "pass": passed,
                    "target_fit": graph["target_fit"],
                    "fir_implementation": graph["fir_implementation"],
                    "maximum_transfer_db": metrics["maximum_transfer_db"],
                    "peak_tap": metrics["peak_tap"],
                }
            matrix.append({"target": target_name, "preset": preset, "channels": channels})

    phase_impulse, phase_graph = design_channel(
        frequencies, measured, variation, phase, "harman", "strong",
        woofer=False, woofer_trim_db=0, phase_mode="bass", phase_cutoff=200, fft=fft,
    )
    phase_metrics = fir_metrics([phase_impulse, phase_impulse], fft)["left"]
    phase_pass = bool(
        phase_metrics["finite"]
        and phase_metrics["transfer_pass"]
        and phase_metrics["early_impulse_pass"]
        and phase_graph["fir_implementation"]["pass"]
    )
    all_pass = all_pass and phase_pass
    return {
        "result": "PASS" if all_pass else "FAIL",
        "targets": list(TARGET_FILES),
        "presets": ["none", "primus360", "strong"],
        "combinations": len(matrix),
        "matrix": matrix,
        "bass_phase": {
            "pass": phase_pass,
            "phase": phase_graph["phase"],
            "fir_implementation": phase_graph["fir_implementation"],
            "metrics": phase_metrics,
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
    configure = sub.add_parser("configure")
    configure.add_argument("mode", choices=tuple(SOURCES))
    configure.add_argument("orientation", choices=("0", "90"))
    configure.add_argument("level_dbfs", type=int)
    configure.add_argument("sweep_seconds", type=int)
    configure.add_argument("noise_level_dbfs", type=int, nargs="?", default=None)
    configure.add_argument("woofer_measurement_attenuation_db", type=int, nargs="?", default=None)
    sub.add_parser("start-level")
    sub.add_parser("start-position")
    sub.add_parser("restart-positions")
    sub.add_parser("start-validation")
    inspect_recording = sub.add_parser("inspect-recording")
    inspect_recording.add_argument("position", type=int, choices=range(1, POSITIONS + 1))
    inspect_recording.add_argument("source", choices=tuple(SOURCE_LABELS))
    reprocess_recording = sub.add_parser("reprocess-recording")
    reprocess_recording.add_argument("position", type=int, choices=range(1, POSITIONS + 1))
    reprocess_recording.add_argument("source", choices=tuple(SOURCE_LABELS))
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
    build.add_argument("max_boost_db", type=int, choices=(0, 3, 6, 9), nargs="?", default=6)
    build.add_argument("max_cut_db", type=int, choices=(6, 9, 12, 18, 24), nargs="?", default=18)
    build.add_argument("mimo_high_hz", type=int, choices=(80, 120, 150), nargs="?", default=150)
    build.add_argument("mimo_strength", choices=("safe", "balanced", "maximum"), nargs="?", default="balanced")
    build.add_argument("mimo_support_penalty_db", type=int, choices=(3, 6, 9, 12), nargs="?", default=6)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("profile", choices=("speaker", "headphone"))
    preview_parser = sub.add_parser("preview")
    preview_parser.add_argument("profile", choices=("speaker", "headphone"))
    sub.add_parser("restore")
    sub.add_parser("cancel")
    sub.add_parser("_worker-level")
    sub.add_parser("_worker-position")
    sub.add_parser("_worker-validation")
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
    worker_build.add_argument("max_boost_db", type=int, nargs="?", default=6)
    worker_build.add_argument("max_cut_db", type=int, nargs="?", default=18)
    worker_build.add_argument("mimo_high_hz", type=int, nargs="?", default=150)
    worker_build.add_argument("mimo_strength", nargs="?", default="balanced")
    worker_build.add_argument("mimo_support_penalty_db", type=int, nargs="?", default=6)
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
                args.noise_level_dbfs, args.woofer_measurement_attenuation_db,
            )
        elif args.command == "configure":
            result = reconfigure_session(
                args.mode, args.orientation, args.level_dbfs, args.sweep_seconds,
                args.noise_level_dbfs, args.woofer_measurement_attenuation_db,
            )
        elif args.command == "start-level":
            prepare_level_check()
            result = spawn_worker("_worker-level")
        elif args.command == "start-position":
            state = load_current()
            if state.get("orientation") != "90":
                raise MeasurementError("UMIK를 천장 방향 90°로 놓고 새 session을 만드세요.")
            result = spawn_worker("_worker-position")
        elif args.command == "restart-positions":
            prepare_position_restart()
            result = spawn_worker("_worker-position")
        elif args.command == "start-validation":
            result = spawn_worker("_worker-validation")
        elif args.command == "inspect-recording":
            result = inspect_saved_recording(args.position, args.source)
        elif args.command == "reprocess-recording":
            result = inspect_saved_recording(args.position, args.source, reprocess=True)
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
            })
            result = spawn_worker("_worker-build", args.target, args.preset, str(args.woofer_trim_db), args.phase_mode, str(args.phase_cutoff), args.spatial_mode, str(args.bass_tilt_db), str(args.treble_tilt_db), str(args.correction_low_hz), str(args.correction_high_hz), str(args.max_boost_db), str(args.max_cut_db), str(args.mimo_high_hz), args.mimo_strength, str(args.mimo_support_penalty_db))
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
        elif args.command == "_worker-position":
            return worker_guard(measure_position_worker)
        elif args.command == "_worker-validation":
            return worker_guard(validation_worker)
        elif args.command == "_worker-build":
            return worker_guard(lambda: build_worker(args.target, args.preset, args.woofer_trim_db, args.phase_mode, args.phase_cutoff, args.spatial_mode, args.bass_tilt_db, args.treble_tilt_db, args.correction_low_hz, args.correction_high_hz, args.max_boost_db, args.max_cut_db, args.mimo_high_hz, args.mimo_strength, args.mimo_support_penalty_db))
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
