#!/usr/bin/env python3
"""Run the static shared SOURCE-session component probe on the actual A9.
No RF, SOURCE DMA, HDMI or SD operation; not video/throughput signoff.
"""
import argparse,hashlib,http.server,json,re,threading,zlib
from pathlib import Path
from hardware_lease import HardwareLease
from board_command import command
from run_g0_source_air import SERIAL,HOST,BOARD

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 proof=json.loads((a.probe/'receipt.json').read_text());binary=a.probe/'g0-source-session-probe'
 assert proof['completed'] and proof['host_normal_ASan_UBSan_byteexact'] and hashlib.sha256(binary.read_bytes()).hexdigest()==proof['ARM_binary_sha256']
 for name,sha in proof['source_sha256'].items():assert hashlib.sha256((v/name).read_bytes()).hexdigest()==sha
 assert re.fullmatch('[A-Za-z0-9_-]{1,64}',out.name)
 remote='/tmp/'+out.name;crc=f'{zlib.crc32(binary.read_bytes()):08x}';server=None
 result=dict(completed=False,RF_started=False,DMA_started=False,HDMI_started=False,SD_written=False,full_G0_passed=False)
 try:
  with HardwareLease() as lease:
   def run(name,text,timeout=25):
    (out/(name+'.sh')).write_text(text+'\n');return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
   text=run('identity',f'''test "$(uname -m)" = armv7l
cat /proc/sys/kernel/random/boot_id
grep ' / ext4 ro,' /proc/mounts
test "$(devmem 0x7c430008)" = 0x00000000
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test ! -e {remote}''')
   ids=re.findall(r'(?m)^([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\r?$',text)
   assert len(ids)==1;result['boot_id']=ids[0]
   class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
     if self.client_address[0]!=BOARD or self.path!='/probe':self.send_error(403);return
     data=binary.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
   server=http.server.ThreadingHTTPServer((HOST,0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   text=run('probe',f'''mkdir {remote}
wget -q -O {remote}/probe http://{HOST}:{server.server_address[1]}/probe
test "$(busybox crc32 {remote}/probe | cut -d ' ' -f1)" = {crc}
chmod 700 {remote}/probe
{remote}/probe > {remote}/result.json
cat {remote}/result.json''')
   rows=[line.strip() for line in text.splitlines() if line.startswith('{"completed":')]
   assert len(rows)==1;native=json.loads(rows[0]);host=proof['host_result']
   assert native['completed'] and native['source_ingress_context_bytes']==48
   for name,value in host.items():
    if name!='source_ingress_context_bytes':assert native[name]==value,(name,native[name],value)
   (out/'native.json').write_text(json.dumps(native,indent=2)+'\n')
   run('release-copy',f'''test "$(busybox crc32 {remote}/probe | cut -d ' ' -f1)" = {crc}
python3 - <<'RELEASE'
import os
target='{remote}/probe'
for pid in os.listdir('/proc'):
 if not pid.isdigit():continue
 try:
  assert target not in open('/proc/'+pid+'/maps').read()
  for fd in os.listdir('/proc/'+pid+'/fd'):
   try:assert os.readlink('/proc/'+pid+'/fd/'+fd)!=target
   except FileNotFoundError:pass
 except (FileNotFoundError,ProcessLookupError):pass
os.unlink(target)
RELEASE''')
   result.update(completed=True,native_CortexA9_component_passed=True,ARM_binary_sha256=proof['ARM_binary_sha256'],native=native)
 finally:
  if server:server.shutdown();server.server_close()
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--probe',type=Path,required=True)
 p.add_argument('--output',type=Path,required=True);main(p.parse_args())
