#!/usr/bin/env python3
"""Spike: log stop hook invocations to verify followup_message support."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".cursor" / "hooks" / "spikes" / "stop-hook.log"


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw or "{}")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "keys": sorted(data.keys()),
        "payload_preview": raw[:500],
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    # Delegate to real stop-check; spike logs every invocation
    import subprocess

    proc = subprocess.run(
        ["python3", str(Path.home() / ".cursor" / "hooks" / "review_gate.py"), "stop-check"],
        input=raw,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout or "{}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
