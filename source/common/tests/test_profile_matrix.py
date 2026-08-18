#!/usr/bin/env python3
"""Exhaustive isolated regression tests for AudioDSP profiles, Web UI, and U7 HID."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile
import io


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_module(name: str, path: Path):
    if os.name == "nt" and "fcntl" not in sys.modules:
        fcntl_stub = types.ModuleType("fcntl")
        fcntl_stub.LOCK_EX = 2
        fcntl_stub.flock = lambda _handle, _operation: None
        sys.modules["fcntl"] = fcntl_stub
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_bytes(format_code: int, bits: int, value: float) -> bytes:
    value = max(-1.0, min(1.0, value))
    if format_code == 3 and bits == 32:
        return struct.pack("<f", value)
    if format_code == 3 and bits == 64:
        return struct.pack("<d", value)
    if format_code == 1 and bits == 16:
        return struct.pack("<h", round(value * 32767))
    if format_code == 1 and bits == 24:
        integer = round(value * 8388607)
        if integer < 0:
            integer += 1 << 24
        return bytes((integer & 0xFF, (integer >> 8) & 0xFF, (integer >> 16) & 0xFF))
    if format_code == 1 and bits == 32:
        return struct.pack("<i", round(value * 2147483647))
    raise ValueError((format_code, bits))


def write_wave(
    path: Path,
    *,
    format_code: int = 3,
    bits: int = 32,
    channels: int = 2,
    rate: int = 48000,
    frames: int = 32,
    left: float = 1.0,
    right: float = 0.5,
    nan_first: bool = False,
) -> None:
    payload = bytearray()
    for frame in range(frames):
        for channel in range(channels):
            value = (left if channel == 0 else right) if frame == 0 else 0.0
            if nan_first and frame == 0 and channel == 0 and format_code == 3:
                payload += struct.pack("<f" if bits == 32 else "<d", math.nan)
            else:
                payload += sample_bytes(format_code, bits, value)
    block_align = channels * ((bits + 7) // 8)
    fmt = struct.pack("<HHIIHH", format_code, channels, rate, rate * block_align, block_align, bits)
    riff_size = 4 + 8 + len(fmt) + 8 + len(payload)
    path.write_bytes(
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(payload)) + payload
    )


def run_checked(arguments: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    require(result.returncode == 0, f"Command failed ({result.returncode}): {' '.join(arguments)}\n{result.stdout}")
    return result


def expected_resolution(settings: dict[str, Any], present: dict[str, dict[str, bool]], factory: bool) -> dict[str, Any] | None:
    requested = settings["requested_profile"]
    other = "headphone" if requested == "speaker" else "speaker"
    if settings["bypass"][requested] or present[requested]["front"]:
        effective = requested
    elif settings["bypass"][other] or present[other]["front"]:
        effective = other
    elif factory:
        effective = "factory"
    else:
        return None
    bypass = False if effective == "factory" else settings["bypass"][effective]
    configured = "copy_front" if effective == "factory" else settings["rear_mode"][effective]
    woofer_trim_db = 0 if effective == "factory" else settings["woofer_trim_db"][effective]
    separate = effective != "factory" and not bypass and configured == "separate" and present[effective]["rear"]
    mode = "bypass" if bypass else ("separate" if separate else "copy_front")
    return {
        "effective_profile": effective,
        "chunksize": settings["chunksize"],
        "bypass": bypass,
        "configured_rear_mode": configured,
        "effective_rear_mode": mode,
        "convolution_channels": 0 if bypass else (4 if separate else 2),
        "woofer_trim_db": woofer_trim_db,
    }


def set_file(path: Path, source: Path, present: bool) -> None:
    if present:
        shutil.copyfile(source, path)
    else:
        path.unlink(missing_ok=True)


def verify_config_shape(config: bytes, resolved: dict[str, Any]) -> None:
    text = config.decode()
    require(f"chunksize: {resolved['chunksize']}" in text, "chunksize mismatch")
    require('channels: 4' in text, "four-channel playback missing")
    require("type: Gain" not in text, "unexpected digital preamp")
    require(text.count("type: Conv") == resolved["convolution_channels"], "convolution count mismatch")
    require(text.count(f"gain: {resolved['woofer_trim_db']}") >= 2, "woofer trim mismatch")
    pipeline = text.split("pipeline:\n", 1)[1]
    if resolved["bypass"]:
        require("filters: {}" in text, "bypass filters are not empty")
        require(pipeline.lstrip().startswith("- type: Mixer"), "bypass must start with mixer")
    elif resolved["effective_rear_mode"] == "copy_front":
        require(pipeline.find("- type: Filter") < pipeline.find("- type: Mixer"), "copy mode order is wrong")
    else:
        require(pipeline.lstrip().startswith("- type: Mixer"), "separate mode must duplicate before filtering")


def post_form(url: str, fields: dict[str, str], expected: int = 200) -> bytes:
    request = Request(url, data=urlencode(fields).encode(), method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read()
            code = response.status
    except HTTPError as exc:
        body = exc.read()
        code = exc.code
    require(code == expected, f"POST {url} expected {expected}, got {code}: {body[:300]!r}")
    return body


def put_json(url: str, payload: Any, expected: int = 200) -> bytes:
    request = Request(url, data=json.dumps(payload).encode(), method="PUT")
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read()
            code = response.status
    except HTTPError as exc:
        body = exc.read()
        code = exc.code
    require(code == expected, f"PUT {url} expected {expected}, got {code}: {body[:300]!r}")
    return body


def post_wave(url: str, profile: str, band: str, path: Path, expected: int = 200) -> bytes:
    boundary = "----AudioDSPMatrixBoundary"
    chunks: list[bytes] = []
    for name, value in (("profile", profile), ("band", band)):
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"wav\"; filename=\"{path.name}\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
        + path.read_bytes() + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    request = Request(url, data=b"".join(chunks), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            code = response.status
    except HTTPError as exc:
        body = exc.read()
        code = exc.code
    require(code == expected, f"UPLOAD {profile}/{band} expected {expected}, got {code}: {body[:300]!r}")
    return body


def post_calibration(url: str, orientation: str, path: Path, expected: int = 200) -> bytes:
    boundary = "----AudioDSPCalibrationBoundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"orientation\"\r\n\r\n{orientation}\r\n".encode()
        + f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\nContent-Type: text/plain\r\n\r\n".encode()
        + path.read_bytes() + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
            code = response.status
    except HTTPError as exc:
        response_body = exc.read()
        code = exc.code
    require(code == expected, f"CAL {orientation} expected {expected}, got {code}: {response_body[:300]!r}")
    return response_body


def post_backup(url: str, payload: bytes, filename: str = "AudioDSP_backup.zip", expected: int = 200) -> bytes:
    boundary = "----AudioDSPBackupBoundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"backup\"; filename=\"{filename}\"\r\nContent-Type: application/zip\r\n\r\n".encode()
        + payload + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
            code = response.status
    except HTTPError as exc:
        response_body = exc.read()
        code = exc.code
    require(code == expected, f"BACKUP UPLOAD expected {expected}, got {code}: {response_body[:300]!r}")
    return response_body


def get_bytes(url: str, expected: int = 200) -> tuple[bytes, dict[str, str]]:
    try:
        with urlopen(url, timeout=15) as response:
            body = response.read()
            require(response.status == expected, f"GET {url} expected {expected}, got {response.status}")
            return body, dict(response.headers.items())
    except HTTPError as exc:
        body = exc.read()
        require(exc.code == expected, f"GET {url} expected {expected}, got {exc.code}: {body[:300]!r}")
        return body, dict(exc.headers.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", type=Path, required=True)
    parser.add_argument("--web", type=Path, required=True)
    parser.add_argument("--monitor", type=Path, required=True)
    parser.add_argument("--switcher", type=Path, required=True)
    parser.add_argument("--camilladsp", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--cal-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()

    algorithm_revision = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in args.measurement.read_text(encoding="utf-8").splitlines()
        if line.startswith("RESULT_ALGORITHM_REVISION = ")
    )

    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="audiodsp-profile-matrix-") as temporary_name:
        root = Path(temporary_name)
        config = root / "config"
        profiles = config / "profiles"
        state = root / "state"
        assets = root / "assets"
        share = root / "share"
        measurements = root / "measurements"
        calibration = root / "calibration"
        targets = root / "targets"
        staging = root / "upload-staging"
        for directory in (profiles, state, assets, share, measurements, calibration, targets, staging):
            directory.mkdir(parents=True)

        volume_state = root / "u7-volume.raw"
        volume_state.write_text("117\n", encoding="ascii")
        fake_amixer = root / "fake-amixer"
        fake_amixer.write_text(
            "#!/bin/sh\n"
            f"state={str(volume_state)!r}\n"
            "if [ \"$3\" = cget ] && [ \"$4\" = numid=6 ]; then\n"
            "  raw=$(cat \"$state\")\n"
            "  echo \"numid=6,iface=MIXER,name='PCM Playback Volume'\"\n"
            "  echo '; type=INTEGER,access=rw---R--,values=8,min=0,max=127,step=0'\n"
            "  echo \"  : values=$raw,$raw,$raw,$raw,$raw,$raw,$raw,$raw\"\n"
            "  echo '  | dBminmax-min=-12700,max=0'\n"
            "elif [ \"$3\" = set ] && [ \"$4\" = PCM,0 ]; then\n"
            "  printf '%s\\n' \"$5\" > \"$state\"\n"
            "  echo \"PCM Playback Volume set to $5\"\n"
            "else\n"
            "  exit 2\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_amixer.chmod(0o755)

        for source in args.cal_dir.glob("*.txt"):
            shutil.copyfile(source, calibration / source.name)
        for source in args.target_dir.glob("*.txt"):
            shutil.copyfile(source, targets / source.name)

        environment = os.environ.copy()
        environment.update({
            "AUDIODSP_CONFIG_DIR": str(config),
            "AUDIODSP_STATE_DIR": str(state),
            "AUDIODSP_LOCK_PATH": str(root / "manager.lock"),
            "AUDIODSP_SELECTOR_STATE_PATH": str(root / "u7-selector-state.json"),
            "AUDIODSP_PREVIEW_STATE_PATH": str(root / "fir-preview.json"),
            "AUDIODSP_CAMILLADSP": str(args.camilladsp),
            "AUDIODSP_DISABLE_SERVICE_RESTART": "1",
            "AUDIODSP_MEASUREMENT": str(args.measurement),
            "AUDIODSP_MEASUREMENT_DIR": str(measurements),
            "AUDIODSP_CAL_DIR": str(calibration),
            "AUDIODSP_TARGET_DIR": str(targets),
            "AUDIODSP_MEASUREMENT_LOCK": str(root / "measurement.lock"),
            "AUDIODSP_AUDIO_LOCK": str(root / "audio.lock"),
            "AUDIODSP_STAGING_DIR": str(staging),
            "AUDIODSP_PREFERENCES_PATH": str(state / "correction-preferences.json"),
            "AUDIODSP_AMIXER": str(fake_amixer),
            "AUDIODSP_U7_MIXER": "hw:U7-test",
        })
        os.environ.update({key: value for key, value in environment.items() if key.startswith("AUDIODSP_")})
        manager = load_module("audiodsp_profile_manager_matrix", args.manager)

        sources = {
            "speaker_front": assets / "speaker-front.wav",
            "speaker_rear": assets / "speaker-rear.wav",
            "headphone_front": assets / "headphone-front.wav",
            "headphone_rear": assets / "headphone-rear.wav",
        }
        write_wave(sources["speaker_front"], left=1.0, right=0.8)
        write_wave(sources["speaker_rear"], left=0.7, right=0.6)
        write_wave(sources["headphone_front"], left=0.5, right=0.4)
        write_wave(sources["headphone_rear"], left=0.3, right=0.2)
        factory_source = assets / "factory.wav"
        write_wave(factory_source, left=0.9, right=0.9)

        # Exhaustive state truth table: profile dimensions across all supported chunksizes.
        matrix_valid = 0
        matrix_errors = 0
        configs: dict[str, bytes] = {}
        booleans = (False, True)
        for values in itertools.product(("speaker", "headphone"), manager.ALLOWED_CHUNKSIZES,
                                        booleans, booleans, booleans, booleans,
                                        booleans, booleans, ("copy_front", "separate"),
                                        ("copy_front", "separate"), booleans):
            (requested, chunksize, bypass_s, bypass_h, front_s, front_h, rear_s, rear_h,
             mode_s, mode_h, factory_present) = values
            present = {
                "speaker": {"front": front_s, "rear": rear_s},
                "headphone": {"front": front_h, "rear": rear_h},
            }
            set_file(manager.PROFILE_FILES["speaker"]["front"], sources["speaker_front"], front_s)
            set_file(manager.PROFILE_FILES["speaker"]["rear"], sources["speaker_rear"], rear_s)
            set_file(manager.PROFILE_FILES["headphone"]["front"], sources["headphone_front"], front_h)
            set_file(manager.PROFILE_FILES["headphone"]["rear"], sources["headphone_rear"], rear_h)
            set_file(manager.FACTORY_FRONT, factory_source, factory_present)
            settings = {
                "requested_profile": requested,
                "chunksize": chunksize,
                "bypass": {"speaker": bypass_s, "headphone": bypass_h},
                "rear_mode": {"speaker": mode_s, "headphone": mode_h},
                # Representative values exercise factory, fallback, bypass, copy, and separate topologies.
                # Every supported integer is covered separately by the transition matrix below.
                "woofer_trim_db": {
                    "speaker": -18 if bypass_s else (-9 if front_s else 0),
                    "headphone": -18 if bypass_h else (-9 if front_h else 0),
                },
            }
            expected = expected_resolution(settings, present, factory_present)
            if expected is None:
                try:
                    manager.resolve_profile(settings)
                except manager.ProfileError:
                    matrix_errors += 1
                else:
                    raise AssertionError(f"Expected no-profile error: {settings}, {present}, factory={factory_present}")
                continue
            resolved = manager.resolve_profile(settings)
            for key, value in expected.items():
                require(resolved[key] == value, f"matrix mismatch {key}: {resolved} expected {expected}")
            generated = manager.build_config(resolved)
            verify_config_shape(generated, resolved)
            configs[hashlib.sha256(generated).hexdigest()] = generated
            matrix_valid += 1
        report["state_matrix"] = {"total": 4096, "valid": matrix_valid, "expected_errors": matrix_errors}
        require(matrix_valid + matrix_errors == 4096, "matrix total mismatch")

        # Every unique generated topology/config is checked by the real CamillaDSP parser.
        for profile, bands in manager.PROFILE_FILES.items():
            for band, path in bands.items():
                shutil.copyfile(sources[f"{profile}_{band}"], path)
        shutil.copyfile(factory_source, manager.FACTORY_FRONT)
        for index, generated in enumerate(configs.values()):
            candidate = root / f"matrix-config-{index}.yml"
            candidate.write_bytes(generated)
            run_checked([str(args.camilladsp), "--check", str(candidate)])
        report["camilladsp_unique_configs"] = len(configs)

        # WAV validation acceptance and rejection matrix.
        accepted = []
        for format_code, bits in ((1, 16), (1, 24), (1, 32), (3, 32), (3, 64)):
            path = assets / f"accepted-{format_code}-{bits}.wav"
            write_wave(path, format_code=format_code, bits=bits)
            metadata = manager.validate_wav(path)
            require(metadata["bits"] == bits and metadata["sample_rate"] == 48000, "accepted WAV metadata mismatch")
            accepted.append(f"{format_code}:{bits}")
        rejected: dict[str, Path] = {}
        rejected["mono"] = assets / "bad-mono.wav"
        write_wave(rejected["mono"], channels=1)
        rejected["rate"] = assets / "bad-rate.wav"
        write_wave(rejected["rate"], rate=44100)
        rejected["nan"] = assets / "bad-nan.wav"
        write_wave(rejected["nan"], nan_first=True)
        rejected["too_long"] = assets / "bad-too-long.wav"
        write_wave(rejected["too_long"], frames=manager.MAX_FIR_FRAMES + 1)
        rejected["not_wave"] = assets / "not-wave.wav"
        rejected["not_wave"].write_bytes(b"not a wave file")
        rejected["too_large"] = assets / "too-large.wav"
        with rejected["too_large"].open("wb") as handle:
            handle.truncate(manager.MAX_WAV_BYTES + 1)
        for name, path in rejected.items():
            try:
                manager.validate_wav(path)
            except manager.ProfileError:
                pass
            else:
                raise AssertionError(f"Invalid WAV was accepted: {name}")
        report["wav_validation"] = {"accepted": accepted, "rejected": sorted(rejected)}

        # All ordered pairs of UI setting operations.
        fake_camilla = root / "fake-camilladsp"
        fake_camilla.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_camilla.chmod(0o755)
        real_camilla = manager.CAMILLADSP
        manager.CAMILLADSP = fake_camilla
        baseline = copy.deepcopy(manager.DEFAULT_SETTINGS)
        operations: list[tuple[str, Callable[[], Any], Callable[[dict[str, Any]], None]]] = []
        for profile in ("speaker", "headphone"):
            operations.append((f"activate:{profile}", lambda p=profile: manager.activate(p, restart=False),
                               lambda s, p=profile: s.__setitem__("requested_profile", p)))
            for mode in ("copy_front", "separate"):
                operations.append((f"rear:{profile}:{mode}", lambda p=profile, m=mode: manager.set_rear_mode(p, m, restart=False),
                                   lambda s, p=profile, m=mode: s["rear_mode"].__setitem__(p, m)))
            for enabled in (False, True):
                operations.append((f"bypass:{profile}:{enabled}", lambda p=profile, e=enabled: manager.set_bypass(p, e, restart=False),
                                   lambda s, p=profile, e=enabled: s["bypass"].__setitem__(p, e)))
            for trim_db in manager.ALLOWED_WOOFER_TRIMS:
                operations.append((f"woofer-trim:{profile}:{trim_db}", lambda p=profile, t=trim_db: manager.set_woofer_trim(p, t, restart=False),
                                   lambda s, p=profile, t=trim_db: s["woofer_trim_db"].__setitem__(p, t)))
        for chunksize in manager.ALLOWED_CHUNKSIZES:
            operations.append((f"chunksize:{chunksize}", lambda c=chunksize: manager.set_chunksize(c, restart=False),
                               lambda s, c=chunksize: s.__setitem__("chunksize", c)))
        for volume_db in (-60, -30, -10, 0):
            operations.append((f"volume:{volume_db}", lambda v=volume_db: manager.set_output_volume(v, apply_hardware=False),
                               lambda s, v=volume_db: s.__setitem__("output_volume_db", v)))
        pair_count = 0
        for _name_a, call_a, expect_a in operations:
            for _name_b, call_b, expect_b in operations:
                manager.save_settings(copy.deepcopy(baseline))
                expected_settings = copy.deepcopy(baseline)
                call_a()
                expect_a(expected_settings)
                call_b()
                expect_b(expected_settings)
                require(manager.load_settings() == expected_settings, "ordered setting transition mismatch")
                manager.resolve_profile(expected_settings)
                pair_count += 1
        report["ordered_setting_pairs"] = {"operations": len(operations), "pairs": pair_count}

        # Restart readiness must require a continuously active process for one full second.
        original_run = manager.subprocess.run
        original_service_active = manager.service_active
        original_monotonic = manager.time.monotonic
        original_sleep = manager.time.sleep
        fake_clock = 0.0
        readiness_polls = 0

        class FakeResult:
            returncode = 0

        def fake_run(arguments: list[str], **_kwargs: Any):
            nonlocal readiness_polls
            if arguments[:2] == ["pgrep", "-x"]:
                readiness_polls += 1
            return FakeResult()

        def fake_sleep(seconds: float) -> None:
            nonlocal fake_clock
            fake_clock += seconds

        manager.subprocess.run = fake_run
        manager.service_active = lambda: True
        manager.time.monotonic = lambda: fake_clock
        manager.time.sleep = fake_sleep
        try:
            manager.restart_camilladsp()
        finally:
            manager.subprocess.run = original_run
            manager.service_active = original_service_active
            manager.time.monotonic = original_monotonic
            manager.time.sleep = original_sleep
        require(readiness_polls >= 11 and fake_clock >= 1.0, "restart readiness returned before one stable second")
        report["restart_readiness"] = {"stable_seconds": round(fake_clock, 1), "polls": readiness_polls}

        # Upload replacement, backup, invalid-file preservation, and apply rollback.
        manager.save_settings(copy.deepcopy(baseline))
        upload_count = 0
        for profile in ("speaker", "headphone"):
            for band in ("front", "rear"):
                replacement = assets / f"upload-{profile}-{band}.wav"
                write_wave(replacement, left=0.11 + upload_count * 0.05, right=0.07)
                result = manager.upload(profile, band, replacement, replacement.name)
                require(result["uploaded"]["metadata"]["frames"] == 32, "upload metadata mismatch")
                upload_count += 1
        require(any(manager.BACKUP_DIR.iterdir()), "upload backup was not created")
        target = manager.PROFILE_FILES["speaker"]["front"]
        preserved = target.read_bytes()
        try:
            manager.upload("speaker", "front", rejected["rate"], "bad-rate.wav")
        except manager.ProfileError:
            pass
        else:
            raise AssertionError("invalid upload was accepted")
        require(target.read_bytes() == preserved, "invalid upload modified active FIR")
        rollback_source = assets / "rollback.wav"
        write_wave(rollback_source, left=0.02, right=0.03)
        original_apply = manager.apply_settings
        apply_calls = 0

        def fail_once(settings: dict[str, Any], restart: bool = True):
            nonlocal apply_calls
            apply_calls += 1
            if apply_calls == 1:
                raise manager.ProfileError("injected apply failure")
            return original_apply(settings, restart=False)

        manager.apply_settings = fail_once
        try:
            try:
                manager.upload("speaker", "front", rollback_source, rollback_source.name)
            except manager.ProfileError:
                pass
            else:
                raise AssertionError("injected apply failure did not propagate")
        finally:
            manager.apply_settings = original_apply
        require(target.read_bytes() == preserved, "failed apply did not restore previous FIR")
        report["uploads"] = {"valid": upload_count, "invalid_preserved": True, "rollback_restored": True}

        # Restore real parser for one final manager apply.
        manager.CAMILLADSP = real_camilla
        manager.apply_settings(manager.load_settings(), restart=False)

        # A combined L+Woofer / R+Woofer result has no separate Rear WAV. Its
        # measured balance must therefore be applied by the temporary runtime
        # mixer while retaining a two-channel convolution topology.
        combined_preview = manager.preview_pair("speaker", sources["speaker_front"], None, -9)
        require(combined_preview["convolution_channels"] == 2, "combined preview did not use one stereo convolution")
        require(combined_preview["woofer_trim_db"] == -9, "combined preview lost measured woofer trim")
        require(b"gain: -9" in manager.CONFIG_PATH.read_bytes(), "combined preview mixer trim missing from CamillaDSP config")
        manager.restore_profile(restart=False)
        report["combined_preview"] = {"convolution_channels": 2, "woofer_trim_db": -9}

        # HID reports: short, unpressed, both known states, extra mask bits, unknown state.
        monitor = load_module("audiodsp_u7_monitor_matrix", args.monitor)
        hid_cases = 0
        require(monitor.decode_pressed_profile(b"") is None, "short HID report accepted")
        hid_cases += 1
        report_bytes = bytearray(64)
        report_bytes[monitor.STATE_BYTE] = 0x30
        require(monitor.decode_pressed_profile(bytes(report_bytes)) is None, "unpressed HID report accepted")
        hid_cases += 1
        for state_byte, expected_profile in ((0x30, "headphone"), (0xA0, "speaker")):
            report_bytes = bytearray(64)
            report_bytes[monitor.PRESS_BYTE] = monitor.PRESS_MASK
            report_bytes[monitor.STATE_BYTE] = state_byte
            require(monitor.decode_pressed_profile(bytes(report_bytes)) == expected_profile, "known HID state mismatch")
            hid_cases += 1
        report_bytes[monitor.PRESS_BYTE] = monitor.PRESS_MASK | 0x01
        require(monitor.decode_pressed_profile(bytes(report_bytes)) == "speaker", "HID mask handling mismatch")
        hid_cases += 1
        report_bytes[monitor.STATE_BYTE] = 0x55
        require(monitor.decode_pressed_profile(bytes(report_bytes)) is None, "unknown HID state accepted")
        hid_cases += 1
        fake_hidraw = root / "hidraw-test"
        fake_hidraw.write_bytes(b"")
        original_ioctl = monitor.fcntl.ioctl

        def fake_ioctl(_descriptor: int, _request: int, data: bytearray, _mutate: bool) -> int:
            payload = bytearray(16)
            payload[monitor.STATE_BYTE] = 0xA0
            data[0] = 0
            data[1:17] = payload
            return 17

        monitor.fcntl.ioctl = fake_ioctl
        try:
            initial_profile, initial_state = monitor.current_profile(str(fake_hidraw))
        finally:
            monitor.fcntl.ioctl = original_ioctl
        require((initial_profile, initial_state) == ("speaker", 0xA0), "initial HIDIOCGINPUT parsing mismatch")
        monitor.save_selector_state(initial_profile, initial_state, "matrix")
        selector = manager.selector_status()
        require(selector["profile"] == "speaker" and not selector["stale"], "selector state persistence mismatch")
        hid_cases += 1
        report["hid_reports"] = hid_cases

        # Isolated live HTTP integration, including every button type and concurrent writes.
        for prompt in ("announce_speaker_48k_front_lr.wav", "announce_headphone_48k_front_lr.wav"):
            (share / prompt).write_bytes(b"test prompt")
        aplay_log = root / "aplay.log"
        fake_aplay = root / "fake-aplay"
        fake_aplay.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {str(aplay_log)!r}\n", encoding="utf-8")
        fake_aplay.chmod(0o755)
        args.switcher.chmod(0o755)
        environment.update({
            "AUDIODSP_PROFILE_MANAGER": str(args.manager),
            "AUDIODSP_SHARE_DIR": str(share),
            "AUDIODSP_APLAY": str(fake_aplay),
            "AUDIODSP_WEB_HOST": "127.0.0.1",
        })
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment["AUDIODSP_WEB_PORT"] = str(port)
        run_checked(["/usr/bin/python3", str(args.manager), "activate", "speaker", "--no-restart"], env=environment)
        web_log = (root / "web.log").open("w", encoding="utf-8")
        server = subprocess.Popen(
            ["/usr/bin/python3", str(args.web)],
            env=environment,
            stdout=web_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        previous_signal_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGHUP, signal.SIGTERM)
        }

        def interrupted_test(_signum, _frame):
            raise KeyboardInterrupt("profile matrix interrupted")

        for signum in previous_signal_handlers:
            signal.signal(signum, interrupted_test)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(50):
                try:
                    get_bytes(base + "/api/status")
                    break
                except Exception:
                    if server.poll() is not None:
                        raise AssertionError(f"Web server exited: {server.returncode}")
                    time.sleep(0.1)
            else:
                raise AssertionError("Web server did not listen")

            status_page, _ = get_bytes(base + "/?woofer=1")
            for marker in ("현황", "측정 · 보정", "프로필 · 설정", "현재 설정", "현재 FIR 보정 전달함수", "목표 청취 음압 그래프가 아닙니다", "시스템 상태", "지금 할 일", "출력 볼륨", "output-volume-control", "오디오 신호 흐름", "signal-flow", "U7 PHYSICAL SELECTOR", "두 U7 경로 모두 스피커에 연결됨"):
                require(marker.encode("utf-8") in status_page, f"Status-page marker missing: {marker}")
            require(b"measurement card-wide" not in status_page and b"Front WAV" not in status_page, "status page contains another screen")
            require("<title>현황 · AudioDSP</title>".encode("utf-8") in status_page, "status page title is not contextual")
            measure_page, _ = get_bytes(base + "/measure")
            for marker in (b"32768", b"UMIK-1", b"target-graph", b"job-progress", b"workflow", b"cal-card", b"session-overview", b"session-library", b'name="noise_level_dbfs"', b'name="woofer_measurement_attenuation_db"', b'value="-42"', b"measurement-path-lock", b'data-measurement-path="unbound"', "Session 생성 · 2단계 출력 설정으로".encode("utf-8"), "실제 White noise·Sweep 출력은 다음 2단계".encode("utf-8"), "L+Woofer / R+Woofer".encode("utf-8"), "정밀 분리+합산".encode("utf-8"), "L/R/W/L+W/R+W".encode("utf-8"), "Front L, Front R, Woofer".encode("utf-8"), "90° · 천장 방향".encode("utf-8"), "0° · 마이크 정면".encode("utf-8"), "Fast · 1위치".encode("utf-8"), "Standard · 3위치".encode("utf-8"), "활성 Session 없음".encode("utf-8")):
                require(marker in measure_page, f"Measurement-page marker missing: {marker!r}")
            web_source = args.web.read_text(encoding="utf-8")
            for marker in ("실제 측정음을 재생합니다", "저역 late/early", "기존 SISO 저역 레벨", "1.5 dB 넘게 악화", "실제 RT60/잔향 예측이 아니며", "디지털 Crossover", "LR4 HPF", "additional_block_latency_samples", "set-session-note", "load-session", "build-fieldset", "validation-checklist", "음색 시작점", "Target 그대로", "맑은 고음", "따뜻한 균형", "야간 균형", "사후 실측 대기", "premeasured_sum_validation", "필터 전 L/R/W 복소 합산 모델", "Woofer 복소합 정렬", "sum_guard_enabled&&j.result.crossover?.channels", "--step-accent", "summary::after", "details[open]>summary::after"):
                require(marker in web_source, f"Measurement/MIMO safety UI source marker missing: {marker}")
            require(b'role="tab" class="flow-step current"' in measure_page, "current measurement step is not an accessible non-destructive tab")
            require(b'aria-current="step"' in measure_page, "current measurement step lacks accessible state")
            require("<title>측정 · 보정 · AudioDSP</title>".encode("utf-8") in measure_page, "measurement page title is not contextual")
            require(b"/measurement/rewind" not in measure_page, "step navigation unexpectedly discards data")
            require(b"current FIR" not in measure_page and b"data-profile=" not in measure_page, "measurement page contains another screen")
            settings_page, _ = get_bytes(base + "/settings")
            for marker in (b"DSP Bypass", b"MIMO 2", b"Front WAV", b"Rear WAV", b"chunksize", b"live_u7_status_poll", b"profile-mini-flow", "Speaker 출력 체인".encode("utf-8"), "Headphone 잭 출력 체인".encode("utf-8"), "전체 백업 · 안전 복원".encode("utf-8"), b"schema v2"):
                require(marker in settings_page, f"Settings-page marker missing: {marker!r}")
            for marker in (b'id="speaker-front-wav-input"', b'id="speaker-rear-wav-input"', b'id="headphone-front-wav-input"', b'id="headphone-rear-wav-input"', b'id="backup-zip-input"', b'class="file-picker-label"'):
                require(marker in settings_page, f"Accessible file-input marker missing: {marker!r}")
            require("<title>프로필 · 설정 · AudioDSP</title>".encode("utf-8") in settings_page, "settings page title is not contextual")
            require(b"measurement card-wide" not in settings_page and b"fir-response" not in settings_page, "settings page contains another screen")
            for page in (status_page, measure_page, settings_page):
                for marker in (b"prefers-color-scheme", b"fetch('/api/status'", b"document.hidden", b'class="skip-link"', b'id="main-content"', b'aria-current="page"', b"--on-accent", b'aria-hidden="true" focusable="false"'):
                    require(marker in page, f"Shared Web marker missing: {marker!r}")
                require(page.count(b'aria-current="page"') == 1, "page has an invalid current-navigation count")
            for marker in (b'role="alert"', b'graph-scroll" tabindex="0" role="region"', b"input::file-selector-button", b"j.measurement_output_match==null?'':"):
                require(marker in web_source.encode("utf-8"), f"Accessibility source marker missing: {marker!r}")
            measurement_status = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(measurement_status["state"] == "idle", "measurement status is not idle")
            require(measurement_status["correction_preferences"]["preset"] == "none" and measurement_status["correction_preferences"]["woofer_trim_db"] == 0, "baseline correction defaults are not target-only / 0 dB trim")
            require(measurement_status["installed_calibrations"]["90"]["available"], "90-degree calibration is not reported as installed")
            legacy_preferences = {key: value for key, value in measurement_status["correction_preferences"].items() if not key.startswith("crossover_")}
            (state / "correction-preferences.json").write_text(json.dumps(legacy_preferences), encoding="utf-8")
            migrated_preferences = json.loads(get_bytes(base + "/api/measurement/status")[0])["correction_preferences"]
            require(migrated_preferences["crossover_enabled"] is True and migrated_preferences["crossover_frequency_hz"] == 100, "legacy preferences did not inherit default crossover settings")
            post_calibration(base + "/measurement/calibration", "0", args.cal_dir / "7200660.txt")
            post_calibration(base + "/measurement/calibration", "90", args.cal_dir / "7200660_90deg.txt")
            calibration_status = json.loads(get_bytes(base + "/api/measurement/status")[0])["installed_calibrations"]
            require(calibration_status["0"]["available"] and calibration_status["90"]["available"], "independent 0/90 calibration upload failed")
            require(calibration_status["0"]["serial"] == "7200660" and calibration_status["90"]["serial"] == "7200660", "calibration serial mismatch")
            target_catalog = json.loads(get_bytes(base + "/api/targets")[0])
            require(set(("flat", "harman", "rtings", "acoustix", "toole", "bk")).issubset(target_catalog["targets"]), "target catalog incomplete")
            health = json.loads(get_bytes(base + "/api/health")[0])
            require("memory_used_percent" in health and len(health.get("load", [])) == 3, "system health response incomplete")

            # Session creation and reconfiguration are silent control-plane paths.
            # They must initialize the physical-output lock as unbound and change
            # only the dependency-affected fields; no level/sweep route is invoked.
            session_fields = {
                "mode": "lrw", "orientation": "90", "level_dbfs": "-42",
                "noise_level_dbfs": "-42", "woofer_measurement_attenuation_db": "-9",
                "sweep_seconds": "8",
            }
            post_form(base + "/measurement/new", session_fields)
            created_session = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(created_session["version"] == 2 and created_session.get("measurement_profile") is None, "new session did not start with an unbound physical output")
            active_measure_page = get_bytes(base + "/measure")[0].decode("utf-8")
            require('action="/measurement/configure-level"' in active_measure_page and 'class="measure-form measurement-output-form level-check-form" data-measurement-step-content="2"' in active_measure_page, "measurement output controls are not owned by level-check step 2")
            require('name="level_dbfs" type="range"' in active_measure_page and 'name="noise_level_dbfs" type="range"' in active_measure_page and 'name="woofer_measurement_attenuation_db" type="range"' in active_measure_page, "step-2 output sliders are incomplete")
            step1_form = active_measure_page.split('action="/measurement/configure"', 1)[1].split("</form>", 1)[0]
            require('type="range"' not in step1_form and "측정 구성 변경 적용" in step1_form, "step 1 still duplicates the step-2 output controls")
            reconfigured_fields = dict(session_fields, noise_level_dbfs="-43", sweep_seconds="4")
            post_form(base + "/measurement/configure", reconfigured_fields)
            configured_session = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(configured_session["noise_level_dbfs"] == -43 and configured_session["sweep_seconds"] == 4, "silent measurement reconfiguration failed")
            first_session_id = configured_session["session_id"]
            checkpoint = {key: configured_session.get(key) for key in ("state", "positions_completed", "level_check", "measurements", "result")}
            post_form(base + "/measurement/session-note", {"note": "소파 중앙 · Woofer 노브 11시"})
            noted_session = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(noted_session["session_note"].startswith("소파 중앙"), "session note was not persisted")
            require({key: noted_session.get(key) for key in checkpoint} == checkpoint, "session note reset measurement progress")
            noted_page, _ = get_bytes(base + "/measure")
            require("소파 중앙 · Woofer 노브 11시".encode("utf-8") in noted_page and b"active-session-note" in noted_page and b"session-save-state" in noted_page, "active session summary/note is not above the wizard")
            post_form(base + "/measurement/new", session_fields)
            second_session = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(second_session["session_id"] != first_session_id, "second saved session was not created")
            second_session_id = second_session["session_id"]
            library_page, _ = get_bytes(base + "/measure")
            require(first_session_id.encode() in library_page and second_session_id.encode() in library_page and "소파 중앙".encode("utf-8") in library_page and "이어하기".encode("utf-8") in library_page and "삭제".encode("utf-8") in library_page and b"session-filter-input" in library_page, "saved session list omitted note/resume/delete/search actions")
            post_form(base + "/measurement/delete-session", {"session_id": second_session_id})
            deleted_status = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(deleted_status["state"] == "idle" and not (measurements / second_session_id).exists(), "active session deletion did not return to a clean idle state")
            post_form(base + "/measurement/delete-session", {"session_id": second_session_id}, expected=400)
            post_form(base + "/measurement/load-session", {"session_id": first_session_id})
            resumed_session = json.loads(get_bytes(base + "/api/measurement/status")[0])
            require(resumed_session["session_id"] == first_session_id and resumed_session["session_note"].startswith("소파 중앙"), "saved session did not resume with its note")
            require({key: resumed_session.get(key) for key in checkpoint} == checkpoint, "saved session did not preserve its completed wizard checkpoint")
            post_form(base + "/measurement/cancel", {}, expected=400)

            # Versioned full backup is downloadable, staged without mutation, integrity
            # checked, and restored only after a separate explicit confirmation.
            backup_payload, backup_headers = get_bytes(base + "/api/backup/download")
            require(backup_headers.get("Content-Type") == "application/zip", "full backup is not a ZIP download")
            with zipfile.ZipFile(io.BytesIO(backup_payload)) as archive:
                backup_names = set(archive.namelist())
                require({"manifest.json", "profile-settings.json", "correction-preferences.json", "profiles/Factory_Speaker_Front_LR.wav"}.issubset(backup_names), "full backup is missing required members")
                backup_manifest = json.loads(archive.read("manifest.json"))
                require(backup_manifest["format"] == "AudioDSP Backup" and backup_manifest["schema_version"] == 2, "backup schema mismatch")
                correction_preferences = json.loads(archive.read("correction-preferences.json"))
                require(correction_preferences["crossover_enabled"] is True and correction_preferences["crossover_frequency_hz"] == 100, "backup lost default digital crossover preferences")
                for name, item in backup_manifest["files"].items():
                    require(hashlib.sha256(archive.read(name)).hexdigest() == item["sha256"], f"backup hash mismatch: {name}")
            restore_settings_before = manager.load_settings()
            restore_front_before = manager.PROFILE_FILES["speaker"]["front"].read_bytes()
            post_backup(base + "/backup/stage", backup_payload)
            require(manager.load_settings() == restore_settings_before and manager.PROFILE_FILES["speaker"]["front"].read_bytes() == restore_front_before, "staging a backup mutated live state")
            valid_staging_directories = set((state / "restore-staging").iterdir())
            invalid_memory = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(backup_payload)) as source_archive:
                invalid_entries = {name: source_archive.read(name) for name in source_archive.namelist()}
            invalid_entries["profile-settings.json"] = b'{"requested_profile":"speaker","chunksize":123}\n'
            invalid_manifest = json.loads(invalid_entries["manifest.json"])
            invalid_manifest["files"]["profile-settings.json"] = {
                "bytes": len(invalid_entries["profile-settings.json"]),
                "sha256": hashlib.sha256(invalid_entries["profile-settings.json"]).hexdigest(),
            }
            invalid_entries["manifest.json"] = json.dumps(invalid_manifest).encode()
            with zipfile.ZipFile(invalid_memory, "w") as invalid_archive:
                for name, data in invalid_entries.items():
                    invalid_archive.writestr(name, data)
            post_backup(base + "/backup/stage", invalid_memory.getvalue(), "invalid-settings.zip", expected=400)
            require(set((state / "restore-staging").iterdir()) == valid_staging_directories, "failed restore validation leaked files or removed the prior valid staging")
            restore_page, _ = get_bytes(base + "/settings")
            require("검증 완료 · 전체 복원".encode("utf-8") in restore_page and "현재 설정은 아직 바뀌지 않았습니다".encode("utf-8") in restore_page, "restore review UI missing")
            post_form(base + "/chunksize", {"chunksize": "512"})
            require(manager.load_settings()["chunksize"] == 512, "pre-restore mutation failed")
            post_form(base + "/backup/apply", {})
            require(manager.load_settings() == restore_settings_before, "full restore did not restore settings")
            require(manager.PROFILE_FILES["speaker"]["front"].read_bytes() == restore_front_before, "full restore did not restore FIR")
            require(list((state / "system-backups").glob("AudioDSP_backup_*.zip")), "full restore did not create automatic rollback ZIP")
            require(not list((state / "restore-staging").glob("*")), "full restore left extracted staging files")
            latest_payload, latest_headers = get_bytes(base + "/api/backup/latest")
            require(latest_headers.get("Content-Type") == "application/zip", "latest rollback backup is not downloadable")
            with zipfile.ZipFile(io.BytesIO(latest_payload)) as latest_archive:
                require("manifest.json" in latest_archive.namelist(), "latest rollback ZIP is invalid")
            restore_page, _ = get_bytes(base + "/settings")
            require("최근 자동 복구 백업 받기".encode("utf-8") in restore_page, "latest rollback download UI missing")
            future_memory = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(backup_payload)) as source_archive, zipfile.ZipFile(future_memory, "w") as future_archive:
                for name in source_archive.namelist():
                    data = source_archive.read(name)
                    if name == "manifest.json":
                        future_manifest = json.loads(data)
                        future_manifest["schema_version"] = 999
                        data = json.dumps(future_manifest).encode()
                    future_archive.writestr(name, data)
            post_backup(base + "/backup/stage", future_memory.getvalue(), "future.zip", expected=400)
            require(manager.load_settings() == restore_settings_before, "future-schema rejection changed settings")
            post_backup(base + "/backup/stage", backup_payload)
            first_staging = set((state / "restore-staging").iterdir())
            require(len(first_staging) == 1, "restore staging directory count mismatch")
            post_backup(base + "/backup/stage", backup_payload)
            second_staging = set((state / "restore-staging").iterdir())
            require(len(second_staging) == 1 and first_staging.isdisjoint(second_staging), "replacement restore staging was not unique or did not clean the previous directory")
            post_form(base + "/backup/discard", {})
            require(not (state / "restore-staging.json").exists(), "restore discard left active staging state")
            require(not list((state / "restore-staging").glob("*")), "restore discard left extracted staging files")

            # A generated result downloads through the browser as an attachment and remains
            # non-mutating until the separate apply route is submitted.
            browser_session = measurements / "browser-download"
            browser_session.mkdir()
            browser_front = browser_session / "Generated_Front_LR_32768.wav"
            browser_rear = browser_session / "Generated_Rear_LR_32768.wav"
            browser_report_md = browser_session / "Room_Tuning_Report.md"
            browser_report_json = browser_session / "Room_Tuning_Report.json"
            write_wave(browser_front, left=0.123, right=0.087)
            write_wave(browser_rear, left=0.234, right=0.156)
            browser_report_md.write_text("# Room tuning report\n", encoding="utf-8")
            browser_report_json.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            selector_path = Path(environment["AUDIODSP_SELECTOR_STATE_PATH"])
            selector_path.write_text(json.dumps({
                "profile": "speaker", "state_byte": "0xa0", "source": "matrix",
                "updated_unix": time.time(), "boot_id": boot_id,
            }), encoding="utf-8")
            browser_job = {
                "state": "built",
                "stage": "32768탭 FIR 생성 완료",
                "progress": 100.0,
                "session_id": "browser-download",
                "session_dir": str(browser_session),
                "mode": "lrw",
                "positions_completed": 3,
                "positions_total": 3,
                "measurement_profile": "speaker",
                "measurement_output": {"profile": "speaker", "label": "U7 Speaker output · speaker chain"},
                "result": {
                    "algorithm_revision": algorithm_revision,
                    "front": browser_front.name,
                    "rear": browser_rear.name,
                    "taps": 32768,
                    "target": "harman",
                    "preset": "strong",
                    "front_sha256": hashlib.sha256(browser_front.read_bytes()).hexdigest(),
                    "front_metrics": {"left": {"peak_tap": 0, "peak_delay_ms": 0.0}},
                    "self_validation": {
                        "overall_pass": True,
                        "target_fit": {},
                        "crossover_sum": {"required": True, "pass": True, "status": "pass_independent_complex_model"},
                    },
                    "crossover": {"enabled": True, "sum_guard_enabled": True, "frequency_hz": 100, "additional_block_latency_samples": 0, "status": "pass_independent_complex_model"},
                    "graphs": {},
                    "report_md": browser_report_md.name,
                    "report_json": browser_report_json.name,
                },
            }
            (measurements / "current.json").write_text(json.dumps(browser_job), encoding="utf-8")
            result_page, _ = get_bytes(base + "/measure")
            for marker in (b"Front WAV", b"Rear WAV", "WAV + 보고서 ZIP".encode("utf-8"), b"measurement-result-graph", "A/B 청취 비교".encode("utf-8"), "자동 백업".encode("utf-8"), "덮어쓰기".encode("utf-8"), b'data-measurement-path="speaker"', "이 결과의 전용 경로".encode("utf-8"), b"Speaker output", b'role="tablist"', b'role="tabpanel"', b"non_destructive_measurement_tabs", "Woofer 최종 trim".encode("utf-8"), "측정 시 Woofer 감쇄".encode("utf-8"), "자동 검증 체크리스트".encode("utf-8"), "MAE는 판정 주파수 전체의 평균 절대오차".encode("utf-8"), b"status-badge na"):
                require(marker in result_page, f"generated-result Web marker missing: {marker!r}")
            require(result_page.count(b'role="tab"') == 6 and result_page.count(b'role="tabpanel"') == 6, "measurement workflow is not a six-tab/six-panel interface")
            require(b'value="headphone"' not in result_page, "speaker-bound result offered the Headphone-jack profile")
            require("Preview FIR 적용 후 합산 실측".encode("utf-8") not in result_page, "standard SISO still asks for a mandatory post-build acoustic sweep")
            stale_job = copy.deepcopy(browser_job)
            stale_job["result"].pop("algorithm_revision")
            (measurements / "current.json").write_text(json.dumps(stale_job), encoding="utf-8")
            stale_page, _ = get_bytes(base + "/measure")
            require("이전 알고리즘으로 계산된 결과".encode("utf-8") in stale_page and "재계산 후 정식 적용 가능".encode("utf-8") in stale_page, "stale-result UI did not block audition/apply")
            post_form(base + "/measurement/preview", {"profile": "speaker"}, expected=400)
            post_form(base + "/measurement/apply", {"profile": "speaker"}, expected=400)
            failed_job = copy.deepcopy(browser_job)
            failed_job["result"]["self_validation"].update({
                "overall_pass": False,
                "independent_positions": {"pass": False, "reused_measurements": [{"position": 2}]},
                "target_fit": {"woofer": {"pass": False, "mae_db": 12.9, "p90_abs_error_db": 19.5}},
                "crossover_sum": {"required": True, "pass": False, "status": "limited_unverified_phase"},
                "measurement_snr_db": {"minimum": 19.0, "recommended_minimum": 15.0},
            })
            (measurements / "current.json").write_text(json.dumps(failed_job), encoding="utf-8")
            failed_page, _ = get_bytes(base + "/measure")
            require("타겟/합산 셀프검증 미통과".encode("utf-8") in failed_page and "셀프검증 통과 후 정식 적용 가능".encode("utf-8") in failed_page, "failed target-fit UI did not block permanent apply")
            for marker in (b"validation-error", b"status-badge fail", b"data-measurement-jump", "해결 방법".encode("utf-8"), "서로 다른 3위치".encode("utf-8"), "Woofer 타겟 달성".encode("utf-8"), "4 · FIR 계산".encode("utf-8"), "3 · 위치 측정".encode("utf-8"), "측정 구성 변경 적용".encode("utf-8"), b"status-badge pass"):
                require(marker in failed_page, f"failed validation guidance marker missing: {marker!r}")
            post_form(base + "/measurement/apply", {"profile": "speaker"}, expected=400)
            directional_failed_job = copy.deepcopy(browser_job)
            directional_failed_job["result"]["self_validation"].update({
                "overall_pass": False,
                "crossover_sum": {"required": True, "pass": False, "status": "fail_target"},
            })
            directional_failed_job["result"]["crossover"].update({
                "status": "fail_target",
                "channels": {
                    "left": {"complex_target_median_error_db": 4.2},
                    "right": {"complex_target_median_error_db": 3.8},
                },
            })
            (measurements / "current.json").write_text(json.dumps(directional_failed_job), encoding="utf-8")
            directional_failed_page, _ = get_bytes(base + "/measure")
            for marker in ("Target보다 +4.0 dB 높습니다.", "Woofer 최종 trim", "한 단계 더 음수", "설정으로 32768탭 FIR 생성"):
                require(marker.encode("utf-8") in directional_failed_page, f"directional target-failure guidance missing: {marker}")
            mimo_failed_job = copy.deepcopy(browser_job)
            mimo_failed_job["mode"] = "mimo_one_sub"
            mimo_failed_job["result"].update({
                "kind": "mimo_2x4",
                "mimo_files": [{"file": f"MIMO_{name}_LR_32768.wav"} for name in ("Front_Left", "Front_Right", "Rear_Left", "Rear_Right")],
                "mimo": {
                    "topology": "mimo_one_sub", "frequency_range_hz": [20, 150], "strength": "balanced", "solution_blend": 0.4,
                    "prediction": {"left": {"before_target_mae_db": 3.0, "after_target_mae_db": 4.0, "before_spatial_std_db": 2.0, "after_spatial_std_db": 1.2, "before_modal_tail_db": -20.0, "after_modal_tail_db": -17.0}},
                    "headroom": {"maximum_correlated_input_row_sum": 0.99, "global_scale_db": -1.0},
                    "actuator_diversity": {"maximum_coherence": 0.8},
                    "target_level_normalization": {"reference_band_hz": [70, 130], "target_offset_db": {"left": -1.0, "right": -1.0}},
                    "resource_budget": {"runtime_dsp_planning_mib": 46, "filter_generation_planning_mib": 309},
                    "crossover": {"enabled": True, "frequency_hz": 100, "additional_block_latency_samples": 0},
                },
                "crossover": {"enabled": True, "frequency_hz": 100, "additional_block_latency_samples": 0, "status": "fail_model"},
                "graphs": {"left": {"frequency": [20.0, 100.0], "predicted_db": [0.0, 0.0], "target_db": [0.0, 0.0]}},
                "self_validation": {
                    "overall_pass": False,
                    "model_pass": False,
                    "core_checks": {"finite": True, "correlated_input_headroom": True, "common_causality": True, "predicted_target_and_spatial_non_regression": False, "predicted_modal_tail_non_regression": False},
                    "independent_positions": {"pass": True, "positions": 3, "response_files": 9, "reused_measurements": [], "spatial_stability_applicable": True},
                    "target_fit": {},
                    "crossover_sum": {"required": True, "pass": False, "status": "fail_model"},
                },
            })
            (measurements / "current.json").write_text(json.dumps(mimo_failed_job), encoding="utf-8")
            mimo_failed_page, _ = get_bytes(base + "/measure")
            for marker in ("MIMO 타겟·좌석 편차 비악화", "MIMO 저역 impulse-tail 비악화", "Safe · 높은 안정성", "Balanced · 권장", "MIMO 공동제어 상한", "지원 제어원 제한", "Crossover 주파수", "설정으로 32768탭 FIR 생성"):
                require(marker.encode("utf-8") in mimo_failed_page, f"MIMO failure guidance marker missing: {marker}")
            post_form(base + "/measurement/apply", {"profile": "speaker"}, expected=400)
            (measurements / "current.json").write_text(json.dumps(browser_job), encoding="utf-8")
            downloaded_front, download_headers = get_bytes(base + "/api/measurement/download/front")
            require(downloaded_front == browser_front.read_bytes(), "browser Front WAV download content mismatch")
            require("attachment" in download_headers.get("Content-Disposition", "") and browser_front.name in download_headers.get("Content-Disposition", ""), "browser Front WAV download header missing")
            downloaded_rear, download_headers = get_bytes(base + "/api/measurement/download/rear")
            require(downloaded_rear == browser_rear.read_bytes(), "browser Rear WAV download content mismatch")
            require("attachment" in download_headers.get("Content-Disposition", "") and browser_rear.name in download_headers.get("Content-Disposition", ""), "browser Rear WAV download header missing")
            downloaded_zip, download_headers = get_bytes(base + "/api/measurement/download/all")
            require("attachment" in download_headers.get("Content-Disposition", "") and download_headers.get("Content-Type") == "application/zip", "browser ZIP download header missing")
            with zipfile.ZipFile(io.BytesIO(downloaded_zip)) as archive:
                require(set(archive.namelist()) == {browser_front.name, browser_rear.name, browser_report_md.name, browser_report_json.name, "manifest.json"}, "browser ZIP members mismatch")
                require(archive.read(browser_front.name) == browser_front.read_bytes(), "ZIP Front content mismatch")
                require(archive.read(browser_rear.name) == browser_rear.read_bytes(), "ZIP Rear content mismatch")
                require(json.loads(archive.read("manifest.json"))["taps"] == 32768, "ZIP manifest tap count mismatch")
            downloaded_report_md, report_md_headers = get_bytes(base + "/api/measurement/download/report-md")
            require(downloaded_report_md == browser_report_md.read_bytes() and "attachment" in report_md_headers.get("Content-Disposition", ""), "browser Markdown report download failed")
            downloaded_report_json, report_json_headers = get_bytes(base + "/api/measurement/download/report-json")
            require(downloaded_report_json == browser_report_json.read_bytes() and report_json_headers.get("Content-Type", "").startswith("application/json"), "browser JSON report download failed")

            # Non-destructive A/B preview switches the live config only. Profile WAVs and
            # saved settings remain byte-for-byte unchanged until permanent apply.
            profiles_before_preview = {path: path.read_bytes() for bands in manager.PROFILE_FILES.values() for path in bands.values()}
            settings_before_preview = manager.SETTINGS_PATH.read_bytes()
            config_before_preview = manager.CONFIG_PATH.read_bytes()
            post_form(base + "/measurement/preview", {"profile": "headphone"}, expected=400)
            post_form(base + "/measurement/preview", {"profile": "speaker"})
            preview_status = json.loads(get_bytes(base + "/api/status")[0])
            require(preview_status["preview"]["active"] and preview_status["preview"]["profile"] == "speaker", "A/B preview status mismatch")
            require(preview_status["resolved"]["effective_rear_mode"] == "separate" and preview_status["resolved"]["convolution_channels"] == 4, "4ch A/B preview status incorrectly followed saved copy mode")
            preview_front, _ = get_bytes(base + "/api/fir/front")
            preview_rear, preview_rear_headers = get_bytes(base + "/api/fir/rear")
            require(preview_front == browser_front.read_bytes() and preview_rear == browser_rear.read_bytes(), "A/B FIR endpoints did not expose the running preview pair")
            require(preview_rear_headers.get("X-AudioDSP-Rear-Mode") == "separate", "A/B Rear endpoint reported the saved mode instead of preview mode")
            require(manager.CONFIG_PATH.read_bytes() != config_before_preview, "A/B preview did not change live config")
            require(manager.SETTINGS_PATH.read_bytes() == settings_before_preview, "A/B preview changed saved settings")
            require(all(path.read_bytes() == content for path, content in profiles_before_preview.items()), "A/B preview modified a managed profile WAV")
            post_form(base + "/measurement/restore", {})
            restored_status = json.loads(get_bytes(base + "/api/status")[0])
            require(not restored_status["preview"]["active"], "A/B restore left preview active")
            require(manager.CONFIG_PATH.read_bytes() == config_before_preview, "A/B restore did not restore the original config")
            require(manager.SETTINGS_PATH.read_bytes() == settings_before_preview, "A/B restore changed saved settings")
            require(all(path.read_bytes() == content for path, content in profiles_before_preview.items()), "A/B restore modified a managed profile WAV")

            # Saved configuration and temporary audition configuration are independent axes.
            # Cover both directions (saved copy -> preview separate and saved separate -> preview copy),
            # both profiles, and saved bypass on/off before returning to the exact saved state.
            original_settings = manager.load_settings()
            preview_resolution_cases = 0
            for profile in ("speaker", "headphone"):
                for saved_mode in ("copy_front", "separate"):
                    for saved_bypass in (False, True):
                        for preview_rear_source in (None, browser_rear):
                            case_settings = copy.deepcopy(original_settings)
                            case_settings["requested_profile"] = profile
                            case_settings["rear_mode"][profile] = saved_mode
                            case_settings["bypass"][profile] = saved_bypass
                            manager.apply_settings(case_settings, restart=False)
                            saved_bytes = manager.SETTINGS_PATH.read_bytes()
                            manager.preview_pair(profile, browser_front, preview_rear_source, -9)
                            during = manager.status()
                            expected_preview_mode = "separate" if preview_rear_source is not None else "copy_front"
                            expected_preview_channels = 4 if preview_rear_source is not None else 2
                            require(during["preview"]["active"], "preview matrix did not report active")
                            require(during["resolved"]["effective_profile"] == profile, "preview matrix profile mismatch")
                            require(during["resolved"]["effective_rear_mode"] == expected_preview_mode, "preview matrix mode followed saved mode")
                            require(during["resolved"]["convolution_channels"] == expected_preview_channels and not during["resolved"]["bypass"], "preview matrix channel/bypass mismatch")
                            require(manager.SETTINGS_PATH.read_bytes() == saved_bytes, "preview matrix changed saved settings")
                            running_config = manager.CONFIG_PATH.read_text(encoding="utf-8")
                            require(("rear_left:" in running_config) == (preview_rear_source is not None), "preview matrix CamillaDSP channel topology mismatch")
                            manager.restore_profile(restart=False)
                            after = manager.status()
                            expected_restored_mode = "bypass" if saved_bypass else saved_mode
                            expected_restored_channels = 0 if saved_bypass else (4 if saved_mode == "separate" else 2)
                            require(not after["preview"]["active"], "preview matrix restore left preview active")
                            require(after["resolved"]["effective_rear_mode"] == expected_restored_mode, "preview matrix restore mode mismatch")
                            require(after["resolved"]["convolution_channels"] == expected_restored_channels, "preview matrix restore channels mismatch")
                            preview_resolution_cases += 1
            manager.apply_settings(original_settings, restart=False)
            require(manager.CONFIG_PATH.read_bytes() == config_before_preview, "preview matrix did not restore the original config")
            report["preview_resolution_matrix"] = {"cases": preview_resolution_cases, "profiles": 2, "saved_modes": 2, "saved_bypass_states": 2, "preview_modes": 2}
            post_form(base + "/measurement/preview", {"profile": "speaker"})
            stale_preview = json.loads(manager.PREVIEW_STATE_PATH.read_text(encoding="utf-8"))
            stale_preview["boot_id"] = "previous-boot-id"
            manager.PREVIEW_STATE_PATH.write_text(json.dumps(stale_preview), encoding="utf-8")
            stale_result = manager.clear_stale_preview()
            require(stale_result["cleared"] and not manager.preview_status()["active"], "stale preview was not cleared at boot boundary")
            require(manager.CONFIG_PATH.read_bytes() == config_before_preview, "stale preview recovery did not restore the profile config")
            before_apply = manager.PROFILE_FILES["speaker"]["front"].read_bytes()
            backups_before_apply = len(list(manager.BACKUP_DIR.glob("*.wav")))
            post_form(base + "/measurement/apply", {"profile": "headphone"}, expected=400)
            post_form(base + "/measurement/apply", {"profile": "speaker"})
            require(manager.PROFILE_FILES["speaker"]["front"].read_bytes() == browser_front.read_bytes(), "generated FIR apply did not overwrite Front")
            require(manager.PROFILE_FILES["speaker"]["front"].read_bytes() != before_apply, "generated FIR apply did not change Front")
            require(len(list(manager.BACKUP_DIR.glob("*.wav"))) >= backups_before_apply + 2, "generated FIR apply did not retain both backups")

            # Physical/HID switch helper changes DSP and announces; the Web UI is display-only.
            direct_env = environment.copy()
            for profile in ("speaker", "headphone"):
                run_checked([str(args.switcher), profile, "--announce"], env=direct_env)
                status = json.loads(get_bytes(base + "/api/status")[0])
                require(status["settings"]["requested_profile"] == profile, "HID switch helper did not persist")
            announcements = aplay_log.read_text(encoding="utf-8").splitlines()
            require(len(announcements) == 2, "HID switch helper did not announce exactly twice")
            require("announce_speaker" in announcements[0] and "announce_headphone" in announcements[1], "wrong switch prompts")

            # Direct no-announcement switch must not invoke aplay.
            before_lines = len(announcements)
            run_checked([str(args.switcher), "speaker"], env=direct_env)
            require(len(aplay_log.read_text(encoding="utf-8").splitlines()) == before_lines, "non-announced switch called aplay")
            invalid_switch = subprocess.run([str(args.switcher), "invalid"], env=direct_env, check=False)
            require(invalid_switch.returncode == 2, "invalid switch profile was accepted")
            post_form(base + "/switch", {"profile": "headphone"}, expected=404)

            # Current U7 state is display-only and highlights exactly one profile card.
            for profile, state_byte in (("speaker", "0xa0"), ("headphone", "0x30")):
                selector_path.write_text(json.dumps({
                    "profile": profile,
                    "state_byte": state_byte,
                    "source": "matrix",
                    "updated_unix": time.time(),
                    "boot_id": boot_id,
                }), encoding="utf-8")
                page, _ = get_bytes(base + "/settings")
                marker = f'<section class="card active-profile" data-profile="{profile}">'.encode()
                require(marker in page, f"active U7 card was not highlighted: {profile}")
                require(page.count(b'<section class="card active-profile"') == 1, "more than one U7 card is highlighted")

            for profile, enabled in itertools.product(("speaker", "headphone"), ("on", "off")):
                post_form(base + "/bypass", {"profile": profile, "enabled": enabled})
                status = json.loads(get_bytes(base + "/api/status")[0])
                require(status["settings"]["bypass"][profile] == (enabled == "on"), "Web bypass mismatch")
            for profile, mode in itertools.product(("speaker", "headphone"), ("copy_front", "separate")):
                post_form(base + "/rear-mode", {"profile": profile, "mode": mode})
                status = json.loads(get_bytes(base + "/api/status")[0])
                require(status["settings"]["rear_mode"][profile] == mode, "Web Rear mode mismatch")
            for profile, trim_db in itertools.product(("speaker", "headphone"), manager.ALLOWED_WOOFER_TRIMS):
                post_form(base + "/woofer-trim", {"profile": profile, "trim_db": str(trim_db)})
                status = json.loads(get_bytes(base + "/api/status")[0])
                require(status["settings"]["woofer_trim_db"][profile] == trim_db, "Web woofer trim mismatch")
            for chunksize in (512, 1024, 2048, 4096):
                post_form(base + "/chunksize", {"chunksize": str(chunksize)})
                status = json.loads(get_bytes(base + "/api/status")[0])
                require(status["settings"]["chunksize"] == chunksize, "Web chunksize mismatch")

            # Global Xonar U7 PCM output volume: actual read, persistent API/form writes,
            # physical-knob divergence, and strict input validation without DSP restart.
            initial_volume = json.loads(get_bytes(base + "/api/volume")[0])
            require(initial_volume["available"] and initial_volume["channels"] == 8, "U7 volume read unavailable")
            for volume_db in (-60, -30, -10, 0):
                value = json.loads(put_json(base + "/api/volume", {"db": volume_db}))
                require(value["actual_db"] == volume_db and value["saved_db"] == volume_db, "Volume API read/write mismatch")
                require(int(volume_state.read_text(encoding="ascii")) == 127 + volume_db, "Volume raw mapping mismatch")
                require(json.loads(get_bytes(base + "/api/status")[0])["settings"]["output_volume_db"] == volume_db, "Volume persistence mismatch")
            post_form(base + "/volume", {"db": "-12"})
            require(int(volume_state.read_text(encoding="ascii")) == 115, "Volume form write mismatch")
            preserved_volume = manager.load_settings()["output_volume_db"]
            for invalid in ({"db": -61}, {"db": 1}, {"db": -10.5}, {"db": "-10"}, {"db": True}, []):
                put_json(base + "/api/volume", invalid, expected=400)
                require(manager.load_settings()["output_volume_db"] == preserved_volume, "Invalid volume request changed settings")
            volume_state.write_text("110\n", encoding="ascii")
            time.sleep(2.6)
            knob_volume = json.loads(get_bytes(base + "/api/volume")[0])
            require(knob_volume["actual_db"] == -17 and knob_volume["saved_db"] == -12, "Physical volume divergence was not reported")
            put_json(base + "/api/volume", {"db": -10})

            # Settings WAV uploads are staged first, compared in-browser, auditioned
            # without mutation, and only overwrite managed files after explicit apply.
            web_uploads = 0
            staged_applies = 0
            for profile in ("speaker", "headphone"):
                before_stage = {band: manager.PROFILE_FILES[profile][band].read_bytes() for band in ("front", "rear")}
                for band in ("front", "rear"):
                    post_wave(base + "/upload-stage", profile, band, sources[f"{profile}_{band}"])
                    web_uploads += 1
                    require(manager.PROFILE_FILES[profile][band].read_bytes() == before_stage[band], "staged upload changed a managed FIR")
                settings_page, _ = get_bytes(base + "/settings")
                for marker in ("적용 대기 중", "기존 / 업로드 FIR 응답 비교", "업로드값 테스트", "검토 완료 · 정식 적용"):
                    require(marker.encode("utf-8") in settings_page, f"staged-upload marker missing: {marker}")
                require(b"stage-workflow" in settings_page and f"stage-graph-{profile}".encode() in settings_page, "staged workflow/graph missing")
                candidate_front, _ = get_bytes(base + f"/api/staging/{profile}/candidate/front")
                candidate_rear, _ = get_bytes(base + f"/api/staging/{profile}/candidate/rear")
                require(candidate_front == sources[f"{profile}_front"].read_bytes(), "staged Front endpoint mismatch")
                require(candidate_rear == sources[f"{profile}_rear"].read_bytes(), "staged Rear endpoint mismatch")
                post_form(base + "/staging/preview", {"profile": profile})
                preview_status = json.loads(get_bytes(base + "/api/status")[0])
                require(preview_status["preview"]["active"] and preview_status["preview"]["profile"] == profile, "staged A/B preview failed")
                require(all(manager.PROFILE_FILES[profile][band].read_bytes() == before_stage[band] for band in ("front", "rear")), "staged A/B preview changed managed FIR")
                post_form(base + "/staging/restore", {})
                require(not json.loads(get_bytes(base + "/api/status")[0])["preview"]["active"], "staged A/B restore failed")
                post_form(base + "/staging/apply", {"profile": profile})
                staged_applies += 1
                require(manager.PROFILE_FILES[profile]["front"].read_bytes() == sources[f"{profile}_front"].read_bytes(), "staged Front apply mismatch")
                require(manager.PROFILE_FILES[profile]["rear"].read_bytes() == sources[f"{profile}_rear"].read_bytes(), "staged Rear apply mismatch")
                get_bytes(base + f"/api/staging/{profile}/candidate/front")  # falls back to newly applied current FIR
                settings_page, _ = get_bytes(base + "/settings")
                require(f"stage-graph-{profile}".encode() not in settings_page, "staging state remained after apply")

            partial_front = assets / "partial-front.wav"
            write_wave(partial_front, left=0.271, right=0.193)
            speaker_rear_before = manager.PROFILE_FILES["speaker"]["rear"].read_bytes()
            post_wave(base + "/upload-stage", "speaker", "front", partial_front)
            post_form(base + "/staging/apply", {"profile": "speaker"})
            require(manager.PROFILE_FILES["speaker"]["front"].read_bytes() == partial_front.read_bytes(), "Front-only staged apply failed")
            require(manager.PROFILE_FILES["speaker"]["rear"].read_bytes() == speaker_rear_before, "Front-only staged apply changed Rear")
            staged_applies += 1

            partial_rear = assets / "partial-rear.wav"
            write_wave(partial_rear, left=0.149, right=0.127)
            speaker_front_before = manager.PROFILE_FILES["speaker"]["front"].read_bytes()
            post_wave(base + "/upload-stage", "speaker", "rear", partial_rear)
            post_form(base + "/staging/preview", {"profile": "speaker"})
            post_form(base + "/staging/discard", {"profile": "speaker"})
            require(not json.loads(get_bytes(base + "/api/status")[0])["preview"]["active"], "discard did not restore staged preview")
            require(manager.PROFILE_FILES["speaker"]["front"].read_bytes() == speaker_front_before and manager.PROFILE_FILES["speaker"]["rear"].read_bytes() == speaker_rear_before, "discard changed managed FIR")
            post_wave(base + "/upload-stage", "speaker", "rear", partial_rear)
            post_form(base + "/staging/apply", {"profile": "speaker"})
            require(manager.PROFILE_FILES["speaker"]["front"].read_bytes() == speaker_front_before, "Rear-only staged apply changed Front")
            require(manager.PROFILE_FILES["speaker"]["rear"].read_bytes() == partial_rear.read_bytes(), "Rear-only staged apply failed")
            staged_applies += 1

            # Download semantics: copy returns Front for Rear; separate returns Rear; bypass rejects both.
            run_checked([str(args.switcher), "speaker"], env=direct_env)
            post_form(base + "/bypass", {"profile": "speaker", "enabled": "off"})
            post_form(base + "/rear-mode", {"profile": "speaker", "mode": "copy_front"})
            front_data, _ = get_bytes(base + "/api/fir/front")
            rear_copy, headers = get_bytes(base + "/api/fir/rear")
            require(front_data == rear_copy and headers.get("X-AudioDSP-Rear-Mode") == "copy_front", "copy FIR download mismatch")
            post_form(base + "/rear-mode", {"profile": "speaker", "mode": "separate"})
            rear_separate, headers = get_bytes(base + "/api/fir/rear")
            require(rear_separate != front_data and headers.get("X-AudioDSP-Rear-Mode") == "separate", "separate FIR download mismatch")
            post_form(base + "/bypass", {"profile": "speaker", "enabled": "on"})
            get_bytes(base + "/api/fir/front", expected=409)
            get_bytes(base + "/api/fir/rear", expected=409)
            post_form(base + "/bypass", {"profile": "speaker", "enabled": "off"})

            # Invalid option and invalid WAV requests return 400 without corrupting the status.
            post_form(base + "/bypass", {"profile": "speaker", "enabled": "maybe"}, expected=400)
            post_form(base + "/rear-mode", {"profile": "speaker", "mode": "invalid"}, expected=400)
            post_form(base + "/chunksize", {"chunksize": "123"}, expected=400)
            post_form(base + "/woofer-trim", {"profile": "speaker", "trim_db": "1"}, expected=400)
            post_wave(base + "/upload-stage", "speaker", "front", rejected["rate"], expected=400)

            # Fallback after HID/helper selection: selected missing -> other -> factory -> selected bypass.
            post_form(base + "/bypass", {"profile": "speaker", "enabled": "off"})
            post_form(base + "/bypass", {"profile": "headphone", "enabled": "off"})
            manager.PROFILE_FILES["headphone"]["front"].unlink(missing_ok=True)
            run_checked([str(args.switcher), "headphone"], env=direct_env)
            status = json.loads(get_bytes(base + "/api/status")[0])
            require(status["resolved"]["effective_profile"] == "speaker", "other-profile fallback failed")
            manager.PROFILE_FILES["speaker"]["front"].unlink(missing_ok=True)
            status = json.loads(get_bytes(base + "/api/status")[0])
            require(status["resolved"]["effective_profile"] == "factory", "factory fallback failed")
            post_form(base + "/bypass", {"profile": "headphone", "enabled": "on"})
            status = json.loads(get_bytes(base + "/api/status")[0])
            require(status["resolved"]["effective_profile"] == "headphone" and status["resolved"]["bypass"], "missing-profile bypass failed")

            # Restore files and perform concurrent button writes; lock must keep JSON/config valid.
            for profile, bands in manager.PROFILE_FILES.items():
                for band, path in bands.items():
                    shutil.copyfile(sources[f"{profile}_{band}"], path)
            post_form(base + "/bypass", {"profile": "headphone", "enabled": "off"})
            concurrent_actions = [
                ("/bypass", {"profile": "speaker", "enabled": "on"}),
                ("/bypass", {"profile": "speaker", "enabled": "off"}),
                ("/bypass", {"profile": "headphone", "enabled": "on"}),
                ("/bypass", {"profile": "headphone", "enabled": "off"}),
                ("/rear-mode", {"profile": "headphone", "mode": "copy_front"}),
                ("/rear-mode", {"profile": "headphone", "mode": "separate"}),
                ("/chunksize", {"chunksize": "1024"}),
                ("/chunksize", {"chunksize": "2048"}),
                ("/woofer-trim", {"profile": "speaker", "trim_db": "-9"}),
                ("/woofer-trim", {"profile": "headphone", "trim_db": "-18"}),
                ("/volume", {"db": "-20"}),
            ] * 3
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(lambda item: post_form(base + item[0], item[1]), concurrent_actions))
            require(len(results) == 33, "concurrent request count mismatch")
            final_status = json.loads(get_bytes(base + "/api/status")[0])
            require(final_status["resolved"]["convolution_channels"] in (0, 2, 4), "concurrent status invalid")
            run_checked([str(args.camilladsp), "--check", str(config / "camilladsp.yml")])
            report["web_integration"] = {
                "hid_profile_switches_with_voice": 2,
                "u7_card_highlights": 2,
                "live_u7_status_poll": True,
                "web_switch_route_removed": True,
                "bypass_changes": 4,
                "rear_mode_changes": 4,
                "chunksize_changes": 4,
                "woofer_trim_changes": len(manager.ALLOWED_WOOFER_TRIMS) * 2,
                "volume_api_writes": 5,
                "volume_form_writes": 1,
                "volume_invalid_requests": 6,
                "physical_volume_detection": True,
                "measurement_status": True,
                "target_curves": len(target_catalog["targets"]),
                "system_health": True,
                "browser_wav_downloads": 2,
                "browser_zip_download": True,
                "non_destructive_ab_preview_profiles": 1,
                "measurement_result_wrong_profile_rejections": 2,
                "preview_reboot_recovery": True,
                "generated_pair_overwrite_with_backup": True,
                "uploads": web_uploads,
                "staged_upload_applies": staged_applies,
                "fir_download_modes": 3,
                "invalid_requests": 12,
                "fallback_paths": 3,
                "concurrent_writes": 33,
                "versioned_backup_restore": True,
                "latest_rollback_download": True,
                "session_resume_note_delete": True,
                "mimo_failure_menu_guidance": True,
            }
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            web_log.close()
            for signum, previous in previous_signal_handlers.items():
                signal.signal(signum, previous)

    report["result"] = "PASS"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
