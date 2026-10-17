from lockstep.shadow_index import compare


def manifest(sources,imports=None,nodes=None):
    return {'schema_version':1,'sources':sources,'imports':imports or {},'policy':'p1','covered_modules':list(sources),
            'nodes':nodes or [{'name':m+'.lemma','module':m,'type_hash':v} for m,v in sources.items()], 'index':[],'destinations':[]}


def test_reverse_dependency_invalidation_and_missing_coverage():
    base=manifest({'A':'a','B':'b'},{'B':['A']})
    oracle=manifest({'A':'new','B':'b'},{'B':['A']})
    target=manifest({'A':'new'})
    failed=compare(base,target,oracle)
    assert failed['missing_coverage']==['B']
    assert failed['invalidated_modules']==['A','B']
    passed=compare(base,oracle,oracle)
    assert passed['equivalent']
    assert not passed['authoritative'] and not passed['frontier_updated']


def test_deletion_rename_and_policy_change():
    base=manifest({'A':'a','B':'b'})
    oracle=manifest({'A':'a','C':'c'})
    target=manifest({'C':'c'})
    result=compare(base,target,oracle)
    assert result['equivalent'] and result['deleted_modules']==['B']
    oracle['policy']='p2';target['policy']='p2'
    assert compare(base,target,oracle)['missing_coverage']==['A']
