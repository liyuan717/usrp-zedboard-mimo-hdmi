#!/usr/bin/env python3
"""Finite actual frontend using one physical training core for both job modes.
Calibrated/notched input fixtures, configured timing; not complete acquisition.
"""
import argparse,json,shutil,subprocess,hashlib
from pathlib import Path
import numpy as np
from training_frontend_cost_probe import make_vectors,TB,pack
from g0_fixed_startup_model import estimate_startup,final_nco_configuration
from g0_fixed_training_model import training_signs
from g0_packet_probe import public_prefix
from fec_inverse_cost_probe import routed_measurements


def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 paths=sorted(a.fixture_directory.glob('packet-air-g12-long-r*.npz'));assert len(paths)==3
 n,nt,init,metadata=make_vectors(out,paths)
 if a.timed_config:
  for path in paths:assert not np.any(np.load(path)['input_iq'][:,1::2]==-32768), 'mirrored fixture exceeds signed16'
 _,signs=training_signs(public_prefix(),0)
 jobs=[];gold=[];reports=[]
 for f in range(4):
  rows=np.load(paths[f])['training_iq'][0] if f<3 else np.zeros((448,4),dtype=np.int64)
  for count in (128,129,400,447,448):
   r=estimate_startup(rows[:count],signs[:count],416,0,0);reports.append(r)
   err={'selected_phase_invalid':2,'CFO_out_of_range':3,'incoherent':4,None:0}[r['error']]
   num=0 if err==2 else r['num_s48'];den=0 if err==2 else r['D_u16']
   sums=[0,0] if err==2 else r['coherent_sums_s36'];quality=sum(x*x for x in sums)
   fields=[(1,1),(int(r['accepted']),1),(pack(sums,36),72),(quality,73),(den,16),(num,48),
           (err,3),(4 if err else 0,3),(0 if err else pack(r['energy_u50'],50),200),
           (0 if err else r['selected_rx'],2),(0 if err else r['step_s32']&0xffffffff,64),(0,208)]
   value=0
   for v,w in fields:value=(value<<w)|(v&((1<<w)-1))
   jobs.append((f,1,count,416,0));gold.append(value)
   # Normal two-column job immediately after each startup, without reset.
   m=metadata[f]['training_columns'];bad=f==3
   h=0 if bad else pack([v for x in m for v in x['H_raw_s26']],26)
   step=0 if bad else pack([x['symbol_step_s32'] for x in m],32)
   energy=0 if bad else pack([v for x in m for v in x['energy_u50']],50)
   selected=0 if bad else pack([x['selected_rx'] for x in m],1)
   value=(2<<(3+200+2+64+208) | 4<<(200+2+64+208)) if bad else (energy<<(2+64+208)|selected<<(64+208)|step<<208|h)
   jobs.append((f,0,448,416,0));gold.append(value)
 # Invalid startup configurations return before launching requests.
 for count,first in [(127,416),(449,416),(128,383),(400,497)]:
  jobs.append((0,1,count,first,0));gold.append((1<<690)|(5<<(3+200+2+64+208))|(4<<(200+2+64+208)))
 # Active and held startup cancellation.
 jobs.extend([(0,1,400,416,1),(1,1,400,416,2)]);gold.extend([0,gold[12]])
 # held fixture1 M400 expected index: fixture1 starts index10, M400 index14.
 gold[-1]=gold[14]
 if a.final_config:jobs.append((0,1,400,416,3));gold.append(0)
 fine_gold=[]
 for j,(f,mode,count,first,action) in enumerate(jobs):
  v=0
  if mode and 128<=count<=448 and first>=384 and first+count<=896 and f<3:
   rows=np.load(paths[f])['training_iq'][0]
   r=estimate_startup(rows[:count],signs[:count],first,0,0)
   if r['accepted'] and action not in (1,3) and a.final_config:
    base=((j+10)<<48)|0xfffffffff000
    target=(base&~((1<<48)-1))|(((base&((1<<48)-1))+metadata[f]['inputs']+1000)&((1<<48)-1))
    step=json.loads(paths[f].with_suffix('.json').read_text())['nco_config']['step_u32']
    cfg=final_nco_configuration(step,r['num_s48'],r['D_u16'],target&((1<<48)-1),j*16384+7)
    v=(target<<64)|(cfg['step_u32']<<32)|cfg['phase_u32']
  fine_gold.append(v)
 (out/'fine-expected.hex').write_text(''.join(f'{v:033x}\n' for v in fine_gold))
 (out/'jobs.txt').write_text(''.join('%d %d %d %d %d\n'%j for j in jobs))
 (out/'expected.hex').write_text(''.join(f'{v:0173x}\n' for v in gold))
 declarations=TB[TB.index(' reg [63:0] adc'):TB.index(' reg [136:0] mix_queue')]
 tb=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,iv=0,jv=0,ready=0,mode=0;
reg [63:0] ct=0,it=0,data=0;reg [31:0] cp=0,cs=0,jid=0,jc=0,jg=0;
reg [15:0] je=0;reg [79:0] jp=0;reg [34:0] js=0;
reg [8:0] count=0;reg [9:0] first_symbol=0;
wire cr,ir,jr,ov,armed,wr,idle,restart;wire [31:0] oid,oc,og,ctx,hgen;
wire [15:0] oe;wire [207:0] h;wire [63:0] steps;wire [1:0] selected;
wire [199:0] energies;wire [2:0] error,detail;wire [63:0] latest;wire [9:0] hcount;
wire sm,admitted;wire [47:0] num;wire [15:0] den;wire [72:0] quality;wire [71:0] coherent;
wire [63:0] final_tag;wire [31:0] final_step,final_phase;wire [1:0] config_error;
wire [129:0] final_result={config_error,final_tag,final_step,final_phase};
reg [63:0] commit_target=0;reg [47:0] phase_origin=0;
wire [690:0] result={sm,admitted,coherent,quality,den,num,detail,error,energies,selected,steps,h};
training_frontend_cost_probe #(.ENABLE_STARTUP(1),.ENABLE_FINAL_CONFIG(@FINAL@)) dut(
.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_tag(ct),.cfg_phase(cp),.cfg_step(cs),
.in_valid(iv),.in_ready(ir),.in_tag(it),.in_data(data),.job_valid(jv),.job_ready(jr),.job_id(jid),
.job_context(jc),.job_generation(jg),.job_epoch(je),.job_first_position(jp),.job_step(js),
.out_valid(ov),.out_ready(ready),.out_id(oid),.out_context(oc),.out_generation(og),.out_epoch(oe),
.out_h(h),.out_phase_steps(steps),.out_selected_rx(selected),.out_energies(energies),.out_error(error),.out_detail(detail),
.armed(armed),.window_ready(wr),.request_idle(idle),.active_context(ctx),.history_generation(hgen),
.latest_tag(latest),.history_count(hcount),.history_restart(restart),.job_startup(mode),.job_count(count),
.job_first_symbol(first_symbol),.out_startup(sm),.out_numerator(num),.out_divisor(den),.out_quality_sum(quality),
.out_coherent(coherent),.out_admitted(admitted),.job_commit_tag(commit_target),
.job_phase_origin_index(phase_origin),.out_commit_tag(final_tag),.out_final_step(final_step),
.out_final_phase(final_phase),.out_config_error(config_error));
@DECL@
reg [690:0] expected[0:@LAST_JOB@];reg [129:0] fine_expected[0:@LAST_JOB@];reg [129:0] held_final;
integer fd,k,parsed,f,m,c,fs,action,j=0,sent=0,samples=0,outputs=0,revoked=0,held_revoked=0;
integer ordinal=0,stop_feed=0,before_samples,held;
reg [63:0] base;reg [690:0] held_result;
always @(posedge clk) if(!rst && !cancel) begin
 if(iv && ir) sent=sent+1;
 if(dut.train_input_valid && dut.train_ready) begin
  if(dut.sample_data!==training_gold[f*896+(dut.column?448:0)+dut.received]) $fatal(1,"sample oracle mismatch");
  samples=samples+1;
 end
end
initial begin
$readmemh("fine-expected.hex",fine_expected);$readmemh("adc.hex",adc);$readmemh("training-oracle.hex",training_gold);$readmemh("expected.hex",expected);
@INIT@
fd=$fopen("jobs.txt","r");repeat(4) @(negedge clk);rst=0;
while(!$feof(fd)) begin
 parsed=$fscanf(fd,"%d %d %d %d %d\n",f,m,c,fs,action);if(parsed!=5) $fatal(1,"parse");
 base={16'(j+10),48'hfffffffff000};ct=base;cp=phases[f];cs=nsteps[f];cv=1;
 @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;ordinal=0;stop_feed=0;ready=0;
 fork
  begin
   for(k=0;k<lengths[f] && !stop_feed;k=k+1) begin
    it={base[63:48],base[47:0]+48'(k)};data=adc[offsets[f]+k];iv=1;
    @(posedge clk);while(!ir && !stop_feed) @(posedge clk);
    @(negedge clk);iv=0;ordinal=k+1;
    repeat(7) @(negedge clk);
   end
   iv=0;
  end
  begin
   while(ordinal<=known_at[f]) @(negedge clk);
   jid=j+100;jc=ctx;jg=hgen;je=base[63:48];jp={base[47:0],32'd0}+positions[f];js=tsteps[f];
   mode=m;count=c;first_symbol=fs;commit_target={base[63:48],base[47:0]+48'(lengths[f]+1000)};phase_origin=j*16384+7;jv=1;
   @(posedge clk);while(!jr) @(posedge clk);@(negedge clk);jv=0;
   // Source descriptors may change after ownership transfers.
   commit_target=0;phase_origin=0;mode=~m;count=1;first_symbol=0;jp=0;js=0;jc=~jc;jg=~jg;
   if(action==1 || action==3) begin
    if(action==3) begin while(dut.state!=4) @(negedge clk);repeat(31) @(negedge clk);end
    else begin before_samples=samples;while(samples-before_samples<8) @(negedge clk);end
    stop_feed=1;cancel=1;repeat(2) @(negedge clk);cancel=0;revoked=revoked+1;
    repeat(30) begin @(negedge clk);if(ov) $fatal(1,"cancelled output");end
   end else begin
    while(!ov) @(negedge clk);
    if(final_result!==fine_expected[j]) $fatal(1,"result mismatch final job%0d got%h expected%h",j,final_result,fine_expected[j]);
    if(result!==expected[j] || oid!==32'(j+100) || oc!==ctx || og!==hgen || oe!==base[63:48])
      $fatal(1,"result mismatch job%0d got%h expected%h",j,result,expected[j]);
    held_result=result;held_final=final_result;
    repeat(19) begin @(negedge clk);if(!ov || result!==held_result || final_result!==held_final || jr) $fatal(1,"held ownership");end
    outputs=outputs+1;
    if(action==2) begin
     stop_feed=1;cancel=1;repeat(2) @(negedge clk);cancel=0;held_revoked=held_revoked+1;
     if(ov) $fatal(1,"held not revoked");
    end else begin ready=1;@(negedge clk);ready=0;end
   end
  end
 join
 repeat(40) @(negedge clk);j=j+1;
end
if(j!=@JOBS@ || outputs!=@OUTPUTS@ || revoked!=@REVOKED@ || held_revoked!=1) $fatal(1,"coverage");
$display("G0_SHARED_FRONTEND_PASS jobs=%0d outputs=%0d inputs=%0d samples=%0d active_revoked=%0d held_revoked=%0d",j,outputs,sent,samples,revoked,held_revoked);$finish;
end
initial begin #300000000;$fatal(1,"timeout");end
endmodule
'''
 if a.timed_config:
  tb=tb.replace('.ENABLE_FINAL_CONFIG(@FINAL@)', '.ENABLE_FINAL_CONFIG(@FINAL@),.ENABLE_CONJUGATE(1),.ENABLE_TIMED_CONFIG(1)')
  tb=tb.replace('reg rst=1,cancel=0', 'reg config_sense=0;wire config_applied;wire [1:0] config_status;\nreg rst=1,cancel=0')
  tb=tb.replace('.cfg_phase(cp),.cfg_step(cs),', '.cfg_phase(cp),.cfg_step(cs),.cfg_conjugate(config_sense),.cfg_applied(config_applied),.cfg_error(config_status),')
  tb=tb.replace("base={16'(j+10),48'hfffffffff000};ct=base;cp=phases[f];cs=nsteps[f];cv=1;", "base={16'(j+10),48'hfffffffff000};ct=base;cp=phases[f];cs=nsteps[f];config_sense=j%2;cv=1;")
  tb=tb.replace(' @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;ordinal=0;', ' ordinal=0;')
  tb=tb.replace(' fork\n  begin', """ fork
  begin
   @(posedge clk);while(!cr) @(posedge clk);
   if(!config_applied) $fatal(1,"timed config rejected");
   @(negedge clk);cv=0;config_sense=~config_sense;
   if(config_status) $fatal(1,"config status");
  end
  begin""",1)
  tb=tb.replace('data=adc[offsets[f]+k];iv=1;', """data=adc[offsets[f]+k];
    if(j%2) begin data[31:16]=-$signed(data[31:16]);data[63:48]=-$signed(data[63:48]);end
    iv=1;""")
 for x,y in {'DECL':declarations,'LAST':n-1,'TRAIN_LAST':nt-1,'LAST_JOB':len(jobs)-1,'INIT':init,'JOBS':len(jobs),'OUTPUTS':45,'REVOKED':2 if a.final_config else 1,'FINAL':int(a.final_config)}.items():tb=tb.replace('@'+x+'@',str(y))
 (out/'tb.sv').write_text(tb)
 project=Path(__file__).resolve().parents[1];sources=[]
 for name in ['fine_nco_config_cost_probe','nco_cost_probe','rrc_cost_probe','timing_request_cost_probe','interpolation_cost_probe','frontend_chain_cost_probe','phase_angle_cost_probe','training_channel_cost_probe','training_frontend_cost_probe']:
  p=out/(name+'.sv');shutil.copyfile(project/'sim/g0'/p.name,p);sources.append(p)
 bp=a.vivado_bin.resolve();commands=[]
 def run(name,args):
  commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
  with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 run('compile',[str(bp/'xvlog'),'--sv',*map(str,sources),str(out/'tb.sv')])
 run('elaborate',[str(bp/'xelab'),'tb','-s','shared_tb','-mt','off'])
 run('simulate',[str(bp/'xsim'),'shared_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_SHARED_FRONTEND_PASS' in log and 'Fatal:' not in log,log[-2000:]
 controls=[('mode','.job_startup(mode)',".job_startup(1'b0)"),('quality','quality,den,num','(quality^73\'d1),den,num')]
 if a.final_config:controls.append(('final_phase','config_error,final_tag,final_step,final_phase',"config_error,final_tag,final_step,(final_phase^32'd1)"))
 for name,old,new in controls:
  fault=tb.replace('module tb;',f'module {name}_tb;').replace(old,new);fp=out/(name+'_tb.sv');fp.write_text(fault)
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(fp)])
  run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off'])
  run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall'])
  l=(out/(name+'-simulate.log')).read_text();assert 'Fatal: result mismatch' in l and 'G0_SHARED_FRONTEND_PASS' not in l
 receipt={'scope':__doc__,'simulation':log[log.index('G0_SHARED_FRONTEND_PASS'):].splitlines()[0],
 'timed_configuration_and_conjugation':a.timed_config,'final_configuration_enabled':a.final_config,'jobs':jobs,'startup_software_reports':reports,'negative_controls':['startup mode forced off','quality output bit'],
 'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'routed_OOC':None,'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if not a.sim_only:
  tcl='set_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(p)+'}\n' for p in sources)+'''synth_design -top training_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic ENABLE_STARTUP=1
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force shared.dcp
'''
  tcl=tcl.replace('-generic ENABLE_STARTUP=1','-generic {ENABLE_STARTUP=1 ENABLE_FINAL_CONFIG='+str(int(a.final_config))+'}')
  if a.timed_config:tcl=tcl.replace('ENABLE_FINAL_CONFIG='+str(int(a.final_config))+'}', 'ENABLE_FINAL_CONFIG='+str(int(a.final_config))+' ENABLE_CONJUGATE=1 ENABLE_TIMED_CONFIG=1}')
  (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
  receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({'simulation':receipt['simulation'],'routed_OOC':receipt['routed_OOC']}))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--timed-config',action='store_true');p.add_argument('--final-config',action='store_true');p.add_argument('--sim-only',action='store_true');main(p.parse_args())
