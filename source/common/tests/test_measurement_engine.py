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
import sys
import tempfile
import time
import types
from typing import Any


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_module(name: str, path: Path):
    # The production engine runs on Linux and uses flock to serialize audio and
    # session writes.  Let the same entirely offline test run on the Windows
    # release workstation; no concurrent worker is created in this process.
    if os.name == "nt" and "fcntl" not in sys.modules:
        fcntl_stub = types.ModuleType("fcntl")
        fcntl_stub.LOCK_EX = 2
        fcntl_stub.flock = lambda _handle, _operation: None
        sys.modules["fcntl"] = fcntl_stub
    specification = importlib.util.spec_from_file_location(name, path)
    require(specification is not None and specification.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def enable_windows_offline_fft(engine) -> None:
    """Use NumPy only for workstation CI; production/Pi tests still exercise FFTW3f."""
    if os.name != "nt":
        return
    import numpy as np

    class NumpyFFTBackend:
        kind = "numpy-offline-test"

        def close(self) -> None:
            return None

        def rfft(self, values, length: int) -> list[complex]:
            numeric = np.asarray(list(values), dtype=np.float32)
            return np.fft.rfft(numeric, n=length).tolist()

        def irfft(self, values, length: int) -> list[float]:
            numeric = np.asarray(values, dtype=np.complex64)
            return np.fft.irfft(numeric, n=length).astype(np.float32).tolist()

    engine.FFTBackend = NumpyFFTBackend


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
        "bulk_delay_reliable": True,
        "bulk_delay": {"reliable": True},
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
        "frequency_quality": {"frequencies": frequencies, "confidence": [0.95] * len(frequencies)},
    }


def synthetic_combined_response(
    engine,
    main: dict[str, Any],
    woofer: dict[str, Any],
    woofer_attenuation_db: float,
) -> dict[str, Any]:
    """Create an exact physical H(main)+scale*H(woofer) capture."""
    scale = 10.0 ** (woofer_attenuation_db / 20.0)
    levels: list[float] = []
    phases: list[float] = []
    for frequency in main["frequencies"]:
        value = engine.response_complex(main, frequency) + scale * engine.response_complex(woofer, frequency)
        levels.append(round(20.0 * math.log10(max(abs(value), 1.0e-15)), 6))
        phases.append(math.atan2(value.imag, value.real))
    for index in range(1, len(phases)):
        while phases[index] - phases[index - 1] > math.pi:
            phases[index] -= 2.0 * math.pi
        while phases[index] - phases[index - 1] < -math.pi:
            phases[index] += 2.0 * math.pi
    result = json.loads(json.dumps(main))
    result.update({
        "db": levels,
        "phase_rad": [round(value, 9) for value in phases],
        "bulk_delay_samples": 0,
        "bulk_delay_reliable": True,
        "bulk_delay": {"reliable": True},
        "measurement_quality": {"snr_db": 30.0, "usable": True, "recommended": True},
    })
    return result


def write_synthetic_phase_references(
    engine,
    session: Path,
    positions: int,
) -> list[dict[str, Any]]:
    """Persist a reliable same-recording L/R/W phase reference per position."""
    index: list[dict[str, Any]] = []
    for position in range(1, positions + 1):
        responses = {
            source: json.loads((session / f"p{position}_{source}_response.json").read_text(encoding="utf-8"))
            for source in ("left", "right", "woofer")
        }
        frequencies = [
            float(value) for value in responses["left"]["frequencies"]
            if 30.0 <= float(value) <= 800.0
        ]
        sources: dict[str, Any] = {}
        for source, response in responses.items():
            values = [engine.response_complex(response, frequency) for frequency in frequencies]
            sources[source] = {
                "frequencies": frequencies,
                "db": [round(20.0 * math.log10(max(abs(value), 1.0e-15)), 6) for value in values],
                "phase_rad": engine.unwrap([math.atan2(value.imag, value.real) for value in values]),
                "median_snr_db": 30.0,
                "phase_repeatability_p90_deg": 0.0,
                "tone_count": len(frequencies),
            }
        result = {
            "version": 1,
            "method": "synthetic same-recording L/R/W multisine",
            "reliable": True,
            "recommended": True,
            "period_correlation": 1.0,
            "minimum_median_snr_db": 30.0,
            "phase_repeatability_p90_deg": 0.0,
            "sources": sources,
            "pairs": {
                "left_right": {"second_minus_first_delay_samples": 8.0, "delay_fit_residual_p90_deg": 0.0},
                "left_woofer": {"second_minus_first_delay_samples": 23.0, "delay_fit_residual_p90_deg": 0.0},
                "right_woofer": {"second_minus_first_delay_samples": 15.0, "delay_fit_residual_p90_deg": 0.0},
            },
            "timing_scope": "synthetic relative timing",
            "normalization_applied": False,
        }
        result_name = f"p{position}_phase_reference.json"
        (session / result_name).write_text(json.dumps(result), encoding="utf-8")
        index.append({
            "position": position,
            "recording": f"p{position}_phase_reference_recorded.wav",
            "signal": f"p{position}_phase_reference_signal.json",
            "result": result_name,
            "reliable": True,
            "minimum_median_snr_db": 30.0,
            "phase_repeatability_p90_deg": 0.0,
        })
    return index


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


def validate_result(engine, state: dict[str, Any], phase: bool, *, allow_sum_blocked: bool = False) -> dict[str, Any]:
    result = state.get("result") or {}
    require(result.get("taps") == 32_768 and result.get("sample_rate") == 48_000, "result metadata is not 32768 taps at 48 kHz")
    validation = result.get("self_validation", {})
    if allow_sum_blocked:
        require(all(validation.get("core_checks", {}).values()), "sum-blocked result also failed FIR integrity")
    else:
        require(
            validation.get("overall_pass"),
            "result self-validation failed: " + json.dumps(
                {"self_validation": validation, "crossover": result.get("crossover")},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
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

    with tempfile.TemporaryDirectory(prefix="audiodsp-measurement-test-") as temporary:
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
        boot_id_path = root / "boot-id"
        selector_state_path = root / "u7-selector.json"
        boot_id_path.write_text("measurement-test-boot\n", encoding="ascii")
        selector_state_path.write_text(json.dumps({
            "profile": "speaker",
            "state_byte": "0xa0",
            "source": "offline-test",
            "boot_id": "measurement-test-boot",
        }), encoding="utf-8")
        os.environ.update({
            "AUDIODSP_MEASUREMENT_DIR": str(measurements),
            "AUDIODSP_CAL_DIR": str(calibration),
            "AUDIODSP_TARGET_DIR": str(targets),
            "AUDIODSP_MEASUREMENT_LOCK": str(root / "measurement.lock"),
            "AUDIODSP_AUDIO_LOCK": str(root / "audio.lock"),
            "AUDIODSP_PREFERENCES_PATH": str(root / "correction-preferences.json"),
            "AUDIODSP_SELECTOR_STATE_PATH": str(selector_state_path),
            "AUDIODSP_BOOT_ID_PATH": str(boot_id_path),
            "AUDIODSP_PLATFORM_CLASS": "test",
        })
        engine = load_module("audiodsp_measurement_test", args.engine)
        enable_windows_offline_fft(engine)

        original_kill = engine.os.kill
        try:
            def permission_denied_for_live_pid(_pid: int, _signal: int) -> None:
                raise PermissionError("different service user")

            engine.os.kill = permission_denied_for_live_pid
            require(
                engine.measurement_worker_alive({"worker_pid": 999_999}),
                "a live root-owned worker was reported dead to a non-root status client",
            )
        finally:
            engine.os.kill = original_kill

        bound_state = {"mode": "lrw"}
        engine.bind_measurement_output(bound_state)
        require(bound_state["measurement_profile"] == "speaker", "level-check output did not bind to the physical U7 path")
        require(engine.ensure_measurement_output_path(bound_state)["profile"] == "speaker", "bound U7 path was rejected")
        selector_state_path.write_text(json.dumps({
            "profile": "headphone", "state_byte": "0x30", "source": "offline-test",
            "boot_id": "measurement-test-boot",
        }), encoding="utf-8")
        try:
            engine.ensure_measurement_output_path(bound_state)
        except engine.MeasurementError:
            pass
        else:
            raise AssertionError("physical U7 path change did not stop a bound measurement")
        selector_state_path.write_text(json.dumps({
            "profile": "speaker", "state_byte": "0xa0", "source": "offline-test",
            "boot_id": "measurement-test-boot",
        }), encoding="utf-8")
        engine.save_current(bound_state)

        # Every audible path must disconnect the normal U7 input before forcing
        # the hardware PCM stage to unity, then restore the exact old volume
        # before CamillaDSP can reconnect the input. Exercise the common
        # transaction directly without playing sound.
        audio_events: list[str] = []
        fake_audio = {"camilla_active": True, "raw": 117, "fail_raw": None}

        class FakeCompleted:
            def __init__(self, returncode: int = 0, stdout: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout

        def fake_audio_run(command, **_kwargs):
            tokens = [str(value) for value in command]
            if tokens[0] == engine.SYSTEMCTL:
                action = tokens[1]
                audio_events.append(f"systemctl:{action}")
                if action == "is-active":
                    return FakeCompleted(0 if fake_audio["camilla_active"] else 3)
                if action == "stop":
                    fake_audio["camilla_active"] = False
                    return FakeCompleted()
                if action == "start":
                    fake_audio["camilla_active"] = True
                    return FakeCompleted()
            if tokens[0] == engine.AMIXER:
                if "cget" in tokens:
                    audio_events.append("amixer:cget")
                    raw = int(fake_audio["raw"])
                    return FakeCompleted(stdout=(
                        "numid=6,iface=MIXER,name='PCM Playback Volume'\n"
                        "; type=INTEGER,access=rw---R--,values=8,min=0,max=127,step=0\n"
                        f"  : values={','.join([str(raw)] * 8)}\n"
                        "  | dBminmax-min=-12700,max=0\n"
                    ))
                if "PCM,0" in tokens:
                    raw = int(tokens[-1])
                    audio_events.append(f"amixer:volume:{raw}")
                    if fake_audio["fail_raw"] == raw:
                        return FakeCompleted(1, "injected restore failure")
                    fake_audio["raw"] = raw
                    return FakeCompleted(stdout=f"PCM Playback Volume set to {raw}")
                action = tokens[-1]
                name = tokens[-2]
                audio_events.append(f"amixer:{name}:{action}")
                return FakeCompleted()
            raise AssertionError(f"unexpected offline audio command: {tokens}")

        original_audio_run = engine.subprocess.run
        original_sleep = engine.time.sleep
        engine.subprocess.run = fake_audio_run
        engine.time.sleep = lambda _seconds: None
        try:
            window = engine.begin_measurement_audio_window(require_camilla=True)
            require(fake_audio["raw"] == 127 and not fake_audio["camilla_active"], "measurement did not force U7 PCM unity after input disconnect")
            engine.ensure_measurement_audio_window(window)
            engine.restore_measurement_audio_window(window)
            require(fake_audio["raw"] == 117 and fake_audio["camilla_active"], "measurement did not restore listening volume and input")
            stop_index = audio_events.index("systemctl:stop")
            mic_off_index = audio_events.index("amixer:Mic:nocap")
            line_off_index = audio_events.index("amixer:Line:nocap")
            unity_index = audio_events.index("amixer:volume:127")
            restore_index = audio_events.index("amixer:volume:117")
            start_index = audio_events.index("systemctl:start")
            require(
                stop_index < mic_off_index < line_off_index < unity_index < restore_index < start_index,
                f"unsafe measurement audio order: {audio_events}",
            )

            audio_events.clear()
            fake_audio.update(camilla_active=True, raw=117, fail_raw=None)
            failed_window = engine.begin_measurement_audio_window(require_camilla=True)
            fake_audio["fail_raw"] = 117
            try:
                engine.restore_measurement_audio_window(failed_window)
            except engine.MeasurementError:
                pass
            else:
                raise AssertionError("injected U7 volume restore failure was accepted")
            require(not fake_audio["camilla_active"], "input was reconnected after U7 volume restore failure")
            require("systemctl:start" not in audio_events, "CamillaDSP started before volume restore PASS")
        finally:
            engine.subprocess.run = original_audio_run
            engine.time.sleep = original_sleep
            fake_audio.update(camilla_active=True, raw=117, fail_raw=None)
            engine.CURRENT.unlink(missing_ok=True)
        engine.validate_result_profile({"measurement_profile": "speaker"}, "speaker")
        try:
            engine.validate_result_profile({"measurement_profile": "speaker"}, "headphone")
        except engine.MeasurementError:
            pass
        else:
            raise AssertionError("measured FIR was accepted by a different output profile")

        fft_test = engine.self_test()
        fft_backend = engine.FFTBackend()
        base_reference = [
            math.sin(2.0 * math.pi * index / 127.0)
            + 0.37 * math.sin(2.0 * math.pi * index / 311.0)
            for index in range(65_536)
        ]
        recovered_transfers = []
        for playback_scale in (10.0 ** (-42.0 / 20.0), 10.0 ** (-18.0 / 20.0)):
            reference = [value * playback_scale for value in base_reference]
            captured = [value * 0.37 for value in reference]
            transfer, _length, _regularization = engine.regularized_transfer_spectrum(captured, reference, fft_backend)
            recovered_transfers.append(transfer)
        base_spectrum = fft_backend.rfft(base_reference, len(base_reference))
        base_powers = [abs(value) ** 2 for value in base_spectrum]
        usable_power = max(base_powers) * 1.0e-8
        transfer_scale_error = max(
            abs(low - high)
            for low, high, power in zip(recovered_transfers[0], recovered_transfers[1], base_powers)
            if power >= usable_power
        )
        require(transfer_scale_error <= 2.0e-5, "recovered transfer changes with measurement playback dBFS in usable reference bins")
        engine_source = args.engine.read_text(encoding="utf-8")
        require(engine_source.count('ARECORD, "-q", "--fatal-errors"') == 4, "one or more UMIK capture paths do not fail closed on ALSA overrun")
        require('/var/lib/audiodsp/u7-selector-state.json' in engine_source, "measurement selector default differs from monitor/manager state path")
        require('environment("PHASE_CLOCK_SHARED", "0")' in engine_source and 'environment("AUDIODSP_PHASE_CLOCK_SHARED"' not in engine_source, "shared-clock environment suffix is double-prefixed")
        require(engine.DEFAULT_SWEEP_LEVEL_DBFS == -42 and engine.DEFAULT_NOISE_LEVEL_DBFS == -42, "night-safe default output is not -42 dBFS")
        require(fft_test["taps"] == 32_768 and fft_test["result"] == "PASS", "engine self-test failed")
        estimates = engine.platform_capabilities().get("offline_estimates_seconds", {})
        require(all(isinstance(estimates.get(key), int) and estimates[key] > 0 for key in ("response_per_channel", "fir_magnitude", "fir_bass_phase")), "platform-specific offline ETA metadata is incomplete")
        require(estimates.get("mimo_2x4") is None or estimates["mimo_2x4"] > 0, "MIMO ETA metadata is invalid")
        catalog = engine.target_catalog()
        require(set(("flat", "harman", "rtings", "acoustix", "toole", "bk")) == set(catalog["targets"]), "target catalog mismatch")
        require(engine.parse_calibration(calibration / "7200660_90deg.txt")["serial"] == "7200660", "calibration serial mismatch")
        for frequency in (20.0, 40.0, 63.0, 96.0, 140.0, 300.0, 1000.0):
            require(engine.bass_modifier_db(frequency, "primus360") <= 1e-6, "Primus mode contains boost")
            require(engine.bass_modifier_db(frequency, "strong") <= 1e-6, "Strong mode contains boost")
        frequency_grid = [20.0 * (1000.0 ** (index / 511.0)) for index in range(512)]
        raw = [0.0] * 512
        raw[100] = 12.0
        db_smoothed = engine.variable_smooth(frequency_grid, raw)
        power_smoothed = engine.variable_power_smooth(frequency_grid, raw)
        require(db_smoothed[100] < power_smoothed[100] < 12.0 and max(power_smoothed) <= 12.0, "power-domain response smoothing failed")
        cut_curve = [0.0] * 512
        cut_curve[100] = -12.0
        cut_db_smoothed = engine.variable_smooth(frequency_grid, cut_curve)
        cut_power_smoothed = engine.variable_power_smooth(frequency_grid, cut_curve)
        require(cut_db_smoothed[100] < cut_power_smoothed[100] <= 0.0, "cut-only guard was weakened by response power smoothing")
        equal_power = engine.weighted_power_mean_db([0.0, 0.0, 12.0], [1.0, 1.0, 1.0])
        expected_equal_power = 10.0 * math.log10((1.0 + 1.0 + 10.0 ** 1.2) / 3.0)
        require(abs(equal_power - expected_equal_power) <= 1.0e-12 and equal_power > 4.0, "weighted mean-square response is not exact")
        equal_spread = engine.weighted_std_db([0.0, 0.0, 12.0], [1.0, 1.0, 1.0])
        require(abs(equal_spread - math.sqrt(32.0)) <= 1.0e-12, "weighted spatial dB spread is not exact")

        aggregation_dir = root / "power-aggregation"
        aggregation_dir.mkdir()
        aggregation_frequencies = [100.0, 1_000.0, 10_000.0]
        for position, level in enumerate((0.0, 0.0, 12.0), start=1):
            (aggregation_dir / f"p{position}_left_response.json").write_text(json.dumps({
                "frequencies": aggregation_frequencies,
                "db": [level] * len(aggregation_frequencies),
                "phase_rad": [0.0] * len(aggregation_frequencies),
                "bulk_delay_samples": 0,
                "bulk_delay_reliable": True,
                "response_algorithm_revision": engine.RESPONSE_ALGORITHM_REVISION,
                "smoothing": engine.SMOOTHING_NAME,
                "frequency_quality": {"confidence": [1.0] * len(aggregation_frequencies)},
            }), encoding="utf-8")
        aggregation = engine.load_average_response(aggregation_dir, "left", "equal", 3)
        require(max(abs(value - expected_equal_power) for value in aggregation["average_db"]) <= 1.0e-12, "spatial prototype is not a weighted power mean")
        require(aggregation["spatial_aggregation"]["legacy_response_count"] == 0, "current response was marked legacy")
        require(abs(aggregation["spatial_aggregation"]["median_power_mean_lift_db"] - (expected_equal_power - 4.0)) <= 1.0e-4, "power/geometric mean diagnostic is wrong")

        legacy_dir = root / "legacy-aggregation"
        legacy_dir.mkdir()
        legacy_curve = [0.0, 12.0, 0.0]
        (legacy_dir / "p1_left_response.json").write_text(json.dumps({
            "frequencies": aggregation_frequencies,
            "db": legacy_curve,
            "phase_rad": [0.0] * len(aggregation_frequencies),
            "smoothing": next(iter(engine.LEGACY_SMOOTHING_NAMES)),
        }), encoding="utf-8")
        legacy = engine.load_average_response(legacy_dir, "left", "equal", 1)
        require(legacy["average_db"] == legacy_curve, "stored legacy response was smoothed a second time")
        require(legacy["spatial_aggregation"]["raw_reprocess_recommended"], "legacy response did not request silent raw reprocessing")
        no_metadata_dir = root / "legacy-no-metadata"
        no_metadata_dir.mkdir()
        (no_metadata_dir / "p1_left_response.json").write_text(json.dumps({
            "frequencies": aggregation_frequencies,
            "db": legacy_curve,
            "phase_rad": [0.0] * len(aggregation_frequencies),
        }), encoding="utf-8")
        no_metadata = engine.load_average_response(no_metadata_dir, "left", "equal", 1)
        require(no_metadata["average_db"] == legacy_curve, "metadata-free legacy response was smoothed a second time")
        try:
            engine.weighted_power_mean_db([0.0, float("nan")], [1.0, 1.0])
        except engine.MeasurementError:
            pass
        else:
            raise AssertionError("non-finite spatial response was not rejected")
        require(abs(engine.preference_modifier_db(20.0, 4, -3) - 4.0) < 1e-6, "bass preference anchor failed")
        require(abs(engine.preference_modifier_db(20_000.0, 4, -3) + 3.0) < 1e-6, "treble preference anchor failed")
        for frequency in (20.0, 60.0, 80.0, 100.0, 120.0, 200.0, 1000.0):
            highpass = engine.linkwitz_riley_4_magnitude(frequency, 100.0, "highpass")
            lowpass = engine.linkwitz_riley_4_magnitude(frequency, 100.0, "lowpass")
            require(abs(highpass + lowpass - 1.0) < 1e-12, "LR4 acoustic branch magnitudes are not complementary")
        require(abs(engine.crossover_transfer_db(100.0, 100, "highpass") + 6.020599913) < 1e-5, "LR4 highpass is not -6.02 dB at crossover")
        branch_band_f = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1_000.0, 2_000.0, 4_000.0]
        branch_band_db = [-18.0 * abs(math.log2(frequency / 80.0)) for frequency in branch_band_f]
        branch_low, branch_high = engine.natural_usable_band(branch_band_f, branch_band_db, 0.0)
        require(40.0 <= branch_low <= 80.0 and 80.0 <= branch_high < 250.0, "woofer-like natural usable band was falsely extended to the front-speaker range")
        require(engine.DEFAULT_CORRECTION_PREFERENCES["crossover_enabled"] is True and engine.DEFAULT_CORRECTION_PREFERENCES["crossover_frequency_hz"] == 100, "digital crossover is not default ON at 100 Hz")
        require(engine.DEFAULT_CORRECTION_PREFERENCES["max_boost_db"] == 10, "maximum relative compensation default is not 10 dB")

        reliability_f = [20.0 * (1000.0 ** (index / 511.0)) for index in range(512)]
        notch_db = [0.0] * len(reliability_f)
        notch_index = min(range(len(reliability_f)), key=lambda index: abs(reliability_f[index] - 1_000.0))
        notch_db[notch_index] = -15.0
        notch_reliability = engine.narrow_notch_reliability(reliability_f, notch_db)
        require(notch_reliability[notch_index] < 0.10, "narrow deep null did not lose boost authority")
        require(notch_reliability[0] == 1.0 and notch_reliability[-1] == 1.0, "measurement edge was falsely classified as a local null")

        broad_left = [
            -14.0 * max(0.0, math.log(max(frequency, 8_000.0) / 8_000.0) / math.log(20_000.0 / 8_000.0))
            for frequency in reliability_f
        ]
        broad_right = [value + 0.8 for value in broad_left]
        raw_confidence = [0.05] * len(reliability_f)
        (
            left_confidence,
            right_confidence,
            left_rolloff_floor,
            right_rolloff_floor,
            rolloff_summary,
        ) = engine.stereo_broad_rolloff_confidence(
            reliability_f, broad_left, raw_confidence,
            reliability_f, broad_right, raw_confidence,
            [20.0, 20_000.0], [0.0, 0.0], 0.0, 0.0,
        )
        upper_index = min(range(len(reliability_f)), key=lambda index: abs(reliability_f[index] - 16_000.0))
        require(left_confidence[upper_index] >= 0.50 and right_confidence[upper_index] >= 0.50, "independent L/R broad roll-off did not raise confidence enough to use the selected relative ceiling")
        require(left_rolloff_floor[upper_index] >= 0.50 and right_rolloff_floor[upper_index] >= 0.50, "broad roll-off evidence was not separated from raw SNR confidence")
        require(rolloff_summary["narrow_null_guard_remains_enabled"], "stereo roll-off inference disabled the null guard")
        rolloff_ir, rolloff_graph = engine.design_channel(
            reliability_f, broad_left, [0.2] * len(reliability_f), [0.0] * len(reliability_f),
            "flat", "none", woofer=False, woofer_trim_db=0,
            phase_mode="magnitude", phase_cutoff=200,
            frequency_confidence=left_confidence,
            corroborated_rolloff_confidence=left_rolloff_floor,
            shared_reference_measure_db=0.0, shared_reference_target_db=0.0,
            max_boost_db=10, fft=fft_backend,
        )
        rolloff_graph_index = min(range(len(rolloff_graph["frequency"])), key=lambda index: abs(rolloff_graph["frequency"][index] - 16_000.0))
        require(4.0 < rolloff_graph["requested_correction_db"][rolloff_graph_index] <= 10.0, "trusted broad high-frequency roll-off did not use the selected relative ceiling")
        deep_ir, deep_graph = engine.design_channel(
            reliability_f, notch_db, [0.2] * len(reliability_f), [0.0] * len(reliability_f),
            "flat", "none", woofer=False, woofer_trim_db=0,
            phase_mode="magnitude", phase_cutoff=200,
            frequency_confidence=[1.0] * len(reliability_f),
            shared_reference_measure_db=0.0, shared_reference_target_db=0.0,
            max_boost_db=10, fft=fft_backend,
        )
        require(deep_graph["narrow_notch_guarded_bins"] > 0 and deep_graph["maximum_narrow_notch_boost_db"] <= 3.01, "narrow deep null could force excessive common attenuation")
        common_bank, common_normalization = engine.normalize_fir_bank([rolloff_ir, deep_ir, deep_ir, rolloff_ir], fft_backend)
        require(common_normalization["scope"] == "complete_l_r_woofer_bank" and not common_normalization["independent_channel_normalization"], "L/R/W bank did not use one common 0 dB origin")
        require(common_normalization["maximum_relative_level_error_db"] <= 1.0e-6, "common normalization changed an inter-branch level delta")

        noise_path = root / "level-white.wav"
        engine.write_white_noise(noise_path, -42, 5)
        header = noise_path.read_bytes()[:44]
        require(header[:4] == b"RIFF" and header[8:12] == b"WAVE", "level noise is not WAV")
        require(struct.unpack_from("<H", header, 22)[0] == 4 and struct.unpack_from("<I", header, 24)[0] == 48_000, "level noise is not 4ch/48k")
        require(struct.unpack_from("<H", header, 34)[0] == 24 and noise_path.stat().st_size == 44 + 2 * 48_000 * 12, "quick level sweep format/length mismatch")
        count = 4 * engine.RATE
        background = [0.001 * math.sin(2 * math.pi * index / 101.0) for index in range(count)]
        good = [background[index] + 0.05 * math.sin(2 * math.pi * index / 37.0) for index in range(count)]
        low = [background[index] + 0.0018 * math.sin(2 * math.pi * index / 37.0) for index in range(count)]
        clipped = list(good)
        clipped[count // 2] = 0.99
        transient_background = list(background)
        for index in range(engine.RATE // 2, engine.RATE // 2 + engine.RATE // 100):
            transient_background[index] += 0.5 * math.sin(2 * math.pi * index / 19.0)
        require(engine.evaluate_level_samples(background, good, 24)["ok"], "valid level was rejected")
        require(engine.evaluate_level_samples(transient_background, good, 24)["ok"], "one switching transient contaminated robust background RMS")
        require(not engine.evaluate_level_samples(background, low, 24)["ok"], "low-SNR level was accepted")
        require(not engine.evaluate_level_samples(background, clipped, 24)["ok"], "clipping level was accepted")
        quick_low = engine.normalize_level_check(
            {"snr_db": 5.99, "peak_dbfs": -20.0, "requested_level_dbfs": -30}, -30,
        )
        quick_pass = engine.normalize_level_check(
            {"snr_db": 6.0, "peak_dbfs": -20.0, "requested_level_dbfs": -30}, -30,
        )
        quick_current = engine.normalize_level_check(
            {"snr_db": 9.53, "peak_dbfs": -30.0, "requested_level_dbfs": -30}, -30,
        )
        quick_legacy_quieter_sweep = engine.normalize_level_check(
            {"snr_db": 18.0, "peak_dbfs": -20.0, "requested_level_dbfs": -30}, -36,
        )
        quick_incomplete_coverage = engine.normalize_level_check(
            {"snr_db": 30.0, "peak_dbfs": -20.0, "requested_level_dbfs": -30},
            -30,
            ["left", "right", "woofer", "left_woofer", "right_woofer"],
        )
        require(not quick_low["ok"] and quick_pass["ok"], "quick-sweep 6 dB PASS boundary is wrong")
        require(
            quick_current["ok"] and quick_current["recommended_sweep_level_dbfs"] == -24
            and quick_current["recommended_raise_db"] == 6,
            "quick-sweep usable PASS / optional 15 dB guidance is wrong",
        )
        require(
            quick_legacy_quieter_sweep["ok"] and not quick_legacy_quieter_sweep["quality_recommended"]
            and quick_legacy_quieter_sweep["assessment_snr_db"] == 12.0,
            "legacy quick check was not projected to the configured full-sweep level",
        )
        require(not quick_incomplete_coverage["ok"] and not quick_incomplete_coverage["coverage_ok"], "legacy partial quick check was accepted for a five-output session")
        require(
            abs(engine.coherent_sweep_integration_gain_db(2.0)) < 1.0e-12
            and abs(engine.coherent_sweep_integration_gain_db(14.0) - 8.45098) < 1.0e-4,
            "full-sweep matched-filter SNR gain is not referenced to the 2 s preflight",
        )
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
        adjustable_reference = engine.write_sweep(root / "woofer-adjustable.wav", "woofer", -30, 2, woofer_attenuation_db=-6)
        require(abs(max(map(abs, adjustable_reference)) / (10.0 ** (-30 / 20.0)) - 10.0 ** (-6 / 20.0)) < 1e-4, "adjustable woofer attenuation mismatch")
        quick_front_reference = engine.write_sweep(root / "quick-front.wav", "left", -30, 2, level_check=True)
        quick_woofer_reference = engine.write_sweep(root / "quick-woofer.wav", "woofer", -30, 2, level_check=True, woofer_attenuation_db=-6)
        require(
            quick_front_reference == engine.reference_sweep_for_source("left", -30, 2, level_check=True)
            and quick_woofer_reference == engine.reference_sweep_for_source("woofer", -30, 2, level_check=True, woofer_attenuation_db=-6),
            "saved quick-sweep reanalysis does not reconstruct the source-specific band, tail, and Woofer attenuation",
        )
        combined_reference = engine.write_sweep(root / "combined-left.wav", "left_woofer", -30, 2, woofer_attenuation_db=-9)
        require(abs(max(map(abs, combined_reference)) / (10.0 ** (-30 / 20.0)) - 1.0) < 1e-4, "combined L+Woofer reference must preserve input level")
        post_reference = engine.write_filtered_stereo_sweep(root / "post-left.wav", "left", -30, 2)
        post_active = [index for index, value in enumerate(post_reference) if abs(value) > 1.0e-12]
        require(
            post_active[0] >= round(engine.POST_VALIDATION_SILENT_LEAD_SECONDS * engine.RATE)
            and all(abs(value) <= 1.0e-12 for value in post_reference[:round(engine.POST_VALIDATION_SILENT_LEAD_SECONDS * engine.RATE)]),
            "post-FIR sweep does not keep U7/CamillaDSP startup inside a two-second silent lead",
        )
        require(engine.SOURCES["lr"] == ("left_woofer", "right_woofer"), "L/R shared-filter mode is not explicit L+Woofer / R+Woofer")

        # A subwoofer is silent during the high-frequency majority of a full
        # sweep. Its quality gate must inspect only the exponential-sweep time
        # segment corresponding to 15-300 Hz, while combined Front+Woofer uses
        # the complete 15 Hz-22 kHz segment.
        capture_lead = round(0.4 * engine.RATE)
        synthetic_capture = [0.0008 * math.sin(2.0 * math.pi * index / 83.0) for index in range(capture_lead + len(woofer_reference))]
        active_indices = [index for index, value in enumerate(woofer_reference) if abs(value) > 1.0e-12]
        low_fraction = math.log(300.0 / 15.0) / math.log(22_000.0 / 15.0)
        low_end = active_indices[0] + round((active_indices[-1] + 1 - active_indices[0]) * low_fraction)
        for index in range(active_indices[0], min(low_end, len(woofer_reference))):
            synthetic_capture[capture_lead + index] += 8.0 * woofer_reference[index]
        full_quality = engine.sweep_capture_quality(synthetic_capture, woofer_reference, "left_woofer")
        woofer_quality = engine.sweep_capture_quality(synthetic_capture, woofer_reference, "woofer")
        require(woofer_quality["analysis_band_hz"][1] <= 500.0, "woofer quality gate is not passband limited")
        require(woofer_quality["subwoofer_passband"] is not None, "woofer adaptive -3 dB passband metadata is missing")
        require(full_quality["analysis_band_hz"] == [15.0, 22_000.0], "combined quality gate lost full-band analysis")
        require(woofer_quality["snr_db"] > full_quality["snr_db"] + 2.5, "subwoofer-only SNR still averages its silent high-frequency interval")

        # A real T5S quick capture exposed a boundary condition: its sustained
        # 53-95 Hz acoustic passband occupies only about 160 ms of a two-second
        # logarithmic sweep. It is valid measurement evidence, not a missing
        # signal, so the quality gate must not retain the former 200 ms floor.
        narrow_capture = [0.0001 * math.sin(2.0 * math.pi * index / 83.0) for index in range(capture_lead + len(woofer_reference))]
        narrow_low = active_indices[0] + round(
            (active_indices[-1] + 1 - active_indices[0])
            * math.log(53.0 / 15.0) / math.log(22_000.0 / 15.0)
        )
        narrow_high = active_indices[0] + round(
            (active_indices[-1] + 1 - active_indices[0])
            * math.log(95.0 / 15.0) / math.log(22_000.0 / 15.0)
        )
        for index in range(narrow_low, narrow_high):
            narrow_capture[capture_lead + index] += 12.0 * woofer_reference[index]
        narrow_quality = engine.sweep_capture_quality(narrow_capture, woofer_reference, "woofer")
        narrow_interval = narrow_quality["active_interval_samples"]
        require(
            narrow_quality["usable"] and narrow_interval[1] - narrow_interval[0] < engine.RATE // 5,
            "valid narrow subwoofer passband was rejected by a fixed 200 ms floor",
        )

        plausible_delay, plausible_details = engine.assess_bulk_delay(2_000, 1_048_576)
        late_delay, late_details = engine.assess_bulk_delay(518_895, 1_048_576)
        wrapped_delay, wrapped_details = engine.assess_bulk_delay(1_048_576 - 500, 1_048_576)
        require(plausible_delay == 2_000 and plausible_details["reliable"], "plausible bulk delay was rejected")
        require(late_delay == 0 and not late_details["reliable"], "late ESS artifact was accepted as acoustic delay")
        require(wrapped_delay == 0 and not wrapped_details["reliable"], "negative wrapped peak was accepted as acoustic delay")

        # ALSA/USB cold-start latency can consume the nominal 400 ms capture
        # arm interval or add more than it.  Quality and later deconvolution
        # must locate the recorded sweep instead of assuming fixed timing.
        timing_noise = lambda count: [0.0004 * math.sin(2.0 * math.pi * index / 97.0) for index in range(count)]
        standard_timing = timing_noise(capture_lead + len(front_reference))
        for index, value in enumerate(front_reference):
            standard_timing[capture_lead + index] += 8.0 * value
        standard_quality = engine.sweep_capture_quality(standard_timing, front_reference, "left_woofer")
        truncated_quality = engine.sweep_capture_quality(standard_timing[capture_lead:], front_reference, "left_woofer")
        extra_delay = round(1.10 * engine.RATE)
        delayed_timing = timing_noise(extra_delay + len(front_reference))
        for index, value in enumerate(front_reference):
            delayed_timing[extra_delay + index] += 8.0 * value
        delayed_quality = engine.sweep_capture_quality(delayed_timing, front_reference, "left_woofer")
        require(standard_quality["usable"] and truncated_quality["usable"] and delayed_quality["usable"], "dynamic sweep timing rejected a valid capture")
        require(abs(standard_quality["capture_delay_ms"] - 400.0) <= 60.0, "normal capture arm delay was not recovered")
        require(abs(truncated_quality["capture_delay_ms"]) <= 60.0, "truncated cold-start capture timing was not recovered")
        require(abs(delayed_quality["capture_delay_ms"] - 1100.0) <= 60.0, "long USB capture startup delay was not recovered")

        # A selector/stream transition may contaminate the pre-roll while the
        # post-roll remains representative. It must be reported, not promoted
        # to the stationary floor that previously produced a negative SNR.
        transient_timing = list(standard_timing)
        for index in range(round(0.10 * engine.RATE), round(0.62 * engine.RATE)):
            transient_timing[index] += 0.02 * math.sin(2.0 * math.pi * index / 31.0)
        transient_quality = engine.sweep_capture_quality(transient_timing, front_reference, "left")
        require(transient_quality["usable"], "pre-roll switching transient caused a false SNR failure")
        require(transient_quality["switching_transient_suspected"], "pre/post noise imbalance was not reported")
        guidance = engine.measurement_level_guidance({"snr_db": 6.4}, -30, -50.0)
        require(guidance["recommended_level_dbfs"] == -21 and guidance["recommended_raise_db"] == 9, "exact SNR level guidance is wrong")

        frequencies = [round(20.0 * (1000.0 ** (index / 511.0)), 6) for index in range(512)]
        measurements_index = []
        for position in range(1, 4):
            for source in ("left", "right", "woofer"):
                response_name = f"p{position}_{source}_response.json"
                response = synthetic_response(frequencies, source, position)
                response["response_algorithm_revision"] = engine.RESPONSE_ALGORITHM_REVISION
                response["smoothing"] = engine.SMOOTHING_NAME
                (session / response_name).write_text(json.dumps(response), encoding="utf-8")
                measurements_index.append({"position": position, "source": source, "response": response_name})
        phase_index = write_synthetic_phase_references(engine, session, 3)
        state = {
            "version": 2,
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
            "noise_level_dbfs": -36,
            "woofer_measurement_attenuation_db": -9,
            "sweep_seconds": 8,
            "measurements": measurements_index,
            "phase_references": phase_index,
            "phase_reference_acquisition_revision": "simultaneous-multisine-v1",
            "validation": None,
            "result": None,
            "measurement_profile": "speaker",
            "measurement_output": {"profile": "speaker", "label": engine.OUTPUT_PROFILE_LABELS["speaker"]},
        }
        engine.save_current(state)

        # Reference acceptance case: Flat target, no extra bass suppression,
        # 0 dB Woofer trim and otherwise default safe settings.  The isolated
        # LPF Woofer branch is diagnostic-only; the final Front+Woofer sum is
        # the target gate; same-recording relative phase drives the joint sum
        # guard and a later post-FIR capture remains optional evidence.
        baseline_started = time.monotonic()
        engine.build_worker("flat", "none", 0, "bass", 200, "equal", 0, 0, 20, 20_000, 10, 18, crossover_enabled=True, crossover_frequency_hz=100)
        baseline_state = engine.load_current()
        baseline_result = validate_result(engine, baseline_state, phase=True)
        baseline_validation = baseline_state["result"]["self_validation"]
        baseline_target_fit = baseline_validation["target_fit"]
        require(baseline_target_fit["left"]["pass"] and baseline_target_fit["right"]["pass"], "Flat/none/0 Front branches missed the selected target")
        require(baseline_target_fit["woofer"]["applicable"] is False and baseline_target_fit["woofer"]["pass"] is None, "Flat/none/0 incorrectly grades an isolated LPF Woofer against the full-system target")
        require(baseline_validation["overall_pass"] and baseline_validation["crossover_sum"]["status"] == "pass_independent_complex_model", "Flat/none/0 did not pass the same-recording L/R/W sum guard")
        require(baseline_state["result"]["crossover"]["safe_deploy_pass"] and baseline_state["result"]["crossover"]["complex_sum_target_pass"] is True, "Flat/none/0 did not use reliable relative phase")
        high_frequency = baseline_state["result"]["high_frequency_compensation"]
        require(high_frequency["maximum_relative_compensation_db"] == 10, "default maximum relative compensation was not recorded")
        require(high_frequency["common_attenuation_db"] == baseline_state["result"]["filter_bank_normalization"]["common_attenuation_db"], "high-frequency diagnostic lost the common bank attenuation")
        require(all(set(high_frequency["channels"][side]) == {"10000", "15000", "20000"} for side in ("left", "right")), "high-frequency residual diagnostic is incomplete")
        require(high_frequency["worst_abs_residual_db_15_20khz"] >= 0, "high-frequency residual diagnostic is invalid")
        for side in ("left", "right"):
            crossover_graph = baseline_state["result"]["crossover"]["channels"][side]
            require(crossover_graph["frequency"][0] <= 20.1 and crossover_graph["frequency"][-1] >= 19_000.0, "A/B sum graph is not full range")
            require(crossover_graph["metric_range_hz"][1] <= 300.0, "crossover target metric escaped its guarded low-frequency band")
            require(len(crossover_graph["phase_agnostic_energy_db"]) == len(crossover_graph["frequency"]), "phase-agnostic graph fallback is incomplete")
            require(
                crossover_graph["spatial_aggregation"]["method"]
                == "frequency-dependent spatial/SNR-weighted mean-square transfer power",
                "Front+Woofer graph does not use the same spatial power prototype as FIR design",
            )
        flat_target = engine.effective_combined_target(baseline_state["result"], frequencies)
        require(max(abs(value) for value in flat_target) <= 1.0e-6, "Flat/none/0 effective system target is not 0 dB")
        baseline_front_hash = baseline_state["result"]["front_sha256"]
        baseline_rear_hash = baseline_state["result"]["rear_sha256"]
        baseline_seconds = round(time.monotonic() - baseline_started, 3)

        # Precision mode must finish every acoustic capture before FIR design.
        # Its two physical sums validate the complex L/R/W model and constrain
        # the dense acoustic cross term without branch averaging.
        precise_index = list(measurements_index)
        for position in range(1, 4):
            woofer_response = json.loads((session / f"p{position}_woofer_response.json").read_text(encoding="utf-8"))
            for side, combined_source in (("left", "left_woofer"), ("right", "right_woofer")):
                main_response = json.loads((session / f"p{position}_{side}_response.json").read_text(encoding="utf-8"))
                combined = synthetic_combined_response(engine, main_response, woofer_response, -9.0)
                response_name = f"p{position}_{combined_source}_response.json"
                (session / response_name).write_text(json.dumps(combined), encoding="utf-8")
                precise_index.append({"position": position, "source": combined_source, "response": response_name})
        precise_state = json.loads(json.dumps(state))
        precise_state.update({
            "mode": "lrw_sum",
            "sources": ["left", "right", "woofer", "left_woofer", "right_woofer"],
            "measurements": precise_index,
            "result": None,
        })
        require(
            engine.position_measurement_source_order(precise_state)
            == ["left", "right", "woofer", "left_woofer", "right_woofer"],
            "Standard measurement order is not stable L/R/W/L+W/R+W at every position",
        )
        engine.save_current(precise_state)
        engine.build_worker("flat", "none", 0, "bass", 200, "equal", 0, 0, 20, 20_000, 10, 18, crossover_enabled=True, crossover_frequency_hz=100)
        precise_built = engine.load_current()
        validate_result(engine, precise_built, phase=True)
        precise_validation = precise_built["result"]["self_validation"]
        require(precise_validation["premeasured_sum_model"]["pass"], "exact premeasured complex sums failed model closure")
        require(precise_validation["crossover_sum"]["status"] == "pass_premeasured_complex_model", "precision mode did not complete six-capture validation")
        require(precise_validation["premeasured_sum_model"]["phase_verification_status"] == "pass", "same-recording relative phase was not used")
        physical_constraints = precise_built["result"]["crossover"]["physical_sum_constraints"]
        require(physical_constraints["used"], "L+W/R+W cross-term was not used by FIR synthesis")
        require(physical_constraints["phase_adjustment_p90_deg"] <= 0.25, "an exact physical sum produced a material phase adjustment")
        require(precise_built["result"]["filter_bank_normalization"]["relative_branch_gain_preserved"], "precision mode did not use one common bank normalization")

        engine.save_current(precise_state)
        engine.build_worker("flat", "none", 0, "bass", 200, "equal", 0, 0, 20, 20_000, 10, 18, crossover_enabled=False, crossover_frequency_hz=100)
        precise_full_range = engine.load_current()
        validate_result(engine, precise_full_range, phase=True)
        require(not precise_full_range["result"]["crossover"]["enabled"], "Crossover OFF unexpectedly embedded LR4 branches")
        require(precise_full_range["result"]["crossover"]["sum_guard_enabled"], "Crossover OFF disabled the mandatory full-system overlap guard")
        require(precise_full_range["result"]["self_validation"]["crossover_sum"]["status"] == "pass_premeasured_complex_model", "Crossover OFF graded Front/Woofer independently instead of validating all six captures")

        engine.save_current(precise_state)
        engine.build_worker("harman", "none", 0, "bass", 200, "equal", 0, 0, 20, 20_000, 10, 18, crossover_enabled=True, crossover_frequency_hz=100)
        precise_harman = engine.load_current()
        validate_result(engine, precise_harman, phase=True)
        harman_effective = engine.effective_combined_target(precise_harman["result"], frequencies)
        low_harman = [value for frequency, value in zip(frequencies, harman_effective) if 30.0 <= frequency <= 80.0]
        high_harman = [value for frequency, value in zip(frequencies, harman_effective) if 8_000.0 <= frequency <= 16_000.0]
        require(sum(low_harman) / len(low_harman) > sum(high_harman) / len(high_harman) + 3.0, "Harman effective target lost its bass-rise / treble-down shape")

        corrupted_path = session / "p1_right_woofer_response.json"
        exact_combined = json.loads(corrupted_path.read_text(encoding="utf-8"))
        corrupted = json.loads(json.dumps(exact_combined))
        corrupted["db"] = [value + 8.0 for value in corrupted["db"]]
        corrupted_path.write_text(json.dumps(corrupted), encoding="utf-8")
        failed_model = engine.evaluate_premeasured_sum_model(session, 3, -9.0, 100)
        require(not failed_model["pass"] and "3 · 위치 측정" in failed_model["action"] and "3곳 처음부터 재측정" in failed_model["action"], "sum-model FAIL lacks an executable wizard-menu action")
        corrupted_path.write_text(json.dumps(exact_combined), encoding="utf-8")

        # Measurement playback levels are acquisition/SNR controls only.  With
        # identical recovered responses, changing sweep and Woofer attenuation
        # must generate byte-identical Flat/none/0 FIRs.
        scaled_state = json.loads(json.dumps(state))
        scaled_state.update({"level_dbfs": -30, "noise_level_dbfs": -36, "woofer_measurement_attenuation_db": -18})
        engine.save_current(scaled_state)
        engine.build_worker("flat", "none", 0, "bass", 200, "equal", 0, 0, 20, 20_000, 10, 18, crossover_enabled=True, crossover_frequency_hz=100)
        scaled_result = engine.load_current()["result"]
        require(scaled_result["front_sha256"] == baseline_front_hash and scaled_result["rear_sha256"] == baseline_rear_hash, "measurement dBFS/Woofer attenuation changed the Flat/none/0 FIR")
        engine.save_current(state)

        started = time.monotonic()
        engine.build_worker("harman", "strong", -9, "magnitude", 200, crossover_enabled=False)
        magnitude_state = engine.load_current()
        magnitude = validate_result(engine, magnitude_state, phase=False, allow_sum_blocked=True)
        level_control = magnitude_state["result"]["woofer_level_control"]
        require(level_control["measurement_attenuation_compensated"], "woofer measurement attenuation was treated as playback level")
        require(level_control["automatic_boost_allowed"] is False, "woofer automatic boost was enabled")
        require(level_control["automatic_target_cut_median_db_40_120"] < 0.0, "loud synthetic woofer was not automatically cut against Front reference")
        woofer_graph = magnitude_state["result"]["graphs"]["woofer"]
        require(woofer_graph["level_reference"] == "shared Front L/R 500-2000 Hz", "woofer used a self-normalized level reference")
        require(woofer_graph["positive_woofer_gain_allowed"] is False, "woofer graph allows positive gain")
        require(max(woofer_graph["correction_db"]) <= 1e-6, "woofer FIR contains positive correction")
        magnitude_seconds = round(time.monotonic() - started, 3)

        started = time.monotonic()
        engine.build_worker("flat", "none", 0, "bass", 200, "center", 2, -2, 30, 5000, 3, 12)
        phase_state = engine.load_current()
        phase_result = validate_result(engine, phase_state, phase=True)
        require(phase_state["result"]["spatial_mode"] == "center", "spatial weighting metadata missing")
        require(phase_state["result"]["preference"] == {"bass_db_at_20_hz": 2, "treble_db_at_20_khz": -2}, "preference metadata mismatch")
        require(phase_state["result"]["correction_limits"]["high_hz"] == 5000, "correction limit metadata mismatch")
        left_phase_details = phase_state["result"]["graphs"]["left"]["phase"]
        right_phase_details = phase_state["result"]["graphs"]["right"]["phase"]
        require(left_phase_details.get("common_lr_phase") and right_phase_details.get("common_lr_phase"), "L/R phase correction is not common-mode")
        require(left_phase_details.get("relative_output_delay_samples") == 0 and right_phase_details.get("relative_output_delay_samples") == 0, "L/R phase correction introduced relative delay")
        require(left_phase_details.get("applied_strength") == right_phase_details.get("applied_strength"), "L/R phase strength differs")
        for channel in ("left", "right", "woofer"):
            graph = phase_state["result"]["graphs"][channel]
            require(graph and graph.get("target_db") and graph.get("spatial_std_db"), f"{channel} advanced graph data missing")
            require(graph.get("actual_correction_db") and graph.get("requested_correction_db"), f"{channel} actual FIR graph data missing")
            require(graph.get("automatic_room_correction_db") and graph.get("preference_correction_db"), f"{channel} room/preference correction split is missing")
            require(graph.get("fir_implementation", {}).get("pass"), f"{channel} FIR implementation verification failed")
            if channel == "woofer":
                require(graph.get("target_fit", {}).get("applicable") is False, "isolated LPF Woofer was incorrectly judged against a full-system target")
            else:
                require(graph.get("target_fit", {}).get("pass"), f"{channel} target-fit verification failed")
        require(max(phase_state["result"]["graphs"]["woofer"]["preference_correction_db"]) > 0.5, "explicit bass preference was erased by automatic woofer cut limiting")
        require(max(phase_state["result"]["graphs"]["woofer"]["correction_db"]) <= 1e-6, "explicit bass preference bypassed the Woofer cut-only safety policy")
        require(any(value < -0.1 for value in phase_state["result"]["graphs"]["woofer"]["decay_control_db"]), "long bass decay did not activate cut-only damping")
        require(phase_state["result"]["room_decay"]["policy"], "room decay policy metadata missing")
        alignment = phase_state["result"]["time_alignment"]
        require(alignment.get("requested") and alignment.get("reliable") and alignment.get("aligned"), "same-recording L/R/W relative timing did not evaluate Front/Woofer alignment")
        require("simultaneous" in str(alignment.get("reference", "")), "alignment falsely claimed an absolute shared U7/UMIK clock")
        crossover = phase_state["result"]["crossover"]
        require(crossover.get("enabled") and crossover.get("embedded_in_fir") and crossover.get("frequency_hz") == 100, "default digital crossover was not embedded in FIR")
        require(crossover.get("additional_runtime_filters") == 0 and crossover.get("additional_block_latency_samples") == 0, "embedded crossover incorrectly adds runtime/block latency")
        phase_search = crossover.get("relative_phase_optimization", {})
        require(
            phase_search.get("cancellation_deficit_p90_db", 999.0)
            <= phase_search.get("baseline_cancellation_deficit_p90_db", -999.0) + 0.251,
            "relative delay search improved target by introducing a deeper destructive crossover notch",
        )
        require(crossover.get("coherent_upper_guard_pass"), "joint Front+Woofer constructive-sum guard failed")
        require(crossover.get("safe_deploy_pass") and crossover.get("complex_sum_target_pass") is True, "joint Front+Woofer relative-phase target guard failed")
        bank_normalization = phase_state["result"]["filter_bank_normalization"]
        require(bank_normalization["relative_branch_gain_preserved"] and bank_normalization["channels"] == 4, "FIR bank did not use one common normalization gain")
        require(bank_normalization["peak_transfer_after_db"] <= 0.01, "common FIR bank normalization exceeded 0 dB")
        require(bank_normalization["scope"] == "complete_l_r_woofer_bank" and bank_normalization["maximum_relative_level_error_db"] <= 1.0e-6, "common FIR normalization did not preserve L/R/W relative level")
        require(bank_normalization["relative_compensation_limit_pass"], "common no-preamp attenuation exceeded the selected maximum relative compensation")
        common_reference = phase_state["result"]["common_level_reference"]
        require(common_reference["independent_channel_normalization"] is False and common_reference["scope"] == "L/R/Woofer complete bank", "automatic validation still uses per-channel 0 dB references")
        for channel in ("left", "right", "woofer"):
            require(phase_state["result"]["graphs"][channel]["common_reference"] == common_reference, f"{channel} graph does not share the bank level reference")
        for key in ("one_common_level_reference", "one_common_bank_gain", "relative_branch_level_preserved", "relative_compensation_limit", "narrow_null_boost_guard"):
            require(phase_state["result"]["self_validation"]["core_checks"].get(key), f"common-reference automatic validation failed: {key}")
        require(phase_state["result"]["graphs"]["left"]["crossover"]["role"] == "highpass", "Front crossover is not HPF")
        require(phase_state["result"]["graphs"]["woofer"]["crossover"]["role"] == "lowpass", "Woofer crossover is not LPF")
        post_prediction = {
            side: engine.predicted_combined_response(phase_state["result"], side, frequencies)[0]
            for side in ("left", "right")
        }
        for position in range(1, 4):
            for side, side_offset in (("left", 0.15), ("right", -0.15)):
                response = {
                    "frequencies": frequencies,
                    "db": [
                        value + 68.0 + side_offset + (position - 2) * 0.10
                        for value in post_prediction[side]
                    ],
                    "smoothing": "variable 1/12 octave <200 Hz; 1/6 octave 200-2000 Hz; 1/3 octave >2 kHz",
                    "measurement_quality": {"snr_db": 28.0, "usable": True},
                }
                (session / f"post_p{position}_{side}_sum_response.json").write_text(json.dumps(response), encoding="utf-8")
        post_evaluation = engine.evaluate_post_filter_sum(session, phase_state, {"level_dbfs": -48})
        require(post_evaluation["overall_pass"] and post_evaluation["target_pass"] and post_evaluation["crossover_pass"], "valid measured post-FIR L+Woofer/R+Woofer sum was rejected")
        require(post_evaluation["prediction_consistency"]["pass"], "matching post-FIR prediction was rejected")
        require(post_evaluation["common_level_reference"]["independent_channel_normalization"] is False, "post-FIR measurements were normalized independently")
        require(abs(post_evaluation["lr_match"]["reference_level_difference_db"] - 0.3) <= 0.02, "shared post-FIR reference did not retain the synthetic L/R level offset")
        left_post_original = {}
        for position in range(1, 4):
            left_post_path = session / f"post_p{position}_left_sum_response.json"
            left_post_original[position] = left_post_path.read_bytes()
            mismatched_post = json.loads(left_post_original[position].decode("utf-8"))
            mismatched_post["db"] = [
                value + (12.0 if frequency <= 300.0 else 0.0)
                for frequency, value in zip(mismatched_post["frequencies"], mismatched_post["db"])
            ]
            left_post_path.write_text(json.dumps(mismatched_post), encoding="utf-8")
        mismatched_evaluation = engine.evaluate_post_filter_sum(session, phase_state, {"level_dbfs": -48})
        require(not mismatched_evaluation["prediction_consistency"]["pass"] and not mismatched_evaluation["overall_pass"], "large predicted-vs-measured mismatch was not blocked")
        for position in range(1, 4):
            left_post_path = session / f"post_p{position}_left_sum_response.json"
            transient_post = json.loads(left_post_path.read_text(encoding="utf-8"))
            transient_post["measurement_quality"]["switching_transient_suspected"] = True
            transient_post["measurement_quality"]["noise_side_spread_db"] = 18.0
            left_post_path.write_text(json.dumps(transient_post), encoding="utf-8")
        transient_evaluation = engine.evaluate_post_filter_sum(session, phase_state, {"level_dbfs": -48})
        require(
            transient_evaluation["inconclusive_switching_transient"]
            and not transient_evaluation["application_blocking"]
            and not transient_evaluation["overall_pass"]
            and transient_evaluation["recommended_retry"]["level_dbfs"] == -48,
            "stream-start contamination was mislabeled PASS or a conclusive DSP failure",
        )
        for position, content in left_post_original.items():
            (session / f"post_p{position}_left_sum_response.json").write_bytes(content)
        clean_transient_path = session / "post_p1_left_sum_response.json"
        clean_transient_original = clean_transient_path.read_bytes()
        clean_transient = json.loads(clean_transient_original.decode("utf-8"))
        clean_transient["measurement_quality"]["switching_transient_suspected"] = True
        clean_transient["measurement_quality"]["noise_side_spread_db"] = 8.0
        clean_transient_path.write_text(json.dumps(clean_transient), encoding="utf-8")
        clean_transient_evaluation = engine.evaluate_post_filter_sum(session, phase_state, {"level_dbfs": -48})
        require(
            clean_transient_evaluation["overall_pass"]
            and not clean_transient_evaluation["inconclusive_switching_transient"]
            and clean_transient_evaluation["prediction_consistency"]["pass"]
            and clean_transient_evaluation["switching_transient"]["suspected"]
            and not clean_transient_evaluation["switching_transient"]["affected_verdict"],
            "stationary pre/post floor change incorrectly demoted an otherwise passing acoustic transfer",
        )
        clean_transient["measurement_quality"].setdefault("frequency_noise", {})["transient_contamination_detected"] = True
        clean_transient_path.write_text(json.dumps(clean_transient), encoding="utf-8")
        active_transient_evaluation = engine.evaluate_post_filter_sum(session, phase_state, {"level_dbfs": -48})
        require(
            active_transient_evaluation["inconclusive_switching_transient"]
            and not active_transient_evaluation["overall_pass"]
            and active_transient_evaluation["switching_transient"]["active_sweep_transient_suspected"],
            "transient detected inside the active sweep was incorrectly accepted",
        )
        clean_transient_path.write_bytes(clean_transient_original)
        advisory_original = {}
        for position in range(1, 4):
            for side in ("left", "right"):
                path = session / f"post_p{position}_{side}_sum_response.json"
                advisory_original[(position, side)] = path.read_bytes()
                response = json.loads(advisory_original[(position, side)].decode("utf-8"))
                response["db"] = [
                    value + (6.0 if 80.0 <= frequency <= 120.0 else 0.0)
                    for frequency, value in zip(response["frequencies"], response["db"])
                ]
                response["measurement_quality"]["snr_db"] = 8.0
                path.write_text(json.dumps(response), encoding="utf-8")
        advisory_evaluation = engine.evaluate_post_filter_sum(session, phase_state, {"level_dbfs": -48, "sweep_seconds": 14})
        require(advisory_evaluation["inconclusive_low_snr"] and not advisory_evaluation["application_blocking"] and not advisory_evaluation["overall_pass"], "marginal low-SNR post-FIR mismatch was mislabeled PASS or conclusive FAIL")
        for (position, side), content in advisory_original.items():
            (session / f"post_p{position}_{side}_sum_response.json").write_bytes(content)

        reprocess_state = json.loads(json.dumps(phase_state))
        reprocess_state["state"] = "built"
        reprocess_state["post_filter_validation"] = {
            "result_fingerprint": engine.result_fingerprint(reprocess_state["result"]),
            "profile": "speaker",
            "level_dbfs": -48,
            "sweep_seconds": 28,
            "positions_total": 3,
            "positions_completed": 3,
            "measurements": [],
            "evaluation": {"verification_status": "stale-fixture"},
        }
        engine.save_current(reprocess_state)
        reprocessed_post = engine.reprocess_post_filter_validation()
        require(
            reprocessed_post["post_filter_validation"]["evaluation"]["overall_pass"]
            and reprocessed_post["result"]["crossover"]["status"] == "pass_measured"
            and reprocessed_post["stage"].endswith("합산 실측 PASS"),
            "saved post-FIR responses were not re-graded and persisted without replay",
        )

        # Resetting a post-FIR failure must restore the premeasurement model
        # verdict.  The old implementation read the overwritten fail_measured
        # status back into the UI and left report.json stale after Reset.
        reset_state = json.loads(json.dumps(phase_state))
        reset_result = reset_state["result"]
        original_front_sha = reset_result["front_sha256"]
        original_rear_sha = reset_result["rear_sha256"]
        reset_result["self_validation"]["post_filter_sum"] = advisory_evaluation
        reset_result["self_validation"]["crossover_sum"] = {
            "required": True,
            "pass": False,
            "status": "fail_measured",
            "premeasurement_status": "pass_premeasured_complex_model",
        }
        reset_result["self_validation"]["overall_pass"] = False
        reset_result["crossover"]["post_filter_measurement"] = advisory_evaluation
        reset_result["crossover"]["status"] = "fail_measured"
        reset_result["crossover"]["overall_acoustic_prediction_pass"] = False
        reset_state["post_filter_validation"] = {"evaluation": advisory_evaluation}
        engine.save_current(reset_state)
        reset_result_state = engine.reset_post_filter_validation()
        reset_validation = reset_result_state["result"]["self_validation"]
        reset_crossover = reset_result_state["result"]["crossover"]
        require(reset_result_state["post_filter_validation"] is None, "post-FIR Reset retained the old measurement state")
        require("post_filter_sum" not in reset_validation and "post_filter_measurement" not in reset_crossover, "post-FIR Reset retained stale measured diagnostics")
        require(reset_validation["overall_pass"] and reset_validation["crossover_sum"]["pass"], "post-FIR Reset did not restore the passing premeasurement model")
        require(reset_validation["crossover_sum"]["status"] == "pass_independent_complex_model", "post-FIR Reset restored the wrong independent-model validation status")
        require(reset_validation["crossover_sum"]["prediction_status"] == "pass" and reset_crossover["status"] == "pass", "post-FIR Reset retained fail_measured in model status")
        require(reset_crossover["overall_acoustic_prediction_pass"] is True, "post-FIR Reset did not restore the model acoustic pass")
        require(reset_result_state["result"]["front_sha256"] == original_front_sha and reset_result_state["result"]["rear_sha256"] == original_rear_sha, "post-FIR Reset changed generated FIR artifacts")
        persisted_report = json.loads((session / reset_result_state["result"]["report_json"]).read_text(encoding="utf-8"))
        require(persisted_report["crossover"]["status"] == "pass" and "post_filter_measurement" not in persisted_report["crossover"], "post-FIR Reset left report JSON stale")

        precise_reset_state = json.loads(json.dumps(precise_built))
        precise_reset_result = precise_reset_state["result"]
        precise_reset_result["self_validation"]["post_filter_sum"] = advisory_evaluation
        precise_reset_result["self_validation"]["crossover_sum"] = {
            "required": True,
            "pass": False,
            "status": "fail_measured",
            "premeasurement_status": "pass_premeasured_complex_model",
        }
        precise_reset_result["self_validation"]["overall_pass"] = False
        precise_reset_result["crossover"]["post_filter_measurement"] = advisory_evaluation
        precise_reset_result["crossover"]["status"] = "fail_measured"
        precise_reset_result["crossover"]["overall_acoustic_prediction_pass"] = False
        precise_reset_state["post_filter_validation"] = {"evaluation": advisory_evaluation}
        engine.save_current(precise_reset_state)
        precise_reset = engine.reset_post_filter_validation()
        require(precise_reset["result"]["self_validation"]["crossover_sum"]["status"] == "pass_premeasured_complex_model", "post-FIR Reset did not restore the six-capture model status")
        require(precise_reset["result"]["self_validation"]["crossover_sum"]["prediction_status"] == "pass" and precise_reset["result"]["crossover"]["status"] == "pass", "six-capture Reset retained fail_measured")
        engine.save_current(phase_state)
        phase_seconds = round(time.monotonic() - started, 3)

        # A deliberately over-attenuated woofer preference can no longer be
        # mislabeled as a successful acoustic crossover merely because the WAV
        # files are structurally valid. It is still generated for Preview, but
        # the target-sum status must remain explicit.
        engine.build_worker("harman", "strong", -9, "bass", 200)
        conservative_state = engine.load_current()
        conservative_crossover = conservative_state["result"]["crossover"]
        require(conservative_crossover.get("embedded_in_fir") and conservative_crossover.get("coherent_upper_guard_pass"), "conservative crossover lost FIR embedding or upper-sum safety")
        require(not conservative_state["result"]["self_validation"]["overall_pass"] and conservative_crossover.get("status") == "fail_target", "over-attenuated synthetic crossover was falsely labeled PASS")

        # Combined SISO must tune the measured L+Woofer / R+Woofer sum with one
        # stereo FIR.  It must not invent a separately scaled Rear FIR, and the
        # measured relative level must be carried into the runtime mixer.
        dependency_state = engine.load_current()
        original_position_responses = {
            (position, source): (session / f"p{position}_{source}_response.json").read_bytes()
            for position in (2, 3) for source in ("left", "right", "woofer")
        }
        for position in (2, 3):
            for source in ("left", "right", "woofer"):
                (session / f"p{position}_{source}_response.json").write_bytes(
                    (session / f"p1_{source}_response.json").read_bytes()
                )
        duplicate_state = json.loads(json.dumps(state))
        duplicate_state.update({"state": "measured", "result": None})
        engine.save_current(duplicate_state)
        engine.build_worker("flat", "none", 0, "magnitude", 200, crossover_enabled=True, crossover_frequency_hz=100)
        duplicate_validation = engine.load_current()["result"]["self_validation"]
        duplicate_rows = duplicate_validation["independent_positions"]["reused_measurements"]
        require(
            not duplicate_validation["overall_pass"]
            and len(duplicate_rows) == 6
            and all(item.get("detected_by") == "exact_response_vector_duplicate" for item in duplicate_rows),
            "legacy exact-copy position responses were falsely accepted as independent measurements",
        )
        for (position, source), content in original_position_responses.items():
            (session / f"p{position}_{source}_response.json").write_bytes(content)
        engine.save_current(dependency_state)
        combined_measurements = []
        for position in range(1, 4):
            for source, donor in (("left_woofer", "left"), ("right_woofer", "right")):
                source_name = f"p{position}_{source}_response.json"
                donor_name = f"p{position}_{donor}_response.json"
                (session / source_name).write_bytes((session / donor_name).read_bytes())
                combined_measurements.append({"position": position, "source": source, "response": source_name})
        combined_state = json.loads(json.dumps(dependency_state))
        combined_state.update({
            "state": "measured",
            "mode": "lr",
            "sources": ["left_woofer", "right_woofer"],
            "measurements": combined_measurements,
            "woofer_measurement_attenuation_db": -9,
            "result": None,
        })
        engine.save_current(combined_state)
        combined_started = time.monotonic()
        try:
            engine.build_worker("harman", "strong", -9, "magnitude", 200)
        except engine.MeasurementError as exc:
            require("L/R/W" in str(exc), "combined mode crossover rejection is unclear")
        else:
            raise AssertionError("combined L+Woofer mode incorrectly accepted independent crossover ON")
        engine.build_worker("harman", "strong", -9, "magnitude", 200, crossover_enabled=False)
        combined_result = engine.load_current()["result"]
        require(combined_result["rear"] is None, "combined SISO unexpectedly generated a separate Rear FIR")
        require(combined_result["rear_metrics"] is None, "combined SISO unexpectedly used four convolution channels")
        require(combined_result["graphs"]["woofer"]["runtime_mixer_trim"], "combined SISO runtime mixer trim metadata missing")
        require(combined_result["graphs"]["woofer"]["woofer_trim_db"] == -9, "combined SISO trim does not match measurement")
        require(combined_result["measurement_output"]["woofer_level_semantics"] == "measured system balance and final runtime trim", "combined level semantics missing")
        combined_seconds = round(time.monotonic() - combined_started, 3)
        engine.save_current(dependency_state)

        # Fast mode is a valid one-position optimization, but must not claim
        # spatial stability that was never measured.
        fast_state = json.loads(json.dumps(dependency_state))
        fast_state.update({
            "state": "measured",
            "positions_total": 1,
            "positions_completed": 1,
            "measurements": [item for item in measurements_index if item["position"] == 1],
            "result": None,
            "post_filter_validation": None,
        })
        engine.save_current(fast_state)
        fast_started = time.monotonic()
        engine.build_worker("flat", "none", 0, "magnitude", 200, crossover_enabled=False)
        fast_result = engine.load_current()["result"]
        fast_seconds = round(time.monotonic() - fast_started, 3)
        fast_crossover = fast_result["self_validation"]["crossover_sum"]
        require(
            fast_result["self_validation"]["overall_pass"]
            and fast_crossover["status"] in ("pass_independent_complex_model", "pass_safe_upper_phase_limited"),
            f"Fast standard SISO did not complete its one-position sum validation: {json.dumps(fast_crossover, sort_keys=True)}",
        )
        require(fast_result["measurement_coverage"]["mode"] == "fast_single_position", "Fast coverage metadata is wrong")
        require(fast_result["measurement_coverage"]["spatial_stability_applicable"] is False, "Fast mode falsely claims spatial stability")
        require(fast_result["self_validation"]["independent_positions"]["spatial_stability_applicable"] is False, "Fast independent-position check is mislabeled")
        engine.save_current(dependency_state)

        # Wizard navigation itself is client-side and non-mutating. Actual setting
        # application invalidates only dependent artifacts.
        dependency_state["level_check"] = {
            "ok": True,
            "snr_db": 30.0,
            "requested_level_dbfs": -42,
            "peak_dbfs": -20.0,
            "channels": [
                {"source": source, "snr_db": 30.0, "peak_dbfs": -20.0, "requested_level_dbfs": -42}
                for source in dependency_state["sources"]
            ],
        }
        engine.save_current(dependency_state)
        same = engine.reconfigure_session("lrw", "90", -42, 8)
        require(same.get("result") and len(same["measurements"]) == 9 and same["level_check"]["ok"], "unchanged settings discarded data")
        mode_changed = engine.reconfigure_session("lr", "90", -42, 8)
        require(mode_changed["result"] is None and mode_changed["measurements"] == [] and mode_changed["level_check"] is None, "mode change did not invalidate the incomplete quick-check coverage")
        require(mode_changed["sources"] == ["left_woofer", "right_woofer"], "combined mode source routing metadata is wrong")
        engine.save_current(dependency_state)
        level_changed = engine.reconfigure_session("lrw", "90", -36, 8)
        require(level_changed["result"] is None and level_changed["measurements"] == [] and level_changed.get("level_check") is None, "sweep-level change preserved a quick check captured at another level")
        engine.save_current(dependency_state)
        noise_changed = engine.reconfigure_session("lrw", "90", -42, 8, -30, -9)
        require(noise_changed.get("level_check", {}).get("ok") and noise_changed["noise_level_dbfs"] == -42, "hidden legacy noise field diverged from the authoritative sweep level")
        engine.save_current(dependency_state)
        woofer_level_changed = engine.reconfigure_session("lrw", "90", -42, 8, -36, -6)
        require(woofer_level_changed.get("level_check") is None and len(woofer_level_changed["measurements"]) == 6 and all(item["source"] in ("left", "right") for item in woofer_level_changed["measurements"]) and woofer_level_changed["woofer_measurement_attenuation_db"] == -6, "woofer measurement attenuation did not invalidate quick check while preserving independent Front measurements")
        engine.save_current(dependency_state)
        prepared = engine.prepare_build()
        require(prepared["result"] is None and len(prepared["measurements"]) == 9 and prepared["level_check"]["ok"], "FIR rebuild invalidated raw measurements")
        preferences = engine.save_correction_preferences({**engine.DEFAULT_CORRECTION_PREFERENCES, "target": "flat", "max_boost_db": 3})
        require(engine.load_correction_preferences() == preferences and preferences["target"] == "flat", "correction preference persistence failed")

        # Session notes are metadata only. Saving them must not invalidate any
        # completed wizard stage, while loading a saved session restores the
        # exact verified checkpoint and rejects fake completion metadata.
        dependency_state["level_check"] = {
            "ok": True,
            "snr_db": 30.0,
            "peak_dbfs": -20.0,
            "requested_level_dbfs": -42,
            "channels": [
                {"source": source, "snr_db": 30.0, "peak_dbfs": -20.0, "requested_level_dbfs": -42}
                for source in dependency_state["sources"]
            ],
        }
        engine.save_current(dependency_state)
        engine.atomic_json(session / "session.json", dependency_state)
        before_note = engine.load_current()
        note_result = engine.set_session_note("중앙 청취점 · Woofer 노브 11시")
        after_note = engine.load_current()
        require(note_result["session_note"].startswith("중앙 청취점"), "session note was not saved")
        for key in ("state", "positions_completed", "measurements", "level_check", "result", "applied_profile"):
            require(after_note.get(key) == before_note.get(key), f"session note unexpectedly changed {key}")
        paused = measurements / "paused"
        paused.mkdir()
        paused_state = dict(dependency_state)
        paused_state.update({
            "session_id": "paused", "session_dir": str(paused), "state": "ready",
            "stage": "위치 2에서 이어가기", "positions_completed": 1,
            "measurements": [item for item in measurements_index if item["position"] == 1],
            "phase_references": [item for item in phase_index if item["position"] == 1],
            "result": None, "applied_profile": None,
        })
        for item in paused_state["measurements"]:
            shutil.copyfile(session / item["response"], paused / item["response"])
        shutil.copyfile(session / "p1_phase_reference.json", paused / "p1_phase_reference.json")
        engine.atomic_json(paused / "session.json", paused_state)
        (paused / "session-note.txt").write_text("소파 왼쪽 위치까지 완료\n", encoding="utf-8")
        loaded_paused = engine.load_session("paused")
        require(loaded_paused["integrity"]["positions_completed"] == 1, "saved session checkpoint was not restored")
        paused_current = engine.load_current()
        require(paused_current["session_note"] == "소파 왼쪽 위치까지 완료" and paused_current["positions_completed"] == 1, "saved session note/progress was not loaded")
        catalog_sessions = engine.list_sessions()["sessions"]
        require(any(item["session_id"] == "paused" and item["note"].startswith("소파") for item in catalog_sessions), "session list omitted the adjacent note")
        loaded_full = engine.load_session("synthetic")
        require(loaded_full["integrity"]["positions_completed"] == 3 and loaded_full["integrity"]["has_result"], "completed FIR session was not restored")
        broken = measurements / "broken"
        broken.mkdir()
        broken_state = dict(paused_state)
        broken_state.update({"session_id": "broken", "session_dir": str(broken)})
        engine.atomic_json(broken / "session.json", broken_state)
        try:
            engine.load_session("broken")
        except engine.MeasurementError as exc:
            require("파일" in str(exc), "corrupt session load error lacks artifact guidance")
        else:
            raise AssertionError("session with missing completed artifacts was loaded")
        engine.save_current(dependency_state)

        interrupted = dict(dependency_state)
        interrupted.update(state="processing", worker_pid=2_000_000_000, active_pids=[2_000_000_001])
        engine.save_current(interrupted)
        recovered = engine.load_current()
        require(recovered["state"] == "error" and recovered.get("worker_pid") is None and recovered.get("interrupted_worker"), "dead measurement worker was not recovered safely")
        persisted = json.loads(engine.CURRENT.read_text(encoding="utf-8"))
        require(persisted["state"] == "processing", "status-only stale-worker recovery unexpectedly mutated the session")

        # Starting an explicit raw-WAV reprocess is the point at which every
        # response-dependent result becomes stale.  The installed production
        # FIR is outside this isolated state and must not be touched, but the UI
        # must never continue to offer the previous result for Preview/Apply.
        prepare_dir = measurements / "prepare-reprocess"
        prepare_dir.mkdir()
        for source in ("left", "right"):
            (prepare_dir / f"p1_{source}_recorded.wav").write_bytes(b"saved raw placeholder")
        prepare_state = dict(dependency_state)
        prepare_state.update({
            "session_id": "prepare-reprocess", "session_dir": str(prepare_dir),
            "state": "measured", "mode": "lr", "sources": ["left", "right"],
            "positions_total": 1, "positions_completed": 1,
            "measurements": [{"position": 1, "source": source} for source in ("left", "right")],
            "phase_references": [{"position": 1}],
            "validation": {"pass": True}, "premeasured_sum_validation": {"pass": True},
            "result": {"front": "stale.wav"}, "post_filter_validation": {"pass": True},
            "preview_active": False,
        })
        engine.save_current(prepare_state)
        prepared = engine.prepare_saved_reprocess()
        require(
            prepared["state"] == "ready"
            and prepared["positions_completed"] == 0
            and not prepared["measurements"]
            and prepared.get("result") is None
            and prepared.get("post_filter_validation") is None
            and not prepared.get("phase_references")
            and prepared.get("premeasured_sum_validation") is None,
            "raw reprocess retained stale FIR/measurement-dependent state",
        )
        engine.save_current(dependency_state)

        # A batch raw-WAV recovery must retain its owner PID until the outer
        # worker finishes. Clearing it after each channel made the status API
        # falsely report an interrupted job while Pi 2 was still calculating.
        batch_dir = measurements / "batch-reprocess"
        batch_dir.mkdir()
        for source in ("left", "right"):
            (batch_dir / f"p1_{source}_recorded.wav").write_bytes(b"saved raw placeholder")
        batch_state = dict(dependency_state)
        batch_state.update({
            "session_id": "batch-reprocess", "session_dir": str(batch_dir),
            "state": "processing", "worker_pid": os.getpid(), "active_pids": [],
            "mode": "lrw", "sources": ["left", "right"], "positions_total": 1,
            "positions_completed": 0, "measurements": [], "result": None,
        })
        engine.save_current(batch_state)
        original_helpers = (
            engine.reference_sweep_for_source, engine.read_pcm_wav,
            engine.sweep_capture_quality, engine.response_from_recording,
            engine.calibration_for, engine.measurement_worker_alive,
        )
        engine.reference_sweep_for_source = lambda *_args, **_kwargs: [0.0, 0.1, -0.1]
        engine.read_pcm_wav = lambda _path: (48_000, 24, [0.0, 0.1, -0.1])
        engine.sweep_capture_quality = lambda *_args, **_kwargs: {"snr_db": 20.0, "recommended": True}
        engine.response_from_recording = lambda *_args, **_kwargs: {
            "peak_dbfs": -20.0, "rms_dbfs": -30.0,
            "measurement_quality": {"snr_db": 20.0, "recommended": True},
        }
        engine.calibration_for = lambda _orientation: {}
        engine.measurement_worker_alive = lambda _state: True
        try:
            engine.inspect_saved_recording(1, "left", reprocess=True, batch_reprocess=True)
            midway = json.loads(engine.CURRENT.read_text(encoding="utf-8"))
            require(midway["state"] == "processing" and midway["worker_pid"] == os.getpid(), "batch recovery lost its live worker after the first channel")
            engine.inspect_saved_recording(1, "right", reprocess=True, batch_reprocess=True)
            finished_batch = json.loads(engine.CURRENT.read_text(encoding="utf-8"))
            require(finished_batch["state"] == "measured" and finished_batch["worker_pid"] == os.getpid(), "batch recovery lost its worker before outer completion")
        finally:
            (
                engine.reference_sweep_for_source, engine.read_pcm_wav,
                engine.sweep_capture_quality, engine.response_from_recording,
                engine.calibration_for, engine.measurement_worker_alive,
            ) = original_helpers
            engine.save_current(dependency_state)

        report = {
            "result": "PASS",
            "fft": fft_test,
            "targets": len(catalog["targets"]),
            "level_check_offline": True,
            "quick_sweep_pass_snr_db": engine.PREFLIGHT_TARGET_SNR_DB,
            "quick_sweep_recommended_snr_db": engine.RECOMMENDED_SNR_DB,
            "response_power_domain_smoothing": True,
            "filter_gain_db_domain_smoothing": True,
            "weighted_power_spatial_prototype": True,
            "shared_clock_environment_override": "AUDIODSP_PHASE_CLOCK_SHARED",
            "legacy_response_reprocess_marker": True,
            "natural_rolloff_guard": True,
            "branch_local_natural_band_guard": True,
            "woofer_measurement_attenuation_db": engine.WOOFER_MEASUREMENT_ATTENUATION_DB,
            "single_authoritative_sweep_level": True,
            "sweep_hardware_unity_independent_of_listening_volume": True,
            "volume_restored_before_input_reconnect": True,
            "restore_failure_keeps_input_disconnected": True,
            "combined_lr_routing": "L+Woofer / R+Woofer",
            "woofer_quality_analysis_band": "adaptive sustained -3 dB acoustic passband; 15-300 Hz fallback",
            "capture_then_batch_response_processing": True,
            "batch_reprocess_worker_liveness": True,
            "raw_reprocess_stale_result_invalidation": True,
            "combined_build_seconds": combined_seconds,
            "combined_single_convolution_then_copy": True,
            "embedded_lr4_crossover_default_on": True,
            "joint_front_woofer_sum_guard": True,
            "joint_sum_weighted_power_spatial_prototype": True,
            "relative_phase_destructive_cancellation_guard": True,
            "acoustic_crossover_false_positive_rejected": True,
            "session_checkpoint_resume": True,
            "session_note_non_invalidating": True,
            "session_artifact_integrity_gate": True,
            "legacy_duplicate_position_detection": True,
            "actual_fir_target_verification": True,
            "measurement_level_transfer_invariance_max_complex_error": transfer_scale_error,
            "post_filter_sum_verified": True,
            "fast_single_position_verified": True,
            "fast_build_seconds": fast_seconds,
            "room_decay_control": True,
            "dependency_invalidation": True,
            "correction_preferences": True,
            "output_contract": "48 kHz / stereo float32 WAV / exactly 32768 taps per channel",
            "magnitude_build_seconds": magnitude_seconds,
            "bass_phase_build_seconds": phase_seconds,
            "flat_none_trim0_baseline_build_seconds": baseline_seconds,
            "flat_none_trim0_measurement_level_invariant": True,
            "flat_none_trim0_final_sum_gate": "PASS: clock-safe upper guard plus phase-agnostic target estimate",
            "precision_premeasured_sum_no_post_sweep": precise_validation["crossover_sum"]["status"] in (
                "pass_premeasured_complex_model", "pass_safe_sum_phase_limited",
            ),
            "magnitude": magnitude,
            "bass_phase": phase_result,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
