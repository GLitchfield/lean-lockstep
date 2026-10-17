from pathlib import Path

from lockstep import proofs, operations, finalize
from lockstep.artifacts import put
from lockstep.commands.automation import result
from lockstep.spec import Arg, CommandSpec, command


def capture(ctx, name, start=None, end=None):
    return result(ctx, {'fixture':proofs.capture(ctx.cfg, name, start=start, end=end)})


def check(ctx, fixture, body, minutes=10, memory='4G'):
    ref = put(ctx.cfg, Path(body).read_bytes(), 'text/x-lean')
    return result(ctx, operations.submit(ctx.cfg, ctx.agent, {'schema_version':1, 'kind':'proof_check', 'stages':[
        {'name':'check', 'action':'proof_check', 'fixture':fixture, 'body':ref['id'], 'minutes':minutes, 'memory':memory}]}))


def prove(ctx, fixture, minutes=15, memory='4G', tactics=None):
    return result(ctx, operations.submit(ctx.cfg, ctx.agent, {'schema_version':1, 'kind':'proof_prove', 'stages':[
        {'name':'native-portfolio', 'action':'proof_prove', 'fixture':fixture, 'minutes':minutes, 'memory':memory, 'tactics':tactics}]}))


command(CommandSpec('proof_capture',['proof','capture'],'Freeze an exact whole or local byte span; acceptance audits the complete parent.',capture,
    [Arg('name','registered node',positional=True,required=True),Arg('start','UTF-8 byte start','int'),Arg('end','exclusive UTF-8 byte end','int')]))
common=[Arg('fixture','fixture artifact ID',positional=True,required=True),Arg('minutes','total worker minutes','int',default=15),Arg('memory','memory cap',default='4G')]
command(CommandSpec('proof_check',['proof','check'],'Queue an isolated original-source candidate check.',check,common+[Arg('body','proof body file',required=True)]))
command(CommandSpec('proof_prove',['proof','prove'],'Try the finite native tactic route in the pinned project; neural/cloud off by default.',prove,common+[Arg('tactics','registered tactics in order','list')]))
command(CommandSpec('node_finish',['node','finish'],'Validate, insert, check, fully extract, audit and prepare a proof for review.',
    lambda ctx,name,body,fixture=None,policy='review',extract_memory=None: result(ctx,finalize.submit(ctx.cfg,ctx.agent,name,body,fixture_id=fixture,policy=policy,extract_memory=extract_memory)),
    [Arg('name','registered node',positional=True,required=True),Arg('body','proof body file',required=True),Arg('fixture','captured fixture ID'),Arg('policy','publication policy',default='review',choices=['review']),Arg('extract_memory','explicit full extraction cap; cannot exceed shared budget')]))
