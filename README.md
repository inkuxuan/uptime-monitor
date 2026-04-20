# uptime-monitor

A lightweight uptime monitor that TCP-pings a target host:port and sends Discord webhook alerts on state transitions.

## Behavior

- Polls the target every `interval` seconds while the target is believed to be **up**.
- On a probe failure, enters a *retry phase*: polls every `retry_interval` seconds.
- After `max_retries` consecutive additional failures (i.e. `1 + max_retries` total failures in a row), marks the target **down** and posts the `down_message`.
- While **down**, keeps polling every `retry_interval` seconds. On the first success, marks the target **up** and posts the `up_message`.
- State is persisted to `.state.json`, so restarts do not replay alerts. On first run (no state file), an initial silent probe sets the starting state with no notification.
- Discord delivery failures retry indefinitely with exponential backoff (5s → 15s → 60s → 300s). If the state flips again while a prior alert is still retrying, the stale alert is abandoned.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.template .env         # then fill in DISCORD_WEBHOOK_URL
cp config.yaml.template config.yaml   # then edit as needed
```

### `.env`

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### `config.yaml`

```yaml
monitor_host_port: "example.com:22"
method: "tcp"          # only "tcp" is currently supported
interval: 30           # seconds between probes while up
retry_interval: 10     # seconds between probes while confirming failure or while down
max_retries: 2         # additional consecutive failures required to declare down
timeout: 5             # per-probe TCP connect timeout (seconds)
down_message: |
  Multi-line message posted when the target goes down.
up_message: |
  Message posted when the target recovers.
```

## Running

```sh
uv run main.py              # normal
uv run main.py --debug      # verbose per-probe logging
```

Logs go to stdout. The process handles `SIGINT` / `SIGTERM` gracefully and exits within a few seconds.

### systemd

Example unit (adjust paths and user):

```ini
[Unit]
Description=uptime-monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/uptime-monitor
ExecStart=/usr/bin/env uv run main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

View logs with `journalctl -u uptime-monitor -f`.

## Files

- `main.py` — the monitor.
- `config.yaml` — user configuration (gitignored as `.config.yaml` if you wish to keep it local).
- `.env` — `DISCORD_WEBHOOK_URL` (gitignored).
- `.state.json` — persisted state (gitignored, created at runtime).
