"""lockstep: a blueprint harness whose project state is derived from the compiled Lean environment.

Nothing in this package may name a project-specific module, theorem, or path;
all of that comes from ``harness/config.toml`` in the project root.
"""

from pathlib import Path

__version__ = "0.1.0"

# Lean templates and drivers shipped with the package (package data, so a wheel install works)
DATA = Path(__file__).resolve().parent / "data"

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2
EXIT_LEAN = 3
