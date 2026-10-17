"""SQLite state that git handles badly: claims, extract cache, jobs, attempts, search index."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from lockstep.config import Config
from lockstep.errors import ReadOnly, UsageError

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  node TEXT PRIMARY KEY, agent TEXT NOT NULL, claimed_at REAL NOT NULL, expires_at REAL NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS extract_cache (
  tree_hash TEXT PRIMARY KEY, nodes_json TEXT NOT NULL, index_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, node TEXT, argv TEXT NOT NULL, cwd TEXT NOT NULL,
  status TEXT NOT NULL, exit_code INTEGER, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  log_path TEXT, budget_min INTEGER, agent TEXT, pid INTEGER, result_json TEXT,
  why TEXT, memory_max TEXT, outputs TEXT, peak_mib INTEGER, seconds REAL);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, node TEXT NOT NULL, agent TEXT, started_at REAL, ended_at REAL, outcome TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, node TEXT, agent TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS attempts_node ON attempts(node);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
"""

# columns added after the first release; older databases are migrated on open
JOB_COLUMNS = {"why": "TEXT", "memory_max": "TEXT", "outputs": "TEXT", "peak_mib": "INTEGER", "seconds": "REAL",
               "cancel_requested": "INTEGER NOT NULL DEFAULT 0", "process_start": "TEXT",
               "heartbeat_at": "REAL", "lock_key": "TEXT", "operation_id": "TEXT", "stage_ordinal": "INTEGER", "scope_unit": "TEXT"}

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(name, module, kind, doc, type_pp, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, cfg: Config, path: Path | None = None):
        self.cfg = cfg
        self.path = path or cfg.db_path
        self.readonly = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            self.conn = sqlite3.connect(self.path, timeout=30)
            self.conn.row_factory = sqlite3.Row
            # a real write: sandboxes and read-only mounts let the file open
            # but refuse the first write, which used to surface as "attempt to write a readonly database"
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            self.conn.execute(f"PRAGMA user_version = {int(version)}")
            self.conn.executescript(SCHEMA)
            self._migrate()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "readonly" not in msg and "read-only" not in msg and "unable to open" not in msg:
                raise
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            except sqlite3.OperationalError as exc2:
                raise ReadOnly(self._readonly_message() + f" ({exc2})") from exc2
            self.readonly = True
        if self.readonly:
            self.fts = bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'search'").fetchone())
        else:
            try:
                self.conn.executescript(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            self.purge_expired_claims()

    def _readonly_message(self) -> str:
        home = self.path.parent.parent
        return (f"harness database {self.path} is read-only from this process (sandbox or permissions): read commands work, "
                f"writes do not. Allow writes to {home} in the agent client's sandbox settings, "
                f"or point LOCKSTEP_HOME at a writable directory that every agent shares")

    def _writable(self) -> None:
        if self.readonly:
            raise ReadOnly(self._readonly_message())

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        if not self.readonly:
            self.conn.rollback() if exc[0] else self.conn.commit()
        self.conn.close()

    def _migrate(self) -> None:
        from lockstep.automation_schema import SCHEMA as AUTOMATION_SCHEMA

        # Back up once before introducing the automation tables. SQLite backup is consistent
        # even with an existing worker; do not copy a live database file with shutil.
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='automation_meta'").fetchone():
            self.conn.commit()
            backup = self.path.parent / 'backups' / 'before-automation-v1.sqlite'
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                target = sqlite3.connect(backup)
                try:
                    self.conn.backup(target)
                finally:
                    target.close()
        self.conn.executescript(AUTOMATION_SCHEMA)
        self.conn.execute('BEGIN IMMEDIATE')
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, typ in JOB_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        if 'scope_unit' not in {r[1] for r in self.conn.execute('PRAGMA table_info(resource_leases)')}:
            self.conn.execute('ALTER TABLE resource_leases ADD COLUMN scope_unit TEXT')
        self.conn.commit()

    def table_counts(self) -> dict[str, int]:
        out = {}
        for t in ("claims", "jobs", "attempts", "extract_cache"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out

    # ---- events -----------------------------------------------------------
    def event(self, kind: str, node: str | None = None, agent: str | None = None, detail: Any = None) -> None:
        self._writable()
        if agent and ("$" in agent or "{" in agent or "}" in agent):
            raise UsageError(f"LOCKSTEP_AGENT={agent!r} looks like an unexpanded shell variable; export a literal id such as <client>:<session id>")
        self.conn.execute(
            "INSERT INTO events(at, kind, node, agent, detail) VALUES (?,?,?,?,?)",
            (time.time(), kind, node, agent, json.dumps(detail, default=str) if detail is not None else None),
        )
        self.conn.commit()

    def events_since(self, since: float, agent: str | None = None, node: str | None = None,
                     kinds: list[str] | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM events WHERE at >= ?"
        params: list[Any] = [since]
        if agent:
            q += " AND agent = ?"
            params.append(agent)
        if node:
            q += " AND node = ?"
            params.append(node)
        if kinds:
            q += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        return self.conn.execute(q + " ORDER BY at", params).fetchall()

    def log(self, text: str, node: str | None, agent: str | None) -> None:
        """A free-text journal entry: what an agent is doing and why."""
        self.event("log", node, agent, {"text": text})

    # ---- claims -----------------------------------------------------------
    def purge_expired_claims(self) -> int:
        if self.readonly:
            return 0
        now = time.time()
        rows = self.conn.execute("SELECT node, agent FROM claims WHERE expires_at < ?", (now,)).fetchall()
        for r in rows:
            self.conn.execute("INSERT INTO attempts(node, agent, started_at, ended_at, outcome) SELECT node, agent, claimed_at, ?, 'expired' FROM claims WHERE node = ?", (now, r["node"]))
            self.conn.execute("INSERT INTO events(at, kind, node, agent) VALUES (?,?,?,?)", (now, "claim-expired", r["node"], r["agent"]))
        self.conn.execute("DELETE FROM claims WHERE expires_at < ?", (now,))
        self.conn.commit()
        return len(rows)

    def claims(self) -> dict[str, dict]:
        if self.readonly:
            # expired claims are purged on every writable open; a read-only reader filters them here
            return {r["node"]: dict(r) for r in self.conn.execute("SELECT * FROM claims WHERE expires_at >= ?", (time.time(),)).fetchall()}
        return {r["node"]: dict(r) for r in self.conn.execute("SELECT * FROM claims").fetchall()}

    def claim(self, node: str, agent: str, ttl_hours: float, note: str | None = None) -> dict:
        self._writable()
        now = time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO claims(node, agent, claimed_at, expires_at, note) VALUES (?,?,?,?,?)",
            (node, agent, now, now + ttl_hours * 3600, note),
        )
        self.conn.execute("INSERT INTO attempts(node, agent, started_at) VALUES (?,?,?)", (node, agent, now))
        self.conn.commit()
        self.event("claim", node, agent, note)
        return self.claims()[node]

    def release(self, node: str, agent: str | None, outcome: str = "abandoned", note: str | None = None) -> bool:
        self._writable()
        row = self.conn.execute("SELECT * FROM claims WHERE node = ?", (node,)).fetchone()
        if row is None:
            return False
        self.conn.execute("DELETE FROM claims WHERE node = ?", (node,))
        self.conn.execute(
            "UPDATE attempts SET ended_at = ?, outcome = ?, note = COALESCE(?, note) WHERE node = ? AND ended_at IS NULL",
            (time.time(), outcome, note, node),
        )
        self.conn.commit()
        self.event("release", node, agent or row["agent"], {"outcome": outcome, "note": note})
        return True

    def latest_attempts(self) -> dict[str, dict]:
        """The most recent ended attempt per node (what happened to it last)."""
        out: dict[str, dict] = {}
        for r in self.conn.execute("SELECT * FROM attempts WHERE ended_at IS NOT NULL ORDER BY ended_at"):
            out[r["node"]] = dict(r)
        return out

    # ---- attempts ---------------------------------------------------------
    def record_attempt(self, node: str, agent: str | None, outcome: str, note: str | None = None, started_at: float | None = None) -> None:
        self._writable()
        now = time.time()
        open_row = self.conn.execute("SELECT id FROM attempts WHERE node = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (node,)).fetchone()
        if open_row is not None:
            self.conn.execute("UPDATE attempts SET ended_at = ?, outcome = ?, note = ? WHERE id = ?", (now, outcome, note, open_row["id"]))
        else:
            self.conn.execute("INSERT INTO attempts(node, agent, started_at, ended_at, outcome, note) VALUES (?,?,?,?,?,?)", (node, agent, started_at or now, now, outcome, note))
        self.conn.commit()
        self.event("attempt", node, agent, {"outcome": outcome, "note": note})

    def attempts(self, node: str | None = None, since: float | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM attempts WHERE 1=1"
        params: list[Any] = []
        if node:
            q += " AND node = ?"
            params.append(node)
        if since is not None:
            q += " AND COALESCE(ended_at, started_at) >= ?"
            params.append(since)
        return self.conn.execute(q + " ORDER BY id", params).fetchall()

    # ---- extract cache ----------------------------------------------------
    def cache_get(self, tree_hash: str) -> tuple[dict, list] | None:
        row = self.conn.execute("SELECT nodes_json, index_json FROM extract_cache WHERE tree_hash = ?", (tree_hash,)).fetchone()
        if row is None:
            return None
        return json.loads(row["nodes_json"]), json.loads(row["index_json"])

    def cache_put(self, tree_hash: str, nodes: dict, index: list) -> None:
        self._writable()
        self.conn.execute(
            "INSERT OR REPLACE INTO extract_cache(tree_hash, nodes_json, index_json, created_at) VALUES (?,?,?,?)",
            (tree_hash, json.dumps(nodes), json.dumps(index), time.time()),
        )
        # keep the cache small, but never let targeted (per-node) entries evict the full-environment
        # ones: those cost a 36 GiB extractor run and gate every commit (`lockstep extract --check`)
        self.conn.execute("DELETE FROM extract_cache WHERE tree_hash LIKE '%+%' AND tree_hash NOT IN "
                          "(SELECT tree_hash FROM extract_cache WHERE tree_hash LIKE '%+%' ORDER BY created_at DESC LIMIT 20)")
        self.conn.execute("DELETE FROM extract_cache WHERE tree_hash NOT LIKE '%+%' AND tree_hash NOT IN "
                          "(SELECT tree_hash FROM extract_cache WHERE tree_hash NOT LIKE '%+%' ORDER BY created_at DESC LIMIT 5)")
        self.conn.commit()

    def cache_clear(self) -> None:
        self._writable()
        self.conn.execute("DELETE FROM extract_cache")
        self.conn.commit()

    # ---- jobs -------------------------------------------------------------
    def job_insert(self, job: dict) -> None:
        self._writable()
        cols = ",".join(job.keys())
        marks = ",".join("?" for _ in job)
        self.conn.execute(f"INSERT INTO jobs({cols}) VALUES ({marks})", tuple(job.values()))
        self.conn.commit()

    def job_update(self, job_id: str, **fields: Any) -> None:
        self._writable()
        sets = ",".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
        self.conn.commit()

    def job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            return dict(row)
        rows = self.conn.execute("SELECT * FROM jobs WHERE substr(id,1,?) = ?", (len(job_id), job_id)).fetchall()
        if len(rows) > 1:
            raise UsageError(f"ambiguous job prefix {job_id!r}; use the full job id")
        return dict(rows[0]) if rows else None

    def jobs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            rows = self.conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def live_jobs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE status IN ('queued', 'starting', 'running') ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def next_queued_job(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---- search -----------------------------------------------------------
    def search_rebuild(self, records: list[dict], stamp: str) -> None:
        self._writable()
        if not self.fts:
            return
        self.conn.execute("DELETE FROM search")
        self.conn.executemany(
            "INSERT INTO search(name, module, kind, doc, type_pp) VALUES (?,?,?,?,?)",
            [(r.get("name", ""), r.get("module", ""), r.get("kind", ""), r.get("doc") or "", r.get("type_pp", "")) for r in records],
        )
        self.conn.execute("INSERT OR REPLACE INTO search_meta(key, value) VALUES ('stamp', ?)", (stamp,))
        self.conn.commit()

    def search_stamp(self) -> str | None:
        row = self.conn.execute("SELECT value FROM search_meta WHERE key = 'stamp'").fetchone()
        return row["value"] if row else None

    def search(self, query: str, limit: int = 20, namespace: str | None = None) -> list[dict]:
        if not self.fts:
            return []
        q = "SELECT name, module, kind, doc, type_pp, bm25(search, 10.0, 1.0, 1.0, 5.0, 3.0) AS score FROM search WHERE search MATCH ?"
        params: list[Any] = [query]
        if namespace:
            q += " AND name LIKE ?"
            params.append(namespace + "%")
        q += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(q, params).fetchall()
        except sqlite3.OperationalError:
            # fall back to a phrase query when the user typed FTS syntax that does not parse
            params[0] = '"' + query.replace('"', " ") + '"'
            rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
