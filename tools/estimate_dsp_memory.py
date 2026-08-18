#!/usr/bin/env python3
"""Print AudioDSP FIR runtime/generator planning budgets without playing sound."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "source" / "common" / "payload" / "audiodsp-mimo.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("audiodsp_mimo_budget_cli", ENGINE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {ENGINE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {
        "stereo_2x4_dense": module.resource_budget(2, 4),
        "five_one_diagonal": module.resource_budget(1, 6),
        "five_one_dual_sub_dense_6x7": module.resource_budget(6, 7),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
