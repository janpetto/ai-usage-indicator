# AI Usage Indicator

GNOME tray indicator for **Claude Code** and **OpenAI Codex** rate limits: 5-hour
window, weekly windows, per-model windows, and Claude's **extra credits** —
with desktop notifications before you run into a wall.

![ring](docs/ring.png)

## Why not one of the existing tools

Every ready-made alternative either skips Codex or reads your OAuth tokens out of
`~/.claude/.credentials.json` and `~/.codex/auth.json`. This one never touches a
credential:

* **Claude Code** — spawns the CLI and sends it a `get_usage` control request over
  the stream-JSON protocol. `claude` talks to Anthropic with its own credential and
  hands back structured data. Takes ~1.5s and costs **no tokens**.
* **OpenAI Codex** — spawns `codex app-server` and calls `account/rateLimits/read`
  over JSON-RPC. Same deal: the CLI holds the credential, not this process. This
  also reports `individualLimit`, Codex's own spend allowance. Requires
  `codex login` once; until then the indicator falls back to the
  `rate_limits` snapshots Codex writes into `~/.codex/sessions/**/*.jsonl`, which
  are only as fresh as your last Codex session. Data older than
  `codex_stale_minutes` is greyed out and never raises an alert.

The Codex figures are **account-wide**, not CLI-specific. If you drive Codex
through another client on the same ChatGPT account — [Pi](https://github.com/earendil-works/pi),
for instance — its consumption is included, because the quota lives on the
account rather than on the tool.

## Requirements

Ubuntu 24.04 / GNOME 46, all shipped by default:

```
python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
```

Tray icons on GNOME need the AppIndicator extension (`ubuntu-appindicators@ubuntu.com`),
which Ubuntu enables out of the box.

## Run

```sh
python3 ai_usage_indicator.py
```

Tick **"Beim Anmelden starten"** in the menu to install
`~/.config/autostart/ai-usage-indicator.desktop`.

## What the panel shows

A progress ring coloured by the worst active window (green < 60 %, yellow < 85 %,
orange < 95 %, red above), plus a label:

```
30% · 24% · cx 8%
 │     │      └─ Codex 5-hour window (hidden when stale)
 │     └──────── Claude weekly, all models
 └────────────── Claude 5-hour window
```

The menu breaks out every window with its percentage and reset countdown, plus
Claude's extra-credit spend and Codex's individual spend limit.

## Notifications

* **Thresholds** — one notification per window each time it crosses 80 %, 95 % and
  100 % (configurable). Keyed on the window's reset timestamp, so each new window
  re-arms the alerts.
* **Extra credits** — Claude reports `extra_usage.used_credits`. When that number
  goes *up* between two polls you are spending real money past your plan limit, and
  you get a critical notification with the delta and the running total. Rate-limited
  to `extra_credit_notify_cooldown_seconds`.

## Configuration

`~/.config/ai-usage-indicator/config.json`, written on first start. Restart to apply.

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_interval_seconds` | `180` | Refresh interval |
| `thresholds` | `[80, 95, 100]` | Percentages that raise a notification |
| `show_codex` | `true` | Show the Codex section at all |
| `codex_stale_minutes` | `60` | Older Codex data is greyed out and never alerts |
| `extra_credit_notify_cooldown_seconds` | `900` | Minimum gap between extra-credit warnings |
| `claude_timeout_seconds` | `60` | Timeout for the Claude CLI call |
| `codex_timeout_seconds` | `30` | Timeout for the Codex app-server call |

`~/.config/ai-usage-indicator/state.json` remembers which alerts already fired and
the last credit balance, so a restart does not re-fire everything.

## Troubleshooting

No icon in the panel:

```sh
gnome-extensions info ubuntu-appindicators@ubuntu.com      # must be ENABLED
gdbus call --session --dest org.kde.StatusNotifierWatcher \
  --object-path /StatusNotifierWatcher \
  --method org.freedesktop.DBus.Properties.Get \
  org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems   # must list ai_usage_indicator
```

Codex shows "nicht angemeldet":

```sh
codex login
```

Check the raw data the indicator sees:

```sh
echo '{"type":"control_request","request_id":"1","request":{"subtype":"get_usage"}}' \
  | claude --print --verbose --input-format stream-json --output-format stream-json | jq
```

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"p","title":"p","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}' \
  | codex app-server
```

## License

MIT — see [LICENSE](LICENSE).
