from __future__ import annotations

import json

from lockstep.commands.status import stale_lines


def test_active_missing_lock_marks_index_stale(cfg):
    cfg.lock_file.write_text(json.dumps({"TCore.missing": {"type_hash": "abc"}}))

    stale = stale_lines(cfg, [])

    assert len(stale) == 1
    assert "1 node(s)" in stale[0]
    assert "missing" in stale[0]


def test_retired_missing_lock_does_not_mark_index_stale(cfg):
    cfg.lock_file.write_text(
        json.dumps({"TCore.retired": {"type_hash": "abc", "retired": "2026-09-16"}})
    )

    assert stale_lines(cfg, []) == []
