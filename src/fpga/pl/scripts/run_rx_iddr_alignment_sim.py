"""Compare real Vivado IDDR modes using the pinned ADI receiver grouping code."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vendor", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--vivado-root", type=Path, default=Path("/vivado/2025.2/Vivado"))
    a = p.parse_args();vendor=a.vendor.resolve();output=a.output.resolve()
    manifest=json.loads((vendor.parent/'source-manifest.json').read_text())
    if (manifest.get('rx_iddr_alignment_trial')!='same_edge_pipelined'
            or manifest.get('rx_observer_abi') not in (None, 0, 2, 3)):
        raise ValueError('requires an isolated aligned vendor candidate')
    rx=vendor/'library/axi_ad9361/xilinx/axi_ad9361_lvds_if.v'
    data_in=vendor/'library/xilinx/common/ad_data_in.v'
    expected={x['path']:x['sha256'] for x in manifest['files']}
    for f in [rx,data_in]:
        if sha(f)!=expected[f.relative_to(vendor).as_posix()]:raise ValueError('source manifest mismatch')
    text=rx.read_text()
    if text.count('.IDDR_CLK_EDGE ("SAME_EDGE_PIPELINED"),')!=2 or 'parameter   IDDR_CLK_EDGE ="SAME_EDGE"' not in data_in.read_text():
        raise ValueError('unexpected candidate/default IDDR configuration')
    primitive=a.vivado_root/'data/verilog/src/unisims/IDDR.v'
    glbl=a.vivado_root/'data/verilog/src/glbl.v'
    bench=ROOT/'pl/tb/tb_rf_rx_iddr_alignment.sv'
    sources=[rx,data_in,primitive,glbl,bench]
    initial_hashes={str(f):sha(f) for f in sources}
    output.mkdir(parents=True,exist_ok=False)
    begin=text.index('  // intel-equivalence\n');end=text.index('  // transfer to common clock\n',begin)
    helper=output/'rx_delineation_under_test.v'
    helper.write_text('''module rx_delineation_under_test (
 input l_clk, input [1:0] rx_frame_s, input [5:0] rx_data_1_s, rx_data_0_s,
 input rx_r1_mode, rx_locked, output reg adc_valid_p=0, output reg adc_status_p=0,
 output reg [47:0] adc_data_p=0
);
 reg [1:0] rx_frame=0;
 reg [5:0] rx_data_1=0, rx_data_0=0;
 wire rx_error;
'''+text[begin:end]+'\nendmodule\n')
    bin=a.vivado_root/'bin'
    commands=[[str(bin/'xvlog'),'--sv',str(primitive),str(glbl),str(helper),str(bench)],
              [str(bin/'xelab'),'tb_rf_rx_iddr_alignment','glbl','-s','iddr_alignment'],
              [str(bin/'xsim'),'iddr_alignment','--runall']]
    outputs=[]
    for i,cmd in enumerate(commands):
        r=subprocess.run(cmd,cwd=output,capture_output=True,text=True)
        log=r.stdout+r.stderr;outputs.append(log);(output/f'step-{i}.txt').write_text(log)
        if r.returncode:raise RuntimeError(log[-3000:])
    logs='\n'.join(outputs)
    found=re.findall(r'IDDR_ALIGNMENT_SIM_PASS baseline_valid=0 candidate_valid=(\d+) ordered_words=1 baseline_observed_4b=1',logs)
    if len(found)!=1 or 'Fatal:' in logs or int(found[0])<900:raise RuntimeError(logs[-3000:])
    if initial_hashes!={str(f):sha(f) for f in sources}:raise ValueError('simulation source changed during run')
    result=dict(passed=True,hardware_tested=False,rx_observer_abi=manifest.get('rx_observer_abi', 0),
                rx_iddr_alignment_trial='same_edge_pipelined',baseline_observed_4b_reproduced=True,
                baseline_valid_count=0,candidate_ordered_samples_verified=int(found[0]),
                candidate_word_order_verified=True,source_sha256=initial_hashes)
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))


if __name__=='__main__':main()
