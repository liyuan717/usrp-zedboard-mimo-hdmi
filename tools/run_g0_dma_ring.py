#!/usr/bin/env python3
"""Finite native ARM DMA-ring/PRBS measurement; exclusive lease, no RF TX/SD/bit changes."""
import argparse,hashlib,http.server,json,re,subprocess,threading
from pathlib import Path
from board_command import command
from hardware_lease import HardwareLease
from workspace_storage_guard import check as disk_check,GIB

SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
HOST='169.254.209.83';BOARD='169.254.209.84'

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);build=a.driver.resolve();old=a.restore_module.resolve()
 receipt=json.loads((build/'receipt.json').read_text());assert receipt['module_layout_checked'] and not receipt['module_force_loaded']
 for p,h in receipt['source_sha256'].items():assert hashlib.sha256((v/p).read_bytes()).hexdigest()==h,p
 files={'ring.ko':build/'g0_dma_ring.ko','probe':build/'g0_dma_ring_probe','original.ko':old}
 checks={name:subprocess.check_output(['cksum',str(path)],text=True).split()[:2] for name,path in files.items()}
 class Handler(http.server.BaseHTTPRequestHandler):
  def do_GET(self):
   path=files.get(self.path.lstrip('/'))
   if self.client_address[0]!=BOARD or path is None:self.send_error(403);return
   data=path.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
  def log_message(self,*args):pass
 before=disk_check('before-native-DMA-ring-run');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 result={'scope':__doc__,'driver_build':str(build.relative_to(v)),'passed':False,'RF_transmission':False,'SD_or_bitstream_changed':False,'g0_passed':False,'production_SOURCE_or_video_passed':False,'native_pass_lines':[],'cleanup_restored':False}
 server=None
 try:
  with HardwareLease() as lease:
   server=http.server.ThreadingHTTPServer((HOST,0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();port=server.server_address[1]
   def run(name,text,timeout=30):
    (out/(name+'.sh')).write_text(text+'\n');return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
   initial=run('identity','''uname -a
cat /proc/sys/kernel/random/boot_id
test "$(uname -r)" = 6.12.0-xilinx
grep ' / ext4 ro,' /proc/mounts
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
ip -4 addr show eth0''')
   ids=re.findall(r'(?m)^([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\r?$',initial);assert len(ids)==1;result['boot_id']=ids[0]
   setup='mkdir -p /tmp/v3-dma-ring\n'
   for name in files:
    setup+=f'wget -q -O /tmp/v3-dma-ring/{name} http://{HOST}:{port}/{name}\n'
    setup+=f'test "$(cksum /tmp/v3-dma-ring/{name} | cut -d " " -f1,2)" = "'+ ' '.join(checks[name])+'"\n'
   setup+='''chmod 700 /tmp/v3-dma-ring/probe
rmmod g0_adc_capture
insmod /tmp/v3-dma-ring/ring.ko
test -c /dev/v3-g0-dma-ring
echo 2 > /sys/kernel/debug/iio/iio:device0/bist_prbs
echo PN9 > /sys/kernel/debug/iio/iio:device2/pseudorandom_err_check
sleep 1
cat /sys/kernel/debug/iio/iio:device2/pseudorandom_err_check
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
'''
   cleanup_needed=False
   try:
    cleanup_needed=True;run('setup',setup)
    native=run('native','/tmp/v3-dma-ring/probe',timeout=30)
    result['native_pass_lines']=[line.strip() for line in native.splitlines() if line.startswith('G0_RING_') or line.startswith('G0_NATIVE_')]
    assert any(line.startswith('G0_NATIVE_DMA_RING_PASS') for line in result['native_pass_lines'])
    result['passed']=True
   finally:
    if cleanup_needed:
     restored=run('restore','''echo 0 > /sys/kernel/debug/iio/iio:device0/bist_prbs
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
if test -d /sys/module/g0_dma_ring; then
 /tmp/v3-dma-ring/probe --status
 rmmod g0_dma_ring
fi
if ! test -d /sys/module/g0_adc_capture; then insmod /tmp/v3-dma-ring/original.ko capture_mib=32; fi
test ! -e /dev/v3-g0-dma-ring
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
cat /proc/sys/kernel/random/boot_id
dmesg | tail -18''')
     assert result['boot_id'] in restored;result['cleanup_restored']=True
   (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:
  if server:server.shutdown();server.server_close()
  (out/'disk-after.json').write_text(json.dumps(disk_check('after-native-DMA-ring-run'),indent=2)+'\n')
  if not (out/'receipt.json').exists():(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','driver','restore-module']:p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
