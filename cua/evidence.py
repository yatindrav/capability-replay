"""
Evidence recording.

Everything written here passes through redaction on the way out. The rule is
that evidence must be sufficient to debug a run and insufficient to reconstruct
a member's data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.safety.policy import redact_text


class EvidenceRecorder:
    def __init__(self, root: str | Path, run_id: str):
        self.dir = Path(root) / run_id
        # A run's evidence must describe exactly one run. `run.jsonl` is opened
        # in append mode and nothing used to clear this directory, so re-running
        # a fixed run-id — which the demo does, deliberately — concatenated
        # every attempt's log while `result.json` kept only the last. The log
        # and the result then disagreed about what happened, `_seq` restarted at
        # zero so the attempts could not even be told apart, and a screenshot
        # from a failed attempt sat beside a successful result as though it
        # belonged to it. Evidence that contradicts itself is worse than none.
        if self.dir.exists():
            for stale in self.dir.iterdir():
                if stale.is_file():
                    stale.unlink()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.log_path = self.dir / "run.jsonl"
        self._seq = 0

    def log(self, event: str, **fields: Any) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
        }
        for k, v in fields.items():
            record[k] = redact_text(v) if isinstance(v, str) else v
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def snapshot(self, name: str, tree: str) -> str:
        p = self.dir / f"{name}.a11y.txt"
        p.write_text(redact_text(tree), encoding="utf-8")
        return str(p)

    def screenshot(self, name: str, png: bytes | None) -> str | None:
        if png is None:
            return None
        p = self.dir / f"{name}.png"
        p.write_bytes(png)
        return str(p)

    def write_json(self, name: str, payload: Any) -> str:
        p = self.dir / f"{name}.json"
        p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return str(p)
