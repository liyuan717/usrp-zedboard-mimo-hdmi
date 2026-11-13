#!/usr/bin/env python3
"""Reuse the existing raw ADC G0 image/driver and verify digital PRBS before finite RF diagnosis."""
import argparse,http.server,json,re,socket,subprocess,threading,zlib
from pathlib import Path
from board_command import command,require_boot_id
from hardware_lease import HardwareLease
from check_g0_adc_capture import inspect
from workspace_storage_guard import check,GIB
HOST,BOARD='169.254.209.83','169.254.209.84'
SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);driver=a.driver.resolve();abi=json.loads((driver/'abi.json').read_text());assert abi['import_count']==38 and not abi['mismatches'] and not abi['module_force_loaded']
 module=driver/'g0_adc_capture.ko';data=module.read_bytes();crc=f'{zlib.crc32(data):08x}';assert crc=='0f88d2fa'
 result={'scope':__doc__,'driver_ABI_imports':38,'RF_started':False,'SD_written':False,'board_TX_enabled':False,'diagnostic_only_not_production_raw_path':True,'PRBS_passed':False}
 before=check('before-raw-ADC-diagnostic-native');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 server=None
 try:
  with HardwareLease() as lease:
   def run(name,text,timeout=25):
    subprocess.run(['sh','-n'],input=text,text=True,check=True);(out/(name+'.sh')).write_text(text+'\n')
    return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
   if a.expected_boot_id:
    require_boot_id(run('boot-identity','cat /proc/sys/kernel/random/boot_id'),a.expected_boot_id)
   class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
     if self.client_address[0]!=BOARD or self.path!='/adc.ko':self.send_error(403);return
     self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
   server=http.server.ThreadingHTTPServer((HOST,0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   setup='''test "$(uname -r)" = 6.12.0-xilinx
cat /proc/sys/kernel/random/boot_id
grep ' / ext4 ro,' /proc/mounts
grep -q ' /sys/kernel/debug debugfs ' /proc/mounts || mount -t debugfs debugfs /sys/kernel/debug
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test ! -d /sys/module/g0_source_ring
test ! -d /sys/module/g0_adc_capture
'''+f'''wget -q -O /tmp/v3-g0-adc-r2.ko http://{HOST}:{server.server_address[1]}/adc.ko
test "$(busybox crc32 /tmp/v3-g0-adc-r2.ko)" = "{crc} /tmp/v3-g0-adc-r2.ko"
insmod /tmp/v3-g0-adc-r2.ko capture_mib=32
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
cat /sys/module/g0_adc_capture/parameters/capture_mib'''
   identity=run('install',setup);ids=re.findall(r'(?m)^([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\r?$',identity);assert len(ids)==1;result['boot_id']=ids[0]
   assert 'buffer_bytes=33554432' in identity
   script='''phy=/sys/bus/iio/devices/iio:device0
adc=
for d in /sys/bus/iio/devices/iio:device*; do if test "$(cat "$d/name")" = cf-ad9361-lpc; then adc=$d; fi; done
test -n "$adc"
adc_debug=/sys/kernel/debug/iio/$(basename "$adc")
phy_debug=/sys/kernel/debug/iio/iio:device0
echo 4000000 > "$phy/in_voltage_sampling_frequency"
test "$(cat "$phy/in_voltage_sampling_frequency")" = 4000000
for a in 0x80000400 0x80000440 0x80000480 0x800004c0; do
 echo "$a 0x251" > "$adc_debug/direct_reg_access"
 echo "$a" > "$adc_debug/direct_reg_access"
 test "$(cat "$adc_debug/direct_reg_access")" = 0x251
done
trap 'echo 0 > "$phy_debug/bist_prbs"' EXIT
echo 2 > "$phy_debug/bist_prbs"
echo PN9 > "$adc_debug/pseudorandom_err_check"
sleep 0.05
echo 65536 > /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_records
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_data > /tmp/g0-adc-prbs.raw
busybox crc32 /tmp/g0-adc-prbs.raw
echo 0 > "$phy_debug/bist_prbs"
trap - EXIT
test "$(cat "$phy_debug/bist_prbs")" = 0
test "$(cat "$phy/out_altvoltage0_RX_LO_powerdown")" = 1
test "$(cat "$phy/out_altvoltage1_TX_LO_powerdown")" = 1'''
   raw=run('PRBS',script);assert 'overflow=0 bad_keep=0 fences=0 status=0x1' in raw
   with socket.socket() as listener:
    listener.bind((HOST,0));listener.listen(1);listener.settimeout(15);port=listener.getsockname()[1];transfer={}
    def receive():
     try:
      connection,peer=listener.accept();assert peer[0]==BOARD
      with connection:
       connection.settimeout(10);parts=[];count=0
       while True:
        part=connection.recv(min(65536,1048577-count))
        if not part:break
        count+=len(part);assert count<=1048576;parts.append(part)
      capture=b''.join(parts);assert len(capture)==1048576;(out/'PRBS.raw').write_bytes(capture);transfer.update(bytes=count,crc32=f'{zlib.crc32(capture):08x}')
     except BaseException as e:transfer['error']=repr(e)
    thread=threading.Thread(target=receive,daemon=True);thread.start()
    run('return-PRBS',f'''python3 -c "import socket,pathlib; s=socket.create_connection(('{HOST}',{port}),5); s.sendall(pathlib.Path('/tmp/g0-adc-prbs.raw').read_bytes()); s.shutdown(socket.SHUT_WR); s.close()"''');thread.join(timeout=15);assert not thread.is_alive() and 'error' not in transfer,transfer
   assert re.search(r'(?m)^'+transfer['crc32']+r' /tmp/g0-adc-prbs.raw\r?$',raw)
   verification=inspect(out/'PRBS.raw',65536,ad9361_bist=True);(out/'PRBS-review.json').write_text(json.dumps(verification,indent=2)+'\n');assert verification['digital_capture_passed']
   result.update(PRBS_passed=True,verification=verification,transfer=transfer,BIST_off_after=True,both_LOs_off_after=True)
 finally:
  if server:server.shutdown();server.server_close()
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');(out/'disk-after.json').write_text(json.dumps(check('after-raw-ADC-diagnostic-native'),indent=2)+'\n')
 print(json.dumps(result),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--driver',type=Path,required=True);p.add_argument('--expected-boot-id');p.add_argument('--output',type=Path,required=True);main(p.parse_args())
