#!/usr/bin/env python3
"""Supplement connected-startup suite with reconfiguration during fine division."""
import argparse,json,shutil,subprocess
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);src=a.reference.resolve()
 for p in src.iterdir():
  if p.suffix in ('.sv','.hex','.txt') and not p.name.endswith('_tb.sv'):shutil.copyfile(p,out/p.name)
 jobs=(out/'jobs.txt').read_text();assert len(jobs.splitlines())==47
 (out/'jobs.txt').write_text(jobs+'0 1 400 416 5\n')
 err=(1<<690)|(1<<(3+200+2+64+208))|(1<<(200+2+64+208))
 p=out/'expected.hex';p.write_text(p.read_text()+f'{err:0173x}\n')
 p=out/'fine-expected.hex';p.write_text(p.read_text()+'0\n')
 p=out/'tb.sv';s=p.read_text().replace('integer fd,k,parsed', 'reg [31:0] saved_ctx,saved_gen;\ninteger fd,k,parsed')
 s=s.replace('jid=j+100;jc=ctx;jg=hgen;', 'saved_ctx=ctx;saved_gen=hgen;jid=j+100;jc=ctx;jg=hgen;')
 s=s.replace('oc!==ctx || og!==hgen','oc!==saved_ctx || og!==saved_gen')
 s=s.replace('   if(action==1 || action==3)', '''   if(action==5) begin
    while(dut.state!=4) @(negedge clk);repeat(31) @(negedge clk);
    stop_feed=1;ct={base[63:48],base[47:0]+48'd100000};cv=1;
    @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;
   end
   if(action==1 || action==3)''')
 s=s.replace('expected[0:46]', 'expected[0:47]')
 s=s.replace('j!=47 || outputs!=45','j!=48 || outputs!=46')
 p.write_text(s)
 bp=a.vivado_bin.resolve();sources=[p for p in out.glob('*.sv') if p.name!='tb.sv']
 for name,args in [('compile',[str(bp/'xvlog'),'--sv',*map(str,sources),str(p)]),('elaborate',[str(bp/'xelab'),'tb','-s','fence_tb','-mt','off']),('simulate',[str(bp/'xsim'),'fence_tb','-runall'])]:
  with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
 log=(out/'simulate.log').read_text();assert 'G0_SHARED_FRONTEND_PASS' in log and 'Fatal:' not in log,log[-1500:]
 receipt={'reference':str(src),'scope':__doc__,'simulation':log[log.index('G0_SHARED_FRONTEND_PASS'):].splitlines()[0],'reconfiguration_after_config_state_cycles':31,'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reference',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
