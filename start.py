#!/usr/bin/env python3
"""
Runs the Telegram bot and the whale monitor, and keeps them running.

The first version launched both processes and waited. When the bot died on a
network timeout, nothing noticed: the monitor kept scanning, the bot answered
nobody, and the product looked alive from the outside while every /start went
unanswered. A subscription service cannot fail that quietly, so this restarts
whichever child dies, with a backoff that stops a hard failure from spinning.
"""

import os
import sys
import time
import fcntl
import signal
import atexit
import logging
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [supervisor] %(message)s",
)
log = logging.getLogger(__name__)

CHILDREN = {
    "bot": [sys.executable, os.path.join(ROOT, "bot", "whale_bot.py")],
    "monitor": [sys.executable, os.path.join(ROOT, "data", "whale_monitor.py")],
}

BACKOFF_START = 5
BACKOFF_MAX = 300
HEALTHY_AFTER = 120  # a child that lasts this long has recovered

procs: dict[str, subprocess.Popen] = {}
backoff = {name: BACKOFF_START for name in CHILDREN}
started_at: dict[str, float] = {}
stopping = False


def spawn(name: str):
    procs[name] = subprocess.Popen(CHILDREN[name], cwd=ROOT)
    started_at[name] = time.time()
    log.info(f"{name} started (pid {procs[name].pid})")


def shutdown(signum, frame):
    global stopping
    stopping = True
    log.info("shutting down")
    for name, p in procs.items():
        if p.poll() is None:
            p.terminate()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Two supervisors means two bots polling Telegram, which means 409 conflicts
# and users whose messages land on whichever instance happens to win. One
# lock file, held for the life of the process, makes that impossible.
_lock_path = os.path.join(ROOT, ".supervisor.lock")
# Opened append-mode on purpose: "w" truncates before the lock is taken, so a
# rejected second instance would blank the running one's pid file. For the
# same reason the lock is only cleaned up by the process that actually won it.
_lock_file = open(_lock_path, "a+")
try:
    fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    _lock_file.seek(0)
    holder = _lock_file.read().strip() or "unknown"
    log.error(f"another supervisor is already running (pid {holder}), exiting")
    sys.exit(1)

_lock_file.seek(0)
_lock_file.truncate()
_lock_file.write(f"{os.getpid()}\n")
_lock_file.flush()
atexit.register(lambda: os.path.exists(_lock_path) and os.remove(_lock_path))

for name in CHILDREN:
    spawn(name)

while not stopping:
    time.sleep(2)
    for name, p in list(procs.items()):
        code = p.poll()
        if code is None:
            # Reset the backoff once it has proven it can stay up.
            if backoff[name] > BACKOFF_START and time.time() - started_at[name] > HEALTHY_AFTER:
                backoff[name] = BACKOFF_START
            continue

        wait = backoff[name]
        log.error(f"{name} exited with code {code}, restarting in {wait}s")
        time.sleep(wait)
        backoff[name] = min(wait * 2, BACKOFF_MAX)
        spawn(name)
