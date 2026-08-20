#!/usr/bin/env python3
"""Render and inspect the live AudioDSP UI at an exact CSS viewport via CDP."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import websockets


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def inspect(
    websocket_url: str,
    url: str,
    width: int,
    height: int,
    wait_seconds: float,
    capture_screenshot: bool = True,
    final_step: int | None = None,
) -> tuple[bytes, dict]:
    command_id = 0
    navigation_events = 0
    async with websockets.connect(
        websocket_url,
        max_size=16 * 1024 * 1024,
        open_timeout=5,
        close_timeout=1,
        ping_interval=None,
    ) as channel:
        async def command(method: str, params: dict | None = None) -> dict:
            nonlocal command_id, navigation_events
            command_id += 1
            expected = command_id
            await channel.send(json.dumps({"id": expected, "method": method, "params": params or {}}))
            while True:
                try:
                    raw_message = await asyncio.wait_for(
                        channel.recv(), timeout=30 if method == "Page.captureScreenshot" else 8
                    )
                except TimeoutError as exc:
                    raise TimeoutError(f"CDP command timed out: {method}") from exc
                message = json.loads(raw_message)
                if message.get("method") == "Page.frameNavigated":
                    navigation_events += 1
                if message.get("id") == expected:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})

        await command("Page.enable")
        await command("Runtime.enable")
        await command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": width <= 760,
                "screenWidth": width,
                "screenHeight": height,
            },
        )
        await command("Page.navigate", {"url": url})
        deadline = time.monotonic() + 15.0
        page_state: dict = {}
        while time.monotonic() < deadline:
            state = await command(
                "Runtime.evaluate",
                {
                    "expression": "({href:location.href, ready:document.readyState})",
                    "returnByValue": True,
                },
            )
            page_state = state.get("result", {}).get("value") or {}
            if page_state.get("href") == url and page_state.get("ready") == "complete":
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError(f"page did not reach requested URL: {page_state!r}")
        initial_navigation_events = navigation_events
        await asyncio.sleep(wait_seconds)
        expression = r"""(() => {
          const rect = selector => {
            const node = document.querySelector(selector);
            if (!node) return null;
            const value = node.getBoundingClientRect();
            return {left:value.left,right:value.right,width:value.width,top:value.top,bottom:value.bottom};
          };
          return {
            innerWidth,
            innerHeight,
            documentScrollWidth: document.documentElement.scrollWidth,
            bodyScrollWidth: document.body.scrollWidth,
            main: rect('main'),
            themeSwitch: rect('.theme-switch'),
            appNav: rect('.app-nav'),
            sessionOverview: rect('.session-overview'),
            sessionMeta: rect('.session-meta-grid'),
            workflow: rect('.workflow'),
            visibleMeasurementPanels: [...document.querySelectorAll('.measurement-panel')]
              .filter(node => !node.hidden && getComputedStyle(node).display !== 'none').length,
            selectedWorkflowTabs: document.querySelectorAll('.flow-step.selected').length,
            currentPageLinks: document.querySelectorAll('[aria-current="page"]').length,
            currentStepTabs: document.querySelectorAll('[aria-current="step"]').length,
          };
        })()"""
        result = await command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        tab_exercise = []
        for step in range(1, 7):
            tab_result = await command(
                "Runtime.evaluate",
                {
                    "expression": f"""(() => {{
                      const tab = document.querySelector('[data-measurement-tab="{step}"]');
                      if (!tab) return null;
                      tab.click();
                      const visible = [...document.querySelectorAll('.measurement-panel')]
                        .filter(node => !node.hidden && getComputedStyle(node).display !== 'none')
                        .map(node => node.id);
                      return {{
                        step: '{step}',
                        selected: tab.classList.contains('selected'),
                        ariaSelected: tab.getAttribute('aria-selected'),
                        visible,
                        hash: location.hash,
                      }};
                    }})()""",
                    "returnByValue": True,
                },
            )
            value = tab_result.get("result", {}).get("value")
            if value is not None:
                tab_exercise.append(value)
        if final_step is not None:
            await command(
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector('[data-measurement-tab=\"{final_step}\"]')?.click()",
                    "returnByValue": True,
                },
            )
            await asyncio.sleep(0.2)
        detail_result = await command(
            "Runtime.evaluate",
            {
                "expression": """(() => {
                  const node = document.querySelector('details');
                  if (!node) return null;
                  const before = node.open;
                  node.querySelector('summary')?.click();
                  return {before, after:node.open};
                })()""",
                "returnByValue": True,
            },
        )
        screenshot = (
            await command(
                "Page.captureScreenshot",
                {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            )
            if capture_screenshot else
            None
        )
        metrics = result["result"]["value"]
        graph_result = await command(
            "Runtime.evaluate",
            {
                "expression": """(() => {
                  const graph = document.getElementById('measurement-result-graph');
                  return {
                    liveState: document.getElementById('job-live-state')?.textContent?.trim() || '',
                    graphExists: Boolean(graph),
                    graphChildren: graph?.childElementCount || 0,
                    graphPolylines: graph?.querySelectorAll('polyline').length || 0,
                    graphSummary: document.getElementById('measurement-result-summary')?.textContent || '',
                  };
                })()""",
                "returnByValue": True,
            },
        )
        metrics["tabExercise"] = tab_exercise
        metrics["detailsToggle"] = detail_result.get("result", {}).get("value")
        metrics["resultGraph"] = graph_result.get("result", {}).get("value")
        metrics["unexpectedNavigationsDuringWait"] = max(0, navigation_events - initial_navigation_events)
        return base64.b64decode(screenshot["data"]) if screenshot else b"", metrics


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, default=4.0)
    parser.add_argument("--skip-screenshot", action="store_true")
    parser.add_argument("--final-step", type=int, choices=range(1, 7))
    args = parser.parse_args()
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="audiodsp-chrome-") as profile:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [
                str(args.chrome),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        try:
            deadline = time.monotonic() + 15.0
            pages = None
            while time.monotonic() < deadline:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/json", timeout=0.5) as response:
                        pages = json.load(response)
                    if pages:
                        break
                except OSError:
                    time.sleep(0.1)
            if not pages:
                raise TimeoutError("Chrome DevTools endpoint did not start")
            page = next(
                (item for item in pages if item.get("type") == "page" and item.get("url") == "about:blank"),
                next((item for item in pages if item.get("type") == "page"), pages[0]),
            )
            image, metrics = asyncio.run(inspect(
                page["webSocketDebuggerUrl"],
                args.url,
                args.width,
                args.height,
                args.wait_seconds,
                not args.skip_screenshot,
                args.final_step,
            ))
            if image:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(image)
                metrics["screenshot"] = str(args.output.resolve())
            metrics["horizontalOverflow"] = max(
                metrics["documentScrollWidth"], metrics["bodyScrollWidth"]
            ) > metrics["innerWidth"]
            print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
