"""Shadow-only incremental merge, always judged against a full compiled oracle.

Inputs are versioned manifests containing nodes/index/destinations, module import
graphs, source hashes, policy identity, and explicit module coverage. This module
has no path that writes the authoritative index or marks its frontier fresh.
"""
from lockstep.artifacts import canonical
from lockstep.errors import UsageError


def compare(baseline, targeted, oracle):
    for data in (baseline,targeted,oracle):
        if data.get('schema_version')!=1:
            raise UsageError('index shadow expects schema_version 1 manifests')
        for key in ('nodes','index','destinations','imports','sources','policy','covered_modules'):
            if key not in data: raise UsageError(f'index shadow manifest missing {key}')
    old, new = baseline['sources'], oracle['sources']
    changed={name for name in set(old)|set(new) if old.get(name)!=new.get(name)}
    reverse={}
    for graph in (baseline['imports'],oracle['imports']):
        for name, imports in graph.items():
            for imported in imports: reverse.setdefault(imported,set()).add(name)
    invalidated=set(changed)
    pending=list(changed)
    while pending:
        for dependent in reverse.get(pending.pop(),()):
            if dependent not in invalidated:
                invalidated.add(dependent); pending.append(dependent)
    if baseline['policy']!=oracle['policy']:
        invalidated.update(set(old)|set(new))
    deleted=set(old)-set(new)
    missing=invalidated-deleted-set(targeted['covered_modules'])
    mismatches=[]; merged={}
    if targeted['policy']!=oracle['policy']: mismatches.append('targeted policy differs from oracle')
    for name in invalidated-deleted:
        if targeted['sources'].get(name)!=new.get(name): mismatches.append('targeted source differs: '+name)
    for key in ('nodes','index','destinations'):
        rows=[r for r in baseline[key] if r['module'] not in invalidated]
        rows += [r for r in targeted[key] if r['module'] in invalidated-deleted]
        names=[r['name'] for r in rows]
        if len(set(names))!=len(names): mismatches.append(key+': duplicate declaration names')
        merged[key]=sorted(rows,key=lambda r:(r['name'],r['module']))
        if canonical(merged[key])!=canonical(sorted(oracle[key],key=lambda r:(r['name'],r['module']))):
            mismatches.append(key+': differs from full oracle')
    return {'schema_version':1,'equivalent':not missing and not mismatches,'authoritative':False,'frontier_updated':False,
            'invalidated_modules':sorted(invalidated),'deleted_modules':sorted(deleted),'missing_coverage':sorted(missing),
            'mismatches':mismatches,'merged':merged}
