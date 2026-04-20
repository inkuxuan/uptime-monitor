from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import yaml
from dotenv import load_dotenv

STATE_FILE = Path(".state.json")
WEBHOOK_BACKOFF_SCHEDULE = [5, 15, 60, 300]
SHUTDOWN_GRACE_SECONDS = 3

log = logging.getLogger("uptime-monitor")


@dataclass
class Config:
    host: str
    port: int
    method: str
    interval: int
    retry_interval: int
    max_retries: int
    timeout: int
    down_message: str
    up_message: str
    webhook_url: str


def load_config() -> Config:
    load_dotenv()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit("DISCORD_WEBHOOK_URL is missing or empty in .env")

    config_path = Path("config.yaml")
    if not config_path.exists():
        raise SystemExit("config.yaml not found")
    raw = yaml.safe_load(config_path.read_text())

    host_port = str(raw.get("monitor_host_port", "")).strip()
    if ":" not in host_port:
        raise SystemExit(f"monitor_host_port must be 'host:port', got: {host_port!r}")
    host, port_str = host_port.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise SystemExit("monitor_host_port host segment is empty")
    try:
        port = int(port_str)
    except ValueError:
        raise SystemExit(f"monitor_host_port port segment is not an integer: {port_str!r}")
    if not (1 <= port <= 65535):
        raise SystemExit(f"monitor_host_port port out of range: {port}")

    method = str(raw.get("method", "")).strip().lower()
    if method not in PROBERS:
        raise SystemExit(f"Unsupported method {method!r}. Supported: {sorted(PROBERS)}")

    def _positive_int(key: str, default: int | None = None) -> int:
        value = raw.get(key, default)
        if value is None:
            raise SystemExit(f"Missing required config field: {key}")
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise SystemExit(f"Config field {key!r} must be an integer, got {value!r}")
        if value <= 0:
            raise SystemExit(f"Config field {key!r} must be > 0, got {value}")
        return value

    interval = _positive_int("interval")
    retry_interval = _positive_int("retry_interval")
    max_retries = _positive_int("max_retries")
    timeout = _positive_int("timeout", default=5)

    down_message = str(raw.get("down_message", "")).strip()
    up_message = str(raw.get("up_message", "")).strip()
    if not down_message or not up_message:
        raise SystemExit("down_message and up_message must be non-empty")

    return Config(
        host=host,
        port=port,
        method=method,
        interval=interval,
        retry_interval=retry_interval,
        max_retries=max_retries,
        timeout=timeout,
        down_message=down_message,
        up_message=up_message,
        webhook_url=webhook_url,
    )


def tcp_probe(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.gaierror):
        return False


PROBERS: dict[str, Callable[[str, int, int], bool]] = {
    "tcp": tcp_probe,
}


def load_state() -> str | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        state = data.get("state")
        if state in ("up", "down"):
            return state
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read state file (%s); treating as first run", exc)
    return None


def save_state(state: str) -> None:
    payload = {
        "state": state,
        "last_transition": datetime.now(timezone.utc).isoformat(),
    }
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATE_FILE)


def build_embed(cfg: Config, kind: str) -> dict:
    is_down = kind == "down"
    now = int(time.time())
    target = f"{cfg.host}:{cfg.port}"
    description = (cfg.down_message if is_down else cfg.up_message).rstrip()
    description += f"\n\n<t:{now}:F>"
    return {
        "username": "uptime-monitor",
        "embeds": [
            {
                "title": f"{'🔴' if is_down else '🟢'} {target} is {'DOWN' if is_down else 'UP'}",
                "description": description,
                "color": 0xE74C3C if is_down else 0x2ECC71,
            }
        ],
    }


class WebhookSender:
    """Sends state-transition alerts with indefinite retry. A newer transition
    cancels any in-flight retry for a stale one."""

    def __init__(self, webhook_url: str, stop_event: threading.Event) -> None:
        self.webhook_url = webhook_url
        self.stop_event = stop_event
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._lock = threading.Lock()

    def send(self, payload: dict) -> None:
        with self._lock:
            self._generation += 1
            gen = self._generation
        thread = threading.Thread(
            target=self._deliver, args=(payload, gen), daemon=True, name="webhook-sender"
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return gen == self._generation

    def _deliver(self, payload: dict, gen: int) -> None:
        attempt = 0
        while not self.stop_event.is_set() and self._is_current(gen):
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(self.webhook_url, json=payload)
                if resp.status_code < 300:
                    log.info("discord webhook delivered (status=%s)", resp.status_code)
                    return
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    log.warning("discord 429; retry-after %.1fs", retry_after)
                    self._sleep(retry_after)
                    continue
                log.warning(
                    "discord webhook failed status=%s body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
            except httpx.HTTPError as exc:
                log.warning("discord webhook error: %s", exc)

            delay = WEBHOOK_BACKOFF_SCHEDULE[min(attempt, len(WEBHOOK_BACKOFF_SCHEDULE) - 1)]
            attempt += 1
            self._sleep(delay)

        if not self._is_current(gen):
            log.info("abandoning stale webhook retry (state changed)")

    def _sleep(self, seconds: float) -> None:
        self.stop_event.wait(timeout=seconds)

    def wait_for_flush(self, timeout: float) -> None:
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)


def run(cfg: Config, stop_event: threading.Event) -> int:
    prober = PROBERS[cfg.method]
    sender = WebhookSender(cfg.webhook_url, stop_event)

    state = load_state()
    if state is None:
        log.info(
            "no prior state; performing silent initial probe of %s:%s", cfg.host, cfg.port
        )
        state = "up" if prober(cfg.host, cfg.port, cfg.timeout) else "down"
        save_state(state)
        log.info("initial state: %s", state)
    else:
        log.info("resumed prior state: %s", state)

    consecutive_failures = 0

    while not stop_event.is_set():
        sleep_for = cfg.interval if (state == "up" and consecutive_failures == 0) else cfg.retry_interval
        if stop_event.wait(timeout=sleep_for):
            break

        ok = prober(cfg.host, cfg.port, cfg.timeout)
        log.debug("probe %s:%s -> %s", cfg.host, cfg.port, "ok" if ok else "fail")

        if ok:
            if state == "down":
                log.info("target recovered; transitioning down -> up")
                state = "up"
                consecutive_failures = 0
                save_state(state)
                sender.send(build_embed(cfg, "up"))
            else:
                if consecutive_failures:
                    log.info("probe recovered before threshold; resetting counter")
                consecutive_failures = 0
        else:
            if state == "down":
                continue
            consecutive_failures += 1
            log.info(
                "probe failed (%d/%d) for %s:%s",
                consecutive_failures,
                cfg.max_retries + 1,
                cfg.host,
                cfg.port,
            )
            if consecutive_failures > cfg.max_retries:
                log.info("threshold reached; transitioning up -> down")
                state = "down"
                save_state(state)
                sender.send(build_embed(cfg, "down"))

    log.info("shutdown signal received; flushing in-flight webhook (<= %ds)", SHUTDOWN_GRACE_SECONDS)
    sender.wait_for_flush(SHUTDOWN_GRACE_SECONDS)
    return 0


def install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum, _frame):
        log.info("received signal %s", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="uptime-monitor")
    parser.add_argument("--debug", action="store_true", help="verbose per-probe logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config()
    log.info(
        "starting: target=%s:%s method=%s interval=%ds retry_interval=%ds max_retries=%d timeout=%ds",
        cfg.host,
        cfg.port,
        cfg.method,
        cfg.interval,
        cfg.retry_interval,
        cfg.max_retries,
        cfg.timeout,
    )

    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    return run(cfg, stop_event)


if __name__ == "__main__":
    sys.exit(main())
