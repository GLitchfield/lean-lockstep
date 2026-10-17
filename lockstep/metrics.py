import os
import time
import uuid
from lockstep.artifacts import canonical
from lockstep.store import Store


def record(cfg, agent, command, full, returned):
    with Store(cfg) as st:
        if st.readonly: return
        st.conn.execute('INSERT INTO tool_metrics VALUES (?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,time.time(),agent,command,len(canonical(full)),len(canonical(returned)),os.environ.get('LOCKSTEP_OPERATION_ID') or None,None))
