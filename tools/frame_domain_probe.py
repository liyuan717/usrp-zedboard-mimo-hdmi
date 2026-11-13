#!/usr/bin/env python3
"""Full499bit H descriptor ownership, current publication fence and next-edge clear.
No arithmetic, native/RF throughput or full G0 claim from this control test.
"""
import argparse,hashlib,json,random,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,fence=0,iv=0,ready=0;reg [498:0] data=0;
wire ir,cv,clear,live,pending;wire [498:0] q;
frame_domain_cost_probe dut(clk,rst,fence,iv,ir,data,cv,ready,q,clear,live,pending);
reg [498:0] words[0:511];integer n,accepted=0,issued=0,revoked=0,holds=0,cycle=0;
reg owned=0;reg [498:0] expected;reg was_live=0;
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst)begin if(owned)revoked=revoked+1;owned=0;end
 else if(fence)begin if(live||ir)$fatal(1,"domain fence admitted public input");if(owned)revoked=revoked+1;owned=0;end
 else begin
  if(cv)begin if(!owned||q!==expected)$fatal(1,"domain opaque descriptor changed");if(ready)begin issued=issued+1;owned=0;end else holds=holds+1;end
  if(iv&&ir)begin if(owned)$fatal(1,"domain input overwrote owner");expected=data;owned=1;accepted=accepted+1;end
 end
 if(clear&&(live||ir||cv))$fatal(1,"domain clear released unflushed work");
end
task submit(input integer i);
 begin ready=1;iv=1;data=words[i];@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;data=~data;end
endtask
task hold_descriptor(input integer clocks);
 begin ready=0;repeat(clocks)begin @(negedge clk);if(!cv||ir||!pending||q!==expected)$fatal(1,"domain held owner");end end
endtask
task source_fence;
 begin
 fence=1;#1;if(live||ir)$fatal(1,"domain publication fence late");
 @(negedge clk);if(!clear||cv||pending||live||ir)$fatal(1,"domain canceled queue survived edge");fence=0;
 #1;if(live||ir)$fatal(1,"domain clear release was not fenced");@(negedge clk);
 if(clear||!live||pending||cv)$fatal(1,"domain did not recover empty");
 end
endtask
initial begin
 $readmemh("words.hex",words);repeat(4)@(negedge clk);rst=0;repeat(2)@(negedge clk);if(!live||cv||pending)$fatal(1,"domain startup");
 for(n=0;n<512;n=n+1)begin
  submit(n);hold_descriptor(1+n%11);
  if(n%17==0)begin ready=n[0];source_fence();end
  else if(n%31==0)begin
   rst=1;#1;if(live||ir||cv)$fatal(1,"domain reset edge");@(negedge clk);rst=0;repeat(2)@(negedge clk);if(cv||pending||clear||!live)$fatal(1,"domain reset recovery");
  end else begin ready=1;@(negedge clk);if(pending||cv||!live||!ir)$fatal(1,"domain completed owner held busy");end
 end
 // A fence while the empty interface has incoming VALID cannot admit it.
 ready=1;iv=1;data=words[0];source_fence();iv=0;
 repeat(4)@(negedge clk);
 if(accepted!=512||accepted!=issued+revoked||owned||pending||cv)$fatal(1,"domain ownership conservation %0d=%0d+%0d",accepted,issued,revoked);
 $display("G0_FRAME_DOMAIN_PASS accepted=%0d issued=%0d revoked=%0d full_width_held_checks=%0d current_publication_fences=32",accepted,issued,revoked,holds);$finish;
end
initial begin #10000000;$fatal(1,"domain timeout");end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 before=disk_check('before-frame-domain');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rng=random.Random(0x48444f4d41494e);words=[rng.getrandbits(499) for _ in range(512)];words[0]=0;words[1]=(1<<499)-1
  (out/'words.hex').write_text(''.join(f'{v:0125x}\n' for v in words));(out/'tb.sv').write_text(TB)
  shutil.copyfile(project/'sim/g0/frame_domain_cost_probe.sv',out/'frame_domain_cost_probe.sv')
  for label,args in [('compile',[str(bp/'xvlog'),'--sv','frame_domain_cost_probe.sv','tb.sv']),('elaborate',[str(bp/'xelab'),'tb','-s','frame_domain_tb','-mt','off']),('simulate',[str(bp/'xsim'),'frame_domain_tb','-runall'])]:
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_FRAME_DOMAIN_PASS' in log and 'Fatal:' not in log,log[-4000:]
  r={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_FRAME_DOMAIN_PASS')),'source_sha256':{'frame_domain_cost_probe.sv':hashlib.sha256((out/'frame_domain_cost_probe.sv').read_bytes()).hexdigest()},'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(r['simulation'],flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-domain'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
