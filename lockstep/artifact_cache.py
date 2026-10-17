"""Verified sharing of immutable Lean certificate build artifacts.

Lake owns the content-addressed blob format.  This module adds the stronger
SHA-256 identity check and atomic adoption needed to deduplicate already-built
outputs without manufacturing Lake input-to-output mappings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import Iterable

from lockstep import DATA
from lockstep.build import lean_compilers_running
from lockstep.config import Config, load_config
from lockstep.errors import CheckFailed, UsageError
from lockstep.modules import module_to_path, scan_modules
from lockstep.storage import StorageTarget, exclusive_storage_maintenance


FACET_DIRS = {
    "olean": Path(".lake/build/lib/lean"),
    "olean.server": Path(".lake/build/lib/lean"),
    "olean.private": Path(".lake/build/lib/lean"),
    "ilean": Path(".lake/build/lib/lean"),
    "ir": Path(".lake/build/lib/lean"),
    "c": Path(".lake/build/ir"),
    "bc": Path(".lake/build/ir"),
}
DEFAULT_FACETS = tuple(FACET_DIRS)
SCHEMA = "lockstep-artifact-share-v1"
SUPPORTED_TOOLCHAINS = {
    "leanprover/lean4:v4.29.1": "lean-v4.29.1-f72c35b",
}


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    links: int
    allocated_bytes: int


@dataclass(frozen=True)
class Candidate:
    module: str
    facet: str
    relative_output: str
    primary: Path
    other: Path
    size: int


def _identity(path: Path) -> FileIdentity:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        kind = "symlink" if stat.S_ISLNK(info.st_mode) else "non-regular file"
        raise CheckFailed(f"refusing {kind}: {path}")
    return FileIdentity(
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        info.st_nlink, info.st_blocks * 512,
    )


def _absolute_without_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def reject_symlink_ancestors(path: Path, boundary: Path | None = None) -> None:
    """Reject an existing symlink in ``path`` or its parents down to ``boundary``.

    ``Path.resolve`` is intentionally not used: resolving first would hide the
    condition this check protects against.
    """
    path = _absolute_without_symlinks(path)
    boundary = _absolute_without_symlinks(boundary or Path(path.anchor))
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise CheckFailed(f"path escapes its trusted boundary {boundary}: {path}") from exc
    current = boundary
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise CheckFailed(f"refusing symlink or symlink ancestor: {current}")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise CheckFailed(f"refusing non-directory path ancestor: {current}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _toolchain(root: Path) -> str:
    path = root / "lean-toolchain"
    if not path.is_file() or path.is_symlink():
        raise CheckFailed(f"missing or unsafe lean-toolchain: {path}")
    return path.read_text().strip()


def supported_toolchain(root: Path) -> tuple[str, str]:
    toolchain = _toolchain(root)
    try:
        namespace = SUPPORTED_TOOLCHAINS[toolchain]
    except KeyError as exc:
        raise CheckFailed(f"unsupported Lean toolchain for artifact sharing: {toolchain}") from exc
    return toolchain, namespace


def default_cache_root(cfg: Config) -> Path:
    _, namespace = supported_toolchain(cfg.root)
    return cfg.state_home / "lake-artifacts" / namespace


def validate_roots(cfg: Config, other_root: Path) -> Config:
    primary = cfg.root.resolve()
    other = other_root.resolve()
    if primary == other:
        raise UsageError("the other checkout must differ from the current checkout")
    other_cfg = load_config(other)
    other_cfg.root = other
    if cfg.project_id != other_cfg.project_id:
        raise CheckFailed(f"project mismatch: {cfg.project_id} != {other_cfg.project_id}")
    if cfg.db_path.resolve() != other_cfg.db_path.resolve():
        raise CheckFailed("checkouts must share the harness database so every writer honors the same storage reservations")
    primary_toolchain, _ = supported_toolchain(primary)
    other_toolchain, _ = supported_toolchain(other)
    if primary_toolchain != other_toolchain:
        raise CheckFailed("Lean toolchain mismatch between checkouts")
    if primary.stat().st_dev != other.stat().st_dev:
        raise CheckFailed("checkouts are on different filesystems; hard-link sharing is unavailable")
    return other_cfg


def _output_path(root: Path, module: str, facet: str) -> Path:
    base = (root / FACET_DIRS[facet] / module_to_path(module)).with_suffix("")
    return Path(str(base) + "." + facet)


def certificate_candidates(
    cfg: Config, other_cfg: Config, facets: Iterable[str] = DEFAULT_FACETS
) -> tuple[list[Candidate], dict[str, int]]:
    selected = tuple(dict.fromkeys(facets))
    unknown = sorted(set(selected) - set(FACET_DIRS))
    if unknown:
        raise UsageError("unsupported compiled facet(s): " + ", ".join(unknown))
    primary_modules = scan_modules(cfg)
    other_modules = scan_modules(other_cfg)
    layer = cfg.certificates_layer
    other_layer = other_cfg.certificates_layer
    generated = {
        name for name, mod in primary_modules.items()
        if mod.generated and mod.layer == layer
        and name in other_modules and other_modules[name].generated
        and other_modules[name].layer == other_layer
    }
    stats = {"generated_modules": len(generated), "missing": 0, "size_mismatch": 0, "already_shared": 0}
    candidates: list[Candidate] = []
    for module in sorted(generated):
        for facet in selected:
            primary = _output_path(cfg.root, module, facet)
            other = _output_path(other_cfg.root, module, facet)
            reject_symlink_ancestors(primary, cfg.root)
            reject_symlink_ancestors(other, other_cfg.root)
            if not primary.exists() or not other.exists():
                stats["missing"] += 1
                continue
            p_id = _identity(primary)
            o_id = _identity(other)
            if p_id.size != o_id.size:
                stats["size_mismatch"] += 1
                continue
            if (p_id.device, p_id.inode) == (o_id.device, o_id.inode):
                stats["already_shared"] += 1
                continue
            rel = str(primary.relative_to(cfg.root))
            candidates.append(Candidate(module, facet, rel, primary, other, p_id.size))
    stats["candidates"] = len(candidates)
    stats["candidate_bytes"] = sum(item.size for item in candidates)
    return candidates, stats


class Receipt:
    def __init__(self, path: Path, header: dict[str, object]):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size:
            rows = self._repair_and_read()
            if not rows:
                self.append(header)
                return
            first = next((row for row in rows if row), "")
            try:
                previous = json.loads(first)
            except json.JSONDecodeError as exc:
                raise CheckFailed(f"invalid existing receipt {path}: {exc}") from exc
            keys = ("schema", "primary", "other", "cache", "toolchain", "facets")
            if any(previous.get(key) != header.get(key) for key in keys):
                raise CheckFailed(f"receipt parameters do not match this invocation: {path}")
        else:
            self.append(header)

    def _repair_and_read(self) -> list[str]:
        """Drop only an invalid torn tail and make a valid unterminated tail append-safe."""
        data = self.path.read_bytes()
        lines = data.splitlines(keepends=True)
        valid: list[str] = []
        offset = 0
        truncate_at: int | None = None
        for index, raw in enumerate(lines):
            text = raw.rstrip(b"\r\n").decode(errors="strict")
            if not text:
                offset += len(raw)
                continue
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                if index != len(lines) - 1:
                    raise CheckFailed(f"invalid receipt record in {self.path}: {exc}") from exc
                truncate_at = offset
                break
            valid.append(text)
            offset += len(raw)
        if truncate_at is not None:
            with self.path.open("r+b") as stream:
                stream.truncate(truncate_at)
                stream.flush()
                os.fsync(stream.fileno())
            data = data[:truncate_at]
        if data and not data.endswith(b"\n"):
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
        return valid

    def append(self, record: dict[str, object]) -> None:
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, encoded.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def adopted(self) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for line in self._repair_and_read()[1:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                break  # an interrupted final append is ignored
            if row.get("event") == "adopted":
                out.add((str(row["module"]), str(row["facet"])))
        return out


def verify_candidates(candidates: Iterable[Candidate], receipt: Receipt) -> list[tuple[Candidate, str]]:
    verified: list[tuple[Candidate, str]] = []
    for item in candidates:
        primary_before = _identity(item.primary)
        other_before = _identity(item.other)
        primary_sha = sha256_file(item.primary)
        other_sha = sha256_file(item.other)
        if _identity(item.primary) != primary_before or _identity(item.other) != other_before:
            raise CheckFailed(f"artifact changed while hashing: {item.relative_output}")
        if primary_sha != other_sha:
            receipt.append({"event": "mismatch", "module": item.module, "facet": item.facet,
                            "bytes": item.size, "primary_sha256": primary_sha, "other_sha256": other_sha})
            continue
        receipt.append({"event": "verified", "module": item.module, "facet": item.facet,
                        "relative_output": item.relative_output, "bytes": item.size, "sha256": primary_sha,
                        "primary_identity": asdict(primary_before), "other_identity": asdict(other_before)})
        verified.append((item, primary_sha))
    return verified


def _driver_path() -> Path:
    return DATA / "CacheArtifacts.lean"


def save_native_artifacts(
    cfg: Config, cache_root: Path, verified: list[tuple[Candidate, str]], work: Path,
    *, timeout_s: int = 21600,
) -> dict[str, Path]:
    manifest = work / "native-input.tsv"
    output = work / "native-output.tsv"
    work.mkdir(parents=True, exist_ok=True)
    for item, _ in verified:
        if any(char in str(item.primary) for char in ("\t", "\n", "\r")):
            raise CheckFailed(f"unsupported control character in artifact path: {item.primary}")
    manifest.write_text("".join(f"{item.primary}\t{item.facet}\n" for item, _ in verified))
    argv = [cfg.lake, "env", "lean", "--run", str(_driver_path()), str(cache_root), str(manifest), str(output)]
    try:
        proc = subprocess.Popen(
            argv, cwd=cfg.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise CheckFailed(f"could not start Lake artifact writer: {exc}") from exc

    def stop_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.wait()
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                proc.wait()
                return
            time.sleep(0.05)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        # Do not return to the storage-reservation context while a descendant
        # from the cache-writing process group can still run.
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)

    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        stop_group()
        raise CheckFailed(f"Lake artifact writer exceeded {timeout_s}s and its process group was stopped") from exc
    except BaseException:
        stop_group()
        raise
    if proc.returncode:
        raise CheckFailed(f"Lake artifact writer failed ({proc.returncode}):\n{stdout[-4000:]}")
    saved: dict[str, Path] = {}
    for line in output.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            raise CheckFailed(f"invalid native artifact writer output: {line}")
        local, relative = parts
        saved[local] = cache_root / "artifacts" / relative
    if set(saved) != {str(item.primary) for item, _ in verified}:
        raise CheckFailed("native artifact writer returned an incomplete or unexpected manifest")
    return saved


def atomic_adopt(path: Path, blob: Path, expected_sha256: str) -> tuple[FileIdentity, FileIdentity]:
    blob_mode = blob.lstat().st_mode
    if blob_mode & 0o222:
        os.chmod(blob, 0o444)
    before = _identity(path)
    blob_id = _identity(blob)
    blob_content_id = (
        blob_id.device, blob_id.inode, blob_id.size, blob_id.mtime_ns, blob_id.allocated_bytes
    )
    if before.device != blob_id.device:
        raise CheckFailed(f"artifact and cache blob are on different filesystems: {path}")
    if before.size != blob_id.size or sha256_file(blob) != expected_sha256 or _identity(blob) != blob_id:
        raise CheckFailed(f"native cache hash collision or corrupt blob for {path}")
    if sha256_file(path) != expected_sha256 or _identity(path) != before:
        raise CheckFailed(f"artifact changed before adoption: {path}")
    if (before.device, before.inode) == (blob_id.device, blob_id.inode):
        return before, before
    temp = path.with_name(f".{path.name}.lockstep-share-{os.getpid()}-{time.time_ns()}")
    try:
        try:
            os.link(blob, temp, follow_symlinks=False)
            temp_id = _identity(temp)
            linked_blob = _identity(blob)
            linked_content_id = (
                linked_blob.device, linked_blob.inode, linked_blob.size,
                linked_blob.mtime_ns, linked_blob.allocated_bytes,
            )
            if linked_content_id != blob_content_id or (temp_id.device, temp_id.inode) != (blob_id.device, blob_id.inode):
                raise CheckFailed(f"native cache blob changed during adoption: {blob}")
            if _identity(path) != before:
                raise CheckFailed(f"artifact changed during adoption: {path}")
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            after = _identity(path)
            final_blob = _identity(blob)
            final_content_id = (
                final_blob.device, final_blob.inode, final_blob.size,
                final_blob.mtime_ns, final_blob.allocated_bytes,
            )
            if final_content_id != blob_content_id or (after.device, after.inode) != (blob_id.device, blob_id.inode):
                raise CheckFailed(f"atomic adoption did not install the native cache inode: {path}")
            return before, after
        except CheckFailed:
            raise
        except OSError as exc:
            raise CheckFailed(f"atomic adoption failed for {path}: {exc}") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def share_certificates(
    cfg: Config,
    other_root: Path,
    *,
    cache_root: Path,
    receipt_path: Path,
    facets: Iterable[str] = DEFAULT_FACETS,
    apply: bool = False,
) -> dict[str, object]:
    if not os.environ.get("LOCKSTEP_JOB_ID") or os.environ.get("LOCKSTEP_IN_GUARDED_STAGE") != "1":
        raise UsageError("artifact sharing must run inside `lockstep job run` (guarded job identity is missing)")
    other_cfg = validate_roots(cfg, other_root)
    reject_symlink_ancestors(cache_root)
    cache_root = _absolute_without_symlinks(cache_root)
    if cache_root.exists() and not cache_root.is_dir():
        raise CheckFailed(f"artifact cache path is not a directory: {cache_root}")
    facets = tuple(dict.fromkeys(facets))
    cache_probe = cache_root
    while not cache_probe.exists() and cache_probe != cache_probe.parent:
        cache_probe = cache_probe.parent
    if cache_probe.stat().st_dev != cfg.root.stat().st_dev:
        raise CheckFailed("artifact cache and checkouts must be on the same filesystem")
    header = {
        "schema": SCHEMA, "event": "start", "primary": str(cfg.root.resolve()),
        "other": str(other_cfg.root.resolve()), "cache": str(cache_root),
        "toolchain": supported_toolchain(cfg.root)[0], "facets": list(facets), "apply": apply,
    }
    owner_job_id = os.environ.get("LOCKSTEP_JOB_ID")
    target = StorageTarget("artifact-cache", cache_root)
    with exclusive_storage_maintenance(
        cfg, roots=(cfg.root, other_cfg.root), targets=(target,),
        reason="verified sharing of immutable certificate artifacts", owner_job_id=owner_job_id,
    ):
        receipt = Receipt(receipt_path.resolve(), header)
        if lean_compilers_running():
            raise CheckFailed("a Lean compiler is running outside this storage reservation")
        candidates, stats = certificate_candidates(cfg, other_cfg, facets)
        adopted_before = receipt.adopted()
        verified = verify_candidates(candidates, receipt)
        mismatch_count = len(candidates) - len(verified)
        adopted_count = 0
        deduplicated_logical_bytes = 0
        eligible_freed_allocated_bytes = 0
        free_before = os.statvfs(cache_probe).f_bavail * os.statvfs(cache_probe).f_frsize
        if apply and verified:
            cache_root.mkdir(parents=True, exist_ok=True)
            reject_symlink_ancestors(cache_root)
            work = receipt_path.resolve().parent / (receipt_path.name + ".work")
            saved = save_native_artifacts(cfg, cache_root, verified, work)
            for item, digest in verified:
                blob = saved[str(item.primary)]
                # A full SHA-256 comparison catches Lake's documented 64-bit-hash collision case.
                if sha256_file(blob) != digest:
                    raise CheckFailed(f"native cache hash collision for {item.relative_output}")
                os.chmod(blob, 0o444)
                before_primary, after_primary = atomic_adopt(item.primary, blob, digest)
                before_other, after_other = atomic_adopt(item.other, blob, digest)
                receipt.append({
                    "event": "adopted", "module": item.module, "facet": item.facet,
                    "relative_output": item.relative_output, "bytes": item.size, "sha256": digest,
                    "lake_artifact": str(blob.relative_to(cache_root)),
                    "primary_before": asdict(before_primary), "primary_after": asdict(after_primary),
                    "other_before": asdict(before_other), "other_after": asdict(after_other),
                })
                adopted_count += 1
                deduplicated_logical_bytes += item.size
                for before, after in ((before_primary, after_primary), (before_other, after_other)):
                    if before.inode != after.inode and before.links == 1:
                        eligible_freed_allocated_bytes += before.allocated_bytes
        free_after = os.statvfs(cache_probe).f_bavail * os.statvfs(cache_probe).f_frsize
        result = {
            "schema": SCHEMA, "mode": "apply" if apply else "dry-run", "receipt": str(receipt.path),
            "cache": str(cache_root), "facets": list(facets), **stats,
            "verified": len(verified), "mismatches": mismatch_count,
            "adopted": adopted_count,
            "deduplicated_logical_bytes": deduplicated_logical_bytes,
            "eligible_freed_allocated_bytes": eligible_freed_allocated_bytes,
            "observed_free_bytes_before": free_before,
            "observed_free_bytes_after": free_after,
            "observed_free_bytes_delta": free_after - free_before,
            "resumed_adoptions": len(adopted_before),
        }
        receipt.append({"event": "complete", **result})
        return result
