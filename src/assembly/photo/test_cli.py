"""Parse the exact supervisor receiver argv without executing receiver.run."""
from pathlib import Path
import ast,sys,json,io,contextlib
D=Path(__file__).resolve().parent.parent
B=Path(__file__).resolve().parent
C=D/'candidates/finite-video-long-r2'
sys.path[:0]=[str(C/'runtime-tools'),str(D/'resume6-candidate/runtime/python/tools'),str(D/'resume6-candidate/runtime/python')]
import rf_native_video_receiver as receiver
seen=[]
def fake(args):
    seen.append(vars(args))
    return {'schema':'offline-cli-only','valid':True,'accepted':[{}]}
receiver.run=fake
reports=[]
for folder,wanted in [
    ('finite-video-r1-20261027', 2),
    ('finite-video-r1d-20261027', 0),
    ('photo-seu-reproduce-r2-20261030', 0),
]:
    tree=ast.parse((D/folder/'board_supervisor.py').read_text())
    assignment=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='video_argv' for t in n.targets))
    argv=eval(compile(ast.Expression(assignment.value),'<supervisor argv>','eval'),{'python':'/usr/bin/python3.12','R':Path('/tmp/link-live'),'O':Path('/tmp/link-share'),'CONSUMER_STOP':Path('/tmp/link-live/cli-fixture.consumer-stop')})
    sys.argv=argv[1:];out=io.StringIO();err=io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
        try:code=receiver.main()
        except SystemExit as e:code=e.code
    assert code==wanted,(folder,code,err.getvalue())
    reports.append({'folder':folder,'returncode':code,'exact_argv':argv,'stderr':err.getvalue(),'hardware_access':False})
assert seen[-1]['idle_timeout_s']==130 and seen[-1]['hold_final_frame_ms']==10000 and seen[-1]['records']==42
(B/'CLI-CHECK.json').write_text(json.dumps({'pass':True,'old_reproduced_and_new_full_argv_accepted':reports},indent=2)+'\n')
print('Exact supervisor CLI: original rejected; r1d and r1h corrected CLI accepted; no devices or subprocesses opened.')
