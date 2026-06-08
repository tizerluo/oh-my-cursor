#!/usr/bin/env python3
"""Spike: log subagentStart payloads for dispatch reliability."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".cursor" / "hooks" / "spikes" / "subagent-start.log"


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw or "{}")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "keys": sorted(data.keys()),
        "subagent_type": data.get("subagent_type") or data.get("subagentType"),
        "subagent_id": data.get("subagent_id") or data.get("subagentId"),
        "payload_preview": raw[:800],
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    proc = __import__("subprocess").run(
        ["python3", str(Path.home() / ".cursor" / "hooks" / "review_gate.py"), "set-role"],
        input=raw,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout or "{}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
