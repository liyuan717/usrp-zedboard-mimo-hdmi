#!/usr/bin/env python3
"""Load the matched, previously qualified HDMI modules into G0 RAM only.
No provider replacement, driver override, SD write or RF startup.
"""
import argparse,hashlib,http.server,json,threading,zlib
from pathlib import Path
from board_command import command, require_boot_id
from hardware_lease import HardwareLease
from workspace_storage_guard import check

SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
HOST,BOARD='169.254.209.83','169.254.209.84'
NAMES=['cec','i2c-xiic','clk-axi-clkgen','adv7511','adi_axi_hdmi_present']

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 reference=v/'build/g0-r9-reference';abi=json.loads((v/'build/g0-r9-abi-r1/receipt.json').read_text())
 assert abi['passed'] and abi['module_layout_checked'] and not abi['mismatches']
 assert zlib.crc32((reference/'zImage').read_bytes())==0x13f60426
 files={}
 for name in NAMES:
  matches=list((reference/'extracted-modules').rglob(name+'.ko'));assert len(matches)==1
  files[name+'.ko']=matches[0]
 crc={n:f'{zlib.crc32(p.read_bytes()):08x}' for n,p in files.items()}
 result={'scope':__doc__,'completed':False,'SD_written':False,'RF_started':False,
         'provider_or_driver_override_changed':False,'forced_module_load':False,
         'kernel_crc32':'13f60426','modules':{n:{'source':str(p.relative_to(v)),'bytes':p.stat().st_size,
         'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'crc32':crc[n]} for n,p in files.items()}}
 (out/'disk-before.json').write_text(json.dumps(check('before-matched-HDMI-modules'),indent=2)+'\n');server=None
 try:
  with HardwareLease() as lease:
   def run(name,script):
    (out/(name+'.sh')).write_text(script+'\n')
    return command(SERIAL,script,out/(name+'.log'),30,lease=lease).decode(errors='replace')
   identity=run('identity','''test "$(uname -r)" = 6.12.0-xilinx
cat /proc/sys/kernel/random/boot_id
grep ' / ext4 ro,' /proc/mounts
test -d /sys/module/g0_source_ring
test ! -e /dev/dri/card0
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(devmem 0x7c430008)" = 0x00000000
''')
   result['boot_id']=require_boot_id(identity,getattr(a,'expected_boot_id',None))
   class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
     p=files.get(self.path.lstrip('/'))
     if self.client_address[0]!=BOARD or p is None:self.send_error(403);return
     data=p.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
   server=http.server.ThreadingHTTPServer((HOST,0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   remote='/tmp/v3-g0-hdmi-r9';script='mkdir -p '+remote+'\n'
   for name in files:
    script+=f'test ! -e {remote}/{name}\nwget -q -O {remote}/{name} http://{HOST}:{server.server_address[1]}/{name}\n'
    script+=f'test "$(busybox crc32 {remote}/{name} | cut -d " " -f1)" = {crc[name]}\n'
   for name in NAMES:
    script+=f'test ! -d /sys/module/{name.replace("-","_")}\ninsmod {remote}/{name}.ko\n'
   script+='''test -c /dev/dri/card0
cat /proc/modules
for s in /sys/class/drm/card0-*/status; do cat "$s"; done
dmesg | tail -25
'''
   run('load-matched-modules',script);result['completed']=True
 finally:
  if server:server.shutdown();server.server_close()
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-matched-HDMI-modules'),indent=2)+'\n')
 print('MATCHED_HDMI_MODULES_LOADED_WITH_SOURCE_OWNER_RETAINED',flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--expected-boot-id',help='require the qualified RAM boot before loading modules')
 main(p.parse_args())
