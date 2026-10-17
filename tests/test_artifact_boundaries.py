from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from lockstep import artifacts
from lockstep.artifacts import canonical, compact, put_json, read
from lockstep.errors import ReadOnly
from lockstep.store import Store


def serialized_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=True, indent=2).encode())


def test_compact_bounds_oversized_promoted_field_and_preserves_full_artifact(cfg):
    payload = {
        'id': 'operation-example',
        'status': 'succeeded',
        'counts': {'closed': 208, 'open': 12},
        'stale': [],
        'result': {'frontier_fresh': True, 'outcome': 'x' * 10_000},
    }

    brief = compact(cfg, payload, limit=4096)

    assert serialized_size(brief) <= 4096
    assert brief['status'] == 'succeeded'
    assert brief['frontier_fresh'] is True
    assert brief['counts'] == payload['counts']
    assert brief['stale_count'] == 0
    assert 'outcome' not in brief
    assert json.loads(read(cfg, brief['detail']['id'])) == payload


@pytest.mark.parametrize('limit', [0, 2, 24, 84, 101, 160])
def test_compact_handles_tiny_limits_without_overshooting_representable_json(cfg, limit):
    brief = compact(cfg, {'status': 'failed', 'diagnostic': 'x' * 1000}, limit=limit)

    assert serialized_size(brief) <= max(2, limit)


def test_compact_real_readonly_store_preserves_full_result_without_publishing(cfg):
    # Initialize the database while it is writable, then reproduce Store's real
    # read-only connection path while leaving runs/lockstep-artifacts writable.
    with Store(cfg):
        pass
    payload = {'nodes': ['large record' * 1000]}
    ident = hashlib.sha256(canonical(payload)).hexdigest()
    artifact_path = cfg.root / 'runs' / 'lockstep-artifacts' / ident[:2] / ident
    db = cfg.db_path
    os.chmod(db, 0o444)
    os.chmod(db.parent, 0o555)
    try:
        with pytest.raises(ReadOnly, match='read-only'):
            put_json(cfg, payload)
        assert not artifact_path.exists()

        result = compact(cfg, payload)
        assert result['nodes'] == payload['nodes']
        assert result['compact_unavailable']
        assert not artifact_path.exists()
    finally:
        os.chmod(db.parent, 0o755)
        os.chmod(db, 0o644)


def test_artifact_directory_sync_precedes_database_reference(cfg, monkeypatch):
    data = canonical({'payload': 'durability boundary'})
    ident = hashlib.sha256(data).hexdigest()
    base = cfg.root / 'runs' / 'lockstep-artifacts'
    directory = base / ident[:2]
    path = directory / ident
    events = []
    real_replace = artifacts.os.replace

    def observed_replace(source, target):
        real_replace(source, target)
        events.append(('replace', Path(target)))

    def fail_final_directory_sync(target):
        target = Path(target)
        events.append(('fsync', target))
        if target == directory:
            assert path.exists()
            raise OSError('simulated directory fsync failure')

    monkeypatch.setattr(artifacts.os, 'replace', observed_replace)
    monkeypatch.setattr(artifacts, '_fsync_directory', fail_final_directory_sync)

    with pytest.raises(OSError, match='simulated directory fsync failure'):
        put_json(cfg, {'payload': 'durability boundary'})

    assert events == [
        ('fsync', cfg.root),
        ('fsync', cfg.root / 'runs'),
        ('fsync', base),
        ('replace', path),
        ('fsync', directory),
    ]
    with Store(cfg) as store:
        assert store.conn.execute('SELECT 1 FROM artifacts WHERE id=?', (ident,)).fetchone() is None


def test_preexisting_artifact_hierarchy_parent_links_are_synced(cfg, monkeypatch):
    value = {'payload': 'pre-existing hierarchy'}
    ident = hashlib.sha256(canonical(value)).hexdigest()
    base = cfg.root / 'runs' / 'lockstep-artifacts'
    directory = base / ident[:2]
    path = directory / ident
    # Model a hierarchy left by a writer that stopped before syncing any of the
    # directory entries.  Publication must not assume existing means durable.
    directory.mkdir(parents=True)
    events = []
    real_replace = artifacts.os.replace

    def observed_replace(source, target):
        real_replace(source, target)
        events.append(('replace', Path(target)))

    def observed_directory_sync(target):
        events.append(('fsync', Path(target)))

    monkeypatch.setattr(artifacts.os, 'replace', observed_replace)
    monkeypatch.setattr(artifacts, '_fsync_directory', observed_directory_sync)

    ref = put_json(cfg, value)

    assert ref['id'] == ident
    assert events == [
        ('fsync', cfg.root),
        ('fsync', cfg.root / 'runs'),
        ('fsync', base),
        ('replace', path),
        ('fsync', directory),
    ]
    assert read(cfg, ident) == canonical(value)
