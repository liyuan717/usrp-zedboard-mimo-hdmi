#!/usr/bin/env python3
"""Run bounded video-unit fixtures on actual Cortex-A9; no RF/DMA/display or video-system signoff."""
import argparse,hashlib,http.server,json,re,socket,struct,subprocess,threading,zlib
from pathlib import Path
from board_command import command
from hardware_lease import HardwareLease
from workspace_storage_guard import check,GIB
SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
HOST,BOARD='169.254.209.83','169.254.209.84'

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 assert re.fullmatch(r'[A-Za-z0-9_-]{1,64}',out.name)
 root=a.fixtures.resolve();host=json.loads((root/'receipt.json').read_text());arm=json.loads((a.arm/'receipt.json').read_text())
 if a.component_au:
  assert host['host_actual_C_ASan_UBSan_passed']
  host['scenarios']=[{'name':r['name'],'probe_drop_AUs':r.get('probe_drop_AUs',0),'results':{'normal':next(x for x in r['results'] if x['name']=='normal')}} for r in host['cases']]
 else:assert host['host_ASan_UBSan_actual_C_passed']
 assert host['source_sha256']==arm['source_sha256']
 for name,digest in arm['source_sha256'].items():assert hashlib.sha256((v/name).read_bytes()).hexdigest()==digest
 files={'probe':a.arm.resolve()/'probe'}
 for i,row in enumerate(host['scenarios']):files['e'+str(i)]=root/row['name']/('input.units' if a.component_au else 'events.bin')
 sizes={k:f.stat().st_size for k,f in files.items()};assert sum(sizes.values())<32*1024*1024
 crc={k:f'{zlib.crc32(f.read_bytes()):08x}' for k,f in files.items()};remote='/tmp/'+out.name
 result={'scope':__doc__,'component':'bounded_partial_AU' if a.component_au else 'video_NAL','native_CortexA9_component_passed':False,'RF_started':False,'DMA_started':False,'SD_written':False,'PL_changed':False,'full_G0_or_video_passed':False,'source_sha256':arm['source_sha256'],'runs':[],'board_directory':remote}
 server=None
 (out/'disk-before.json').write_text(json.dumps(check('before-native-video-fragments'),indent=2)+'\n')
 try:
  with HardwareLease() as lease:
   def run(name,text,timeout=30):
    subprocess.run(['sh','-n'],input=text,text=True,check=True);(out/(name+'.sh')).write_text(text+'\n')
    return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
   identity=run('identity','''test "$(uname -m)" = armv7l
test "$(uname -r)" = 6.12.0-xilinx
cat /proc/sys/kernel/random/boot_id
grep ' / ext4 ro,' /proc/mounts
grep -q ' /sys/kernel/debug debugfs ' /proc/mounts || mount -t debugfs debugfs /sys/kernel/debug
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test "$(devmem 0x7c430008)" = 0x00000000
python3 -c "import os; s=os.statvfs('/tmp'); assert s.f_bavail*s.f_frsize>=20000000"''')
   ids=re.findall(r'(?m)^([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\r?$',identity);assert len(ids)==1;result['boot_id']=ids[0]
   class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
     f=files.get(self.path.lstrip('/'))
     if self.client_address[0]!=BOARD or f is None:self.send_error(403);return
     data=f.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
   server=http.server.ThreadingHTTPServer((HOST,0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   setup=f'test ! -e {remote}\nmkdir {remote}\n'
   for name in ['probe']:
    setup+=f'wget -q -O {remote}/{name} http://{HOST}:{server.server_address[1]}/{name}\n'
    setup+=f'busybox crc32 {remote}/{name}\n'
   setup+=f'chmod 700 {remote}/probe\n'
   log=run('transfer-inputs',setup)
   assert re.search(r'(?m)^'+crc['probe']+' '+re.escape(remote+'/probe')+r'\r?$',log)
   for i,row in enumerate(host['scenarios']):
    name='e'+str(i)
    log=run('transfer-case-'+str(i),f'wget -q -O {remote}/{name} http://{HOST}:{server.server_address[1]}/{name}\nbusybox crc32 {remote}/{name}')
    assert re.search(r'(?m)^'+crc[name]+' '+re.escape(remote+'/'+name)+r'\r?$',log)
    expected=(root/row['name']/('normal.aus' if a.component_au else 'normal.units')).read_bytes();expected_size=len(expected);assert expected_size<2000000
    # Fast component output can interleave with UART shell echo. Return the
    # bounded JSON file together with the full units through the same TCP
    # stream instead of treating an echoed terminal line as structured data.
    extra=' '+str(row['probe_drop_AUs']) if a.component_au else ''
    run('run-'+str(i),f'{remote}/probe {remote}/e{i} {remote}/u{i}{extra} > {remote}/r{i}')
    with socket.socket() as listener:
     listener.bind((HOST,0));listener.listen(1);listener.settimeout(15);port=listener.getsockname()[1];transfer={}
     def receive():
      try:
       conn,peer=listener.accept();assert peer[0]==BOARD
       with conn:
        conn.settimeout(15);parts=[];count=0
        while True:
         data=conn.recv(min(65536,expected_size+16389-count))
         if not data:break
         parts.append(data);count+=len(data);assert count<=expected_size+16388
       packed=b''.join(parts);assert len(packed)>=4
       size,=struct.unpack_from('<I',packed);assert 0<size<=16384 and len(packed)==4+size+expected_size
       report=packed[4:4+size];actual=packed[4+size:];assert actual==expected
       transfer.update(bytes=len(actual),sha256=hashlib.sha256(actual).hexdigest(),native_stats=json.loads(report))
       (out/('u'+str(i)+'.units')).write_bytes(actual);(out/('r'+str(i)+'.json')).write_bytes(report)
      except BaseException as e:transfer['error']=repr(e)
     thread=threading.Thread(target=receive,daemon=True);thread.start()
     text=f'''python3 -c "import socket,pathlib,struct; p=pathlib.Path('{remote}'); j=(p/'r{i}').read_bytes(); u=(p/'u{i}').read_bytes(); s=socket.create_connection(('{HOST}',{port}),5); s.sendall(struct.pack('<I',len(j))+j+u); s.shutdown(socket.SHUT_WR); s.close()"'''
     run('return-'+str(i),text);thread.join(timeout=20);assert not thread.is_alive() and 'error' not in transfer,transfer
    stats=transfer.pop('native_stats');old=row['results']['normal']['stats']
    for key,value in old.items():
     if key not in ('cpu_seconds','wall_seconds','context_bytes'):assert stats[key]==value,(row['name'],key,stats[key],value)
    result['runs'].append({'name':row['name'],'native_stats':stats,'full_output_bytes_equal_host_independent_original_NAL_oracle':True,'returned_output':transfer})
    # Only regenerated input and byte-identical returned component output in
    # this unique RAM directory; retain the host oracle and all raw RF data.
    run('release-case-'+str(i),f'rm {remote}/e{i} {remote}/u{i} {remote}/r{i}')
   run('idle-after','''test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(devmem 0x7c430008)" = 0x00000000''')
   result['native_CortexA9_component_passed']=True
 finally:
  if server:server.shutdown();server.server_close()
  (out/'disk-after.json').write_text(json.dumps(check('after-native-video-fragments'),indent=2)+'\n');(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['fixtures','arm','output']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--component-au',action='store_true')
 main(p.parse_args())
