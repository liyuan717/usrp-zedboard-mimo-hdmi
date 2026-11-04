"""Exercise ABI3 with real pinned ADI PN checker, held CDC and CPU read mux."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
CASES = ['known_sequence_real_checker_and_valid_gaps', 'held_CDC_and_CPU_read',
         'swapped_words', 'bit_error', 'repeated_state', 'skipped_state',
         'selector9_not_device_specific', 'reset']
VENDOR_FILES = ['library/common/' + n for n in
                ('up_xfer_cntrl.v', 'up_xfer_status.v', 'up_adc_channel.v', 'ad_pnmon.v')]
VENDOR_FILES += ['library/axi_ad9361/' + n for n in
                 ('rf_rx_pn_pair_observer.v', 'axi_ad9361_rx_pnmon.v')]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--vivado-bin', type=Path, default=Path('/vivado/2025.2/Vivado/bin'))
    a = p.parse_args();vendor=a.vendor.resolve();out=a.output.resolve()
    manifest=json.loads((vendor.parent/'source-manifest.json').read_text())
    if manifest.get('rx_observer_abi')!=3 or manifest.get('rx_iddr_alignment_trial')!='same_edge_pipelined':
        raise ValueError('requires aligned ABI3 source')
    expected={r['path']:r['sha256'] for r in manifest['files']}
    files=[vendor / rel for rel in VENDOR_FILES]
    for f in files:
        if sha(f)!=expected[f.relative_to(vendor).as_posix()]:raise ValueError('manifest mismatch')
    files += [ROOT/'pl/tb/tb_rf_rx_pn_pair.sv']
    before={str(f):sha(f) for f in files}
    out.mkdir(parents=True,exist_ok=False)
    commands=[[str(a.vivado_bin/'xvlog'),'--sv',*map(str,files)],
              [str(a.vivado_bin/'xelab'),'tb_rf_rx_pn_pair','-s','pn_pair'],
              [str(a.vivado_bin/'xsim'),'pn_pair','--runall']]
    logs=[]
    for i,cmd in enumerate(commands):
        r=subprocess.run(cmd,cwd=out,capture_output=True,text=True)
        log=r.stdout+r.stderr;logs.append(log);(out/f'step-{i}.txt').write_text(log)
        if r.returncode:raise RuntimeError(log[-4000:])
    log='\n'.join(logs)
    if (log.count('RF_RX_PN_PAIR_SIM_PASS')!=1 or 'Fatal:' in log or
            any(log.count('PN_PAIR_CASE '+c+' PASS')!=1 for c in CASES)):
        raise RuntimeError(log[-4000:])
    if before!={str(f):sha(f) for f in files}:raise ValueError('source changed during simulation')
    result=dict(passed=True,hardware_tested=False,rx_observer_abi=3,cases=CASES,source_sha256=before)
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print('RF_RX_PN_PAIR_SIM_PASS')


if __name__=='__main__':main()
