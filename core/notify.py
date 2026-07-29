"""Telegram notifications for pipeline runs: crash, kill, stop, or clean finish.

Stdlib-only (safe to import before runtime_setup). Credentials live in
notify_config.json next to this file — it is gitignored, never commit it:

    {
      "telegram_bot_token": "123456789:AAF...your-token...",
      "telegram_chat_id": "123456789"
    }

How it works
------------
`install_crash_notifier(label)` spawns a tiny detached watchdog process that
polls whether the pipeline process is still alive. When running inside a
run_safe.sh memory cgroup (CAP_MEM_BUDGET_GB set) the watchdog is launched in
its own systemd scope so a group OOM kill or `systemctl stop` of the pipeline
scope cannot take the watchdog down with it — that is exactly the moment it
must survive to report the death.

Three exit paths, three reporters:
  - clean finish        -> main.py calls mark_success() (in-process send);
  - Python exception /
    KeyboardInterrupt   -> main.py calls notify_exception() (in-process send,
                           includes the traceback tail);
  - anything else (OOM SIGKILL, terminal crash, kill -9, machine issue)
                        -> the process disappears without writing the sentinel
                           file, and the WATCHDOG sends the alert.

The sentinel file is how the in-process side tells the watchdog "already
reported, stand down". Sends are single-attempt with a 10s timeout and never
raise: a notification failure must not hang or crash the pipeline.

A user Ctrl+C is intentional, so main.py calls mark_interrupted() — it stands
the watchdog down (writes the sentinel) but sends NO message.

Interactive command: while the run is live, sending "/log" to the bot from the
configured chat replies with the last lines the terminal printed. The watchdog
polls getUpdates for this; the pipeline mirrors its stdout/stderr to a small
file (last ~40 completed lines) that the watchdog tails.

CLI:
    python notify.py --test               # send a test message
    python notify.py --discover-chat-id   # print chat ids seen by the bot
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "notify_config.json"
_POLL_SECONDS = 2.0
_SEND_TIMEOUT_S = 10

# Set by install_crash_notifier(); None means notifications are not armed and
# mark_success()/notify_exception() are no-ops.
_sentinel_path: str | None = None
_label: str = ""


def load_notify_config() -> dict:
    """Read and validate notify_config.json. Raises with a clear message if
    missing/incomplete — call this at startup so a bad setup fails fast, not
    at crash time when the message is actually needed."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Telegram notify config not found: {_CONFIG_PATH}\n"
            "Create it (it is gitignored) with:\n"
            '  {"telegram_bot_token": "<token from @BotFather>", '
            '"telegram_chat_id": "<your chat id>"}\n'
            "Then verify with: python notify.py --test"
        )
    for key in ("telegram_bot_token", "telegram_chat_id"):
        if not cfg.get(key):
            raise KeyError(f"{_CONFIG_PATH} is missing or has an empty '{key}'")
        # Trailing whitespace here silently breaks the chat-id match used by /log
        # (Telegram trims it server-side for sends, so it looks fine outbound).
        if isinstance(cfg[key], str):
            cfg[key] = cfg[key].strip()
    return cfg


def _format_message(label: str, body: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"[{socket.gethostname()}] {label}\n{stamp}\n{body}"


def send_telegram_message(text: str) -> bool:
    """Send one message. Single attempt, bounded timeout, never raises."""
    try:
        cfg = load_notify_config()
        url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
        # Telegram caps messages at 4096 chars.
        data = urllib.parse.urlencode(
            {"chat_id": cfg["telegram_chat_id"], "text": text[:4000]}
        ).encode()
        with urllib.request.urlopen(url, data=data, timeout=_SEND_TIMEOUT_S) as resp:
            if not 200 <= resp.status < 300:
                print(f"[Notify] Telegram API returned HTTP {resp.status}", flush=True)
                return False
        return True
    except Exception as exc:
        print(f"[Notify] Failed to send Telegram message: {exc}", flush=True)
        return False


# ── Terminal log mirror (for the /log bot command) ───────────────────────────
#
# So the watchdog can answer a /log query with what the terminal last showed, we
# mirror the last few *completed* log lines to a small file. Design constraints:
#   - The terminal must be untouched: every write still goes straight through.
#   - The hot loops must not pay for it: completed lines (newline) are cheap and
#     infrequent; a tqdm progress bar redraws with '\r' (no newline) many times a
#     second, so its live state is mirrored as a throttled "partial" last line
#     (at most once per _PARTIAL_MIN_INTERVAL) — /log shows where the bar is now,
#     without a file write per redraw.

_PARTIAL_MIN_INTERVAL = 1.0  # seconds; cap on how often the live bar is mirrored


class _LogMirror:
    """Shared sink: a bounded ring of the last completed lines plus the current
    in-progress line (e.g. a live progress bar), flushed to a small file. One
    instance is shared by the stdout and stderr wrappers."""

    def __init__(self, path: str, keep: int = 40) -> None:
        self._path = path
        self._lines: collections.deque[str] = collections.deque(maxlen=keep)
        self._partial = ""
        self._last_partial_write = 0.0
        self._lock = threading.Lock()

    def _write_file(self) -> None:
        rows = list(self._lines)
        if self._partial:
            rows.append(self._partial)
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rows))
            os.replace(tmp, self._path)
        except OSError:
            pass  # mirroring is best-effort; never disturb the pipeline

    def add_line(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._partial = ""  # the completed line supersedes any live bar
            self._write_file()

    def set_partial(self, text: str) -> None:
        """Mirror the current unfinished line (a live progress bar), throttled."""
        now = time.monotonic()
        with self._lock:
            self._partial = text
            if now - self._last_partial_write < _PARTIAL_MIN_INTERVAL:
                return
            self._last_partial_write = now
            self._write_file()


class _TeeStream:
    """Wraps a text stream: forwards everything to the real stream, and feeds the
    shared mirror. '\\r' resets the current line (matches terminal overwrite
    semantics), '\\n' commits it; between the two, the in-progress line is mirrored
    as a throttled partial so /log can show a live progress bar."""

    def __init__(self, stream, mirror: _LogMirror) -> None:
        self._stream = stream
        self._mirror = mirror
        self._cur = ""

    def write(self, text: str) -> int:
        n = self._stream.write(text)
        for ch in text:
            if ch == "\n":
                self._mirror.add_line(self._cur)
                self._cur = ""
            elif ch == "\r":
                self._cur = ""
            else:
                self._cur += ch
        if self._cur:  # an unfinished line remains (e.g. a redrawing progress bar)
            self._mirror.set_partial(self._cur)
        return n

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):  # isatty/fileno/encoding/... for tqdm & friends
        return getattr(self._stream, name)


def _install_log_mirror(path: str) -> None:
    """Tee stdout and stderr through a shared mirror. Safe no-op on failure."""
    try:
        mirror = _LogMirror(path)
        sys.stdout = _TeeStream(sys.stdout, mirror)
        sys.stderr = _TeeStream(sys.stderr, mirror)
    except Exception as exc:  # never let instrumentation break the run
        print(f"[Notify] Could not install log mirror: {exc}", flush=True)


def _own_cgroup_dir() -> str | None:
    """Cgroup-v2 directory of the calling process (the pipeline), so the watchdog
    can read its memory.current/memory.max from outside. None if unavailable."""
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("0::"):
                    path = "/sys/fs/cgroup" + line.split("::", 1)[1].strip()
                    return path if os.path.isdir(path) else None
    except OSError:
        pass
    return None


def install_crash_notifier(label: str) -> None:
    """Arm notifications for this process: validate the config, then spawn the
    detached watchdog. Call once, early, in the final (post-re-exec) process."""
    global _sentinel_path, _label
    if _sentinel_path is not None:
        return  # already armed

    load_notify_config()  # fail fast on a broken setup

    _label = label
    _notify_dir = tempfile.mkdtemp(prefix="cap_notify_")
    _sentinel_path = os.path.join(_notify_dir, "status")

    # Mirror the terminal so the watchdog can answer /log with the latest output.
    log_path = os.path.join(_notify_dir, "pipeline.log")
    _install_log_mirror(log_path)

    watchdog_argv = [
        sys.executable,
        os.path.abspath(__file__),
        "--watchdog",
        str(os.getpid()),
        _sentinel_path,
        label,
        _own_cgroup_dir() or "-",
        log_path,
    ]

    # Preferred: a transient systemd --user SERVICE (no --scope). The watchdog is
    # then spawned by the user manager itself — no process-tree or cgroup link to
    # the pipeline whatsoever — so a scope stop, group OOM kill, or terminal death
    # cannot take it down with the pipeline. (An earlier version used
    # `systemd-run --scope`, but that leaves a wrapper process inside the
    # pipeline's cgroup: stopping the pipeline scope SIGTERMed the wrapper and
    # watchdog in the same teardown storm they were supposed to survive.)
    # Its output lands in the journal: journalctl --user -u <unit>.
    unit = f"cap-notify-watchdog-{os.getpid()}"
    if shutil.which("systemd-run"):
        cmd = [
            "systemd-run", "--user", "--collect", "--quiet",
            "--unit", unit,
            "-p", "MemoryMax=128M",
        ] + watchdog_argv
        try:
            subprocess.run(cmd, check=True, timeout=15)
            print(
                f"[Notify] Crash notifier armed (unit {unit}; "
                f"logs: journalctl --user -u {unit}).",
                flush=True,
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"[Notify] systemd-run failed ({exc}); using a plain watchdog process.", flush=True)

    # Fallback (no systemd, e.g. Windows/macOS or a sandbox): plain detached child.
    # Log to a file instead of DEVNULL so a failed send is diagnosable after the fact.
    log_path = os.path.join(os.path.dirname(_sentinel_path), "watchdog.log")
    log_handle = open(log_path, "ab")
    popen_kwargs: dict = {"stdout": log_handle, "stderr": subprocess.STDOUT}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(watchdog_argv, **popen_kwargs)
    log_handle.close()
    print(f"[Notify] Crash notifier armed (watchdog log: {log_path}).", flush=True)


def _write_sentinel(status: str) -> None:
    if _sentinel_path is None:
        return
    try:
        with open(_sentinel_path, "w", encoding="utf-8") as fh:
            fh.write(status)
    except OSError as exc:
        print(f"[Notify] Could not write sentinel file: {exc}", flush=True)


def mark_success(detail: str = "") -> None:
    """Record a clean finish (watchdog stands down) and send a success message.
    No-op if install_crash_notifier() was never called."""
    if _sentinel_path is None:
        return
    _write_sentinel("ok")
    body = "✅ Finished successfully."
    if detail:
        body += f"\n{detail}"
    send_telegram_message(_format_message(_label, body))


def mark_interrupted() -> None:
    """User pressed Ctrl+C: stand the watchdog down but send NO Telegram
    message — an intentional interrupt is not worth a notification.
    No-op if install_crash_notifier() was never called."""
    if _sentinel_path is None:
        return
    _write_sentinel("reported")


def notify_exception(traceback_text: str) -> None:
    """Report an in-process failure (exception) with the traceback tail, and
    tell the watchdog it is already handled.
    No-op if install_crash_notifier() was never called."""
    if _sentinel_path is None:
        return
    _write_sentinel("reported")
    tail = traceback_text.strip()[-1500:]
    send_telegram_message(
        _format_message(_label, f"❌ Stopped with an exception:\n{tail}")
    )


# ── Watchdog process ─────────────────────────────────────────────────────────


def _read_log_tail(log_path: str, max_lines: int = 25) -> str:
    """Last few completed terminal lines the mirror captured, or a note if none."""
    if log_path in ("-", ""):
        return "(log mirror not available)"
    try:
        with open(log_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return "(no log output captured yet)"
    if not lines:
        return "(no log output captured yet)"
    return "\n".join(lines[-max_lines:])


def _serve_bot_commands(offset: int | None, log_path: str, label: str,
                        respond: bool = True) -> int | None:
    """Poll getUpdates once and answer /log from the configured chat. Returns the
    new update offset. Single short-poll attempt, never raises: a bot hiccup must
    not delay death detection or crash the watchdog. With respond=False it only
    drains the backlog (so a stale /log from before the run isn't answered)."""
    try:
        cfg = load_notify_config()
        token = cfg["telegram_bot_token"]
        chat_id = str(cfg["telegram_chat_id"])
    except Exception:
        return offset
    params: dict = {"timeout": 0, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=_SEND_TIMEOUT_S) as resp:
            data = json.load(resp)
    except Exception:
        return offset
    for update in data.get("result", []):
        offset = update["update_id"] + 1
        if not respond:
            continue
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat_id:
            continue  # ignore anyone but the configured chat
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@", 1)[0].lower() if text else ""
        if cmd in ("/log", "/tail", "log"):
            send_telegram_message(
                _format_message(label, f"🪵 Last log lines:\n{_read_log_tail(log_path)}")
            )
    return offset


def _watchdog_main(pid: int, sentinel: str, label: str, cgroup_dir: str = "-",
                   log_path: str = "-") -> None:
    def pipeline_alive() -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not ours — treat as alive

    def already_reported() -> bool:
        return os.path.exists(sentinel)

    def cleanup() -> None:
        try:
            os.remove(sentinel)
            os.rmdir(os.path.dirname(sentinel))
        except OSError:
            pass

    # If something tears the watchdog itself down while the pipeline is still
    # unreported (e.g. scope/session teardown reaching us anyway), report before
    # dying: a possibly-duplicate message beats silence.
    def on_term(signum: int, _frame) -> None:
        if not already_reported():
            send_telegram_message(
                _format_message(
                    label,
                    f"⚠️ Watchdog received signal {signum} while the pipeline "
                    "was still running — the whole session is being torn down "
                    "(terminal closed, logout, or scope stop).",
                )
            )
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, on_term)

    def read_cgroup_memory() -> tuple[int, int] | None:
        """(current, max) bytes of the pipeline's cgroup, or None if unreadable
        or uncapped. Non-invasive: just reads two kernel counters."""
        if cgroup_dir in ("-", ""):
            return None
        try:
            with open(os.path.join(cgroup_dir, "memory.current"), encoding="utf-8") as fh:
                current = int(fh.read())
            with open(os.path.join(cgroup_dir, "memory.max"), encoding="utf-8") as fh:
                limit = fh.read().strip()
            return (current, int(limit)) if limit != "max" else None
        except (OSError, ValueError):
            return None

    # Throttle alert: a run pinned near its cgroup memory cap gets reclaim-throttled
    # by the kernel — from the terminal it looks exactly like a hang, and nothing
    # dies, so death detection alone would stay silent for hours. Warn once when
    # memory sits at >=95% of the cap for 2+ minutes.
    pressure_since: float | None = None
    pressure_warned = False

    # Drain any backlog first so a /log sent before the run isn't answered now.
    cmd_offset = _serve_bot_commands(None, log_path, label, respond=False)

    while pipeline_alive():
        time.sleep(_POLL_SECONDS)

        # Answer interactive bot commands (e.g. /log) — every iteration.
        cmd_offset = _serve_bot_commands(cmd_offset, log_path, label)

        if pressure_warned:
            continue
        mem = read_cgroup_memory()
        if mem is None or mem[0] < 0.95 * mem[1]:
            pressure_since = None
            continue
        if pressure_since is None:
            pressure_since = time.monotonic()
        elif time.monotonic() - pressure_since > 120:
            current_gb, max_gb = mem[0] / 1024**3, mem[1] / 1024**3
            send_telegram_message(
                _format_message(
                    label,
                    f"⚠️ Memory at {current_gb:.1f}G of the {max_gb:.1f}G cgroup cap "
                    "for 2+ minutes — the run is reclaim-throttled and will look "
                    "hung in the terminal. It may be OOM-killed at the cap soon.",
                )
            )
            pressure_warned = True

    if not already_reported():
        send_telegram_message(
            _format_message(
                label,
                f"💀 Pipeline process (pid {pid}) disappeared WITHOUT a clean "
                "exit — killed by the OOM/cgroup limit, a hard crash "
                "(SIGSEGV/SIGILL), kill -9, or the terminal dying.",
            )
        )
    cleanup()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _discover_chat_id() -> None:
    """Print the chat ids of everyone who has messaged the bot recently.
    Requires only the token in notify_config.json (chat_id may still be empty)."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            token = json.load(fh).get("telegram_bot_token", "")
    except FileNotFoundError:
        token = ""
    if not token:
        sys.exit(
            f"Put your bot token in {_CONFIG_PATH} first "
            '({"telegram_bot_token": "...", "telegram_chat_id": ""}).'
        )
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    with urllib.request.urlopen(url, timeout=_SEND_TIMEOUT_S) as resp:
        updates = json.load(resp)
    chats = {}
    for update in updates.get("result", []):
        chat = (update.get("message") or update.get("edited_message") or {}).get("chat")
        if chat:
            name = chat.get("username") or chat.get("first_name") or chat.get("title", "?")
            chats[chat["id"]] = name
    if not chats:
        print(
            "No chats found. Open Telegram, send any message to your bot "
            "(e.g. /start), then run this again."
        )
        return
    for chat_id, name in chats.items():
        print(f"chat_id: {chat_id}  ({name})")
    print(f'\nPut the id into {_CONFIG_PATH} as "telegram_chat_id".')


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--watchdog"] and len(args) in (4, 5, 6):
        _watchdog_main(
            int(args[1]), args[2], args[3],
            args[4] if len(args) >= 5 else "-",
            args[5] if len(args) == 6 else "-",
        )
    elif args[:1] == ["--test"]:
        ok = send_telegram_message(
            _format_message("notify.py", "🔔 Test message — Telegram notifications work.")
        )
        print("Sent." if ok else "FAILED — see the error above.")
        sys.exit(0 if ok else 1)
    elif args[:1] == ["--discover-chat-id"]:
        _discover_chat_id()
    else:
        sys.exit(
            "Usage:\n"
            "  python notify.py --test\n"
            "  python notify.py --discover-chat-id\n"
            "  python notify.py --watchdog <pid> <sentinel> <label> [cgroup-dir] [log-path]   (internal)"
        )
