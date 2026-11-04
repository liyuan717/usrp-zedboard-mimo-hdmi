"""One explicitly authorized HDMI-only provider transition; no retry/rollback."""
from pathlib import Path
import hashlib,json,subprocess,os,sys,struct
R=Path('/tmp/link-live');D=R/'hdmi-dmac-r2';manifest=json.loads((D/'MANIFEST.json').read_text())
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==sys.argv[1]
assert int(Path('/proc/sys/kernel/tainted').read_text())==0,'requires fresh untainted boot'
assert not Path('/proc/modules').read_text().strip(),'requires fresh boot with no external modules'
assert 'rootflags=noload' in Path('/proc/cmdline').read_text(),'no SD journal replay'
for row in manifest['files']:
 p=D/row['path'];assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'],row['path']
for attr in ['out_altvoltage0_RX_LO_powerdown','out_altvoltage1_TX_LO_powerdown']:
 assert Path('/sys/bus/iio/devices/iio:device0',attr).read_text().strip()=='1'
assert Path('/sys/bus/iio/devices/iio:device3/buffer/enable').read_text().strip()=='0'
node=Path('/sys/bus/platform/devices/43000000.dma-controller');assert (node/'driver').resolve().name=='dma-axi-dmac'
assert (node/'driver_override').read_text().strip() in ['','(null)']
def run(*args):
 subprocess.run(['/bin/busybox',*args],check=True,timeout=15)
 assert not (int(Path('/proc/sys/kernel/tainted').read_text())&128),'kernel Oops during transition'
 print('COMPLETED',args[0],flush=True)
assert struct.unpack('>II',(node/'of_node/reg').read_bytes())==(0x43000000,0x1000)
for name in ['cec','i2c-xiic','clk-axi-clkgen','adv7511']:
 run('insmod',str(R/'modules'/(name+'.ko')))
run('insmod',str(D/'hdmi_dmac_oneshot.ko'),'enable_diag=1')
(node/'driver_override').write_text('hdmi-dmac-oneshot\n')
Path('/sys/bus/platform/drivers/dma-axi-dmac/unbind').write_text(node.name+'\n')
assert not (node/'driver').exists()
Path('/sys/bus/platform/drivers/hdmi-dmac-oneshot/bind').write_text(node.name+'\n')
assert (node/'driver').resolve().name=='hdmi-dmac-oneshot'
run('insmod',str(R/'modules/adi_axi_hdmi_present.ko'))
assert Path('/dev/dri/card0').exists()
assert Path('/sys/bus/platform/devices/7c440000.dma/driver').resolve().name=='dma-axi-dmac'
print('HDMI_ONLY_PROVIDER_BOUND',flush=True)
