import time

from lockstep.store import Store


def test_claims_expire(cfg):
    with Store(cfg) as st:
        st.claim("TCore.a", "agent1", ttl_hours=0.0000001, note="n")
        assert "TCore.a" in st.claims()
    time.sleep(0.01)
    with Store(cfg) as st:
        assert "TCore.a" not in st.claims()
        outcomes = [a["outcome"] for a in st.attempts("TCore.a")]
        assert "expired" in outcomes


def test_claim_release_records_attempt(cfg):
    with Store(cfg) as st:
        st.claim("TCore.b", "agent1", ttl_hours=1)
        assert st.release("TCore.b", "agent1", outcome="abandoned")
        assert not st.release("TCore.b", "agent1")
        assert [a["outcome"] for a in st.attempts("TCore.b")] == ["abandoned"]


def test_two_agents_see_each_other(cfg):
    with Store(cfg) as st1:
        st1.claim("TCore.c", "agent1", ttl_hours=1)
    with Store(cfg) as st2:
        assert st2.claims()["TCore.c"]["agent"] == "agent1"


def test_job_state_machine(cfg):
    from lockstep.jobs import run_job, submit

    job = submit(cfg, kind="build", node=None, argv=["doctor"], agent="tester", queue=True)
    with Store(cfg) as st:
        assert st.job(job["id"])["status"] == "queued"
    done = run_job(cfg, job["id"])
    assert done["status"] in ("done", "failed") and done["exit_code"] is not None
    with Store(cfg) as st:
        assert st.job(job["id"][:8])["id"] == job["id"]


def test_search_index(cfg):
    with Store(cfg) as st:
        st.search_rebuild([{"name": "TCore.sum_two_squares_mod_four", "module": "TCore.X", "kind": "theorem", "doc": "a sum of two squares is never three mod four", "type_pp": "True"},
                           {"name": "TCore.other", "module": "TCore.Y", "kind": "def", "doc": "unrelated", "type_pp": "Nat"}], "s1")
        hits = st.search("sum of two squares mod four")
        assert hits and hits[0]["name"] == "TCore.sum_two_squares_mod_four"
        assert st.search_stamp() == "s1"


def test_cache_roundtrip(cfg):
    with Store(cfg) as st:
        st.cache_put("t1", {"a": 1}, [{"b": 2}])
        assert st.cache_get("t1") == ({"a": 1}, [{"b": 2}])
        assert st.cache_get("nope") is None


def test_targeted_cache_entries_do_not_evict_full_ones(cfg):
    with Store(cfg) as st:
        st.cache_put("full1", {"a": 1}, [])
        for i in range(12):
            st.cache_put(f"t{i}+Mod{i}", {"t": i}, [])
        assert st.cache_get("full1") == ({"a": 1}, [])
        assert st.cache_get("t0+Mod0") == ({"t": 0}, [])
