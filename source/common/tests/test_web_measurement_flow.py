#!/usr/bin/env python3
"""Silent cross-platform smoke test for the measurement workflow HTML."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def load(path: Path):
    spec = importlib.util.spec_from_file_location("audiodsp_web_measurement_flow_test", path)
    require(spec is not None and spec.loader is not None, "Web module import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def job(module, mode: str) -> dict:
    sources = {
        "lrw_sum": ["front_left", "front_right", "woofer", "left_sum", "right_sum"],
        "mimo_one_sub": ["front_left", "front_right", "sub_pair"],
    }[mode]
    return {
        "state": "measured",
        "stage": "측정 완료",
        "mode": mode,
        "session_id": f"ui-{mode}",
        "session_note": "UI 무음 회귀",
        "created_at": "2026-08-21T00:00:00+09:00",
        "positions_completed": 3,
        "positions_total": 3,
        "sources": sources,
        "level_check": {"ok": True},
        "installed_calibrations": {},
        "correction_preferences": dict(module.DEFAULT_CORRECTION_PREFERENCES),
        "capabilities": {
            "mimo_supported": True,
            "mimo_compute_supported": True,
            "phase_clock_shared": True,
        },
        "checkpoints": {"configured": True, "level_checked": True, "measured": True},
        "measurement_output_match": True,
        "output_selector": {"profile": "speaker", "stale": False},
        "measurement_profile": "speaker",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    arguments = parser.parse_args()
    os.environ["AUDIODSP_MEASUREMENT"] = str(arguments.measurement.resolve())
    module = load(arguments.web.resolve())
    module.measurement = lambda *args: {"sessions": []} if args == ("list-sessions",) else {}

    mimo_html = module.measurement_panel(job(module, "mimo_one_sub"), {})
    require(mimo_html.count('role="tab"') == 7, "measurement workflow must have seven tabs")
    require(mimo_html.count('role="tabpanel"') == 7, "measurement workflow must have seven panels")
    for marker in (
        "measurement-tab-session", "measurement-panel-session", "측정 구성", "결과 검토",
        "저역 공동제어", "세 위치 전달행렬 H(f)", "불확실성·조건수 안정화",
        "안정성/효과", "보조 출력 사용 제한", "실제 1-노름 조건수",
    ):
        require(marker in mimo_html, f"MIMO measurement UI marker missing: {marker}")
    require('<section class="mimo-options-card">' in mimo_html, "MIMO controls are not grouped")

    siso_html = module.measurement_panel(job(module, "lrw_sum"), {})
    require('<section class="mimo-options-card">' not in siso_html, "SISO UI exposes MIMO-only controls")
    require("정밀 분리+합산" in siso_html, "SISO measurement method is not explained")
    require("L/R/우퍼/L+우퍼/R+우퍼" in siso_html, "SISO acquisition sequence is missing")
    require("전 대역의 유일한 절대 감쇄 상한" in siso_html, "maximum room-cut semantics are not visible beside the setting")
    require("숨은 3/6 dB 고역 제한은 없으며" in siso_html, "removed high-frequency cut caps are not documented in the algorithm panel")
    print("PASS: 7-tab measurement flow, MIMO-only controls, and SISO/MIMO explanations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
