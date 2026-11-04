from pathlib import Path
import subprocess,json,hashlib,sys,time,base64,re
R=Path('/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3/deliverables/usrp-zedboard-integration-20261026');B=Path(__file__).resolve().parent;sys.path.insert(0,str(R));from serial_query import query
p=query('mkdir /tmp/link-live/photo-seu-reobserve-r1',timeout=10);(B/'uart-mkdir.log').write_bytes(p)
for name in ['receiver_offline_diag.py','run_display.py','hdmi-presenter','make_runtime_closure.py','received-live.rfd']:
 receipt=B/(name+'.receipt.json');log=B/(name+'.http.log')
 with log.open('xb') as output:
  server=subprocess.Popen([sys.executable,str(R/'serve_once.py'),'--file',str(B/name),'--resource','/display-reobserve/'+name,'--receipt',str(receipt)],stdout=output,stderr=subprocess.STDOUT)
  try:
   end=time.monotonic()+5
   while '"ready": true' not in log.read_text():
    if server.poll() is not None or time.monotonic()>end:raise RuntimeError('transfer server failed')
    time.sleep(.05)
   out=query(f'wget -O /tmp/link-live/photo-seu-reobserve-r1/{name} http://169.254.209.83:8002/display-reobserve/{name}; sha256sum /tmp/link-live/photo-seu-reobserve-r1/{name}',timeout=20);(B/('uart-transfer-'+name+'.log')).write_bytes(out)
   assert hashlib.sha256((B/name).read_bytes()).hexdigest().encode() in out
   assert server.wait(timeout=5)==0 and json.loads(receipt.read_text())['served']
  finally:
   if server.poll() is None:server.terminate();server.wait(timeout=5)
out=query('python3 /tmp/link-live/photo-seu-reobserve-r1/make_runtime_closure.py',timeout=15);(B/'uart-closure.log').write_bytes(out)
print('PHOTO_REOBSERVE_START 60s quiet UART hold',flush=True)
out=query('python3 /tmp/link-live/photo-seu-reobserve-r1/run_display.py > /tmp/link-share/display-reobserve-controller.log 2>&1; cat /tmp/link-share/display-reobserve-controller.log',timeout=125);(B/'uart-run.log').write_bytes(out);print(out.decode(errors='replace')[-500:],flush=True)
cmd="""python3 - <<'PY2'
from pathlib import Path
import json,base64
p=Path('/tmp/link-share/photo-seu-reobserve-r1')
v={q.name:base64.b64encode(q.read_bytes()).decode() for q in p.iterdir() if q.is_file()}
print('EVIDENCE_B64='+base64.b64encode(json.dumps(v).encode()).decode())
PY2"""
out=query(cmd,timeout=25);(B/'uart-evidence.log').write_bytes(out)
match=re.search(rb'\r?\nEVIDENCE_B64=([A-Za-z0-9+/=]+)',out);assert match
files=json.loads(base64.b64decode(match.group(1)));E=B/'board-evidence';E.mkdir()
for name,data in files.items():
 assert '/' not in name and name!='..'
 (E/name).write_bytes(base64.b64decode(data))
result=json.loads((E/'result.json').read_text());summary={k:result.get(k) for k in ['scope','valid','failure','hold_completed','returncode','visual_confirmation','visual_pass','taint','LO','ADC','new_kernel_errors']};summary['proof']={k:result.get('proof',{}).get(k) for k in ['presented_at_hdmi_boundary','scanout_repeats','backend_failures','proof_failures']}
(B/'HOST-RESULT.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
