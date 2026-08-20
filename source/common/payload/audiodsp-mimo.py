#!/usr/bin/env python3
"""Robust low-frequency MIMO room correction for AudioDSP Pi 4/5."""

from __future__ import annotations

import argparse
import cmath
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import statistics
import tempfile
from typing import Any


RATE = 48_000
TAPS = 32_768
FFT_LENGTH = 65_536
PATHS = 8
MIMO_MODES = {
    "mimo_stereo": ("front_left", "front_right"),
    "mimo_one_sub": ("front_left", "front_right", "sub_pair"),
    "mimo_dual_sub": ("front_left", "front_right", "sub_left", "sub_right"),
}
OUTPUT_LABELS = ("Front_Left", "Front_Right", "Rear_Left", "Rear_Right")


class MimoError(RuntimeError):
    pass


def linux_memory_mb() -> dict[str, int | None]:
    """Return Linux memory capacity without making procfs a hard dependency."""
    values: dict[str, int | None] = {"total_mb": None, "available_mb": None}
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, raw = line.split(":", 1)
            fields[name] = int(raw.strip().split()[0])
        values["total_mb"] = fields.get("MemTotal", 0) // 1024 or None
        values["available_mb"] = fields.get("MemAvailable", 0) // 1024 or None
    except (OSError, ValueError, IndexError):
        pass
    return values


def resource_budget(inputs: int = 2, outputs: int = 4, taps: int = TAPS, chunksize: int = 1024) -> dict[str, Any]:
    """Conservative memory/compute model for dense FIR matrices.

    CamillaDSP uses float32 partitioned convolution.  The generator is Python
    and therefore budgets much more per spectrum/impulse element than the raw
    WAV size.  Values are planning bounds, not a substitute for a Pi hardware
    XRUN/load test.
    """
    if min(inputs, outputs, taps, chunksize) <= 0:
        raise ValueError("resource dimensions must be positive")
    paths = inputs * outputs
    segments = math.ceil(taps / chunksize)
    bins = chunksize + 1
    raw_coefficients = paths * taps * 4
    partition_spectra = paths * segments * bins * 8
    # Filter spectra + convolution histories + overlap/scratch, then a 4x
    # allocator/implementation margin.  Add a small fixed engine allowance.
    runtime_dsp = 24 * 1024 * 1024 + 4 * (
        raw_coefficients + 2 * partition_spectra + paths * chunksize * 4 * 8
    )
    fft_length = 1 << math.ceil(math.log2(max(taps * 2, chunksize * 2)))
    full_bins = fft_length // 2 + 1
    # 64-bit CPython planning bound: pointer + complex object for spectra,
    # pointer + float object for impulses.  The implementation releases the
    # path spectra before causalization and scales output paths in-place.
    spectrum_per_path = 64 + full_bins * (8 + 32)
    impulse_per_path = 64 + fft_length * (8 + 24)
    causal_per_path = 64 + taps * (8 + 24)
    generator_live = paths * max(
        spectrum_per_path + impulse_per_path,
        impulse_per_path + causal_per_path,
        causal_per_path + spectrum_per_path,
    )
    # Response processing is sequential.  Allow 256 MiB for a long recording,
    # FFTW plans, graphs and solver temporaries, then double the live estimate.
    generator_peak = 256 * 1024 * 1024 + 2 * generator_live
    complex_macs_per_second = paths * segments * bins * RATE / chunksize
    mib = 1024 * 1024
    return {
        "inputs": inputs,
        "outputs": outputs,
        "matrix_paths": paths,
        "taps": taps,
        "chunksize": chunksize,
        "segments_per_path": segments,
        "raw_coefficients_mib": round(raw_coefficients / mib, 2),
        "runtime_dsp_planning_mib": math.ceil(runtime_dsp / mib),
        "filter_generation_planning_mib": math.ceil(generator_peak / mib),
        "partition_complex_macs_per_second": round(complex_macs_per_second),
        "interpretation": "memory planning bound; CPU/USB/XRUN acceptance still requires target-hardware load testing",
    }


def environment(suffix: str, default: str) -> str:
    return os.environ.get(f"AUDIODSP_{suffix}", default)


def load_measurement_engine(path: Path | None = None):
    source = path or Path(environment("MEASUREMENT_ENGINE", "/usr/local/bin/audiodsp-measurement.py"))
    spec = importlib.util.spec_from_file_location("audiodsp_measurement_for_mimo", source)
    if spec is None or spec.loader is None:
        raise MimoError(f"측정 엔진을 불러올 수 없습니다: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def platform_capability() -> dict[str, Any]:
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
    supported = kind in ("pi4plus", "development", "test")
    memory = linux_memory_mb()
    return {
        "platform_class": kind,
        "mimo_supported": supported,
        "minimum": "Raspberry Pi 4, 64-bit AudioDSP release",
        "reason": "8 FFT convolution paths require Pi 4/5-class CPU and memory" if not supported else None,
        "memory": memory,
        "resource_budget": resource_budget(),
        "projected_5_1_dual_sub_dense_budget": resource_budget(inputs=6, outputs=7),
    }


def require_supported() -> None:
    capability = platform_capability()
    if not capability["mimo_supported"]:
        raise MimoError(f"MIMO는 Pi 4/5 전용입니다. 현재 플랫폼: {capability['platform_class']}")


def interpolate_log(frequencies: list[float], values: list[float], frequency: float) -> float:
    if frequency <= frequencies[0]:
        return float(values[0])
    if frequency >= frequencies[-1]:
        return float(values[-1])
    lo, hi = 0, len(frequencies) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if frequencies[mid] <= frequency:
            lo = mid
        else:
            hi = mid
    fraction = math.log(frequency / frequencies[lo]) / math.log(frequencies[hi] / frequencies[lo])
    return float(values[lo]) + fraction * (float(values[hi]) - float(values[lo]))


def response_value(response: dict[str, Any], frequency: float, reference_db: float) -> complex:
    magnitude_db = interpolate_log(response["frequencies"], response["db"], frequency) - reference_db
    # Measurement stores excess phase after removing each path's bulk arrival
    # delay. A multichannel inverse must restore that delay or it loses the
    # relative phase between independently placed speakers/subwoofers.
    phase = interpolate_log(response["frequencies"], response["phase_rad"], frequency)
    phase -= 2.0 * math.pi * frequency * float(response.get("bulk_delay_samples", 0.0)) / RATE
    return 10.0 ** (magnitude_db / 20.0) * cmath.exp(1j * phase)


def response_confidence(response: dict[str, Any], frequency: float) -> float:
    quality = response.get("frequency_quality", {})
    frequencies = quality.get("frequencies")
    confidence = quality.get("confidence")
    if not isinstance(frequencies, list) or not isinstance(confidence, list) or len(frequencies) != len(confidence) or not frequencies:
        return 1.0
    return max(0.0, min(1.0, interpolate_log(frequencies, confidence, frequency)))


def raised_cosine(frequency: float, low: float, high: float, rising: bool) -> float:
    if high <= low:
        return 1.0 if (frequency >= high if rising else frequency <= low) else 0.0
    position = max(0.0, min(1.0, (frequency - low) / (high - low)))
    value = 0.5 - 0.5 * math.cos(math.pi * position)
    return value if rising else 1.0 - value


def solve_complex(matrix: list[list[complex]], vector: list[complex]) -> tuple[list[complex], float]:
    """Pivoted Gaussian solve with a conservative pivot condition surrogate."""
    size = len(vector)
    augmented = [list(row) + [vector[index]] for index, row in enumerate(matrix)]
    pivots: list[float] = []
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        magnitude = abs(augmented[pivot][column])
        if magnitude < 1.0e-12:
            raise MimoError("MIMO 정규화 행렬이 특이합니다.")
        pivots.append(magnitude)
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) <= 1.0e-20:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    condition = max(pivots) / max(min(pivots), 1.0e-12)
    return [augmented[index][size] for index in range(size)], condition


def median_db(values: list[complex]) -> float:
    return 20.0 * math.log10(max(statistics.median(abs(value) for value in values), 1.0e-12))


def db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1.0e-12))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    blend = position - lower
    return ordered[lower] * (1.0 - blend) + ordered[upper] * blend


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_session(session_path: Path) -> tuple[dict[str, Any], list[dict[str, dict[str, Any]]], list[str]]:
    state = json.loads(session_path.read_text(encoding="utf-8"))
    mode = state.get("mode")
    if mode not in MIMO_MODES:
        raise MimoError("MIMO 측정 session이 아닙니다.")
    sources = list(MIMO_MODES[mode])
    if int(state.get("positions_completed", 0)) != 3:
        raise MimoError("세 위치 MIMO 측정을 먼저 완료하세요.")
    directory = Path(state.get("session_dir", session_path.parent)).resolve()
    positions: list[dict[str, dict[str, Any]]] = []
    for position in range(1, 4):
        row: dict[str, dict[str, Any]] = {}
        for source in sources:
            response_path = directory / f"p{position}_{source}_response.json"
            if not response_path.is_file():
                raise MimoError(f"MIMO 응답이 없습니다: {response_path.name}")
            response = json.loads(response_path.read_text(encoding="utf-8"))
            quality = response.get("measurement_quality", {})
            if float(quality.get("snr_db", -300.0)) < 6.0:
                raise MimoError(f"{response_path.name} SNR이 6 dB 미만입니다.")
            if not bool(response.get("bulk_delay_reliable", response.get("bulk_delay", {}).get("reliable", True))):
                raise MimoError(f"{response_path.name}의 직접음 도착시간이 신뢰할 수 없어 복소 MIMO 계산을 중단합니다.")
            row[source] = response
        positions.append(row)
    return state, positions, sources


def independent_response_audit(directory: Path, sources: list[str]) -> dict[str, Any]:
    """Reject byte-identical response reuse across a nominal three-position MIMO session."""
    seen: dict[str, str] = {}
    reused = []
    files = []
    for position in range(1, 4):
        for source in sources:
            path = directory / f"p{position}_{source}_response.json"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            label = f"p{position}_{source}"
            if digest in seen:
                reused.append({"measurement": label, "same_as": seen[digest], "sha256": digest})
            else:
                seen[digest] = label
            files.append({"measurement": label, "sha256": digest})
    return {
        "pass": not reused and len(files) == 3 * len(sources),
        "positions": 3,
        "response_files": len(files),
        "reused_measurements": reused,
        "spatial_stability_applicable": True,
    }


def pairwise_diversity(positions: list[dict[str, dict[str, Any]]], sources: list[str], reference_db: float) -> dict[str, Any]:
    pairs = []
    maximum = 0.0
    for left_index in range(len(sources)):
        for right_index in range(left_index + 1, len(sources)):
            left_values: list[complex] = []
            right_values: list[complex] = []
            for frequency in (31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 150.0):
                for position in positions:
                    left_values.append(response_value(position[sources[left_index]], frequency, reference_db))
                    right_values.append(response_value(position[sources[right_index]], frequency, reference_db))
            numerator = abs(sum(a.conjugate() * b for a, b in zip(left_values, right_values)))
            denominator = math.sqrt(sum(abs(value) ** 2 for value in left_values) * sum(abs(value) ** 2 for value in right_values))
            coherence = numerator / max(denominator, 1.0e-12)
            maximum = max(maximum, coherence)
            pairs.append({"a": sources[left_index], "b": sources[right_index], "coherence": round(coherence, 4)})
    return {
        "pairs": pairs,
        "maximum_coherence": round(maximum, 4),
        "independence_warning": maximum >= 0.985,
        "interpretation": "1.0에 가까우면 두 출력이 같은 위치/같은 물리 우퍼처럼 동작해 MIMO 자유도가 줄어듭니다.",
    }


def temporal_and_room_audit(positions: list[dict[str, dict[str, Any]]], sources: list[str], high_hz: int) -> list[dict[str, Any]]:
    snr = [float(position[source].get("measurement_quality", {}).get("snr_db", -300.0)) for position in positions for source in sources]
    decay_values = [
        float(band["t20_rt60_s"])
        for position in positions for source in sources
        for band in position[source].get("room_decay", {}).get("bands", [])
        if band.get("reliable") and isinstance(band.get("t20_rt60_s"), (int, float))
    ]
    temporal = [position[source].get("temporal", {}) for position in positions for source in sources]
    c80 = [float(item["c80_db"]) for item in temporal if isinstance(item.get("c80_db"), (int, float))]
    group_delay = [position[source].get("group_delay", {}) for position in positions for source in sources]
    gd = [float(item["bass_p90_ms"]) for item in group_delay if isinstance(item.get("bass_p90_ms"), (int, float))]
    return [
        {"id": "noise_headroom", "label": "배경소음·SNR·클리핑", "classification": "measurement_gate", "status": "pass" if min(snr) >= 6.0 else "fail", "evidence": {"minimum_snr_db": round(min(snr), 2), "recommended_db": 15.0}, "action": "SNR 15 dB 이상 권장; 6 dB 미만이면 FIR 생성을 중단"},
        {"id": "magnitude_target", "label": "주파수 응답·타깃", "classification": "fir_correctable", "status": "evaluated", "action": "공간 정규화와 boost/cut 제한을 적용한 FIR 보정"},
        {"id": "spatial_variance", "label": "좌석 간 저역 편차", "classification": "mimo_correctable", "status": "evaluated", "action": f"20–{high_hz} Hz 다중 제어원 pressure matching; 미측정 영역은 보장하지 않음"},
        {"id": "arrival_phase", "label": "도착시간·극성·저역 위상", "classification": "limited_fir", "status": "evaluated", "evidence": {"bass_group_delay_p90_ms_median": round(statistics.median(gd), 2) if gd else None}, "action": "공통 인과 지연과 저역 위상만 보정; 위치별 고역 phase는 역보정하지 않음"},
        {"id": "modal_decay", "label": "저역 모드 감쇠", "classification": "limited_mimo", "status": "evaluated" if decay_values else "insufficient_data", "evidence": {"median_t20_rt60_s": round(statistics.median(decay_values), 3) if decay_values else None}, "action": "MIMO와 cut-only 감쇄로 초기 저역 꼬리를 줄임; 물리 RT60 전체 제거는 불가"},
        {"id": "early_reflections", "label": "초기반사·명료도", "classification": "diagnostic_placement", "status": "evaluated" if c80 else "insufficient_data", "evidence": {"median_c80_db": round(statistics.median(c80), 2) if c80 else None}, "action": "SBIR/반사 위치를 진단하고 배치·흡음 권고; 날카로운 null boost 금지"},
        {"id": "late_reverberation", "label": "중·고역 늦은 잔향", "classification": "physical_treatment", "status": "diagnostic_only", "action": "FIR 역보정 대상 아님; 흡음·확산·배치로 개선"},
        {"id": "nonlinear_distortion", "label": "고조파·압축·기계 잡음", "classification": "not_measured", "status": "not_available", "action": "다중 레벨 Farina harmonic 분리 측정이 필요; 선형 FIR로 보정 불가"},
        {"id": "directivity", "label": "스피커 지향성·오프축 응답", "classification": "not_measured", "status": "not_available", "action": "회전/근접 다각도 측정이 필요; 단일 청취영역 측정으로 분리 불가"},
        {"id": "stereo_spatial", "label": "IACC·양이간 공간감", "classification": "not_measured", "status": "not_available", "action": "단일 UMIK-1로 측정 불가; 더미헤드/2마이크 필요"},
        {"id": "absolute_spl", "label": "절대 SPL·청력/민원 안전", "classification": "not_certified", "status": "not_available", "action": "UMIK sensitivity와 체인 검교정·공인 소음 측정 필요; 필터만으로 층간소음 무발생 보장 불가"},
    ]


def target_level(engine, target_name: str, preset: str, frequency: float, bass_tilt: int, treble_tilt: int) -> float:
    target_f, target_db = engine.target_curve(target_name)
    value = engine.interpolate_log(target_f, target_db, frequency)
    value += engine.preference_modifier_db(frequency, bass_tilt, treble_tilt)
    value += engine.bass_modifier_db(frequency, preset)
    return value


def source_usable_lows(engine, positions: list[dict[str, dict[str, Any]]], sources: list[str]) -> dict[str, float]:
    result = {}
    for source in sources:
        frequencies = positions[0][source]["frequencies"]
        average = [statistics.median(float(position[source]["db"][index]) for position in positions) for index in range(len(frequencies))]
        reference_values = [level for frequency, level in zip(frequencies, average) if 70.0 <= frequency <= 130.0]
        reference = statistics.median(reference_values or average)
        low, _high = engine.natural_usable_band(frequencies, average, reference)
        result[source] = max(20.0, min(150.0, float(low)))
    return result


def causalize(paths: list[list[float]], target_peak: int = 1024) -> tuple[list[list[float]], dict[str, Any]]:
    aggregate = [sum(path[index] * path[index] for path in paths) for index in range(FFT_LENGTH)]
    peak = max(range(FFT_LENGTH), key=lambda index: aggregate[index])
    shift = (target_peak - peak) % FFT_LENGTH
    output = []
    fade = 2048
    for path in paths:
        rotated = path[-shift:] + path[:-shift] if shift else list(path)
        truncated = rotated[:TAPS]
        for index in range(TAPS - fade, TAPS):
            fraction = (index - (TAPS - fade)) / max(1, fade - 1)
            truncated[index] *= 0.5 + 0.5 * math.cos(math.pi * fraction)
        output.append(truncated)
    total_energy = sum(sum(value * value for value in path) for path in output)
    pre_energy = sum(sum(value * value for value in path[:target_peak]) for path in output)
    return output, {
        "common_peak_before_rotation": peak,
        "common_shift_samples": shift if shift <= FFT_LENGTH // 2 else shift - FFT_LENGTH,
        "target_peak_sample": target_peak,
        "target_delay_ms": round(target_peak * 1000.0 / RATE, 3),
        "pre_peak_energy_percent": round(100.0 * pre_energy / max(total_energy, 1.0e-30), 3),
    }


def modal_tail_ratio(fft, spectrum: list[complex]) -> float:
    impulse = fft.irfft(spectrum, FFT_LENGTH)
    peak = max(range(len(impulse)), key=lambda index: abs(impulse[index]))
    aligned = impulse[peak:] + impulse[:peak]
    early_end = round(0.080 * RATE)
    late_end = round(0.500 * RATE)
    early = sum(value * value for value in aligned[:early_end])
    late = sum(value * value for value in aligned[early_end:late_end])
    return 10.0 * math.log10(max(late, 1.0e-30) / max(early, 1.0e-30))


def write_report(path: Path, result: dict[str, Any]) -> None:
    metrics = result["mimo"]
    audit = result["room_tuning_audit"]
    validation = result["self_validation"]
    validation_status = (
        "PASS" if validation.get("overall_pass") else
        "FAIL"
    )
    lines = [
        "# AudioDSP MIMO 룸 튜닝 보고서",
        "",
        f"- 토폴로지: `{metrics['topology']}`",
        f"- 제어원: {', '.join(metrics['actuators'])}",
        f"- MIMO 범위: {metrics['frequency_range_hz'][0]}–{metrics['frequency_range_hz'][1]} Hz",
        f"- FIR: {result['sample_rate']} Hz / {result['taps']} taps / 2×4 matrix",
        f"- 전체 검증: {validation_status}",
        "",
        "## 예측 개선",
        "",
    ]
    for channel in ("left", "right"):
        item = metrics["prediction"][channel]
        lines += [
            f"### {channel.title()}",
            "",
            f"- 타깃 MAE: {item['before_target_mae_db']} → {item['after_target_mae_db']} dB",
            f"- 좌석 편차: {item['before_spatial_std_db']} → {item['after_spatial_std_db']} dB",
            f"- 저역 late/early energy: {item['before_modal_tail_db']} → {item['after_modal_tail_db']} dB (모델 예측)",
            "",
        ]
    lines += ["## 보정 가능성 분류", ""]
    for item in audit:
        lines.append(f"- **{item['label']}** — `{item['classification']}` / `{item['status']}`: {item['action']}")
    lines += [
        "",
        "## 한계",
        "",
        "이 결과는 측정한 세 위치와 선형·시간불변 모델에 대한 예측이다. 미측정 위치, 비선형 왜곡, 중·고역 late reverb, 절대 SPL과 층간소음은 보장하지 않는다. 실제 적용 전 Preview와 재측정 검증이 필요하다.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build(session_path: Path, output_dir: Path, options: dict[str, Any], engine_path: Path | None = None, *, allow_unsupported: bool = False) -> dict[str, Any]:
    if not allow_unsupported:
        require_supported()
    engine = load_measurement_engine(engine_path)
    state, positions, sources = load_session(session_path)
    topology = state["mode"]
    independent_positions = independent_response_audit(Path(state["session_dir"]), sources)
    high_hz = int(options.get("mimo_high_hz", 150))
    if high_hz not in (80, 120, 150):
        raise MimoError("MIMO 상한은 80/120/150 Hz 중 하나여야 합니다.")
    strength = str(options.get("mimo_strength", "balanced"))
    if strength not in ("safe", "balanced", "maximum"):
        raise MimoError("MIMO 강도는 safe/balanced/maximum 중 하나여야 합니다.")
    support_penalty_db = int(options.get("mimo_support_penalty_db", 6))
    if support_penalty_db not in (3, 6, 9, 12):
        raise MimoError("지원 스피커 제약은 3/6/9/12 dB 중 하나여야 합니다.")
    target_name = str(options.get("target", "harman"))
    preset = str(options.get("preset", "strong"))
    spatial_mode = str(options.get("spatial_mode", "equal"))
    position_weights = [1.0 / 3.0] * 3 if spatial_mode == "equal" else [0.60, 0.20, 0.20]
    bass_tilt = int(options.get("bass_tilt_db", 0))
    treble_tilt = int(options.get("treble_tilt_db", 0))
    woofer_trim = int(options.get("woofer_trim_db", -9))
    phase_mode = str(options.get("phase_mode", "bass"))
    phase_cutoff = int(options.get("phase_cutoff", 200))
    max_boost = int(options.get("max_boost_db", 10))
    max_cut = int(options.get("max_cut_db", 18))
    correction_low_hz = int(options.get("correction_low_hz", 20))
    crossover_requested = bool(options.get("crossover_enabled", True))
    crossover_enabled = crossover_requested and topology in ("mimo_one_sub", "mimo_dual_sub")
    crossover_frequency_hz = int(options.get("crossover_frequency_hz", 100))
    if crossover_frequency_hz not in getattr(engine, "CROSSOVER_FREQUENCIES", (60, 70, 80, 90, 100, 120)):
        raise MimoError("디지털 crossover 주파수가 잘못되었습니다.")

    reference_levels = []
    for position in positions:
        for source in ("front_left", "front_right"):
            for frequency, level in zip(position[source]["frequencies"], position[source]["db"]):
                if 70.0 <= float(frequency) <= 130.0:
                    reference_levels.append(float(level))
    reference_db = statistics.median(reference_levels)
    usable_lows = source_usable_lows(engine, positions, sources)
    diversity = pairwise_diversity(positions, sources, reference_db)

    base_phase_mode = "magnitude" if phase_mode == "bass" else phase_mode
    common_arguments = dict(
        target_name=target_name, preset=preset, woofer=False, woofer_trim_db=0,
        phase_mode=base_phase_mode, phase_cutoff=phase_cutoff, spatial_mode=spatial_mode,
        bass_tilt_db=bass_tilt, treble_tilt_db=treble_tilt,
        correction_low_hz=correction_low_hz,
        correction_high_hz=int(options.get("correction_high_hz", 20_000)),
        max_boost_db=max_boost, max_cut_db=max_cut,
    )
    fft = engine.FFTBackend()
    base_impulses = []
    base_graphs = []
    base_averages = [
        engine.load_average_response(Path(state["session_dir"]), source, spatial_mode)
        for source in ("front_left", "front_right")
    ]
    shared_front_reference_db = statistics.median([
        float(value)
        for average in base_averages
        for frequency, value in zip(average["frequencies"], average["average_db"])
        if 500.0 <= float(frequency) <= 2_000.0
    ])
    target_f, target_db = engine.target_curve(target_name)
    shared_target_reference_db = statistics.median([
        engine.interpolate_log(target_f, target_db, float(frequency))
        + engine.preference_modifier_db(float(frequency), bass_tilt, treble_tilt)
        for frequency in base_averages[0]["frequencies"]
        if 500.0 <= float(frequency) <= 2_000.0
    ])
    preferred_target_db = [
        value + engine.preference_modifier_db(frequency, bass_tilt, treble_tilt)
        for frequency, value in zip(target_f, target_db)
    ]
    (
        left_confidence,
        right_confidence,
        left_rolloff_floor,
        right_rolloff_floor,
        stereo_rolloff_summary,
    ) = engine.stereo_broad_rolloff_confidence(
        base_averages[0]["frequencies"], base_averages[0]["average_db"], base_averages[0]["frequency_confidence"],
        base_averages[1]["frequencies"], base_averages[1]["average_db"], base_averages[1]["frequency_confidence"],
        target_f, preferred_target_db, shared_front_reference_db, shared_target_reference_db,
    )
    confidence_banks = (left_confidence, right_confidence)
    rolloff_banks = (left_rolloff_floor, right_rolloff_floor)
    for channel, average in enumerate(base_averages):
        impulse, graph = engine.design_channel(
            average["frequencies"], average["average_db"], average["spatial_std_db"],
            average["center_phase_rad"], fft=fft,
            decay_frequency_hz=average.get("decay_frequency_hz"),
            decay_t20_rt60_s=average.get("decay_t20_rt60_s"),
            frequency_confidence=confidence_banks[channel],
            corroborated_rolloff_confidence=rolloff_banks[channel],
            shared_reference_measure_db=shared_front_reference_db,
            shared_reference_target_db=shared_target_reference_db,
            **common_arguments,
        )
        base_impulses.append(impulse)
        base_graphs.append(graph)
    if phase_mode == "bass":
        left, right, phase_details = engine.apply_common_lr_low_frequency_phase(
            base_impulses[0], base_impulses[1], base_averages[0]["frequencies"],
            base_averages[0]["average_db"], base_averages[1]["average_db"],
            base_averages[0]["center_phase_rad"], base_averages[1]["center_phase_rad"],
            phase_cutoff, fft,
        )
        base_impulses = [left, right]
        base_graphs[0]["phase"] = {**phase_details, "channel": "left"}
        base_graphs[1]["phase"] = {**phase_details, "channel": "right"}
        base_graphs[0] = engine.finalize_graph_with_fir(base_graphs[0], left, fft)
        base_graphs[1] = engine.finalize_graph_with_fir(base_graphs[1], right, fft)
    # Match the deployable SISO baseline before any MIMO optimization.  The
    # channel designer intentionally leaves final no-preamp normalization to
    # the complete bank; comparing an unnormalized baseline with a bounded
    # MIMO bank creates a false target-shape regression even at zero crossfeed.
    base_impulses, base_normalization = engine.normalize_fir_bank(base_impulses, fft)
    base_graphs[0] = engine.finalize_graph_with_fir(base_graphs[0], base_impulses[0], fft)
    base_graphs[1] = engine.finalize_graph_with_fir(base_graphs[1], base_impulses[1], fft)
    base_common_reference = engine.apply_common_graph_reference(base_graphs[0], base_graphs[1], None)
    base_spectra = [fft.rfft(impulse, FFT_LENGTH) for impulse in base_impulses]
    crossover_spectra: dict[str, list[complex]] = {}
    for role in ("highpass", "lowpass"):
        gains = [
            engine.crossover_transfer_db(max(3.0, index * RATE / FFT_LENGTH), crossover_frequency_hz, role)
            for index in range(FFT_LENGTH // 2 + 1)
        ]
        crossover_spectra[role] = fft.rfft(engine.minimum_phase_fir(gains, fft, FFT_LENGTH), FFT_LENGTH)

    def baseline_physical_path(output: int, channel: int, bin_index: int) -> complex:
        """Deployable SISO prior for one physical output/input path."""
        if output == channel:
            front_xover = crossover_spectra["highpass"][bin_index] if crossover_enabled else 1.0 + 0.0j
            return base_spectra[channel][bin_index] * front_xover
        if not crossover_enabled:
            return 0j
        lowpass = crossover_spectra["lowpass"][bin_index]
        if topology == "mimo_one_sub" and output in (2, 3):
            # One logical sub actuator is copied 0.5/0.5 to the physical Rear
            # pair, exactly as the exported MIMO bank does.
            return 0.5 * base_spectra[channel][bin_index] * lowpass
        if topology == "mimo_dual_sub" and output == 2 + channel:
            return base_spectra[channel][bin_index] * lowpass
        return 0j

    # The room transfer functions are normalized to their measured 70-130 Hz
    # level. Anchor the selected target to the existing SISO output in the same
    # band. MIMO then improves target shape and seat consistency without an
    # arbitrary broadband bass level jump.
    reference_frequencies = [
        float(value) for value in positions[0]["front_left"]["frequencies"]
        if 70.0 <= float(value) <= 130.0
    ]
    target_reference_db = statistics.median(
        target_level(engine, target_name, preset, frequency, bass_tilt, treble_tilt)
        for frequency in reference_frequencies
    )
    baseline_reference_db = []
    for channel, source in enumerate(("front_left", "front_right")):
        levels = []
        for frequency in reference_frequencies:
            bin_index = min(FFT_LENGTH // 2, round(frequency * FFT_LENGTH / RATE))
            for position in positions:
                h_physical = []
                for output in range(4):
                    if topology == "mimo_one_sub" and output >= 2:
                        h_physical.append(response_value(position["sub_pair"], frequency, reference_db))
                    elif topology == "mimo_stereo" and output >= 2:
                        h_physical.append(0j)
                    else:
                        physical_source = ("front_left", "front_right", "sub_left", "sub_right")[output]
                        h_physical.append(response_value(position[physical_source], frequency, reference_db))
                pressure = sum(
                    h_physical[output] * baseline_physical_path(output, channel, bin_index)
                    for output in range(4)
                )
                levels.append(db(abs(pressure)))
        baseline = statistics.median(levels)
        baseline_reference_db.append(baseline)
    common_baseline_reference_db = statistics.median(baseline_reference_db)
    common_target_offset_db = common_baseline_reference_db - target_reference_db
    target_offsets_db = [common_target_offset_db, common_target_offset_db]

    actuator_count = len(sources)
    path_spectra = [[0j] * (FFT_LENGTH // 2 + 1) for _ in range(actuator_count * 2)]
    condition_values = []
    control_weights = []
    support_linear = 10.0 ** (support_penalty_db / 20.0)
    rear_limit = 10.0 ** (max(0, -woofer_trim) / 20.0)
    for source in sources:
        if source.startswith("sub"):
            control_weights.append(support_linear * rear_limit)
        else:
            control_weights.append(support_linear)
    strength_regularization = {"safe": 0.080, "balanced": 0.030, "maximum": 0.012}[strength]
    prior = {"safe": 0.080, "balanced": 0.035, "maximum": 0.015}[strength]
    spectral_continuity = {"safe": 0.120, "balanced": 0.060, "maximum": 0.025}[strength]
    solution_blend = {"safe": 0.25, "balanced": 0.40, "maximum": 0.65}[strength]
    if topology == "mimo_stereo":
        # With only the two programme speakers there is no dedicated support
        # actuator.  Large cross-feed can trade stereo target shape for only a
        # tiny seat-variance gain, so keep this topology intentionally close
        # to the independently validated SISO solution.  Subwoofer topologies
        # retain the selected strength because they have extra control DOF.
        solution_blend *= 0.15
    elif topology == "mimo_one_sub":
        # One physical low-frequency actuator cannot independently flatten
        # three seats.  With a 10 dB relative-compensation ceiling, the raw
        # inverse can otherwise trade a small target improvement for a longer
        # low-frequency impulse tail. Keep more of the validated common-level
        # SISO prior; the model gate below still requires target/seat
        # non-regression and a <=1.5 dB tail change.
        solution_blend *= 0.65
    elif topology == "mimo_dual_sub":
        # Four actuators provide more freedom but also more opportunities for
        # a narrow solution to lengthen the synthetic low-frequency tail.
        # Keep a modest safety margin below the selected strength; the model
        # validator still rejects any measured non-regression failure.
        solution_blend *= 0.85
    previous_solutions: list[list[complex] | None] = [None, None]

    for bin_index in range(FFT_LENGTH // 2 + 1):
        frequency = bin_index * RATE / FFT_LENGTH
        actuator_xover = []
        for source in sources:
            if not crossover_enabled:
                actuator_xover.append(1.0 + 0.0j)
            elif source.startswith("sub"):
                actuator_xover.append(crossover_spectra["lowpass"][bin_index])
            else:
                actuator_xover.append(crossover_spectra["highpass"][bin_index])
        base_vectors = []
        for channel in range(2):
            vector = [0j] * actuator_count
            vector[channel] = base_spectra[channel][bin_index]
            if crossover_enabled and topology == "mimo_one_sub":
                vector[2] = base_spectra[channel][bin_index]
            elif crossover_enabled and topology == "mimo_dual_sub":
                vector[2 + channel] = base_spectra[channel][bin_index]
            base_vectors.append(vector)
        if frequency < correction_low_hz or frequency > high_hz + 30.0:
            for channel in range(2):
                for actuator in range(actuator_count):
                    path_spectra[actuator * 2 + channel][bin_index] = base_vectors[channel][actuator] * actuator_xover[actuator]
                previous_solutions[channel] = list(base_vectors[channel])
            continue
        low_transition_end = min(float(high_hz), correction_low_hz * math.sqrt(2.0))
        blend = raised_cosine(frequency, float(correction_low_hz), low_transition_end, True) * raised_cosine(frequency, float(high_hz), float(high_hz + 30), False)
        h = [[response_value(position[source], max(20.0, frequency), reference_db) * actuator_xover[index] for index, source in enumerate(sources)] for position in positions]
        for channel in range(2):
            gram = [[0j for _ in range(actuator_count)] for _ in range(actuator_count)]
            rhs = [0j] * actuator_count
            # Keep the weighted SISO arrival phase instead of asking the inverse
            # for a non-causal zero-phase room response. MIMO changes level and
            # seat variance while the common causal delay remains short.
            baseline_pressure = sum(
                position_weights[index] * row[channel] * base_spectra[channel][bin_index]
                for index, row in enumerate(h)
            )
            desired_phase = cmath.phase(baseline_pressure) if abs(baseline_pressure) > 1.0e-12 else 0.0
            normalized_target_amp = 10.0 ** ((target_level(
                engine, target_name, preset, max(20.0, frequency), bass_tilt, treble_tilt,
            ) + target_offsets_db[channel]) / 20.0)
            desired = normalized_target_amp * cmath.exp(1j * desired_phase)
            confidence_weights = [
                position_weights[position_index] * min(response_confidence(positions[position_index][source], max(20.0, frequency)) for source in sources)
                for position_index in range(len(positions))
            ]
            confidence_total = sum(confidence_weights)
            if confidence_total <= 1.0e-9:
                confidence_weights = list(position_weights)
                confidence_total = sum(confidence_weights)
            confidence_weights = [value / confidence_total for value in confidence_weights]
            for position_index, row in enumerate(h):
                weight = confidence_weights[position_index]
                for a in range(actuator_count):
                    rhs[a] += weight * row[a].conjugate() * desired
                    for b in range(actuator_count):
                        gram[a][b] += weight * row[a].conjugate() * row[b]
            energy_scale = max(sum(abs(value) ** 2 for value in row) for row in h)
            continuity = spectral_continuity * max(energy_scale, 1.0e-6)
            previous = previous_solutions[channel] or base_vectors[channel]
            for actuator in range(actuator_count):
                usable_penalty = 1.0
                natural_low = usable_lows[sources[actuator]]
                if frequency < natural_low:
                    usable_penalty += 30.0 * (1.0 - max(0.0, frequency / max(natural_low, 1.0))) ** 2
                primary_weight = 1.0 if actuator == channel else control_weights[actuator]
                regularization = strength_regularization * max(energy_scale, 1.0e-6) * primary_weight ** 2 * usable_penalty
                gram[actuator][actuator] += regularization + prior + continuity
                rhs[actuator] += prior * base_vectors[channel][actuator] + continuity * previous[actuator]
            solution, condition = solve_complex(gram, rhs)
            solution = [
                base + solution_blend * (candidate - base)
                for base, candidate in zip(base_vectors[channel], solution)
            ]
            previous_solutions[channel] = list(solution)
            condition_values.append(condition)
            for actuator in range(actuator_count):
                control = base_vectors[channel][actuator] * (1.0 - blend) + solution[actuator] * blend
                path_spectra[actuator * 2 + channel][bin_index] = control * actuator_xover[actuator]

    # A single pathological bin must not attenuate the entire bank. Project
    # every physical output row onto the correlated-input L1 headroom bound
    # before causal FIR conversion. For one physical sub fed by Rear L/R, the
    # logical sub row is split 0.5/0.5 and may therefore use twice the bound.
    for bin_index in range(FFT_LENGTH // 2 + 1):
        for actuator in range(actuator_count):
            row_sum = abs(path_spectra[actuator * 2][bin_index]) + abs(path_spectra[actuator * 2 + 1][bin_index])
            is_sub = sources[actuator].startswith("sub")
            physical_sub_limit = 0.999 * 10.0 ** (min(0, woofer_trim) / 20.0)
            limit = (2.0 * physical_sub_limit if topology == "mimo_one_sub" else physical_sub_limit) if is_sub else 0.999
            if row_sum > limit:
                projection = limit / row_sum
                path_spectra[actuator * 2][bin_index] *= projection
                path_spectra[actuator * 2 + 1][bin_index] *= projection

    logical_paths = [fft.irfft(spectrum, FFT_LENGTH) for spectrum in path_spectra]
    # Spectra are no longer needed.  Releasing them before causalization keeps
    # the future 5.1 dense-matrix generator well inside a 2 GB system budget.
    del path_spectra
    logical_paths, causality = causalize(logical_paths)
    if topology == "mimo_stereo":
        output_paths = [logical_paths[0], logical_paths[1], logical_paths[2], logical_paths[3], [0.0] * TAPS, [0.0] * TAPS, [0.0] * TAPS, [0.0] * TAPS]
    elif topology == "mimo_one_sub":
        output_paths = [logical_paths[0], logical_paths[1], logical_paths[2], logical_paths[3],
                        [0.5 * value for value in logical_paths[4]], [0.5 * value for value in logical_paths[5]],
                        [0.5 * value for value in logical_paths[4]], [0.5 * value for value in logical_paths[5]]]
    else:
        output_paths = logical_paths

    actual_spectra = [fft.rfft(path, FFT_LENGTH) for path in output_paths]
    per_output_row_sum_before = [
        max(abs(actual_spectra[output * 2][bin_index]) + abs(actual_spectra[output * 2 + 1][bin_index]) for bin_index in range(1, FFT_LENGTH // 2 + 1))
        for output in range(4)
    ]
    physical_sub_limit = 0.999 * 10.0 ** (min(0, woofer_trim) / 20.0)
    per_output_limits = [0.999, 0.999] + ([physical_sub_limit, physical_sub_limit] if topology in ("mimo_one_sub", "mimo_dual_sub") else [0.999, 0.999])
    scale = min(1.0, *(limit / max(row_sum, 1.0e-12) for limit, row_sum in zip(per_output_limits, per_output_row_sum_before)))
    if scale < 1.0:
        # Mutate in place so a dense matrix never holds old and scaled impulse
        # banks at the same time.
        del actual_spectra
        for path in output_paths:
            for index, value in enumerate(path):
                path[index] = value * scale
        actual_spectra = [fft.rfft(path, FFT_LENGTH) for path in output_paths]
    per_output_row_sum_after = [
        max(abs(actual_spectra[output * 2][bin_index]) + abs(actual_spectra[output * 2 + 1][bin_index]) for bin_index in range(1, FFT_LENGTH // 2 + 1))
        for output in range(4)
    ]
    maximum_row_sum = max(per_output_row_sum_before)
    maximum_row_sum_after = max(per_output_row_sum_after)

    output_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for output, label in enumerate(OUTPUT_LABELS):
        path = output_dir / f"MIMO_{label}_LR_32768.wav"
        engine.write_float_stereo(path, output_paths[output * 2], output_paths[output * 2 + 1])
        files.append({"output": output, "label": label, "file": path.name, "sha256": sha256(path), "channels": 2, "frames": TAPS, "format": "float32"})

    graph_frequencies = [20.0 * (25.0 ** (index / 159.0)) for index in range(160)]
    prediction: dict[str, Any] = {}
    graphs: dict[str, Any] = {}
    channel_response_samples: dict[str, list[dict[str, Any]]] = {}
    tail_before_values = {"left": [], "right": []}
    tail_after_values = {"left": [], "right": []}
    for channel, channel_name in enumerate(("left", "right")):
        before_std, after_std = [], []
        before_curve_raw, after_curve_raw, target_curve_values = [], [], []
        response_samples: list[dict[str, Any]] = []
        before_tail_spectra = [[0j] * (FFT_LENGTH // 2 + 1) for _ in positions]
        after_tail_spectra = [[0j] * (FFT_LENGTH // 2 + 1) for _ in positions]
        for frequency in graph_frequencies:
            target_db_value = target_level(engine, target_name, preset, frequency, bass_tilt, treble_tilt) + target_offsets_db[channel]
            target_curve_values.append(target_db_value)
            bin_index = min(FFT_LENGTH // 2, round(frequency * FFT_LENGTH / RATE))
            before_values, after_values = [], []
            for position in positions:
                h_physical = []
                for output in range(4):
                    if topology == "mimo_one_sub" and output >= 2:
                        h_physical.append(response_value(position["sub_pair"], frequency, reference_db))
                    elif topology == "mimo_stereo" and output >= 2:
                        h_physical.append(0j)
                    else:
                        source = ("front_left", "front_right", "sub_left", "sub_right")[output]
                        h_physical.append(response_value(position[source], frequency, reference_db))
                before = sum(
                    h_physical[output] * baseline_physical_path(output, channel, bin_index)
                    for output in range(4)
                )
                after = sum(h_physical[output] * actual_spectra[output * 2 + channel][bin_index] for output in range(4))
                before_values.append(before)
                after_values.append(after)
            before_levels = [db(abs(value)) for value in before_values]
            after_levels = [db(abs(value)) for value in after_values]
            before_curve_raw.append(statistics.median(before_levels))
            after_curve_raw.append(statistics.median(after_levels))
            response_samples.append({
                "frequency": frequency,
                "target_db": target_db_value,
                "before_levels": before_levels,
                "after_levels": after_levels,
            })
            if 20.0 <= frequency <= high_hz:
                before_std.append(statistics.pstdev(before_levels))
                after_std.append(statistics.pstdev(after_levels))
        # A worst-case correlated-input bound may attenuate the whole matrix.
        # That is a volume/headroom change, not a target-shape error.  Compare
        # both the existing SISO path and the candidate MIMO path after one
        # common reference-band level alignment, while reporting the raw bank
        # attenuation separately in `headroom.global_scale_db`.  Per-frequency
        # or per-seat normalization is deliberately forbidden here.
        alignment_low_hz = max(float(correction_low_hz), 40.0)
        alignment_high_hz = min(float(high_hz), 130.0)
        if alignment_high_hz <= alignment_low_hz:
            alignment_low_hz = float(correction_low_hz)
            alignment_high_hz = float(high_hz)
        alignment_samples = [
            sample for sample in response_samples
            if alignment_low_hz <= sample["frequency"] <= alignment_high_hz
        ]
        if not alignment_samples:
            alignment_samples = [
                sample for sample in response_samples
                if correction_low_hz <= sample["frequency"] <= high_hz
            ]
        before_level_alignment_db = statistics.median([
            sample["target_db"] - level
            for sample in alignment_samples for level in sample["before_levels"]
        ])
        after_level_alignment_db = statistics.median([
            sample["target_db"] - level
            for sample in alignment_samples for level in sample["after_levels"]
        ])
        before_errors, after_errors = [], []
        for sample in response_samples:
            if 20.0 <= sample["frequency"] <= high_hz:
                before_errors.extend(
                    abs(level + before_level_alignment_db - sample["target_db"])
                    for level in sample["before_levels"]
                )
                after_errors.extend(
                    abs(level + after_level_alignment_db - sample["target_db"])
                    for level in sample["after_levels"]
                )
        before_curve = [value + before_level_alignment_db for value in before_curve_raw]
        after_curve = [value + after_level_alignment_db for value in after_curve_raw]
        for bin_index in range(1, min(FFT_LENGTH // 2, round((high_hz + 30) * FFT_LENGTH / RATE)) + 1):
            frequency = bin_index * RATE / FFT_LENGTH
            for position_index, position in enumerate(positions):
                h_physical = []
                for output in range(4):
                    if topology == "mimo_one_sub" and output >= 2:
                        h_physical.append(response_value(position["sub_pair"], frequency, reference_db))
                    elif topology == "mimo_stereo" and output >= 2:
                        h_physical.append(0j)
                    else:
                        source = ("front_left", "front_right", "sub_left", "sub_right")[output]
                        h_physical.append(response_value(position[source], frequency, reference_db))
                before_tail_spectra[position_index][bin_index] = sum(
                    h_physical[output] * baseline_physical_path(output, channel, bin_index)
                    for output in range(4)
                )
                after_tail_spectra[position_index][bin_index] = sum(h_physical[output] * actual_spectra[output * 2 + channel][bin_index] for output in range(4))
        tail_before_values[channel_name] = [modal_tail_ratio(fft, spectrum) for spectrum in before_tail_spectra]
        tail_after_values[channel_name] = [modal_tail_ratio(fft, spectrum) for spectrum in after_tail_spectra]
        prediction[channel_name] = {
            "before_target_mae_db": round(statistics.mean(before_errors), 3),
            "after_target_mae_db": round(statistics.mean(after_errors), 3),
            "before_spatial_std_db": round(statistics.mean(before_std), 3),
            "after_spatial_std_db": round(statistics.mean(after_std), 3),
            "before_modal_tail_db": round(statistics.mean(tail_before_values[channel_name]), 3),
            "after_modal_tail_db": round(statistics.mean(tail_after_values[channel_name]), 3),
            "shape_reference_band_hz": [round(alignment_low_hz, 1), round(alignment_high_hz, 1)],
            "before_level_alignment_db": round(before_level_alignment_db, 3),
            "after_level_alignment_db": round(after_level_alignment_db, 3),
        }
        graphs[channel_name] = {
            "frequency": [round(value, 3) for value in graph_frequencies],
            "measured_db": [round(value, 4) for value in before_curve],
            "predicted_db": [round(value, 4) for value in after_curve],
            "target_db": [round(value, 4) for value in target_curve_values],
            "effective_target_db": [round(value, 4) for value in target_curve_values],
            "raw_measured_db": [round(value, 4) for value in before_curve_raw],
            "raw_predicted_db": [round(value, 4) for value in after_curve_raw],
            "level_alignment_db": {
                "measured": round(before_level_alignment_db, 3),
                "predicted": round(after_level_alignment_db, 3),
                "reference_band_hz": [round(alignment_low_hz, 1), round(alignment_high_hz, 1)],
            },
        }
        channel_response_samples[channel_name] = response_samples

    # Re-score both programme channels with one shared alignment scalar.  The
    # per-channel values above are useful only as intermediate diagnostics;
    # using them as the application gate would silently give L and R different
    # 0 dB origins and could hide an inter-channel level error.
    common_alignment_low_hz = max(float(correction_low_hz), 40.0)
    common_alignment_high_hz = min(float(high_hz), 130.0)
    if common_alignment_high_hz <= common_alignment_low_hz:
        common_alignment_low_hz = float(correction_low_hz)
        common_alignment_high_hz = float(high_hz)
    common_alignment_samples = [
        sample
        for samples in channel_response_samples.values()
        for sample in samples
        if common_alignment_low_hz <= sample["frequency"] <= common_alignment_high_hz
    ]
    if not common_alignment_samples:
        common_alignment_samples = [
            sample
            for samples in channel_response_samples.values()
            for sample in samples
            if correction_low_hz <= sample["frequency"] <= high_hz
        ]
    common_before_level_alignment_db = statistics.median([
        sample["target_db"] - level
        for sample in common_alignment_samples for level in sample["before_levels"]
    ])
    common_after_level_alignment_db = statistics.median([
        sample["target_db"] - level
        for sample in common_alignment_samples for level in sample["after_levels"]
    ])
    for channel_name in ("left", "right"):
        samples = channel_response_samples[channel_name]
        before_errors = [
            abs(level + common_before_level_alignment_db - sample["target_db"])
            for sample in samples if 20.0 <= sample["frequency"] <= high_hz
            for level in sample["before_levels"]
        ]
        after_errors = [
            abs(level + common_after_level_alignment_db - sample["target_db"])
            for sample in samples if 20.0 <= sample["frequency"] <= high_hz
            for level in sample["after_levels"]
        ]
        prediction[channel_name].update({
            "before_target_mae_db": round(statistics.mean(before_errors), 3),
            "after_target_mae_db": round(statistics.mean(after_errors), 3),
            "shape_reference_band_hz": [round(common_alignment_low_hz, 1), round(common_alignment_high_hz, 1)],
            "before_level_alignment_db": round(common_before_level_alignment_db, 3),
            "after_level_alignment_db": round(common_after_level_alignment_db, 3),
            "level_reference_scope": "one common Left/Right/MIMO-bank reference",
        })
        graphs[channel_name]["measured_db"] = [
            round(value + common_before_level_alignment_db, 4)
            for value in graphs[channel_name]["raw_measured_db"]
        ]
        graphs[channel_name]["predicted_db"] = [
            round(value + common_after_level_alignment_db, 4)
            for value in graphs[channel_name]["raw_predicted_db"]
        ]
        graphs[channel_name]["level_alignment_db"] = {
            "measured": round(common_before_level_alignment_db, 3),
            "predicted": round(common_after_level_alignment_db, 3),
            "reference_band_hz": [round(common_alignment_low_hz, 1), round(common_alignment_high_hz, 1)],
            "scope": "one common Left/Right/MIMO-bank reference",
            "independent_channel_normalization": False,
        }

    improvement_pass = all(
        prediction[channel]["after_target_mae_db"] <= prediction[channel]["before_target_mae_db"] + 0.25
        and prediction[channel]["after_spatial_std_db"] <= prediction[channel]["before_spatial_std_db"] + 0.10
        for channel in ("left", "right")
    )
    finite_pass = all(math.isfinite(value) for path in output_paths for value in path)
    headroom_pass = all(row_sum <= limit + 0.001 for row_sum, limit in zip(per_output_row_sum_after, per_output_limits))
    causality_pass = causality["target_peak_sample"] <= 2048 and causality["pre_peak_energy_percent"] <= 80.0
    modal_tail_pass = all(
        prediction[channel]["after_modal_tail_db"] <= prediction[channel]["before_modal_tail_db"] + 1.5
        for channel in ("left", "right")
    )
    common_reference_pass = bool(
        abs(target_offsets_db[0] - target_offsets_db[1]) <= 1.0e-9
        and all(
            graphs[channel]["level_alignment_db"].get("independent_channel_normalization") is False
            for channel in ("left", "right")
        )
        and abs(
            float(graphs["left"]["level_alignment_db"]["predicted"])
            - float(graphs["right"]["level_alignment_db"]["predicted"])
        ) <= 1.0e-9
    )
    base_common_attenuation_db = max(0.0, -float(base_normalization.get("applied_common_gain_db", 0.0)))
    mimo_common_attenuation_db = max(0.0, -db(scale))
    total_common_attenuation_db = base_common_attenuation_db + mimo_common_attenuation_db
    relative_compensation_limit_pass = total_common_attenuation_db <= float(max_boost) + 0.25
    warnings = []
    if diversity["independence_warning"]:
        warnings.append("일부 제어원의 공간 응답이 거의 같아 독립 MIMO 자유도가 낮습니다. 우퍼 위치/배선을 확인하세요.")
    if any(not position[source].get("measurement_quality", {}).get("recommended", False) for position in positions for source in sources):
        warnings.append("일부 sweep SNR이 권장 15 dB 미만입니다. 결과는 생성되지만 재측정을 권장합니다.")
    if scale < 0.70:
        warnings.append("최악의 상관 입력 headroom을 지키기 위해 MIMO bank 전체가 3 dB 이상 감쇄되었습니다.")
    for channel in ("left", "right"):
        if prediction[channel]["after_modal_tail_db"] > prediction[channel]["before_modal_tail_db"] + 1.5:
            warnings.append(f"{channel.title()}의 평활 전달함수 기반 impulse-tail proxy가 1.5 dB 넘게 악화되었습니다. 적용을 차단하며 이를 실제 RT60/잔향 측정으로 해석하지 않습니다.")

    model_pass = (
        finite_pass and headroom_pass and causality_pass and improvement_pass
        and modal_tail_pass and independent_positions["pass"]
        and common_reference_pass and relative_compensation_limit_pass
    )
    # All actuator responses were captured against the same playback/capture
    # clock before design.  A successful multichannel complex prediction is an
    # application gate; a quieter post-filter sweep remains optional evidence,
    # never a second mandatory wizard measurement.
    application_pass = model_pass
    result: dict[str, Any] = {
        "kind": "mimo_2x4",
        "sample_rate": RATE,
        "taps": TAPS,
        "target": target_name,
        "preset": preset,
        "preference": {"bass_db_at_20_hz": bass_tilt, "treble_db_at_20_khz": treble_tilt},
        "mimo_files": files,
        "mimo": {
            "topology": topology,
            "actuators": sources,
            "inputs": ["Left", "Right"],
            "physical_outputs": list(OUTPUT_LABELS),
            "matrix_paths": PATHS,
            "resource_budget": resource_budget(),
            "frequency_range_hz": [correction_low_hz, high_hz],
            "transition_end_hz": high_hz + 30,
            "strength": strength,
            "solution_blend": solution_blend,
            "regularization": "frequency-dependent Tikhonov, adjacent-bin spectral continuity, primary-path prior and measured usable-band penalty",
            "support_penalty_db": support_penalty_db,
            "woofer_trim_constraint_db": woofer_trim,
            "crossover": {
                "requested": crossover_requested,
                "enabled": crossover_enabled,
                "embedded_in_fir_bank": crossover_enabled,
                "type": "Linkwitz-Riley 4th-order minimum-phase branch transfer",
                "frequency_hz": crossover_frequency_hz if crossover_enabled else None,
                "additional_runtime_filters": 0,
                "additional_block_latency_samples": 0,
                "front_branch": "highpass" if crossover_enabled else "full-range",
                "sub_branch": "lowpass" if crossover_enabled else "not-applicable" if topology == "mimo_stereo" else "solver-controlled",
            },
            "target_level_normalization": {
                "reference_band_hz": [70, 130],
                "target_reference_db": round(target_reference_db, 3),
                "baseline_reference_db": {
                    "left": round(baseline_reference_db[0], 3),
                    "right": round(baseline_reference_db[1], 3),
                },
                "target_offset_db": {
                    "left": round(target_offsets_db[0], 3),
                    "right": round(target_offsets_db[1], 3),
                },
                "common_baseline_reference_db": round(common_baseline_reference_db, 3),
                "independent_channel_normalization": False,
                "policy": "preserve one shared existing SISO bass anchor in the solver; score L/R target shape after one common reference-band alignment and report physical bank attenuation separately",
                "siso_bank_normalization": base_normalization,
                "siso_common_level_reference": base_common_reference,
            },
            "stereo_broad_rolloff_corroboration": stereo_rolloff_summary,
            "usable_low_hz": {key: round(value, 2) for key, value in usable_lows.items()},
            "condition_surrogate": {"median": round(statistics.median(condition_values), 3), "p95": round(percentile(condition_values, 0.95), 3), "maximum": round(max(condition_values), 3)},
            "actuator_diversity": diversity,
            "prediction": prediction,
            "headroom": {
                "before_global_scale_row_sum": round(maximum_row_sum, 6),
                "global_scale_db": round(db(scale), 3),
                "base_common_attenuation_db": round(base_common_attenuation_db, 3),
                "total_common_attenuation_db": round(total_common_attenuation_db, 3),
                "max_relative_compensation_db": max_boost,
                "relative_compensation_limit_pass": relative_compensation_limit_pass,
                "maximum_correlated_input_row_sum": round(maximum_row_sum_after, 6),
                "physical_output_row_sum_before": {label: round(value, 6) for label, value in zip(OUTPUT_LABELS, per_output_row_sum_before)},
                "physical_output_row_sum_after": {label: round(value, 6) for label, value in zip(OUTPUT_LABELS, per_output_row_sum_after)},
                "physical_output_limits": {label: round(value, 6) for label, value in zip(OUTPUT_LABELS, per_output_limits)},
                "woofer_trim_is_actual_transfer_bound": topology in ("mimo_one_sub", "mimo_dual_sub"),
            },
            "causality": causality,
            "limitations": ["세 측정 위치 안의 선형 모델만 최적화", "미측정 위치와 비선형 왜곡은 보장하지 않음", f"{high_hz} Hz 위 전이대역 이후는 기존 L/R 개별 FIR", "한 우퍼의 stereo 입력은 하나의 물리 제어원으로 처리", "동일 clock 복소 모델 PASS는 적용 기준이며 실제 적용 후 저레벨 sweep은 선택 검증", "modal-tail 값은 평활 전달함수의 ringing proxy이며 실제 RT60 예측이 아님"],
        },
        "diagnostics": {"warnings": warnings},
        "room_tuning_audit": temporal_and_room_audit(positions, sources, high_hz),
        "self_validation": {
            "overall_pass": application_pass,
            "model_pass": model_pass,
            "core_checks": {
                "finite": finite_pass,
                "correlated_input_headroom": headroom_pass,
                "common_causality": causality_pass,
                "predicted_target_and_spatial_non_regression": improvement_pass,
                "predicted_modal_tail_non_regression": modal_tail_pass,
                "one_common_level_reference": common_reference_pass,
                "one_common_bank_gain": True,
                "relative_compensation_limit": relative_compensation_limit_pass,
            },
            "independent_positions": independent_positions,
            "crossover_sum": {
                "required": crossover_enabled,
                "pass": model_pass if crossover_enabled else None,
                "status": "pass_multichannel_complex_model" if crossover_enabled and model_pass else "fail_model" if crossover_enabled else "not_applicable",
                "prediction_status": "model_pass" if crossover_enabled and model_pass else "fail" if crossover_enabled else "not_applicable",
                "verification": "multichannel_same_clock_complex_model" if crossover_enabled else "not_applicable",
            },
        },
        "graphs": graphs,
        "algorithm": {
            "family": "robust multichannel weighted pressure matching",
            "not_product_clone": "independent AudioDSP implementation; not Dirac ART",
            "spatial": "three-position weighted complex pressure error",
            "stability": "Tikhonov control effort, adjacent-frequency continuity, primary prior, usable-band and per-frequency measurement-confidence weighting, actual sub-output trim bound, global worst-case row-sum headroom",
            "phase": "bulk-arrival-restored complex low-frequency optimization, common L/R base phase and one common causal delay",
            "crossover": "minimum-phase LR4 branch spectra are part of the transfer matrix and exported FIR bank; no extra runtime stage",
        },
        "common_level_reference": {
            "scope": "complete 2x4 MIMO L/R/woofer bank",
            "reference_band_hz": [round(common_alignment_low_hz, 1), round(common_alignment_high_hz, 1)],
            "predicted_reference_db": round(common_after_level_alignment_db, 4),
            "target_reference_db": 0.0,
            "independent_channel_normalization": False,
        },
        "filter_bank_normalization": {
            "method": "one common gain across the complete 2x4 MIMO bank",
            "scope": "complete_mimo_l_r_woofer_bank",
            "zero_db_reference": "single_common_bank_peak",
            "independent_channel_normalization": False,
            "applied_common_gain_db": round(db(scale), 4),
            "base_common_gain_db": base_normalization.get("applied_common_gain_db"),
            "common_attenuation_db": round(total_common_attenuation_db, 4),
            "max_relative_compensation_db": max_boost,
            "relative_compensation_limit_pass": relative_compensation_limit_pass,
            "relative_branch_gain_preserved": True,
        },
    }
    result["crossover"] = result["mimo"]["crossover"] | {
        "status": "pass_multichannel_complex_model" if crossover_enabled and model_pass else "fail" if crossover_enabled else "not_applicable" if topology == "mimo_stereo" else "disabled",
        "model_prediction_pass": model_pass if crossover_enabled else None,
        "post_filter_measurement_required": False,
        "post_filter_measurement_optional": crossover_enabled,
    }
    for audit_item in result["room_tuning_audit"]:
        if audit_item.get("id") == "crossover_integration":
            audit_item["status"] = result["crossover"]["status"]
            audit_item["evidence"] = result["crossover"]
            audit_item["action"] = (
                "LR4 branch를 MIMO 전달행렬과 FIR bank에 내장해 동일 clock 복소 전달함수로 공동 최적화·검증함; 적용 후 저레벨 sweep은 선택 검증"
                if crossover_enabled else "디지털 crossover 비활성 또는 독립 sub 출력이 없는 topology"
            )
    manifest_path = output_dir / "MIMO_manifest.json"
    report_json = output_dir / "Room_Tuning_Report.json"
    report_md = output_dir / "Room_Tuning_Report.md"
    result["mimo_manifest"] = manifest_path.name
    result["report_json"] = report_json.name
    result["report_md"] = report_md.name
    manifest = {
        "format": "AudioDSP MIMO Bank",
        "schema_version": 1,
        "sample_rate": RATE,
        "taps": TAPS,
        "inputs": 2,
        "outputs": 4,
        "files": files,
        "topology": topology,
        "frequency_range_hz": [correction_low_hz, high_hz],
        "self_validation": result["self_validation"],
        "application_requires_post_filter_measurement": False,
        "application_model_gate": "multichannel_same_clock_complex_model",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    report_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    write_report(report_md, result)
    return result


def synthetic_response(frequencies: list[float], actuator: int, position: int) -> dict[str, Any]:
    levels, phases = [], []
    for frequency in frequencies:
        modal = 5.0 * math.exp(-0.5 * (math.log2(frequency / (48.0 + 11.0 * actuator + 4.0 * position)) / 0.16) ** 2)
        dip = -3.5 * math.exp(-0.5 * (math.log2(frequency / (90.0 + 7.0 * position - 3.0 * actuator)) / 0.13) ** 2)
        rolloff = -12.0 * max(0.0, math.log2(max(30.0, 55.0 - 8.0 * actuator) / max(frequency, 1.0)))
        levels.append(modal + dip + rolloff + 0.6 * position)
        phases.append(-0.003 * frequency * (1.0 + 0.13 * actuator) + 0.08 * position)
    return {
        "frequencies": frequencies,
        "db": levels,
        "phase_rad": phases,
        "bulk_delay_samples": 240 + actuator * 19 + position * 7,
        "bulk_delay_reliable": True,
        "bulk_delay": {"reliable": True},
        "measurement_quality": {"snr_db": 32.0, "usable": True, "recommended": True},
        "frequency_quality": {"frequencies": frequencies, "confidence": [0.95] * len(frequencies)},
        "room_decay": {"bands": [{"center_hz": 63, "t20_rt60_s": 0.72 - 0.04 * actuator, "reliable": True}, {"center_hz": 125, "t20_rt60_s": 0.48, "reliable": True}]},
        "temporal": {"c80_db": 8.0 - position, "d50_percent": 72.0},
        "group_delay": {"bass_p90_ms": 18.0 + actuator},
    }


def self_test(engine_path: Path) -> dict[str, Any]:
    os.environ["AUDIODSP_PLATFORM_CLASS"] = "test"
    frequencies = [20.0 * (1000.0 ** (index / 511.0)) for index in range(512)]
    results = []
    fixtures: dict[str, tuple[Path, Path]] = {}
    probe = synthetic_response(frequencies, 1, 1)
    probe_frequency = 80.0
    expected_phase = interpolate_log(probe["frequencies"], probe["phase_rad"], probe_frequency)
    expected_phase -= 2.0 * math.pi * probe_frequency * probe["bulk_delay_samples"] / RATE
    actual_phase = cmath.phase(response_value(probe, probe_frequency, 0.0))
    wrapped_error = abs(cmath.phase(cmath.exp(1j * (actual_phase - expected_phase))))
    if wrapped_error > 1.0e-6:
        raise MimoError("MIMO 전달함수에서 경로 bulk delay가 복원되지 않았습니다.")
    with tempfile.TemporaryDirectory(prefix="audiodsp-mimo-test-") as temporary_name:
        root = Path(temporary_name)
        for mode in MIMO_MODES:
            directory = root / mode
            directory.mkdir()
            sources = MIMO_MODES[mode]
            measurements = []
            for position in range(1, 4):
                for actuator, source in enumerate(sources):
                    response = synthetic_response(frequencies, actuator, position - 1)
                    name = f"p{position}_{source}_response.json"
                    (directory / name).write_text(json.dumps(response) + "\n", encoding="utf-8", newline="\n")
                    measurements.append({"position": position, "source": source, "response": name, "snr_db": 32.0})
            session = {"mode": mode, "session_dir": str(directory), "positions_completed": 3, "measurements": measurements}
            session_path = directory / "session.json"
            session_path.write_text(json.dumps(session) + "\n", encoding="utf-8", newline="\n")
            fixtures[mode] = (session_path, directory)
            result = build(session_path, directory, {"target": "flat", "preset": "none", "mimo_high_hz": 150, "mimo_strength": "safe", "mimo_support_penalty_db": 6, "woofer_trim_db": 0, "crossover_enabled": True, "crossover_frequency_hz": 100}, engine_path, allow_unsupported=True)
            invariant_checks = result["self_validation"]["core_checks"]
            if not all(invariant_checks[key] for key in ("finite", "correlated_input_headroom", "common_causality")):
                raise MimoError(f"합성 MIMO 구조·안전 검증 실패: {mode}: {json.dumps(result['self_validation'], ensure_ascii=False)}")
            if len(result["mimo_files"]) != 4 or any(item["frames"] != TAPS for item in result["mimo_files"]):
                raise MimoError(f"MIMO 파일 형식 실패: {mode}")
            expected_crossover = mode in ("mimo_one_sub", "mimo_dual_sub")
            if bool(result["crossover"]["enabled"]) != expected_crossover:
                raise MimoError(f"MIMO crossover topology 적용 실패: {mode}")
            if not result["self_validation"]["model_pass"]:
                raise MimoError(
                    f"Flat / 추가 억제 없음 / Woofer trim 0 dB 기준 MIMO 모델 검증 실패: {mode}: "
                    f"{json.dumps({'validation': result['self_validation'], 'prediction': result['mimo']['prediction']}, ensure_ascii=False)}"
                )
            results.append({
                "mode": mode,
                "pass": True,
                "application_allowed": result["self_validation"]["overall_pass"],
                "safe_rejection": not result["self_validation"]["overall_pass"],
                "model_pass": result["self_validation"]["model_pass"],
                "core_checks": result["self_validation"]["core_checks"],
                "independent_positions": result["self_validation"]["independent_positions"],
                "prediction": result["mimo"]["prediction"],
                "headroom": result["mimo"]["headroom"],
                "causality": result["mimo"]["causality"],
                "target_level_normalization": result["mimo"]["target_level_normalization"],
            })
        # Every MIMO-specific value exposed by the FIR form gets an actual
        # 32768-tap 2x4 build.  Common target/voicing controls are covered by
        # the SISO target matrix, while representative target endpoints here
        # verify that they also reach the multichannel solver.
        session_path, directory = fixtures["mimo_one_sub"]
        baseline = {
            "target": "flat", "preset": "none", "woofer_trim_db": 0,
            "phase_mode": "bass", "phase_cutoff": 200, "spatial_mode": "equal",
            "bass_tilt_db": 0, "treble_tilt_db": 0,
            "correction_low_hz": 20, "correction_high_hz": 20_000,
        "max_boost_db": 10, "max_cut_db": 18,
            "mimo_high_hz": 150, "mimo_strength": "balanced",
            "mimo_support_penalty_db": 6,
            "crossover_enabled": True, "crossover_frequency_hz": 100,
        }
        requests: list[tuple[str, str, Any, dict[str, Any]]] = []
        for field, values in (
            ("mimo_high_hz", (80, 120, 150)),
            ("mimo_strength", ("safe", "balanced", "maximum")),
            ("mimo_support_penalty_db", (3, 6, 9, 12)),
            ("crossover_enabled", (False, True)),
            ("crossover_frequency_hz", (60, 70, 80, 90, 100, 120)),
            ("target", ("flat", "harman", "bk")),
        ):
            for value in values:
                requests.append(("single_value", field, value, {field: value}))
        requests.extend((
            ("interaction", "strength_support", "safe/12", {"mimo_strength": "safe", "mimo_support_penalty_db": 12}),
            ("interaction", "strength_support", "maximum/3", {"mimo_strength": "maximum", "mimo_support_penalty_db": 3}),
            ("interaction", "range_crossover", "80/60", {"mimo_high_hz": 80, "crossover_frequency_hz": 60}),
            ("interaction", "range_crossover", "150/120", {"mimo_high_hz": 150, "crossover_frequency_hz": 120}),
        ))
        unique: dict[str, tuple[str, str, Any, dict[str, Any]]] = {}
        for family, field, value, updates in requests:
            options = dict(baseline)
            options.update(updates)
            unique.setdefault(json.dumps(options, sort_keys=True), (family, field, value, options))
        option_results = []
        remediation_baseline = None
        for family, field, value, options in unique.values():
            option_result = build(session_path, directory, options, engine_path, allow_unsupported=True)
            checks = option_result["self_validation"]["core_checks"]
            structural_pass = bool(
                all(checks[name] for name in ("finite", "correlated_input_headroom", "common_causality"))
                and len(option_result["mimo_files"]) == 4
                and all(item["frames"] == TAPS for item in option_result["mimo_files"])
                and option_result["mimo"]["frequency_range_hz"][1] == options["mimo_high_hz"]
                and option_result["mimo"]["strength"] == options["mimo_strength"]
                and option_result["mimo"]["support_penalty_db"] == options["mimo_support_penalty_db"]
                and bool(option_result["crossover"]["enabled"]) == bool(options["crossover_enabled"])
            )
            if not structural_pass:
                raise MimoError(f"MIMO option matrix failed: {field}={value}")
            option_results.append({
                "family": family, "field": field, "value": value, "pass": True,
                "model_pass": option_result["self_validation"]["model_pass"],
                "application_status": option_result["self_validation"]["crossover_sum"]["status"],
                "core_checks": option_result["self_validation"]["core_checks"],
                "prediction": option_result["mimo"]["prediction"],
            })
            if options == baseline:
                remediation_baseline = {
                    "settings": baseline,
                    "model_pass": option_result["self_validation"]["model_pass"],
                    "application_status": option_result["self_validation"]["crossover_sum"]["status"],
                    "core_checks": option_result["self_validation"]["core_checks"],
                }
        if not remediation_baseline or not remediation_baseline["model_pass"]:
            raise MimoError("Web FAIL 안내의 권장 기준 조합을 실제로 재계산했지만 PASS하지 못했습니다.")
    return {"result": "PASS", "bulk_delay_restored": True, "topologies": results, "mimo_option_matrix": option_results, "remediation_baseline": remediation_baseline, "paths": PATHS, "taps": TAPS, "rate": RATE}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--measurement-engine", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    build_parser = sub.add_parser("build")
    build_parser.add_argument("session", type=Path)
    build_parser.add_argument("output", type=Path)
    build_parser.add_argument("options", type=Path)
    sub.add_parser("self-test")
    args = parser.parse_args()
    try:
        if args.command == "capabilities":
            result = platform_capability()
        elif args.command == "build":
            options = json.loads(args.options.read_text(encoding="utf-8"))
            result = build(args.session, args.output, options, args.measurement_engine)
        else:
            if args.measurement_engine is None:
                raise MimoError("self-test에는 --measurement-engine이 필요합니다.")
            result = self_test(args.measurement_engine)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
