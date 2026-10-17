"""Internal worker entry point; always run under the enclosing guarded job."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from lockstep.artifacts import canonical, read
from lockstep.config import load_config
from lockstep.operations import show


def main():
    if not os.environ.get('LOCKSTEP_IN_GUARDED_STAGE'):
        raise SystemExit('stage runner must be launched by a guarded operation')
    cfg = load_config()
    request = json.loads(read(cfg, sys.argv[1]))
    op = show(cfg, request['operation_id'])
    stage = op['spec']['stages'][request['stage']]
    try:
        action = stage['action']
        if action in ('proof_check', 'proof_prove'):
            from lockstep import proofs
            if action == 'proof_check':
                result = proofs.check(cfg, stage['fixture'], stage['body'], minutes=stage.get('attempt_minutes', stage['minutes']/2), memory=stage['memory'])
            else:
                result = proofs.prove(cfg, stage['fixture'], tactics=stage.get('tactics'), minutes=stage.get('attempt_minutes', 1), memory=stage['memory'])
        elif action.startswith('node_'):
            from lockstep.finalize import run
            result = run(cfg, op, stage)
        elif action == 'probe':
            from lockstep.probes import execute
            result = execute(cfg, stage)
        else:
            raise ValueError('unsupported action')
    except Exception as exc:
        result = {'ok':False, 'proof_accepted':False, 'error':str(exc), 'error_type':type(exc).__name__}
    out = Path(sys.argv[2])
    temp = out.with_suffix('.tmp')
    temp.write_bytes(canonical(result))
    temp.replace(out)
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
