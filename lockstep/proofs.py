"""Frozen proof spans and independent kernel-environment audits.

Candidates are compiled in a networkless namespace with only toolchain, compiled
libraries and an isolated output directory mounted. Provider state is never mounted.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from lockstep import DATA
from lockstep.artifacts import canonical, put, put_json, read
from lockstep.build import run_guarded
from lockstep.errors import UsageError
from lockstep.modules import scan_modules, transitive_closure, module_to_path
from lockstep.nodes import find_node, local_name


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mask_comments(text):
    """Preserve character positions while hiding nested comments and string literals."""
    out = list(text)
    i, depth, string = 0, 0, False
    while i < len(text):
        if depth:
            if text.startswith('/-', i):
                out[i:i+2] = '  '; depth += 1; i += 2; continue
            if text.startswith('-/', i):
                out[i:i+2] = '  '; depth -= 1; i += 2; continue
            if text[i] != '\n': out[i] = ' '
        elif string:
            if text[i] == '\\' and i+1 < len(text):
                out[i:i+2] = '  '; i += 2; continue
            if text[i] == '"': string = False
            if text[i] != '\n': out[i] = ' '
        elif text.startswith('/-', i):
            out[i:i+2] = '  '; depth = 1; i += 2; continue
        elif text.startswith('--', i):
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            out[i:end] = ' ' * (end-i); i = end; continue
        elif text[i] == '"':
            out[i] = ' '; string = True
        i += 1
    if depth or string:
        raise UsageError('unterminated comment/string in source')
    return ''.join(out)


def whole_span(text, short):
    masked = mask_comments(text)
    starts = list(re.finditer(r'(?m)^\s*(?:theorem|lemma)\s+' + re.escape(short) + r'(?=\s|[({:\[])', masked))
    if len(starts) != 1:
        raise UsageError('ambiguous declaration; an exact registered parent is required')
    start = starts[0].end()
    depth = 0
    pending_bindings = 0
    body_start = None
    for i in range(start, len(masked)-1):
        c = masked[i]
        if c in '([{': depth += 1
        elif c in ')]}': depth -= 1
        # Generated signatures can have `let A := ...; let B := ...; P A B`
        # as their result type. Those assignments belong to the signature.
        if depth == 0 and (i == start or not (masked[i-1].isalnum() or masked[i-1] in "_'.")):
            binding = re.match(r'(?:let|have|letI|haveI)\b', masked[i:])
            if binding:
                pending_bindings += 1
        if depth == 0 and masked[i:i+2] == ':=':
            if pending_bindings:
                pending_bindings -= 1
                continue
            body_start = i+2; break
    if body_start is None:
        raise UsageError('unsupported declaration syntax: expected a := proof body')
    # Automatic capture is deliberately restricted to the generated node-module
    # shape. Explicit byte spans handle nested obligations without guessing names.
    tail = re.search(r'(?m)^(?:end(?:\s+[^\n]+)?|(?:theorem|lemma|def|namespace|section|attribute|set_option|open|#\w+)\b)', masked[body_start:])
    body_end = body_start + tail.start() if tail else len(text)
    while body_start < body_end and text[body_start].isspace(): body_start += 1
    while body_end > body_start and masked[body_end-1].isspace(): body_end -= 1
    return len(text[:body_start].encode()), len(text[:body_end].encode())


def capture(cfg, name, *, start=None, end=None):
    node = find_node(cfg, name)
    rel = str(module_to_path(node.module))
    path = cfg.root / rel
    source = path.read_bytes()
    whole_start, whole_end = whole_span(source.decode(), local_name(cfg, node.name))
    if (start is None) != (end is None):
        raise UsageError('supply both start and end byte offsets')
    start, end = (whole_start, whole_end) if start is None else (start, end)
    if not whole_start <= start < end <= whole_end:
        raise UsageError('span is outside the parent proof body')
    try:
        source[:start].decode(); source[start:end].decode(); source[end:].decode()
    except UnicodeDecodeError:
        raise UsageError('span must end at UTF-8 character boundaries')
    mods = scan_modules(cfg)
    closure = transitive_closure(mods, [node.module])
    paths = [module_to_path(m) for m in closure if m != node.module]
    paths += [Path(p) for p in ('lean-toolchain', 'lake-manifest.json', 'lakefile.lean', 'lakefile.toml', 'harness/allowlist.json', 'harness/statements.lock')]
    deps = {str(p): digest(cfg.root / p) for p in paths if (cfg.root / p).is_file()}
    compiled = {}
    for module in closure:
        if module == node.module: continue
        marker = Path('.lake/build/lib/lean') / module_to_path(module).with_suffix('.olean.hash')
        if (cfg.root/marker).is_file(): compiled[str(marker)] = digest(cfg.root/marker)
    fixture = {'schema_version': 1, 'root': str(cfg.root.resolve()), 'parent': node.name, 'module': node.module,
               'type_hash': node.type_hash, 'file': rel, 'source_hash': hashlib.sha256(source).hexdigest(),
               'source': put(cfg, source, 'text/x-lean'), 'span': [start, end], 'whole_span': [whole_start, whole_end],
               'scope': 'whole_declaration' if (start,end)==(whole_start,whole_end) else 'parent_checked_local_span',
               'dependencies': deps, 'compiled_dependencies':compiled, 'standard_axioms': cfg.standard_axioms,
               'capture_policy': 'exact-byte-span-v1; local acceptance requires the complete parent to be closed'}
    return put_json(cfg, fixture, 'proof-fixture-v1')


def fixture(cfg, ident):
    f = json.loads(read(cfg, ident))
    if f.get('schema_version') != 1 or f.get('root') != str(cfg.root.resolve()):
        raise UsageError('fixture schema/worktree mismatch')
    if 'compiled_dependencies' not in f:
        raise UsageError('fixture predates compiled-dependency capture; capture again')
    return f


def validate(cfg, f):
    if digest(cfg.root / f['file']) != f['source_hash']:
        raise UsageError('source changed after capture; capture again')
    for rel, expected in {**f['dependencies'],**f.get('compiled_dependencies',{})}.items():
        path = cfg.root / rel
        if not path.is_file() or digest(path) != expected:
            raise UsageError(f'fixture dependency changed: {rel}')
    if cfg.standard_axioms != f['standard_axioms']:
        raise UsageError('axiom policy changed after capture')


def assembled(cfg, f, body):
    source = read(cfg, f['source']['id'])
    start, end = f['span']
    return source[:start] + body + source[end:]


def sandbox(cfg, out):
    if not shutil.which('bwrap'):
        raise UsageError('proof isolation unavailable: bwrap is required; no unsafe fallback')
    prefix = subprocess.check_output([cfg.lake, 'env', 'lean', '--print-prefix'], cwd=cfg.root, text=True, timeout=30).strip()
    paths = subprocess.check_output([cfg.lake, 'env', 'printenv', 'LEAN_PATH'], cwd=cfg.root, text=True, timeout=30).strip().split(os.pathsep)
    argv = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--clearenv']
    for system in ('/usr', '/lib', '/lib64', '/bin'):
        if Path(system).exists(): argv += ['--ro-bind', system, system]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--ro-bind', prefix, '/toolchain', '--bind', str(out), '/candidate']
    libs = ['/candidate']
    for i, value in enumerate(paths):
        path = (cfg.root / value).resolve()
        if path.is_dir():
            if tuple(path.parts[-2:]) != ('lib','lean'):
                raise UsageError('proof isolation only mounts compiled lib/lean roots; unsupported LEAN_PATH entry')
            dest = f'/libs/{i}'
            argv += ['--ro-bind', str(path), dest]
            libs.append(dest)
    return argv + ['--setenv', 'LEAN_PATH', ':'.join(libs), '--setenv', 'PATH', '/toolchain/bin:/usr/bin:/bin', '--chdir', '/candidate', '--', '/toolchain/bin/lean']


def diagnostic_summary(raw: bytes) -> dict:
    """Bounded compiler headers with byte offsets into the retained diagnostic."""
    errors = []
    count = offset = 0
    for line in raw.splitlines(keepends=True):
        match = re.match(rb'^([^\r\n]+\.lean):(\d+):(\d+): error(?:\([^\r\n)]*\))?: (.*)', line)
        if match:
            count += 1
            item = {'file': match[1].decode('utf-8', errors='replace')[:240],
                    'line': int(match[2]), 'column': int(match[3]),
                    'message': match[4].decode('utf-8', errors='replace').strip()[:240],
                    'log_byte_offset': offset}
            if len(errors) < 6 and len(canonical(errors + [item])) <= 1800:
                errors.append(item)
        offset += len(line)
    return {'errors': errors, 'error_count': count, 'truncated': len(errors) < count}


def check(cfg, fixture_id, body_id, *, minutes=5, memory='4G'):
    f = fixture(cfg, fixture_id)
    validate(cfg, f)
    body = read(cfg, body_id)
    # A body remains a term, not a channel for additional module commands. The
    # independent compiled-type/axiom check below is the actual acceptance gate.
    masked = mask_comments(body.decode())
    if re.search(r'\b(sorry|admit|native_decide|axiom|unsafe)\b|(?m:^\s*(?:import|theorem|lemma|def|#\w+)\b)', masked):
        return {'ok': False, 'proof_accepted': False, 'outcome': 'unsupported_candidate', 'fixture': fixture_id}
    out = cfg.root / 'runs' / 'lockstep-proof-checks' / uuid.uuid4().hex
    out.mkdir(parents=True)
    source = assembled(cfg, f, body)
    (out / 'Candidate.lean').write_bytes(source)
    command = sandbox(cfg, out)
    compiled = run_guarded(cfg, command + ['-o', 'Candidate.olean', 'Candidate.lean'], log_path=out/'compile.log', budget_min=minutes, memory_max=memory, force=True, count_built=False)
    if not compiled.ok:
        diagnostic = ((out/'compile.log').read_bytes() if (out/'compile.log').exists()
                      else str(compiled.refused).encode())
        return {'ok': False, 'proof_accepted': False, 'outcome': 'timeout' if compiled.budget_exceeded else ('infrastructure_failure' if compiled.refused else 'compile_failed'),
                'diagnostic': put(cfg, diagnostic, 'text/plain'),
                'diagnostic_summary': diagnostic_summary(diagnostic),
                'candidate_file': str(out/'Candidate.lean'), 'fixture': fixture_id}
    inspector = DATA / 'AuditCandidate.lean'
    (out/'AuditCandidate.lean').write_bytes(inspector.read_bytes())
    audited = run_guarded(cfg, command + ['--run', 'AuditCandidate.lean', f['parent']], log_path=out/'audit.log', budget_min=minutes, memory_max=memory, force=True, count_built=False)
    record = None
    for line in reversed((out/'audit.log').read_text().splitlines() if (out/'audit.log').exists() else []):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and parsed.get('parent') == f['parent']:
                record = parsed; break
        except json.JSONDecodeError: pass
    validate(cfg, f)
    accepted = bool(audited.ok and record and record.get('type_hash') == f['type_hash'] and set(record.get('axioms', [])) <= set(f['standard_axioms']))
    return {'ok': accepted, 'proof_accepted': accepted, 'outcome': 'proof_accepted' if accepted else 'audit_failed', 'fixture': fixture_id, 'body': body_id,
            'audit': record, 'candidate_source_hash': hashlib.sha256(source).hexdigest(), 'diagnostic': put(cfg, (out/'audit.log').read_bytes(), 'text/plain'),
            'frontier_fresh': False, 'isolation': 'bwrap-no-network-no-provider-state',
            'dependency_validation':'project sources, Lake olean hash markers and package/toolchain pins; finalization also rebuilds original source'}


TACTICS = ('rfl', 'simp', 'simp_all', 'omega', 'norm_num', 'ring', 'linarith', 'nlinarith', 'positivity', 'grind', 'aesop')


def prove(cfg, fixture_id, *, tactics=None, minutes=1, memory='4G'):
    attempts = []
    for tactic in tactics or TACTICS:
        if tactic not in TACTICS and tactic not in ('duper [*]', 'hammer'):
            raise UsageError('tactic must be in the finite registered portfolio')
        # A single-line term also fits nested inline spans; a fixed two-space
        # tactic indent is invalid when the enclosing proof is deeper.
        body = put(cfg, ('by '+tactic).encode(), 'text/x-lean')
        receipt = check(cfg, fixture_id, body['id'], minutes=minutes, memory=memory)
        attempts.append({'tactic': tactic, **receipt})
        if receipt['proof_accepted']:
            return {'ok': True, 'proof_accepted': True, 'body': body, 'attempts': attempts, 'fixture': fixture_id}
        if receipt['outcome'] in ('infrastructure_failure', 'unsupported_candidate'):
            break
    return {'ok': False, 'proof_accepted': False, 'attempts': attempts, 'fixture': fixture_id, 'next_action': 'agent mathematical review'}
