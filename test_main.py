from __future__ import annotations

import json
import socket
import textwrap
from pathlib import Path

import pytest

from main import (
    Config,
    PROBERS,
    build_embed,
    load_config,
    load_state,
    save_state,
    step,
    tcp_probe,
)


# ---------- step() state-machine ----------


class TestStep:
    def test_up_probe_ok_stays_up_no_transition(self):
        assert step("up", 0, True, 2) == ("up", 0, None)

    def test_up_first_failure_enters_retry_no_transition(self):
        assert step("up", 0, False, 2) == ("up", 1, None)

    def test_up_second_failure_still_no_transition(self):
        assert step("up", 1, False, 2) == ("up", 2, None)

    def test_up_third_failure_flips_to_down(self):
        assert step("up", 2, False, 2) == ("down", 3, "down")

    def test_up_recovery_midway_clears_counter(self):
        assert step("up", 2, True, 2) == ("up", 0, None)

    def test_down_probe_ok_flips_to_up(self):
        assert step("down", 3, True, 2) == ("up", 0, "up")

    def test_down_probe_fail_stays_down(self):
        assert step("down", 3, False, 2) == ("down", 3, None)

    def test_max_retries_zero_fails_on_first_failure(self):
        assert step("up", 0, False, 0) == ("down", 1, "down")

    def test_max_retries_one_needs_two_failures(self):
        assert step("up", 0, False, 1) == ("up", 1, None)
        assert step("up", 1, False, 1) == ("down", 2, "down")


# ---------- full three-strikes sequence ----------


def test_three_strikes_then_recovery_sequence():
    state, failures = "up", 0
    transitions = []
    # three consecutive failures -> down
    for _ in range(3):
        state, failures, t = step(state, failures, False, 2)
        transitions.append(t)
    # then one success -> up
    state, failures, t = step(state, failures, True, 2)
    transitions.append(t)
    assert transitions == [None, None, "down", "up"]
    assert state == "up"
    assert failures == 0


# ---------- tcp_probe ----------


class TestTcpProbe:
    def test_probe_succeeds_on_listening_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            assert tcp_probe("127.0.0.1", port, timeout=2) is True

    def test_probe_fails_on_closed_port(self):
        # bind+close to reserve a port, then call on it
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        assert tcp_probe("127.0.0.1", port, timeout=1) is False

    def test_probe_fails_on_unresolvable_host(self):
        assert tcp_probe("no-such-host.invalid", 22, timeout=1) is False

    def test_probe_registered_in_dispatch_table(self):
        assert PROBERS["tcp"] is tcp_probe


# ---------- state file persistence ----------


class TestStatePersistence:
    def _chdir(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)

    def test_load_state_returns_none_when_missing(self, monkeypatch, tmp_path):
        self._chdir(monkeypatch, tmp_path)
        assert load_state() is None

    def test_save_then_load_up(self, monkeypatch, tmp_path):
        self._chdir(monkeypatch, tmp_path)
        save_state("up")
        assert load_state() == "up"
        data = json.loads((tmp_path / ".state.json").read_text())
        assert data["state"] == "up"
        assert "last_transition" in data

    def test_save_then_load_down(self, monkeypatch, tmp_path):
        self._chdir(monkeypatch, tmp_path)
        save_state("down")
        assert load_state() == "down"

    def test_load_state_returns_none_on_corrupt_json(self, monkeypatch, tmp_path):
        self._chdir(monkeypatch, tmp_path)
        (tmp_path / ".state.json").write_text("{not valid json")
        assert load_state() is None

    def test_load_state_returns_none_on_unknown_state_value(self, monkeypatch, tmp_path):
        self._chdir(monkeypatch, tmp_path)
        (tmp_path / ".state.json").write_text('{"state": "maybe"}')
        assert load_state() is None

    def test_save_state_is_atomic_replace(self, monkeypatch, tmp_path):
        self._chdir(monkeypatch, tmp_path)
        save_state("up")
        save_state("down")
        # no tmp file left behind
        assert not (tmp_path / ".state.json.tmp").exists()
        assert load_state() == "down"


# ---------- build_embed ----------


def _sample_cfg(**overrides) -> Config:
    base = dict(
        host="example.com",
        port=22,
        method="tcp",
        interval=30,
        retry_interval=10,
        max_retries=2,
        timeout=5,
        down_message="Target down. See [status](https://example.com/status)",
        up_message="Target up.",
        webhook_url="https://discord.example/hook",
    )
    base.update(overrides)
    return Config(**base)


class TestBuildEmbed:
    def test_down_embed_has_red_color_and_down_title(self):
        payload = build_embed(_sample_cfg(), "down")
        embed = payload["embeds"][0]
        assert embed["color"] == 0xE74C3C
        assert "DOWN" in embed["title"]
        assert "example.com:22" in embed["title"]

    def test_up_embed_has_green_color_and_up_title(self):
        payload = build_embed(_sample_cfg(), "up")
        embed = payload["embeds"][0]
        assert embed["color"] == 0x2ECC71
        assert "UP" in embed["title"]

    def test_description_contains_configured_message(self):
        cfg = _sample_cfg(down_message="Pi offline. [TEPCO](https://tepco.example)")
        desc = build_embed(cfg, "down")["embeds"][0]["description"]
        assert "Pi offline." in desc
        assert "[TEPCO](https://tepco.example)" in desc

    def test_description_ends_with_discord_timestamp_token(self):
        desc = build_embed(_sample_cfg(), "up")["embeds"][0]["description"]
        # pattern: "<t:<unix>:F>"
        assert desc.rstrip().endswith(":F>")
        assert "<t:" in desc

    def test_username_is_set(self):
        assert build_embed(_sample_cfg(), "up")["username"] == "uptime-monitor"


# ---------- load_config ----------


def _write_config(dir_: Path, body: str) -> None:
    (dir_ / "config.yaml").write_text(textwrap.dedent(body).lstrip())


def _write_env(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", value)


VALID_CONFIG = """
monitor_host_port: "example.com:22"
method: "tcp"
interval: 30
retry_interval: 10
max_retries: 2
timeout: 5
down_message: |
  Target is down.
up_message: |
  Target is up.
"""


class TestLoadConfig:
    def test_happy_path(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://discord.example/hook")
        _write_config(tmp_path, VALID_CONFIG)
        cfg = load_config()
        assert cfg.host == "example.com"
        assert cfg.port == 22
        assert cfg.method == "tcp"
        assert cfg.interval == 30
        assert cfg.retry_interval == 10
        assert cfg.max_retries == 2
        assert cfg.timeout == 5
        assert cfg.webhook_url == "https://discord.example/hook"
        assert cfg.down_message == "Target is down."
        assert cfg.up_message == "Target is up."

    def test_missing_webhook_url_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "")
        _write_config(tmp_path, VALID_CONFIG)
        with pytest.raises(SystemExit, match="DISCORD_WEBHOOK_URL"):
            load_config()

    def test_missing_config_file_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        with pytest.raises(SystemExit, match="config.yaml"):
            load_config()

    def test_host_port_without_colon_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        _write_config(tmp_path, VALID_CONFIG.replace('"example.com:22"', '"example.com"'))
        with pytest.raises(SystemExit, match="monitor_host_port"):
            load_config()

    def test_host_port_empty_host_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        _write_config(tmp_path, VALID_CONFIG.replace('"example.com:22"', '":22"'))
        with pytest.raises(SystemExit, match="host segment"):
            load_config()

    def test_host_port_non_integer_port_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        _write_config(tmp_path, VALID_CONFIG.replace('"example.com:22"', '"example.com:ssh"'))
        with pytest.raises(SystemExit, match="port segment"):
            load_config()

    def test_host_port_out_of_range_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        _write_config(tmp_path, VALID_CONFIG.replace('"example.com:22"', '"example.com:70000"'))
        with pytest.raises(SystemExit, match="out of range"):
            load_config()

    def test_unsupported_method_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        _write_config(tmp_path, VALID_CONFIG.replace('method: "tcp"', 'method: "icmp"'))
        with pytest.raises(SystemExit, match="Unsupported method"):
            load_config()

    @pytest.mark.parametrize("field", ["interval", "retry_interval", "max_retries", "timeout"])
    def test_non_positive_numeric_field_exits(self, monkeypatch, tmp_path, field):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        body = VALID_CONFIG
        for line in VALID_CONFIG.splitlines():
            if line.startswith(f"{field}:"):
                body = body.replace(line, f"{field}: 0")
                break
        _write_config(tmp_path, body)
        with pytest.raises(SystemExit, match=field):
            load_config()

    @pytest.mark.parametrize("field", ["interval", "retry_interval", "max_retries"])
    def test_missing_required_numeric_field_exits(self, monkeypatch, tmp_path, field):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        body = "\n".join(
            line for line in VALID_CONFIG.splitlines() if not line.startswith(f"{field}:")
        )
        _write_config(tmp_path, body)
        with pytest.raises(SystemExit, match=field):
            load_config()

    def test_missing_timeout_uses_default_5(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        body = "\n".join(
            line for line in VALID_CONFIG.splitlines() if not line.startswith("timeout:")
        )
        _write_config(tmp_path, body)
        assert load_config().timeout == 5

    def test_empty_down_message_exits(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _write_env(monkeypatch, "https://x")
        body = VALID_CONFIG.replace("Target is down.", "")
        _write_config(tmp_path, body)
        with pytest.raises(SystemExit, match="down_message"):
            load_config()
