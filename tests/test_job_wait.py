import pytest

from lockstep.commands.jobs import run_wait
from lockstep.context import Ctx
from lockstep.errors import UsageError
from lockstep.jobs import submit_run
from lockstep.store import Store


def queued(cfg):
    return submit_run(cfg, command=["true"], why="wait test", outputs=["runs/result"],
                      agent="tester", queue=True)


@pytest.mark.parametrize("status,rc", [("done", 0), ("failed", 1), ("killed", None)])
def test_wait_reports_every_terminal_outcome(cfg, status, rc, monkeypatch):
    job = queued(cfg)
    with Store(cfg) as st:
        st.job_update(job["id"], status=status, exit_code=rc)
    monkeypatch.setattr("lockstep.commands.jobs.time.sleep", lambda _: pytest.fail("terminal job slept"))
    result = run_wait(Ctx(cfg), job["id"][:15])
    assert result.payload == {"job_id": job["id"], "status": status, "terminal": True,
                              "timed_out": False, "job_exit_code": rc,
                              "log_path": job["log_path"], "outputs": ["runs/result"]}
    assert "report the result" in result.human[0]


def test_wait_observes_completion_without_user_polling(cfg, monkeypatch):
    job = queued(cfg)
    now = [0.0]
    transitions = iter(["starting", "running", "done"])
    monkeypatch.setattr("lockstep.commands.jobs.time.monotonic", lambda: now[0])

    def advance(seconds):
        # Another connection can publish state changes while the waiter is sleeping.
        now[0] += seconds
        state = next(transitions)
        with Store(cfg) as st:
            st.job_update(job["id"], status=state, exit_code=0 if state == "done" else None)

    monkeypatch.setattr("lockstep.commands.jobs.time.sleep", advance)
    result = run_wait(Ctx(cfg), job["id"], timeout=5)
    assert result.payload["terminal"] and result.payload["status"] == "done"
    assert now[0] == 3


def test_wait_expiration_does_not_finish_or_cancel_job(cfg, monkeypatch):
    job = queued(cfg)
    now = [0.0]
    monkeypatch.setattr("lockstep.commands.jobs.time.monotonic", lambda: now[0])
    monkeypatch.setattr("lockstep.commands.jobs.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    result = run_wait(Ctx(cfg), job["id"], timeout=2)
    assert now[0] == 2
    assert result.payload == {"job_id": job["id"], "status": "queued", "terminal": False,
                              "timed_out": True}
    with Store(cfg) as st:
        assert st.job(job["id"])["status"] == "queued"


def test_wait_zero_is_an_immediate_check(cfg, monkeypatch):
    job = queued(cfg)
    monkeypatch.setattr("lockstep.commands.jobs.time.sleep", lambda _: pytest.fail("zero timeout slept"))
    assert run_wait(Ctx(cfg), job["id"], timeout=0).payload["timed_out"]


@pytest.mark.parametrize("timeout", [-1, 61])
def test_wait_rejects_unbounded_or_negative_windows(cfg, timeout):
    with pytest.raises(UsageError, match="between 0 and 60"):
        run_wait(Ctx(cfg), "missing", timeout=timeout)


def test_wait_unknown_job_is_not_reported_as_completion(cfg):
    with pytest.raises(UsageError, match="unknown job"):
        run_wait(Ctx(cfg), "missing", timeout=0)


def test_mcp_exposes_the_same_pending_state(cfg):
    from lockstep.mcp_server import _make_tool
    from lockstep.spec import REGISTRY

    job = queued(cfg)
    tool = _make_tool(next(s for s in REGISTRY if s.name == "job_wait"))
    result = tool(id=job["id"], timeout=0)
    assert result["ok"] and not result["terminal"] and result["timed_out"]
