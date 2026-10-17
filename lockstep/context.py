"""Per-invocation context: configuration, agent identity, output mode."""

from __future__ import annotations

import getpass
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from lockstep.config import Config, load_config


def default_agent() -> str:
    env = os.environ.get("LOCKSTEP_AGENT")
    if env:
        return env
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover
        user = "user"
    return f"{user}@{socket.gethostname()}:{os.getppid()}"


@dataclass
class Ctx:
    cfg: Config
    json_mode: bool = False
    agent: str = field(default_factory=default_agent)

    @classmethod
    def load(cls, root: Path | None = None, json_mode: bool = False) -> "Ctx":
        return cls(cfg=load_config(root), json_mode=json_mode)
