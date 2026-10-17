"""Human/JSON output helpers."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    exit_code: int = 0
    payload: Any = None
    human: list[str] = field(default_factory=list)

    def line(self, text: str = "") -> None:
        self.human.append(text)


def table(rows: list[list[Any]], headers: list[str] | None = None) -> list[str]:
    if not rows and not headers:
        return []
    data = [[("" if c is None else str(c)) for c in r] for r in rows]
    if headers:
        data = [headers] + data
    widths = [max(len(r[i]) for r in data) for i in range(len(data[0]))]
    lines = []
    for idx, r in enumerate(data):
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        if headers and idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return lines


def emit(result: Result, json_mode: bool, stream=None) -> None:
    stream = stream or sys.stdout
    if json_mode:
        payload = result.payload if result.payload is not None else {}
        if isinstance(payload, dict) and "ok" not in payload:
            payload = {"ok": result.exit_code == 0, **payload}
        stream.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        for line in result.human:
            stream.write(line + "\n")
