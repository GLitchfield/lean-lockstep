"""Content-addressed node context; freshness remains explicit."""
import json

from lockstep.artifacts import put_json, read
from lockstep.commands.extract import state_dirs
from lockstep.gitutil import tree_hash
from lockstep.modules import module_to_path
from lockstep.nodes import find_node, load_nodes_json
from lockstep.store import Store
from lockstep.proofs import digest


def context(cfg, name, previous=None):
    node = find_node(cfg,name)
    data = load_nodes_json(cfg)
    current = tree_hash(cfg,state_dirs(cfg))
    with Store(cfg) as st:
        recent = [dict(r) for r in st.conn.execute('SELECT at,kind,detail FROM events WHERE node=? ORDER BY at DESC LIMIT 8',(node.name,))]
        operations = [dict(r) for r in st.conn.execute('SELECT id,kind,status,result_json FROM operations WHERE node=? ORDER BY created_at DESC LIMIT 8',(node.name,))]
        dependencies = []
        for dep in node.deps_actual[:12]:
            rows = st.conn.execute('SELECT name,module,type_pp FROM search WHERE name=? LIMIT 1',(dep,)).fetchall() if st.fts else []
            dependencies += [dict(r) for r in rows]
    path = cfg.root/module_to_path(node.module)
    payload = {'schema_version':1,'node':node.to_json(),'source':{'path':str(path),'sha256':digest(path) if path.exists() else None},
               'frontier_fresh':data.get('tree_hash')==current,'current_tree_hash':current,'indexed_tree_hash':data.get('tree_hash'),
               'selected_dependency_signatures':dependencies,'availability':'recorded compiled project index; check frontier_fresh',
               'recent_events':recent,'operations':operations}
    ref = put_json(cfg,payload,'context-bundle-v1')
    if previous:
        old = json.loads(read(cfg,previous))
        from lockstep.errors import UsageError
        if old.get('node',{}).get('name') != node.name:
            raise UsageError('previous context belongs to a different node')
        return {'bundle':ref,'changed':{k:v for k,v in payload.items() if old.get(k)!=v},'frontier_fresh':payload['frontier_fresh']}
    return {'bundle':ref,**payload}

