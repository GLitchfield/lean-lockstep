from __future__ import annotations

import json
from pathlib import Path

from lockstep import operations, delivery
from lockstep.artifacts import compact, read
from lockstep.output import Result
from lockstep.spec import Arg, CommandSpec, command
from lockstep.store import Store


def result(ctx, payload):
    brief=payload if getattr(ctx,'full_output',False) else compact(ctx.cfg,payload)
    return Result(0, brief, [json.dumps(brief, ensure_ascii=False)])


def submit(ctx, spec, key=None):
    return result(ctx, operations.submit(ctx.cfg, ctx.agent, json.loads(Path(spec).read_text()), key=key))


def show(ctx, id):
    return result(ctx, operations.show(ctx.cfg, id))


def listing(ctx):
    with Store(ctx.cfg) as st:
        rows = [dict(r) for r in st.conn.execute('SELECT id,kind,agent,node,status,updated_at FROM operations ORDER BY created_at DESC LIMIT 50')]
    return result(ctx, {'operations': rows})


def artifact(ctx, id, offset=0, limit=4096):
    from lockstep.errors import UsageError
    if offset < 0 or not 1 <= limit <= 65536:
        raise UsageError('offset must be nonnegative; limit must be 1..65536 bytes')
    raw = read(ctx.cfg, id)
    end=min(len(raw),offset+limit)
    while True:
        try:
            data=raw[offset:end].decode('utf-8'); break
        except UnicodeDecodeError as exc:
            if exc.reason!='unexpected end of data':
                raise UsageError('artifact is not UTF-8 text at this byte offset')
            end-=1
    if end==offset and offset<len(raw):
        raise UsageError('limit is too small for the next UTF-8 character')
    return Result(0, {'id': id, 'text': data, 'bytes': len(raw), 'offset': offset, 'next_offset': end if end < len(raw) else None}, [data])


def inbox(ctx):
    return result(ctx, {'deliveries': delivery.pending(ctx.cfg, ctx.agent)})


def context(ctx, name, previous=None):
    from lockstep.bundles import context as bundle
    return result(ctx, bundle(ctx.cfg,name,previous))


command(CommandSpec('operation_submit', ['operation', 'submit'], 'Submit a finite durable workflow; no automatic external retries.', submit,
                    [Arg('spec', 'JSON workflow path', required=True), Arg('key', 'idempotency key')]))
command(CommandSpec('operation_show', ['operation', 'show'], 'Compact operation outcome and receipt references.', show, [Arg('id', 'operation ID', positional=True, required=True)]))
command(CommandSpec('operation_list', ['operation', 'list'], 'Recent workflows.', listing))
command(CommandSpec('context', ['context'], 'Compact source-identified context, recorded signatures and recent outcomes.', context,
                    [Arg('name','registered node',positional=True,required=True),Arg('previous','previous bundle artifact ID for a delta')]))
for action in ('resume', 'retry', 'cancel'):
    def handler(ctx, id, _action=action):
        return result(ctx, operations.control(ctx.cfg, id, _action))
    command(CommandSpec('operation_' + action, ['operation', action], f'{action.capitalize()} a workflow; accepted stages are preserved.', handler, [Arg('id', 'operation ID', positional=True, required=True)]))
command(CommandSpec('operation_reconcile', ['operation', 'reconcile'], 'Detect dead runners without replaying uncertain effects.', lambda ctx: result(ctx, operations.reconcile(ctx.cfg))))
def resources(ctx):
    from lockstep.resources import reconcile
    return result(ctx,reconcile(ctx.cfg))
command(CommandSpec('resources_reconcile',['resources','reconcile'],'Release reservations only after confirming the owning cgroup stopped.',resources))
command(CommandSpec('artifact_show', ['artifact', 'show'], 'Fetch an immutable artifact page explicitly.', artifact,
                    [Arg('id', 'content hash', positional=True, required=True), Arg('offset', 'byte offset', 'int', default=0), Arg('limit', 'maximum bytes', 'int', default=4096)]))
command(CommandSpec('inbox_list', ['inbox', 'list'], 'Unacknowledged results for LOCKSTEP_AGENT; does not wake a client.', inbox))
command(CommandSpec('inbox_claim', ['inbox', 'claim'], 'Lease one event to a connected consumer.',
                    lambda ctx, consumer: result(ctx, {'delivery': delivery.claim(ctx.cfg, ctx.agent, consumer)}), [Arg('consumer', 'connection identity', required=True)]))
command(CommandSpec('inbox_ack', ['inbox', 'ack'], 'Acknowledge a leased event after the client accepted it.',
                    lambda ctx, id, consumer: result(ctx, delivery.ack(ctx.cfg, ctx.agent, id, consumer)), [Arg('id', 'event ID', positional=True, required=True), Arg('consumer', 'connection identity', required=True)]))
for action in ('pause', 'resume'):
    def handler(ctx, _pause=action == 'pause'):
        return result(ctx, delivery.pause(ctx.cfg, ctx.agent, _pause))
    command(CommandSpec('inbox_' + action, ['inbox', action], f'{action.capitalize()} result delivery; compute continues.', handler))
