#!/usr/bin/env python3
"""Follow Xonar U7 raw HID output-selector reports and switch AudioDSP profiles."""

from __future__ import annotations

import glob
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time


U7_HID_ID = "0003:00001043:0000857C"
PRESS_MASK = 0x20
PRESS_BYTE = 7
STATE_BYTE = 10
STATE_TO_PROFILE = {
    0x30: "headphone",
    0xA0: "speaker",
}
SELECTOR_STATE_PATH = Path(os.environ.get(
    "AUDIODSP_SELECTOR_STATE_PATH",
    os.environ.get("GSONIC_SELECTOR_STATE_PATH", "/var/lib/audiodsp/u7-selector-state.json"),
))
OUTPUT_PROFILE = os.environ.get(
    "AUDIODSP_OUTPUT_PROFILE",
    os.environ.get("GSONIC_OUTPUT_PROFILE", "/usr/local/bin/audiodsp-output-profile"),
)
HID_INPUT_REPORT_SIZE = 17


def hid_ioctl_code(number: int, size: int) -> int:
    nr_bits = 8
    type_bits = 8
    size_bits = 14
    nr_shift = 0
    type_shift = nr_shift + nr_bits
    size_shift = type_shift + type_bits
    direction_shift = size_shift + size_bits
    read_write = 1 | 2
    return ((read_write << direction_shift) | (ord("H") << type_shift)
            | (number << nr_shift) | (size << size_shift))


HIDIOCGINPUT = hid_ioctl_code(0x0A, HID_INPUT_REPORT_SIZE)


def decode_pressed_profile(report: bytes) -> str | None:
    if len(report) <= max(STATE_BYTE, PRESS_BYTE):
        return None
    if not (report[PRESS_BYTE] & PRESS_MASK):
        return None
    return STATE_TO_PROFILE.get(report[STATE_BYTE])


def read_current_report(device: str) -> bytes:
    descriptor = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        data = bytearray(HID_INPUT_REPORT_SIZE)
        returned = fcntl.ioctl(descriptor, HIDIOCGINPUT, data, True)
    finally:
        os.close(descriptor)
    # HIDIOCGINPUT prepends the unnumbered report ID byte on this U7. Normal
    # read() reports contain only the following 16-byte payload.
    if returned == HID_INPUT_REPORT_SIZE and data[0] == 0:
        return bytes(data[1:returned])
    return bytes(data[:returned])


def current_profile(device: str) -> tuple[str, int]:
    report = read_current_report(device)
    if len(report) <= STATE_BYTE:
        raise OSError(f"U7 HID input report is too short: {len(report)}")
    state = report[STATE_BYTE]
    profile = STATE_TO_PROFILE.get(state)
    if profile is None:
        raise OSError(f"Unknown U7 selector state 0x{state:02x}")
    return profile, state


def save_selector_state(profile: str, state: int, source: str) -> None:
    SELECTOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        boot_id = ""
    payload = {
        "profile": profile,
        "state_byte": f"0x{state:02x}",
        "source": source,
        "updated_unix": time.time(),
        "boot_id": boot_id,
    }
    temporary = SELECTOR_STATE_PATH.with_name(f".{SELECTOR_STATE_PATH.name}.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, SELECTOR_STATE_PATH)


def log(message: str) -> None:
    print(message, flush=True)


def find_u7_hidraw() -> str | None:
    for device in sorted(glob.glob("/dev/hidraw*")):
        name = os.path.basename(device)
        uevent = f"/sys/class/hidraw/{name}/device/uevent"
        try:
            with open(uevent, "r", encoding="ascii") as handle:
                properties = handle.read()
        except OSError:
            continue
        if f"HID_ID={U7_HID_ID}" in properties:
            return device
    return None


def apply_profile(profile: str, announce: bool = True) -> None:
    arguments = [OUTPUT_PROFILE, profile]
    if announce:
        arguments.append("--announce")
    result = subprocess.run(
        arguments,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = result.stdout.strip()
    if output:
        log(output)
    if result.returncode != 0:
        log(f"Profile switch failed: profile={profile} exit={result.returncode}")


def monitor(device: str) -> None:
    log(f"Monitoring Xonar U7 output selector on {device}")
    try:
        profile, state = current_profile(device)
        log(f"Initial U7 selector state 0x{state:02x} -> {profile}")
        save_selector_state(profile, state, "hidio_get_input")
        apply_profile(profile, announce=False)
    except OSError as exc:
        log(f"Could not query initial U7 selector state: {exc}")
    last_press = 0.0
    with open(device, "rb", buffering=0) as handle:
        while True:
            report = handle.read(64)
            if not report:
                raise OSError("hidraw returned EOF")
            profile = decode_pressed_profile(report)
            if profile is None:
                if len(report) > STATE_BYTE and len(report) > PRESS_BYTE and (report[PRESS_BYTE] & PRESS_MASK):
                    log(f"Ignoring unknown U7 selector state 0x{report[STATE_BYTE]:02x}: {report.hex(' ')}")
                continue
            now = time.monotonic()
            if now - last_press < 0.35:
                continue
            last_press = now
            state = report[STATE_BYTE]
            log(f"U7 selector state 0x{state:02x} -> {profile}")
            save_selector_state(profile, state, "button")
            apply_profile(profile)


def main() -> None:
    while True:
        device = find_u7_hidraw()
        if device is None:
            log("Xonar U7 HID interface not found; retrying")
            time.sleep(2.0)
            continue
        try:
            monitor(device)
        except OSError as exc:
            log(f"Xonar U7 HID monitor disconnected: {exc}; retrying")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
