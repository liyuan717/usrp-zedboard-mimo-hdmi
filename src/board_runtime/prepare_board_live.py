"""One authorized MIMO-provider preparation; no TX/RX sampling or HDMI start."""
from pathlib import Path
import hashlib,json,subprocess,sys
R=Path('/tmp/link-live')
P=Path('/sys/bus/iio/devices/iio:device0')
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==sys.argv[1]
assert '/dev/root / ext4 ro,' in Path('/proc/mounts').read_text()
assert 'rootflags=noload' in Path('/proc/cmdline').read_text()
assert int(Path('/proc/sys/kernel/tainted').read_text())==4096
assert Path('/sys/bus/platform/devices/43000000.dma-controller/driver').resolve().name=='hdmi-dmac-oneshot'
assert not any(name in Path('/proc/modules').read_text() for name in ('rxth_dmac_provider','zedboard_rx_transport','zedboard_interleaved_candidate'))
for a in ('out_altvoltage0_RX_LO_powerdown','out_altvoltage1_TX_LO_powerdown'):
    assert (P/a).read_text().strip()=='1'
assert Path('/sys/bus/iio/devices/iio:device3/buffer/enable').read_text().strip()=='0'
manifest=json.loads((R/'MANIFEST.json').read_text())
for row in manifest['files']:
    f=R/row['path'];assert hashlib.sha256(f.read_bytes()).hexdigest()==row['sha256'],row['path']
assert hashlib.sha256((R/'make_runtime_closure.py').read_bytes()).hexdigest()=='a4618edfe7af8586932c1fd42f887e86188a0b438d0b52aed52a03178c8855ba'
node=Path('/sys/bus/platform/devices/7c440000.dma')
assert (node/'driver').resolve().name=='dma-axi-dmac'
assert (node/'driver_override').read_text().strip() in ('','(null)')
def insmod(name,*args):
    subprocess.run(['/bin/busybox','insmod',str(R/'modules'/name),*args],check=True,timeout=10)
    assert int(Path('/proc/sys/kernel/tainted').read_text())==4096
    print('LIVE_MODULE_LOADED',name,flush=True)
try:
    insmod('rxth_dmac_provider.ko','enable_diag=1','accept_tlast=1','advance_issued=1')
    (node/'driver_override').write_text('rxth-dmac-diag\n')
    Path('/sys/bus/platform/drivers/dma-axi-dmac/unbind').write_text(node.name+'\n')
    assert not (node/'driver').exists()
    Path('/sys/bus/platform/drivers/rxth-dmac-diag/bind').write_text(node.name+'\n')
    assert (node/'driver').resolve().name=='rxth-dmac-diag'
    insmod('zedboard_rx_transport.ko')
    insmod('zedboard_interleaved_candidate.ko')
    assert Path('/dev/zed-rxth').is_char_device()
    assert Path('/dev/zed-mimo-interleaved-candidate').is_char_device()
    # Restore the previously successful receive-only radio profile. Keep both
    # LOs powered down until the independently supervised live test starts.
    values={'in_voltage_sampling_frequency':'4000000','in_voltage_rf_bandwidth':'3000000',
            'out_altvoltage0_RX_LO_frequency':'5499773070',
            'in_voltage0_gain_control_mode':'manual','in_voltage1_gain_control_mode':'manual',
            'in_voltage0_hardwaregain':'60','in_voltage1_hardwaregain':'60'}
    for a,v in values.items(): (P/a).write_text(v+'\n')
    for a in ('out_altvoltage0_RX_LO_powerdown','out_altvoltage1_TX_LO_powerdown'):
        (P/a).write_text('1\n');assert (P/a).read_text().strip()=='1'
    readback={a:(P/a).read_text().strip() for a in values}
    assert readback['in_voltage_sampling_frequency']=='4000000'
    assert readback['in_voltage_rf_bandwidth']=='3000000'
    assert abs(int(readback['out_altvoltage0_RX_LO_frequency'])-5499773070)<=5
    assert all(readback[f'in_voltage{i}_hardwaregain'].startswith('60.') for i in (0,1))
    assert Path('/sys/bus/platform/devices/43000000.dma-controller/driver').resolve().name=='hdmi-dmac-oneshot'
    assert int(Path('/proc/sys/kernel/tainted').read_text())==4096
    (R/'radio-readback.json').write_text(json.dumps(readback,indent=2)+'\n')
    print('LIVE_RXTH_IMBC_READY_NO_CAPTURE',json.dumps(readback),flush=True)
except BaseException:
    for a in ('out_altvoltage0_RX_LO_powerdown','out_altvoltage1_TX_LO_powerdown'):
        try:(P/a).write_text('1\n')
        except OSError:pass
    print('LIVE_PREPARATION_FAILED_NO_RETRY',flush=True)
    raise
