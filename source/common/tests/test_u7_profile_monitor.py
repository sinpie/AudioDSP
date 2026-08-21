#!/usr/bin/env python3
"""Fast, silent regression test for the Xonar U7 selector monitor."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import tempfile


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("audiodsp_u7_monitor_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load monitor: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", type=Path, required=True)
    args = parser.parse_args()
    monitor = load_module(args.monitor)

    assert monitor.BUTTON_STATE_TO_PROFILE == {0x30: "headphone", 0xA0: "speaker"}
    assert monitor.CURRENT_STATE_TO_PROFILE == {0x88: "headphone", 0xE0: "speaker"}

    for state, expected in ((0x30, "headphone"), (0xA0, "speaker")):
        report = bytearray(16)
        report[monitor.PRESS_BYTE] = monitor.PRESS_MASK
        report[monitor.STATE_BYTE] = state
        assert monitor.decode_pressed_profile(bytes(report)) == expected

    original_open = monitor.os.open
    original_ioctl = monitor.fcntl.ioctl
    current_payload = bytearray(16)
    observed_flags: list[int] = []

    def fake_open(path: str, flags: int) -> int:
        observed_flags.append(flags)
        return original_open(path, flags)

    def fake_ioctl(_descriptor: int, _request: int, data: bytearray, _mutate: bool) -> int:
        data[0] = 0
        data[1:17] = current_payload
        return 17

    with tempfile.NamedTemporaryFile() as fake_hidraw:
        monitor.os.open = fake_open
        monitor.fcntl.ioctl = fake_ioctl
        try:
            for state, expected in ((0x88, "headphone"), (0xE0, "speaker")):
                current_payload[monitor.STATE_BYTE] = state
                assert monitor.current_profile(fake_hidraw.name) == (expected, state)
        finally:
            monitor.os.open = original_open
            monitor.fcntl.ioctl = original_ioctl

    assert observed_flags
    assert not (observed_flags[-1] & os.O_WRONLY)
    assert not (observed_flags[-1] & os.O_RDWR)
    source = args.monitor.read_text(encoding="utf-8")
    assert "HIDIOCSOUTPUT" not in source
    assert "HIDIOCSFEATURE" not in source
    assert "os.O_RDONLY | os.O_NONBLOCK" in source
    print("result=PASS button_states=2 steady_states=2 hid_open=read-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
