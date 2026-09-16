#!/usr/bin/env python3
"""Tray indicator for Claude Code and OpenAI Codex usage limits.

Claude Code data comes from the CLI's own `get_usage` control request, so this
process never touches a credential: `claude` talks to Anthropic with its own.
Codex data is read passively from the rollout logs it writes under
~/.codex/sessions, which means it is only as fresh as the last Codex session.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Notify", "0.7")

import cairo  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk, Notify  # noqa: E402

APP_ID = "ai-usage-indicator"
CONFIG_DIR = Path(GLib.get_user_config_dir()) / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
ICON_DIR = Path(GLib.get_user_cache_dir()) / APP_ID / "icons"
AUTOSTART_FILE = Path(GLib.get_user_config_dir()) / "autostart" / f"{APP_ID}.desktop"

DEFAULT_CONFIG = {
    # How often to refresh, in seconds. Each Claude refresh spawns the CLI for
    # about 1.5s and costs no tokens.
    "poll_interval_seconds": 180,
    # Percentages at which to raise a desktop notification, per limit window.
    # Re-arms once the window resets.
    "thresholds": [80, 95, 100],
    "show_codex": True,
    # Codex figures older than this are shown greyed out and ignored for alerts.
    "codex_stale_minutes": 60,
    # Warn again about extra-credit burn at most this often.
    "extra_credit_notify_cooldown_seconds": 900,
    "claude_timeout_seconds": 60,
    "codex_timeout_seconds": 30,
}

GREEN = (0.18, 0.63, 0.26)
YELLOW = (0.82, 0.60, 0.13)
ORANGE = (0.91, 0.35, 0.05)
RED = (0.85, 0.21, 0.20)
GREY = (0.55, 0.55, 0.55)

# Blocks in the text bar drawn in each menu row.
BAR_SEGMENTS = 12

def severity_color(percent: float):
    if percent >= 95:
        return RED
    if percent >= 85:
        return ORANGE
    if percent >= 60:
        return YELLOW
    return GREEN


def render_bar(percent: float) -> str:
    """A block bar as plain text.

    AppIndicator exports the menu over DBus via libdbusmenu, which carries only
    an item's plain label: composite widgets and Pango markup are dropped. So
    every visual cue has to survive as text.
    """
    fraction = max(0.0, min(percent, 100.0)) / 100.0
    filled = int(round(fraction * BAR_SEGMENTS))
    if percent > 0:
        filled = max(filled, 1)
    return "\u2588" * filled + "\u2591" * (BAR_SEGMENTS - filled)


def severity_marker(percent: float) -> str:
    """Colour, carried by an emoji because markup cannot cross the DBus menu."""
    if percent >= 95:
        return "\U0001f534"
    if percent >= 85:
        return "\U0001f7e0"
    if percent >= 60:
        return "\U0001f7e1"
    return "\U0001f7e2"


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        config.update(json.loads(CONFIG_FILE.read_text()))
    except FileNotFoundError:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001 - a broken config must not be fatal
        print(f"[{APP_ID}] ignoring unreadable config: {exc}", file=sys.stderr)
    return config


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[{APP_ID}] could not persist state: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# data sources
# --------------------------------------------------------------------------

CONTROL_REQUEST = json.dumps(
    {"type": "control_request", "request_id": "1", "request": {"subtype": "get_usage"}}
)


def fetch_claude(timeout: int) -> dict:
    """Ask the Claude Code CLI for its own rate-limit view.

    Returns the `rate_limits` object. Raises on any failure.
    """
    proc = subprocess.run(
        [
            "claude",
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ],
        input=CONTROL_REQUEST + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path.home()),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300] or "claude failed")

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("type") != "control_response":
            continue
        response = message.get("response", {})
        if response.get("subtype") != "success":
            raise RuntimeError(str(response.get("error", "control request rejected"))[:300])
        payload = response.get("response", {})
        if not payload.get("rate_limits_available"):
            raise RuntimeError("no rate limit data (API key auth or third-party provider?)")
        return payload.get("rate_limits") or {}

    raise RuntimeError("no control_response in CLI output")


class CodexAuthError(RuntimeError):
    """Codex is installed but its stored token cannot be refreshed."""


CODEX_INIT = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "ai-usage-indicator",
                "title": "AI Usage Indicator",
                "version": "1.0.0",
            }
        },
    }
)
CODEX_LIMITS_REQUEST = {"jsonrpc": "2.0", "method": "account/rateLimits/read", "params": {}}


def _pick(source: dict, *names):
    """Read the first present key. The app-server uses camelCase, the rollout
    logs snake_case, and both describe the same windows."""
    for name in names:
        value = source.get(name)
        if value is not None:
            return value
    return None


def _find_windows(payload) -> dict | None:
    """Locate the {primary, secondary} object wherever the response nests it."""
    if not isinstance(payload, dict):
        return None
    if "primary" in payload or "secondary" in payload:
        return payload
    for key in ("rateLimits", "rate_limits", "limits", "result", "usage"):
        found = _find_windows(payload.get(key))
        if found:
            return found
    return None


def fetch_codex_live(timeout: int) -> dict:
    """Ask the Codex CLI's app-server for the account's rate limits.

    Like the Claude path, the CLI uses its own credential; this process never
    reads a token. The figures cover the whole ChatGPT account, so usage burned
    by other clients on the same account (Pi, for instance) is included.
    """
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        cwd=str(Path.home()),
    )
    deadline = time.monotonic() + timeout

    def send(request: str) -> None:
        proc.stdin.write(request + "\n")
        proc.stdin.flush()

    def await_id(wanted: int) -> dict:
        """Read responses until the one we asked for arrives."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("codex app-server timed out")
            if not select.select([proc.stdout], [], [], remaining)[0]:
                raise TimeoutError("codex app-server timed out")
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed the connection")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == wanted:
                return message

    try:
        # The request must follow a completed handshake, so wait for its reply
        # rather than writing both at once.
        send(CODEX_INIT)
        await_id(1)
        # Immediately after the handshake the server sometimes reports the
        # account as unauthenticated because it has not finished loading the
        # stored token. That clears within a moment, so give it one retry.
        message = None
        for attempt, request_id in enumerate((2, 3)):
            send(json.dumps(CODEX_LIMITS_REQUEST | {"id": request_id}))
            message = await_id(request_id)
            error = message.get("error")
            if not error or attempt:
                break
            if "authentication required" not in str(error.get("message", "")).lower():
                break
            time.sleep(0.5)
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    if "error" in message:
        detail = str(message["error"].get("message", ""))
        lowered = detail.lower()
        # A dead or missing token shows up either as a refresh failure or as a
        # 401 from the usage endpoint; both mean "run codex login".
        if any(
            marker in lowered
            for marker in ("refresh", "sign in", "log in", "401", "unauthorized", "authentica")
        ):
            raise CodexAuthError(detail)
        raise RuntimeError(detail[:200])

    windows = _find_windows(message.get("result"))
    if not windows:
        raise RuntimeError("no rate limit windows in app-server response")
    return {"limits": windows, "observed_at": time.time(), "live": True}


def _tail_lines(path: Path, max_bytes: int = 512 * 1024) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        chunk = handle.read()
    return chunk.decode("utf-8", errors="replace").splitlines()


def fetch_codex() -> dict | None:
    """Read the last rate-limit snapshot Codex wrote to its rollout logs.

    Codex has no non-interactive status command, so this is passive: the figures
    are only refreshed while Codex itself is running.
    """
    sessions = Path.home() / ".codex" / "sessions"
    if not sessions.is_dir():
        return None

    rollouts = sorted(
        (p for p in sessions.rglob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for rollout in rollouts[:5]:
        for line in reversed(_tail_lines(rollout)):
            if '"rate_limits"' not in line:
                continue
            try:
                limits = json.loads(line)["payload"]["rate_limits"]
            except Exception:  # noqa: BLE001 - skip malformed lines
                continue
            if not limits:
                continue
            return {"limits": limits, "observed_at": rollout.stat().st_mtime, "live": False}
    return None


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_age(minutes: float) -> str:
    if minutes >= 1440:
        return f"{minutes / 1440:.0f}d"
    if minutes >= 60:
        return f"{minutes / 60:.0f}h"
    return f"{minutes:.0f}m"


def format_reset(when: datetime | None) -> str:
    if when is None:
        return "kein Reset-Zeitpunkt"
    local = when.astimezone()
    remaining = (when - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return f"Reset fällig ({local:%H:%M})"
    hours, minutes = divmod(int(remaining) // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        countdown = f"{days}d {hours}h"
    elif hours:
        countdown = f"{hours}h {minutes}m"
    else:
        countdown = f"{minutes}m"
    stamp = f"{local:%H:%M}" if remaining < 18 * 3600 else f"{local:%a %H:%M}"
    return f"in {countdown} ({stamp})"


def normalise_claude(rate_limits: dict) -> dict:
    """Flatten the CLI payload into the windows we display."""
    windows = []

    labels = {
        "session": "5-Stunden-Fenster",
        "weekly_all": "Woche (alle Modelle)",
        "weekly_scoped": "Woche",
    }
    for limit in rate_limits.get("limits") or []:
        kind = limit.get("kind")
        name = labels.get(kind, kind or "Limit")
        scope = (limit.get("scope") or {}).get("model") or {}
        if kind == "weekly_scoped" and scope.get("display_name"):
            name = f"Woche ({scope['display_name']})"
        windows.append(
            {
                "key": f"claude:{kind}:{scope.get('display_name') or ''}",
                "name": name,
                "percent": float(limit.get("percent") or 0),
                "resets_at": parse_iso(limit.get("resets_at")),
                "is_session": kind == "session",
                "alerting": True,
            }
        )

    # Fall back to the flat fields if `limits` is ever absent.
    if not windows:
        for key, name, is_session in (
            ("five_hour", "5-Stunden-Fenster", True),
            ("seven_day", "Woche (alle Modelle)", False),
        ):
            entry = rate_limits.get(key)
            if not entry:
                continue
            windows.append(
                {
                    "key": f"claude:{key}:",
                    "name": name,
                    "percent": float(entry.get("utilization") or 0),
                    "resets_at": parse_iso(entry.get("resets_at")),
                    "is_session": is_session,
                    "alerting": True,
                }
            )

    extra = rate_limits.get("extra_usage") or {}
    credits = None
    if extra.get("is_enabled"):
        places = extra.get("decimal_places", 2)
        divisor = 10**places
        credits = {
            "used": (extra.get("used_credits") or 0) / divisor,
            "limit": (extra.get("monthly_limit") or 0) / divisor,
            "used_minor": extra.get("used_credits") or 0,
            "currency": extra.get("currency") or "USD",
            "percent": float(extra.get("utilization") or 0),
            "spend_limit_reached": bool(extra.get("spend_limit_reached")),
        }

    return {"windows": windows, "credits": credits}


def _codex_reset(value) -> datetime | None:
    """Rollout logs store epoch seconds, the app-server an ISO timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return parse_iso(value)


def normalise_codex(payload: dict, stale_minutes: int) -> dict:
    limits = payload["limits"]
    live = payload.get("live", False)
    age_minutes = (time.time() - payload["observed_at"]) / 60
    # Live readings are current by definition; only the log fallback can age out.
    stale = (not live) and age_minutes > stale_minutes

    windows = []
    for key in ("primary", "secondary"):
        entry = limits.get(key)
        if not entry:
            continue
        window_minutes = _pick(entry, "window_minutes", "windowMinutes", "windowDurationMins") or 0
        if window_minutes >= 1440:
            name = f"{round(window_minutes / 1440)}-Tage-Fenster"
        elif window_minutes:
            name = f"{round(window_minutes / 60)}-Stunden-Fenster"
        else:
            name = "5-Stunden-Fenster" if key == "primary" else "Wochen-Fenster"
        windows.append(
            {
                "key": f"codex:{key}",
                "name": name,
                "percent": float(_pick(entry, "used_percent", "usedPercent") or 0),
                "resets_at": _codex_reset(_pick(entry, "resets_at", "resetsAt")),
                "is_session": key == "primary",
                # Stale figures must never trigger an alert.
                "alerting": not stale,
            }
        )

    # Codex's answer to Claude's extra credits: a spend allowance with its own
    # reset. Only the app-server reports it; the rollout logs do not.
    quota = None
    individual = limits.get("individualLimit") or {}
    if individual.get("limit") is not None:
        try:
            quota = {
                "used": float(individual.get("used") or 0),
                "limit": float(individual["limit"]),
                "percent": 100.0 - float(individual.get("remainingPercent") or 0),
                "resets_at": _codex_reset(individual.get("resetsAt")),
            }
        except (TypeError, ValueError):
            quota = None

    return {
        "windows": windows,
        "stale": stale,
        "age_minutes": age_minutes,
        "live": live,
        "quota": quota,
        "spend_limit_reached": bool(limits.get("spendControlReached")),
    }


# --------------------------------------------------------------------------
# icon
# --------------------------------------------------------------------------


def render_icon(percent: float, colour, generation: int) -> str:
    """Draw a progress ring and return the icon name for the theme path."""
    size = 22
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    # AppIndicator only reloads when the icon *name* changes, so rotate names.
    name = f"usage-{generation % 8}"
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    centre = size / 2
    radius = centre - 2.5
    ctx.set_line_width(3.0)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)

    ctx.set_source_rgba(*GREY, 0.35)
    ctx.arc(centre, centre, radius, 0, 2 * 3.141592653589793)
    ctx.stroke()

    fraction = max(0.0, min(percent, 100.0)) / 100.0
    if fraction > 0:
        start = -3.141592653589793 / 2
        ctx.set_source_rgb(*colour)
        ctx.arc(centre, centre, radius, start, start + fraction * 2 * 3.141592653589793)
        ctx.stroke()

    if percent >= 100:
        # Solid centre dot: the window is exhausted.
        ctx.set_source_rgb(*colour)
        ctx.arc(centre, centre, 3.0, 0, 2 * 3.141592653589793)
        ctx.fill()

    surface.write_to_png(str(ICON_DIR / f"{name}.png"))
    return name


# --------------------------------------------------------------------------
# indicator
# --------------------------------------------------------------------------


class UsageIndicator:
    def __init__(self, config: dict):
        self.config = config
        self.state = load_state()
        self.generation = 0
        self.snapshot: dict = {
            "claude": None,
            "codex": None,
            "codex_hint": None,
            "error": None,
            "updated_at": None,
        }
        self._stop = threading.Event()
        self._wake = threading.Event()

        ICON_DIR.mkdir(parents=True, exist_ok=True)
        Notify.init("AI Usage")

        self.indicator = AppIndicator.Indicator.new(
            APP_ID,
            "dialog-information",
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_icon_theme_path(str(ICON_DIR))
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self.rebuild_menu()

        self.worker = threading.Thread(target=self._poll_loop, daemon=True)
        self.worker.start()

    # -- polling ----------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            snapshot = {
                "claude": None,
                "codex": None,
                "codex_hint": None,
                "error": None,
                "updated_at": time.time(),
            }
            try:
                snapshot["claude"] = normalise_claude(
                    fetch_claude(self.config["claude_timeout_seconds"])
                )
            except Exception as exc:  # noqa: BLE001 - surface in the menu, keep running
                snapshot["error"] = str(exc)

            if self.config.get("show_codex", True):
                raw = None
                try:
                    raw = fetch_codex_live(self.config["codex_timeout_seconds"])
                except CodexAuthError:
                    # Expected when the Codex CLI has never been logged in on this
                    # machine, or its refresh token expired. Fall back to the logs.
                    snapshot["codex_hint"] = (
                        "nicht angemeldet \u2014 `codex login` f\u00fcr Live-Werte"
                    )
                except FileNotFoundError:
                    snapshot["codex_hint"] = "codex nicht installiert"
                except Exception as exc:  # noqa: BLE001
                    snapshot["codex_hint"] = f"Live-Abfrage fehlgeschlagen: {exc}"

                if raw is None:
                    try:
                        raw = fetch_codex()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[{APP_ID}] codex log read failed: {exc}", file=sys.stderr)

                if raw:
                    snapshot["codex"] = normalise_codex(raw, self.config["codex_stale_minutes"])

            GLib.idle_add(self._apply_snapshot, snapshot)
            self._wake.wait(timeout=self.config["poll_interval_seconds"])
            self._wake.clear()

    def refresh_now(self, *_args) -> None:
        self._wake.set()

    # -- UI ---------------------------------------------------------------

    def _apply_snapshot(self, snapshot: dict) -> bool:
        self.snapshot = snapshot
        self._check_alerts(snapshot)
        self.update_icon()
        self.rebuild_menu()
        return False

    def _all_windows(self) -> list[dict]:
        windows = []
        for source in ("claude", "codex"):
            data = self.snapshot.get(source)
            if data:
                windows.extend(data["windows"])
        return windows

    def update_icon(self) -> None:
        claude = self.snapshot.get("claude")
        codex = self.snapshot.get("codex")

        if self.snapshot.get("error") and not claude:
            self.indicator.set_icon_full("dialog-warning", "AI Usage: Fehler")
            self.indicator.set_label("AI ?", "AI 00%")
            return

        alerting = [w["percent"] for w in self._all_windows() if w["alerting"]]
        worst = max(alerting) if alerting else 0.0

        self.generation += 1
        name = render_icon(worst, severity_color(worst), self.generation)
        self.indicator.set_icon_full(name, f"AI Usage: {worst:.0f}%")

        parts = []
        if claude:
            session = next((w for w in claude["windows"] if w["is_session"]), None)
            weekly = next(
                (w for w in claude["windows"] if not w["is_session"] and "alle" in w["name"]), None
            )
            if session:
                parts.append(f"{session['percent']:.0f}%")
            if weekly:
                parts.append(f"{weekly['percent']:.0f}%")
        if codex and not codex["stale"]:
            primary = next((w for w in codex["windows"] if w["is_session"]), None)
            if primary:
                parts.append(f"cx {primary['percent']:.0f}%")

        label = " · ".join(parts) if parts else "n/a"
        self.indicator.set_label(label, "100% · 100% · cx 100%")

    def rebuild_menu(self) -> None:
        for child in self.menu.get_children():
            self.menu.remove(child)

        claude = self.snapshot.get("claude")
        codex = self.snapshot.get("codex")
        error = self.snapshot.get("error")

        self._heading("Claude Code")
        if error:
            self._note(f"\u26a0  {error}")
        elif claude:
            for window in claude["windows"]:
                self._gauge_row(window["name"], window["percent"], format_reset(window["resets_at"]))
            credits = claude.get("credits")
            if credits:
                self.menu.append(Gtk.SeparatorMenuItem())
                symbol = "\u20ac" if credits["currency"] == "EUR" else credits["currency"]
                self._gauge_row(
                    "Extra Credits",
                    credits["percent"],
                    f"{credits['used']:.2f} / {credits['limit']:.2f} {symbol}",
                )
                if credits["spend_limit_reached"]:
                    self._note("\u26a0  Ausgabelimit erreicht")
        else:
            self._note("l\u00e4dt \u2026")

        if self.config.get("show_codex", True):
            self.menu.append(Gtk.SeparatorMenuItem())
            hint = self.snapshot.get("codex_hint")
            if codex:
                stale = codex["stale"]
                if stale:
                    suffix = f"  \u2014  veraltet, vor {format_age(codex['age_minutes'])}"
                else:
                    suffix = "" if codex.get("live") else "  \u2014  aus Session-Logs"
                self._heading("OpenAI Codex" + suffix)
                for window in codex["windows"]:
                    self._gauge_row(
                        window["name"],
                        window["percent"],
                        format_reset(window["resets_at"]),
                        dim=stale,
                    )
                quota = codex.get("quota")
                if quota:
                    self._gauge_row(
                        "Individuelles Limit",
                        quota["percent"],
                        f"{quota['used']:.0f} / {quota['limit']:.0f}"
                        f"  \u00b7  {format_reset(quota['resets_at'])}",
                        dim=stale,
                    )
                if codex.get("spend_limit_reached"):
                    self._note("\u26a0  Ausgabelimit erreicht")
                if hint:
                    self._note(hint)
            else:
                self._heading("OpenAI Codex")
                self._note(hint or "keine Daten gefunden")

        self.menu.append(Gtk.SeparatorMenuItem())
        updated = self.snapshot.get("updated_at")
        if updated:
            self._note(f"Aktualisiert {datetime.fromtimestamp(updated):%H:%M:%S}")
        self._action("Jetzt aktualisieren", self.refresh_now)

        autostart = Gtk.CheckMenuItem(label="Beim Anmelden starten")
        autostart.set_active(AUTOSTART_FILE.exists())
        autostart.connect("toggled", self.on_autostart_toggled)
        self.menu.append(autostart)

        self._action("Einstellungen \u00f6ffnen", self.on_open_config)
        self._action("Beenden", self.on_quit)
        self.menu.show_all()

    # -- menu building ----------------------------------------------------

    def _heading(self, text: str) -> None:
        """Section title. Deliberately insensitive: grey reads as a header."""
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        self.menu.append(item)

    def _note(self, text: str) -> None:
        item = Gtk.MenuItem(label=f"    {text}")
        self.menu.append(item)

    def _gauge_row(self, name: str, percent: float, trailing: str, dim: bool = False) -> None:
        """One limit window.

        Everything before the name is fixed width, so the rows line up even in
        the menu's proportional font.
        """
        marker = "\u26aa" if dim else severity_marker(percent)
        item = Gtk.MenuItem(
            label=f"  {marker}  {percent:3.0f} %  {render_bar(percent)}   {name} \u00b7 {trailing}"
        )
        # Sensitive on purpose: an insensitive item is drawn in the theme's
        # disabled colour, which greyed out every figure.
        self.menu.append(item)

    def _action(self, text: str, handler) -> None:
        item = Gtk.MenuItem(label=text)
        item.connect("activate", handler)
        self.menu.append(item)

    # -- notifications ----------------------------------------------------

    def _notify(self, summary: str, body: str, urgent: bool = False) -> None:
        note = Notify.Notification.new(summary, body, "utilities-system-monitor")
        note.set_urgency(Notify.Urgency.CRITICAL if urgent else Notify.Urgency.NORMAL)
        try:
            note.show()
        except Exception as exc:  # noqa: BLE001
            print(f"[{APP_ID}] notification failed: {exc}", file=sys.stderr)

    def _check_alerts(self, snapshot: dict) -> None:
        fired = self.state.setdefault("fired", {})
        dirty = False

        for window in self._windows_of(snapshot):
            if not window["alerting"]:
                continue
            # Keying on the reset timestamp re-arms the alert for the next window.
            stamp = window["resets_at"].isoformat() if window["resets_at"] else "none"
            key = f"{window['key']}@{stamp}"
            already = set(fired.get(key, []))
            for threshold in sorted(self.config["thresholds"]):
                if window["percent"] >= threshold and threshold not in already:
                    already.add(threshold)
                    dirty = True
                    source = "Codex" if window["key"].startswith("codex") else "Claude Code"
                    if threshold >= 100:
                        self._notify(
                            f"{source}: {window['name']} erschöpft",
                            f"100 % verbraucht – {format_reset(window['resets_at'])}.",
                            urgent=True,
                        )
                    else:
                        self._notify(
                            f"{source}: {window['name']} bei {window['percent']:.0f} %",
                            f"Schwelle {threshold} % überschritten – "
                            f"Reset {format_reset(window['resets_at'])}.",
                            urgent=threshold >= 95,
                        )
            if already:
                fired[key] = sorted(already)

        # Prune keys for windows that have already reset.
        live = {
            f"{w['key']}@{w['resets_at'].isoformat() if w['resets_at'] else 'none'}"
            for w in self._windows_of(snapshot)
        }
        for stale_key in [k for k in fired if k not in live]:
            del fired[stale_key]
            dirty = True

        dirty = self._check_credit_burn(snapshot) or dirty
        if dirty:
            save_state(self.state)

    @staticmethod
    def _windows_of(snapshot: dict) -> list[dict]:
        windows = []
        for source in ("claude", "codex"):
            data = snapshot.get(source)
            if data:
                windows.extend(data["windows"])
        return windows

    def _check_credit_burn(self, snapshot: dict) -> bool:
        claude = snapshot.get("claude")
        credits = claude.get("credits") if claude else None
        if not credits:
            return False

        previous = self.state.get("credits_used_minor")
        now = time.time()
        self.state["credits_used_minor"] = credits["used_minor"]

        if previous is None or credits["used_minor"] <= previous:
            return True

        last_notify = self.state.get("credits_notified_at", 0)
        cooldown = self.config["extra_credit_notify_cooldown_seconds"]
        if now - last_notify < cooldown:
            return True

        symbol = "€" if credits["currency"] == "EUR" else credits["currency"]
        delta = (credits["used_minor"] - previous) / 100
        self.state["credits_notified_at"] = now
        self._notify(
            "Claude Code verbraucht Extra Credits",
            f"+{delta:.2f} {symbol} seit der letzten Prüfung – "
            f"insgesamt {credits['used']:.2f} von {credits['limit']:.2f} {symbol} "
            f"({credits['percent']:.0f} %).",
            urgent=True,
        )
        return True

    # -- menu actions -----------------------------------------------------

    def on_autostart_toggled(self, item: Gtk.CheckMenuItem) -> None:
        if item.get_active():
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            script = Path(__file__).resolve()
            AUTOSTART_FILE.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=AI Usage Indicator\n"
                f"Exec={sys.executable} {script}\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)

    def on_open_config(self, *_args) -> None:
        if not CONFIG_FILE.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        subprocess.Popen(["xdg-open", str(CONFIG_FILE)])

    def on_quit(self, *_args) -> None:
        self._stop.set()
        self._wake.set()
        save_state(self.state)
        Notify.uninit()
        Gtk.main_quit()


def main() -> int:
    indicator = UsageIndicator(load_config())
    try:
        Gtk.main()
    except KeyboardInterrupt:
        indicator.on_quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
