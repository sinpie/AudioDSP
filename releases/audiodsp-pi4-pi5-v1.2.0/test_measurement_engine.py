#!/usr/bin/env python3
"""Offline end-to-end checks for the UMIK measurement/FIR generator."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time
from typing import Any


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    require(specification is not None and specification.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def synthetic_response(frequencies: list[float], source: str, position: int) -> dict[str, Any]:
    db: list[float] = []
    phase: list[float] = []
    offset = (-1.2, 0.0, 1.0)[position - 1]
    for frequency in frequencies:
        logf = math.log2(frequency)
        if source == "woofer":
            value = 12.0 - 2.5 * math.log2(max(40.0, frequency) / 60.0)
            if frequency > 130.0:
                value -= 20.0 * math.log10(frequency / 130.0)
            value += offset * math.exp(-0.5 * (math.log2(frequency / 75.0) / 0.45) ** 2)
            phase_value = -0.55 * math.atan(frequency / 75.0)
            delay = 315
        else:
            side = -0.7 if source == "left" else 0.6
            room_peak = 7.0 * math.exp(-0.5 * (math.log2(frequency / (82.0 + side * 5.0)) / 0.30) ** 2)
            broad = -1.2 * math.log10(max(1000.0, frequency) / 1000.0)
            ripple = 1.1 * math.sin(logf * 3.1 + side)
            value = room_peak + broad + ripple
            value += offset * math.exp(-0.5 * (math.log2(frequency / 105.0) / 0.40) ** 2)
            phase_value = -0.35 * math.atan(frequency / 110.0)
            delay = 292 if source == "left" else 300
        db.append(round(value, 6))
        phase.append(round(phase_value, 9))
    return {
        "frequencies": frequencies,
        "db": db,
        "phase_rad": phase,
        "bulk_delay_samples": delay,
        "peak": 0.1,
        "rms": 0.02,
        "measurement_quality": {
            "snr_db": 30.0,
            "usable": True,
            "recommended": True,
        },
        "room_decay": {
            "bands": [
                {"center_hz": center, "t20_rt60_s": (0.82 + 0.02 * offset) if center <= 125 else 0.38, "reliable": True}
                for center in (63, 125, 250, 500, 1000, 2000, 4000)
            ]
        },
    }


def read_float_stereo(path: Path) -> tuple[int, int, list[tuple[float, float]]]:
    data = path.read_bytes()
    require(data[:4] == b"RIFF" and data[8:12] == b"WAVE", "not a RIFF/WAVE file")
    offset = 12
    format_code = channels = rate = bits = 0
    payload = b""
    while offset + 8 <= len(data):
        chunk = data[offset:offset + 4]
        length = struct.unpack_from("<I", data, offset + 4)[0]
        body = data[offset + 8:offset + 8 + length]
        if chunk == b"fmt ":
            format_code, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", body)
        elif chunk == b"data":
            payload = body
        offset += 8 + length + (length & 1)
    require((format_code, channels, rate, bits) == (3, 2, 48_000, 32), "wrong output WAV format")
    frames = [pair for pair in struct.iter_unpack("<ff", payload)]
    return rate, len(frames), frames


def validate_result(engine, state: dict[str, Any], phase: bool) -> dict[str, Any]:
    result = state.get("result") or {}
    require(result.get("taps") == 32_768 and result.get("sample_rate") == 48_000, "result metadata is not 32768 taps at 48 kHz")
    require(result.get("self_validation", {}).get("overall_pass"), "result self-validation failed")
    directory = Path(state["session_dir"])
    details: dict[str, Any] = {}
    for band in ("front", "rear"):
        name = result.get(band)
        require(bool(name), f"missing {band} result")
        path = directory / name
        rate, frames, values = read_float_stereo(path)
        require(frames == 32_768, f"{band} is not exactly 32768 frames")
        require(all(math.isfinite(item) for pair in values for item in pair), f"{band} contains non-finite samples")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == result[f"{band}_sha256"], f"{band} SHA-256 mismatch")
        metrics = result[f"{band}_metrics"]
        for channel in ("left", "right"):
            require(metrics[channel]["maximum_transfer_db"] <= 0.01, f"{band}/{channel} transfer exceeds 0 dB")
            peak_limit = 2500 if phase else 128
            require(metrics[channel]["peak_tap"] <= peak_limit, f"{band}/{channel} impulse is not early enough")
        details[band] = {
            "frames": frames,
            "rate": rate,
            "sha256": digest,
            "metrics": metrics,
        }
    return details


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--cal-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="gsonic-measurement-test-") as temporary:
        root = Path(temporary)
        measurements = root / "measurements"
        session = measurements / "synthetic"
        calibration = root / "calibration"
        targets = root / "targets"
        for directory in (session, calibration, targets):
            directory.mkdir(parents=True)
        for source in args.cal_dir.glob("*.txt"):
            shutil.copyfile(source, calibration / source.name)
        for source in args.target_dir.glob("*.txt"):
            shutil.copyfile(source, targets / source.name)
        os.environ.update({
            "GSONIC_MEASUREMENT_DIR": str(measurements),
            "GSONIC_CAL_DIR": str(calibration),
            "GSONIC_TARGET_DIR": str(targets),
            "GSONIC_MEASUREMENT_LOCK": str(root / "measurement.lock"),
            "GSONIC_AUDIO_LOCK": str(root / "audio.lock"),
            "GSONIC_PREFERENCES_PATH": str(root / "correction-preferences.json"),
        })
        engine = load_module("gsonic_measurement_test", args.engine)

        fft_test = engine.self_test()
        require(fft_test["taps"] == 32_768 and fft_test["result"] == "PASS", "engine self-test failed")
        catalog = engine.target_catalog()
        require(set(("flat", "harman", "rtings", "acoustix", "toole", "bk")) == set(catalog["targets"]), "target catalog mismatch")
        require(engine.parse_calibration(calibration / "7200660_90deg.txt")["serial"] == "7200660", "calibration serial mismatch")
        for frequency in (20.0, 40.0, 63.0, 96.0, 140.0, 300.0, 1000.0):
            require(engine.bass_modifier_db(frequency, "primus360") <= 1e-6, "Primus mode contains boost")
            require(engine.bass_modifier_db(frequency, "strong") <= 1e-6, "Strong mode contains boost")
        raw = [0.0] * 512
        raw[100] = 12.0
        smoothed = engine.variable_smooth([20.0 * (1000.0 ** (index / 511.0)) for index in range(512)], raw)
        require(smoothed[100] < 12.0 and max(smoothed) <= 12.0, "variable smoothing failed")
        require(abs(engine.preference_modifier_db(20.0, 4, -3) - 4.0) < 1e-6, "bass preference anchor failed")
        require(abs(engine.preference_modifier_db(20_000.0, 4, -3) + 3.0) < 1e-6, "treble preference anchor failed")

        noise_path = root / "level-white.wav"
        engine.write_white_noise(noise_path, -42, 5)
        header = noise_path.read_bytes()[:44]
        require(header[:4] == b"RIFF" and header[8:12] == b"WAVE", "level noise is not WAV")
        require(struct.unpack_from("<H", header, 22)[0] == 4 and struct.unpack_from("<I", header, 24)[0] == 48_000, "level noise is not 4ch/48k")
        require(struct.unpack_from("<H", header, 34)[0] == 24 and noise_path.stat().st_size == 44 + 5 * 48_000 * 12, "level noise format/length mismatch")
        count = 4 * engine.RATE
        background = [0.001 * math.sin(2 * math.pi * index / 101.0) for index in range(count)]
        good = [background[index] + 0.05 * math.sin(2 * math.pi * index / 37.0) for index in range(count)]
        low = [background[index] + 0.003 * math.sin(2 * math.pi * index / 37.0) for index in range(count)]
        clipped = list(good)
        clipped[count // 2] = 0.99
        require(engine.evaluate_level_samples(background, good, 24)["ok"], "valid level was rejected")
        require(not engine.evaluate_level_samples(background, low, 24)["ok"], "low-SNR level was accepted")
        require(not engine.evaluate_level_samples(background, clipped, 24)["ok"], "clipping level was accepted")
        desired_rt60 = 0.60
        tau = desired_rt60 / 6.907755
        decaying = [math.exp(-(index / engine.RATE) / tau) for index in range(2 * engine.RATE)]
        decay = engine.decay_fit(decaying)
        require(decay["reliable"] and abs(decay["t20_rt60_s"] - desired_rt60) < 0.03, "Schroeder decay estimate failed")

        front_sweep = root / "front-sweep.wav"
        woofer_sweep = root / "woofer-sweep.wav"
        front_reference = engine.write_sweep(front_sweep, "left", -42, 2)
        woofer_reference = engine.write_sweep(woofer_sweep, "woofer", -42, 2)
        front_peak = max(abs(value) for value in front_reference)
        woofer_peak = max(abs(value) for value in woofer_reference)
        expected_scale = 10.0 ** (engine.WOOFER_MEASUREMENT_ATTENUATION_DB / 20.0)
        require(abs(woofer_peak / front_peak - expected_scale) < 1e-5, "woofer sweep/reference attenuation mismatch")

        frequencies = [round(20.0 * (1000.0 ** (index / 511.0)), 6) for index in range(512)]
        measurements_index = []
        for position in range(1, 4):
            for source in ("left", "right", "woofer"):
                response_name = f"p{position}_{source}_response.json"
                response = synthetic_response(frequencies, source, position)
                (session / response_name).write_text(json.dumps(response), encoding="utf-8")
                measurements_index.append({"position": position, "source": source, "response": response_name})
        state = {
            "version": 1,
            "state": "measured",
            "stage": "synthetic",
            "progress": 100.0,
            "eta_seconds": None,
            "session_id": "synthetic",
            "session_dir": str(session),
            "mode": "lrw",
            "sources": ["left", "right", "woofer"],
            "positions_completed": 3,
            "positions_total": 3,
            "orientation": "90",
            "level_dbfs": -42,
            "sweep_seconds": 8,
            "measurements": measurements_index,
            "validation": None,
            "result": None,
        }
        engine.save_current(state)

        started = time.monotonic()
        engine.build_worker("harman", "strong", -9, "magnitude", 200)
        magnitude_state = engine.load_current()
        magnitude = validate_result(engine, magnitude_state, phase=False)
        magnitude_seconds = round(time.monotonic() - started, 3)

        started = time.monotonic()
        engine.build_worker("flat", "primus360", -6, "bass", 200, "center", 2, -2, 30, 5000, 3, 12)
        phase_state = engine.load_current()
        phase_result = validate_result(engine, phase_state, phase=True)
        require(phase_state["result"]["spatial_mode"] == "center", "spatial weighting metadata missing")
        require(phase_state["result"]["preference"] == {"bass_db_at_20_hz": 2, "treble_db_at_20_khz": -2}, "preference metadata mismatch")
        require(phase_state["result"]["correction_limits"]["high_hz"] == 5000, "correction limit metadata mismatch")
        for channel in ("left", "right", "woofer"):
            graph = phase_state["result"]["graphs"][channel]
            require(graph and graph.get("target_db") and graph.get("spatial_std_db"), f"{channel} advanced graph data missing")
            require(graph.get("actual_correction_db") and graph.get("requested_correction_db"), f"{channel} actual FIR graph data missing")
            require(graph.get("fir_implementation", {}).get("pass"), f"{channel} FIR implementation verification failed")
            require(graph.get("target_fit", {}).get("pass"), f"{channel} target-fit verification failed")
        require(any(value < -0.1 for value in phase_state["result"]["graphs"]["woofer"]["decay_control_db"]), "long bass decay did not activate cut-only damping")
        require(phase_state["result"]["room_decay"]["policy"], "room decay policy metadata missing")
        phase_seconds = round(time.monotonic() - started, 3)

        # Wizard navigation itself is client-side and non-mutating. Actual setting
        # application invalidates only dependent artifacts.
        dependency_state = engine.load_current()
        dependency_state["level_check"] = {"ok": True, "snr_db": 30.0}
        engine.save_current(dependency_state)
        same = engine.reconfigure_session("lrw", "90", -42, 8)
        require(same.get("result") and len(same["measurements"]) == 9 and same["level_check"]["ok"], "unchanged settings discarded data")
        mode_changed = engine.reconfigure_session("lr", "90", -42, 8)
        require(mode_changed["result"] is None and mode_changed["measurements"] == [] and mode_changed["level_check"]["ok"], "mode change invalidation scope is wrong")
        engine.save_current(dependency_state)
        level_changed = engine.reconfigure_session("lrw", "90", -36, 8)
        require(level_changed["result"] is None and level_changed["measurements"] == [] and level_changed.get("level_check") is None, "level change did not invalidate level and downstream")
        engine.save_current(dependency_state)
        prepared = engine.prepare_build()
        require(prepared["result"] is None and len(prepared["measurements"]) == 9 and prepared["level_check"]["ok"], "FIR rebuild invalidated raw measurements")
        preferences = engine.save_correction_preferences({**engine.DEFAULT_CORRECTION_PREFERENCES, "target": "flat", "max_boost_db": 3})
        require(engine.load_correction_preferences() == preferences and preferences["target"] == "flat", "correction preference persistence failed")

        report = {
            "result": "PASS",
            "fft": fft_test,
            "targets": len(catalog["targets"]),
            "level_check_offline": True,
            "variable_smoothing": True,
            "natural_rolloff_guard": True,
            "woofer_measurement_attenuation_db": engine.WOOFER_MEASUREMENT_ATTENUATION_DB,
            "actual_fir_target_verification": True,
            "room_decay_control": True,
            "dependency_invalidation": True,
            "correction_preferences": True,
            "output_contract": "48 kHz / stereo float32 WAV / exactly 32768 taps per channel",
            "magnitude_build_seconds": magnitude_seconds,
            "bass_phase_build_seconds": phase_seconds,
            "magnitude": magnitude,
            "bass_phase": phase_result,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
