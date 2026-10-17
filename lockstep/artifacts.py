"""Immutable content-addressed results shared by CLI, worker and client adapters."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from lockstep.config import primary_root
from lockstep.errors import UsageError
from lockstep.errors import ReadOnly
from lockstep.store import Store


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(root: Path, prefix: str) -> Path:
    # The hierarchy is deliberately fixed and bounded beneath the primary
    # checkout.  Sync every parent link even when another writer created it;
    # that writer may have stopped before making its mkdir durable.
    hierarchy = (root / 'runs', root / 'runs' / 'lockstep-artifacts',
                 root / 'runs' / 'lockstep-artifacts' / prefix)
    for path in hierarchy:
        path.mkdir(exist_ok=True)
        # Persist each name in its containing directory before an artifact
        # referring to that directory can be committed to SQLite.
        _fsync_directory(path.parent)
    return hierarchy[-1]


def put(cfg, data: bytes, kind: str = 'application/json') -> dict:
    ident = hashlib.sha256(data).hexdigest()
    root = primary_root(cfg.root)
    with Store(cfg) as st:
        # Read-only commands may still have a writable checkout.  Refuse before
        # publishing a file so compact() can preserve the full read result.
        st._writable()
        directory = _mkdir_durable(root, ident[:2])
        path = directory / ident
        if path.exists():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != ident:
                raise UsageError('artifact store content mismatch')
        else:
            fd, temp = tempfile.mkstemp(dir=directory, prefix='.artifact-')
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)
        # This also makes a valid orphan from an interrupted earlier attempt
        # durable before it is indexed on retry.
        _fsync_directory(directory)
        st.conn.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)',
                        (ident, str(path), len(data), kind, time.time()))
    return {'id': ident, 'bytes': len(data), 'kind': kind}


def put_json(cfg, value, kind='application/json') -> dict:
    return put(cfg, canonical(value), kind)


def read(cfg, ident: str) -> bytes:
    with Store(cfg) as st:
        row = st.conn.execute('SELECT * FROM artifacts WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise UsageError(f'unknown artifact {ident}')
    path = Path(row['path'])
    data = path.read_bytes()
    if path.is_symlink() or hashlib.sha256(data).hexdigest() != ident:
        raise UsageError('artifact content changed')
    return data


def _serialized_size(value) -> int:
    # Match the most expansive built-in consumer (pretty CLI JSON).  MCP JSON
    # serialization is no larger for ordinary ASCII payloads.
    return len(json.dumps(value, ensure_ascii=True, indent=2).encode())


def _bounded_reply(summary: dict, ref: dict, limit: int) -> dict:
    # Two bytes is the smallest possible serialized JSON object.  Smaller
    # configured limits cannot be represented exactly, so return that object.
    if limit < 2:
        return {}

    ident = ref['id']
    bases = (
        {'truncated': True, 'detail': {'id': ident}},
        {'detail': {'id': ident}},
        {'truncated': True},
        {},
    )
    reply = next(candidate for candidate in bases if _serialized_size(candidate) <= limit)

    # Preserve decision metadata before previews and convenience fields.  A
    # field is included only when the complete serialized reply still fits.
    priority = (
        'ok', 'exit_code', 'status', 'id', 'operation_id', 'terminal', 'accepted',
        'proof_accepted', 'targeted', 'frontier_fresh', 'counts', 'stale_count',
        'problems_count', 'ready_count', 'stale', 'problems', 'ready_nodes', 'job',
        'stage', 'stages_done', 'stages_total', 'published',
        'termination_acknowledged', 'outcome', 'diagnostic_summary', 'diagnostic',
        'candidate_file',
    )
    for key in priority:
        if key not in summary:
            continue
        candidate = {**reply, key: summary[key]}
        if _serialized_size(candidate) <= limit:
            reply = candidate

    # Restore optional reference metadata when it does not displace more useful
    # status and freshness fields.
    if isinstance(reply.get('detail'), dict) and reply['detail'].get('id') == ident:
        for key in ('bytes', 'kind'):
            detail = {**reply['detail'], key: ref[key]}
            candidate = {**reply, 'detail': detail}
            if _serialized_size(candidate) <= limit:
                reply = candidate
    return reply


def compact(cfg, payload: dict, limit: int = 4096) -> dict:
    # Keep outcome metadata; raw diagnostics and prose live in the full artifact.
    small = {k: payload[k] for k in ('ok', 'exit_code', 'id', 'operation_id', 'status', 'accepted',
                                    'proof_accepted', 'terminal', 'targeted') if k in payload}
    # Large frontier replies must still tell a planner whether the index is usable.
    counts = payload.get('counts')
    if isinstance(counts, dict) and len(counts) <= 20 and all(isinstance(v, int) for v in counts.values()):
        small['counts'] = counts
    for field in ('stale', 'problems'):
        values = payload.get(field)
        if isinstance(values, list):
            small[field + '_count'] = len(values)
            small[field] = [str(v)[:160] for v in values[:4]]
    ready = payload.get('ready')
    if isinstance(ready, list):
        small['ready_count'] = len(ready)
        small['ready_nodes'] = []
        for node in ready[:4]:
            if not isinstance(node, dict) or not isinstance(node.get('name'), str):
                break
            brief = {'name': node['name'], 'description': str(node.get('description', ''))[:160]}
            proposed = small['ready_nodes'] + [brief]
            if len(canonical(proposed)) > limit // 2:
                break
            small['ready_nodes'] = proposed
    if isinstance(payload.get('job'),dict):
        small['job'] = {k:payload['job'][k] for k in ('id','status','kind','seconds','peak_mib','operation_id') if k in payload['job']}
    stages = payload.get('stages')
    if isinstance(stages, list) and stages and all(isinstance(s, dict) for s in stages):
        current = next((s for s in stages if s.get('status') != 'succeeded'), stages[-1])
        small['stage'] = {k: current[k] for k in ('name', 'status', 'job_id', 'attempt') if k in current}
        small['stages_done'] = sum(s.get('status') == 'succeeded' for s in stages)
        small['stages_total'] = len(stages)
    # A retry retains the previous receipt for inspection; it is not the current outcome.
    active = payload.get('status') in ('pending', 'running', 'cancel_requested')
    if isinstance(payload.get('result'),dict) and not active:
        for k in ('proof_accepted','frontier_fresh','published','outcome','termination_acknowledged',
                  'diagnostic_summary','diagnostic','candidate_file'):
            if k in payload['result']: small[k]=payload['result'][k]
    # Keep the convenience fields stable even when a small reply fits in full.
    enriched = {**payload, **small}
    if _serialized_size(enriched) <= limit:
        return enriched
    try:
        ref = put_json(cfg, payload)
    except (OSError,ReadOnly):
        return {**enriched,'compact_unavailable':'artifact storage is unavailable; full read result preserved'}
    return _bounded_reply(small, ref, limit)
