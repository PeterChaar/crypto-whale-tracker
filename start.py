#!/usr/bin/env python3
"""Single entry point that runs both the Telegram bot and whale monitor."""

import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

procs = [
    subprocess.Popen([sys.executable, os.path.join(ROOT, "bot", "whale_bot.py")]),
    subprocess.Popen([sys.executable, os.path.join(ROOT, "data", "whale_monitor.py")]),
]

try:
    for p in procs:
        p.wait()
except KeyboardInterrupt:
    for p in procs:
        p.terminate()
