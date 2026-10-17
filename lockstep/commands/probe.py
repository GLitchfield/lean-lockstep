import json
from pathlib import Path
from lockstep import probes
from lockstep.commands.automation import result
from lockstep.spec import Arg, CommandSpec, command

command(CommandSpec('probe_run',['probe','run'],'Queue a versioned finite probe; negative results are bounded and give no proof credit.',
    lambda ctx,spec:result(ctx,probes.submit(ctx.cfg,ctx.agent,json.loads(Path(spec).read_text()))),
    [Arg('spec','JSON probe specification',required=True)]))
command(CommandSpec('probe_replay',['probe','replay'],'Independently replay a saved witness under the same source and semantic version.',
    lambda ctx,id:result(ctx,probes.replay(ctx.cfg,id)),[Arg('id','probe result artifact ID',positional=True,required=True)]))
