"""Quick manual test for the Telegram /log command, without a full pipeline run.

Run it, then send "/log" to your bot from your chat while it idles:

    python test_log_bot.py

It arms the same crash-notifier/watchdog main.py uses, prints a few log lines
(so there is something to return), idles 120s, then exits cleanly. Bounded and
fail-fast: it never hangs — after 120s it sends the ✅ finish message and quits.
"""

import time

import core.notify as notify

notify.install_crash_notifier("log-bot test")

for i in range(3):
    print(f"[Test] example log line {i}", flush=True)
print("[Test] Now send /log to the bot from your chat. Idling 600s...", flush=True)

time.sleep(600)
notify.mark_success("log-bot test finished")
print("[Test] Done.", flush=True)
