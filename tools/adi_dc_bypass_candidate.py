#!/usr/bin/env python3
"""Prove a compile-disabled ADI DC filter preserves its current zero-offset pipeline."""
import argparse,hashlib,json,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB

def bypass(text):
    old="""  if (DISABLE == 1) begin
  assign valid_out = valid;
  assign data_out = data;
"""
    new="""  if (DISABLE == 1) begin
  // V3 static zero-offset bypass: preserve the enabled core's three clocks.
  reg [2:0] v3_valid = 3'b000;
  reg [15:0] v3_data0 = 16'd0, v3_data1 = 16'd0, v3_data2 = 16'd0;
  always @(posedge clk) begin
    v3_valid <= {v3_valid[1:0], valid};
    if (valid) v3_data0 <= data;
    v3_data1 <= v3_data0;
    v3_data2 <= v3_data1;
  end
  assign valid_out = v3_valid[2];
  assign data_out = v3_data2;
"""
    assert text.count(old)==1
    return text.replace(old,new)

TB=chr(96)+r'''timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;
reg valid=0;reg[15:0] data=0,coefficient=0;
wire ov,nv;wire[15:0] od,nd;
ad_dcfilter #(.DISABLE(0)) original(clk,valid,data,ov,od,1'b0,coefficient,16'd0);
v3_ad_dcfilter #(.DISABLE(1)) candidate(clk,valid,data,nv,nd,1'b0,coefficient,16'd0);
reg [2:0] expected_valid=0;reg[15:0] d0=0,d1=0,d2=0;reg [31:0] rng=32'h1538b2a7;
integer checked=0,accepted=0;
always @(posedge clk)begin
 expected_valid={expected_valid[1:0],valid};
 d2=d1;d1=d0;if(valid)d0=data;
 #1;
 if(nv!==ov||nd!==od||nv!==expected_valid[2]||nd!==d2)$fatal(1,"DC bypass cycle/byte mismatch cycle%0d old%h new%h",checked,od,nd);
 checked=checked+1;if(nv)accepted=accepted+1;
end
initial begin
 for(integer i=0;i<6000;i=i+1)begin
  @(negedge clk);
  rng={rng[30:0],rng[31]^rng[21]^rng[1]^rng[0]};
  valid=(i%29<18)||(i%37==4);
  case(i%13)
   0:data=16'h8000;1:data=16'h7fff;2:data=16'hffff;3:data=0;
   default:data=rng[15:0];
  endcase
  coefficient=rng[31:16];
 end
 @(negedge clk);valid=0;repeat(5)@(negedge clk);
 if(checked<6000||accepted<3000)$fatal(1,"insufficient live coverage");
 $display("ADI_DC_BYPASS_PASS checked=%0d live=%0d",checked,accepted);$finish;
end
initial begin #100000;$fatal(1,"bounded DC bypass timeout");end
endmodule
'''

def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False)
    before=check('before-ADI-DC-bypass-proof');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    assert before['disk']['available_bytes']>=5*GIB
    original=a.source.read_text();candidate=bypass(original)
    audit=json.loads((a.native_audit/'receipt.json').read_text())
    assert audit['all_four_IIR_filter_enables_off'] and audit['all_four_DC_offsets_zero']
    result=dict(scope=__doc__,completed=False,RF_started=False,PL_or_SD_changed=False,
                original_sha256=hashlib.sha256(original.encode()).hexdigest(),
                candidate_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
                current_native_audit=audit,core_IQ_correction_or_PL_DC_removed=False,
                actual_net_savings_measured=False,full_G0_or_video_passed=False)
    try:
        cases={'normal':candidate,'wrong_delay':candidate.replace("assign valid_out = v3_valid[2]","assign valid_out = v3_valid[1]"),
               'wrong_data':candidate.replace('assign data_out = v3_data2;',"assign data_out = v3_data2 ^ 16'h0001;")}
        rows=[]
        for name,text in cases.items():
            case=out/name;case.mkdir()
            (case/'original.v').write_text(original)
            (case/'candidate.v').write_text(text.replace('module ad_dcfilter #(','module v3_ad_dcfilter #('))
            (case/'tb.sv').write_text(TB)
            commands=[('compile',[str(a.vivado_bin/'xvlog'),'--sv','original.v','candidate.v','tb.sv',str(a.vivado_bin.parent/'data/verilog/src/glbl.v')]),
                      ('elaborate',[str(a.vivado_bin/'xelab'),'tb','glbl','-L','unisims_ver','-s','dc_tb','-mt','off']),
                      ('simulate',[str(a.vivado_bin/'xsim'),'dc_tb','-runall'])]
            (case/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
            for step,cmd in commands:
                with (case/(step+'.log')).open('w') as log:subprocess.run(cmd,cwd=case,stdout=log,stderr=subprocess.STDOUT,check=True)
            log=(case/'simulate.log').read_text()
            passed='ADI_DC_BYPASS_PASS' in log and 'Fatal:' not in log
            assert passed==(name=='normal'),(name,log[-2000:])
            rows.append(dict(name=name,comparison_passed=passed,expected_result_observed=True))
        (out/'candidate.v').write_text(candidate)
        result.update(completed=True,zero_offset_filter_off_all_cycles_data_valid_equal_original=True,
                      latency_clocks=3,actual_compiled_fault_controls=rows,
                      net_cost_pending='Original four filters gross160LUT/704FF/4DSP; three-stage bypass and synthesis effects must be subtracted, not claim gross as savings')
    finally:
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        (out/'disk-after.json').write_text(json.dumps(check('after-ADI-DC-bypass-proof'),indent=2)+'\n')
    print('ADI_DC_BYPASS_PROOF_PASS',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['source','native-audit','output']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--vivado-bin',type=Path,default=Path('/vivado/2025.2/Vivado/bin'))
    main(p.parse_args())
