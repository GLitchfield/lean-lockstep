import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from lockstep import operations, delivery
from lockstep.artifacts import compact, read
from lockstep.errors import UsageError
from lockstep.jobs import run_job, reserve_job, request_cancel
from lockstep.store import Store


def spec(*commands):
    return {'schema_version': 1, 'stages': [{'name': f'step-{i}', 'action': 'command', 'argv': [sys.executable, '-c', cmd], 'memory': '1G', 'minutes': 1} for i, cmd in enumerate(commands)]}


def test_operation_restart_idempotence_and_delivery(cfg):
    request = spec('print(1)', 'print(2)')
    op = operations.submit(cfg, 'tester', request)
    assert operations.submit(cfg, 'tester', request)['id'] == op['id']
    for _ in range(2):
        current = operations.show(cfg, op['id'])
        stage = next(s for s in current['stages'] if s['status'] != 'succeeded')
        run_job(cfg, stage['job_id'])
    final = operations.show(cfg, op['id'])
    assert final['status'] == 'succeeded'
    assert final['result']['proof_accepted'] is False
    operations.advance(cfg)
    assert len(delivery.pending(cfg, 'tester')) == 1
    delivery.pause(cfg, 'tester')
    assert delivery.claim(cfg, 'tester', 'client') is None
    delivery.pause(cfg, 'tester', False)
    event = delivery.claim(cfg, 'tester', 'client')
    assert delivery.claim(cfg, 'tester', 'second') is None
    delivery.ack(cfg, 'tester', event['event_id'], 'client')
    delivery.ack(cfg, 'tester', event['event_id'], 'client')
    assert delivery.pending(cfg, 'tester') == []


def test_atomic_admission(cfg):
    op = operations.submit(cfg, 'tester', spec('pass'))
    job = op['stages'][0]['job_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve_job(cfg, job, launching=True), range(2)))
    assert sorted(results) == [False, True]


def test_cancel_queued_does_not_execute(cfg):
    op = operations.submit(cfg, 'tester', spec('raise AssertionError()'))
    result = operations.control(cfg, op['id'], 'cancel')
    assert result['status'] == 'cancelled'
    assert run_job(cfg, op['stages'][0]['job_id'])['status'] == 'killed'


def test_failed_stage_retains_prior_receipt(cfg):
    op = operations.submit(cfg, 'tester', spec('pass', 'raise SystemExit(3)'))
    run_job(cfg, op['stages'][0]['job_id'])
    next_job = operations.show(cfg, op['id'])['stages'][1]['job_id']
    run_job(cfg, next_job)
    failed = operations.show(cfg, op['id'])
    assert failed['status'] == 'failed'
    after_cancel = operations.control(cfg, op['id'], 'cancel')
    assert after_cancel['status'] == 'failed'
    assert after_cancel['result'] == failed['result']
    with pytest.raises(UsageError):
        operations.control(cfg, op['id'], 'resume')
    retry = operations.control(cfg, op['id'], 'retry')
    assert retry['stages'][0]['attempt'] == 1
    assert retry['stages'][0]['status'] == 'succeeded'
    assert retry['stages'][1]['attempt'] == 2


def test_resource_block_is_visible(cfg):
    request = spec('pass')
    request['stages'][0]['memory'] = '999G'
    op = operations.submit(cfg, 'tester', request)
    assert op['status'] == 'blocked_resource'
    assert op['stages'][0]['job_id'] is None


def test_artifact_integrity_and_compact_output(cfg):
    payload = {'status': 'failed', 'diagnostic': 'x' * 10000}
    brief = compact(cfg, payload)
    assert len(json.dumps(brief)) < 4096
    assert json.loads(read(cfg, brief['detail']['id'])) == payload


def test_transaction_rollback(cfg):
    with pytest.raises(RuntimeError):
        with Store(cfg) as st:
            st.conn.execute("INSERT INTO automation_meta VALUES ('rollback','bad')")
            raise RuntimeError('crash')
    with Store(cfg) as st:
        assert not st.conn.execute("SELECT * FROM automation_meta WHERE key='rollback'").fetchall()
