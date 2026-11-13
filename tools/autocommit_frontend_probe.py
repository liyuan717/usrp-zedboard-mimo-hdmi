#!/usr/bin/env python3
"""Actual shared frontend startup -> final calculator -> self NCO configuration.
Configured final-corrected fixtures plus repeated last-sample tails, not complete
coarse acquisition or a physical continuous-ADC FIFO/latency signoff.
"""
import argparse,json,math,shutil,subprocess,hashlib
from pathlib import Path
import numpy as np
from training_frontend_cost_probe import make_vectors,TB,pack
from g0_fixed_startup_model import estimate_startup,final_nco_configuration
from g0_fixed_training_model import training_signs
from g0_packet_probe import public_prefix
from g0_sounder import rrc
from fec_inverse_cost_probe import routed_measurements

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1]
 paths=sorted(a.fixture_directory.glob('packet-air-g12-long-r*.npz'));assert len(paths)==3
 n,nt,init,metadata=make_vectors(out,paths);_,signs=training_signs(public_prefix(),0)
 # kind0 success,1 late,2 wrong epoch,3 cancelled while waiting,4 rejected zero.
 cases=[(f,s,0) for f in range(3) for s in range(2)]+[(0,1,1),(1,0,2),(2,1,3),(3,0,4)]
 stride=max(x['inputs'] for x in metadata)+1100
 taps=[int(x) for x in np.rint(rrc()*131072)];ng=[];rg=[];initial=[];reports=[]
 for j,(f,sense,kind) in enumerate(cases):
  original=np.load(paths[min(f,2)]);raw=original['input_iq'] if f<3 else np.zeros((metadata[f]['inputs'],4),dtype=np.int64)
  train=original['training_iq'][0][:400] if f<3 else np.zeros((400,4),dtype=np.int64)
  rep=estimate_startup(train,signs[:400],416,0,0);reports.append(rep)
  m=json.loads(paths[min(f,2)].with_suffix('.json').read_text());coarse=int(m['nco_config']['step_u32']);phase=int(m['nco_config']['phase_u32']);base=(1<<48)-4096;epoch=30+j
  last_position=int(original['timing_first_position_relative_q32'][0])+399*m['timing']['step_q32_u35']
  commit=(last_position+(1<<32)-1)//(1<<32)+20+512 if kind!=1 else metadata[f]['known_at_input_ordinal']+10
  tag=((epoch+int(kind==2))<<48)|((base+commit)&((1<<48)-1));origin=j*1987+11
  cfg=final_nco_configuration(coarse,rep['num_s48'],rep['D_u16'],tag&((1<<48)-1),origin) if rep['accepted'] else dict(step_u32=0,phase_u32=0)
  new_step,new_phase=cfg['step_u32'],cfg['phase_u32'];rows=[];last=len(raw)+1100
  for k in range(stride):
   row=raw[min(k,len(raw)-1)]
   # Successful cases are the only ones that actually switch NCO.
   if kind==0 and k==commit:phase=new_phase
   b=((phase+0x80000)&0xffffffff)>>20;c=round(65536*math.cos(2*math.pi*b/4096));si=round(65536*math.sin(2*math.pi*b/4096))
   mixed=[]
   for rx in range(2):
    i,q=map(int,row[2*rx:2*rx+2]);mixed.extend(((i*c-q*si+16384)//32768,(i*si+q*c+16384)//32768))
   rows.append(mixed);ng.append(pack(mixed,18))
   first=commit if kind==0 and k>=commit else 0
   filt=[(sum(taps[t]*rows[k-t][l] for t in range(41))+65536)//131072 for l in range(4)] if k>=first+40 else [0]*4
   rg.append(pack(filt,20));phase=(phase+(new_step if kind==0 and k>=commit else coarse))&0xffffffff
  initial.append(f"fixtures[{j}]={f};senses[{j}]={sense};kinds[{j}]={kind};targets[{j}]=64'h{tag:016x};origins[{j}]=48'd{origin};final_steps[{j}]=32'h{new_step:08x};final_phases[{j}]=32'h{new_phase:08x};commits[{j}]={commit};")
 for name,vals,w in [('mixed.hex',ng,18),('filtered.hex',rg,20)]: (out/name).write_text(''.join(f'{v:0{w}x}\n' for v in vals))
 decl=TB[TB.index(' reg [63:0] adc'):TB.index(' reg [136:0] mix_queue')]
 tb=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,iv=0,jv=0,ready=0,sense=0;
reg [63:0] ct=0,it=0,data=0;reg [31:0] cp=0,cs=0,jid=0,jc=0,jg=0;
reg [15:0] je=0;reg [79:0] jp=0;reg [34:0] js=0;reg [63:0] target=0;reg [47:0] origin=0;
wire cr,ir,jr,ov,armed,wr,idle,restart,applied;wire [31:0] oid,oc,og,ctx,hgen;
wire [15:0] oe;wire [207:0] h;wire [63:0] steps;wire [1:0] selected;
wire [199:0] energies;wire [2:0] error,detail;wire [63:0] latest;wire [9:0] hcount;
wire sm,admitted,committed;wire [47:0] num;wire [15:0] den;wire [72:0] quality;wire [71:0] coherent;
wire [63:0] final_tag;wire [31:0] final_step,final_phase,commit_context;wire [1:0] config_error,cfg_error;
training_frontend_cost_probe #(.ENABLE_STARTUP(1),.ENABLE_FINAL_CONFIG(1),.ENABLE_TIMED_CONFIG(1),.ENABLE_CONJUGATE(1),.ENABLE_AUTOCOMMIT(1)) dut(
.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_tag(ct),.cfg_phase(cp),.cfg_step(cs),
.cfg_conjugate(sense),.cfg_applied(applied),.cfg_error(cfg_error),
.in_valid(iv),.in_ready(ir),.in_tag(it),.in_data(data),.job_valid(jv),.job_ready(jr),.job_id(jid),.job_context(jc),.job_generation(jg),
.job_epoch(je),.job_first_position(jp),.job_step(js),.job_startup(1'b1),.job_count(9'd400),.job_first_symbol(10'd416),
.job_commit_tag(target),.job_phase_origin_index(origin),.out_valid(ov),.out_ready(ready),.out_id(oid),.out_context(oc),.out_generation(og),.out_epoch(oe),
.out_h(h),.out_phase_steps(steps),.out_selected_rx(selected),.out_energies(energies),.out_error(error),.out_detail(detail),
.armed(armed),.window_ready(wr),.request_idle(idle),.active_context(ctx),.history_generation(hgen),.latest_tag(latest),.history_count(hcount),.history_restart(restart),
.out_startup(sm),.out_numerator(num),.out_divisor(den),.out_quality_sum(quality),.out_coherent(coherent),.out_admitted(admitted),
.out_commit_tag(final_tag),.out_final_step(final_step),.out_final_phase(final_phase),.out_config_error(config_error),.out_committed(committed),.out_commit_context(commit_context));
@DECL@
reg [71:0] mixed_gold[0:@GOLDLAST@];reg [79:0] filtered_gold[0:@GOLDLAST@];
integer fixtures[0:9],senses[0:9],kinds[0:9],commits[0:9];reg [63:0] targets[0:9];reg [47:0] origins[0:9];reg [31:0] final_steps[0:9],final_phases[0:9];
integer j,f,k,kind,ordinal=0,stop_feed=0,nco_outputs=0,rrc_outputs=0,success=0,rejected=0,revoked=0,configs=0,slot,endpoint;
integer saved_context,saved_generation,min_lead=10000,lead;reg wait_seen=0;reg [63:0] base;reg [31:0] held_step,held_phase;reg held_committed;
always @(posedge clk) if(!rst && !cancel) begin
 if(dut.state==5 && !wait_seen && kind==0) begin
  wait_seen=1;lead=48'(targets[j][47:0]-it[47:0]);
  if(lead<=0 || lead>512) $fatal(1,"configured startup deadline");
  if(lead<min_lead) min_lead=lead;
 end
 if(applied) begin
  configs=configs+1;
  if(dut.automatic_config && (it!==targets[j] || ir || cv || dut.effective_phase!==final_phases[j] || dut.effective_step!==final_steps[j] || dut.effective_tag!==targets[j] || dut.effective_sense!==1'(senses[j]))) $fatal(1,"automatic boundary");
 end
 if(dut.frontend.mixed_valid && dut.frontend.mixed_ready) begin
  slot=48'(dut.frontend.mixed_tag[47:0]-base[47:0]);
  if(slot>=@STRIDE@ || dut.frontend.mixed_data!==mixed_gold[j*@STRIDE@+slot] ||
   dut.frontend.mixed_start!==(slot==0 || (kind==0 && slot==commits[j]))) $fatal(1,"mixed oracle %0d slot%0d",j,slot);
  nco_outputs=nco_outputs+1;
 end
 if(dut.frontend.filtered_valid && dut.frontend.filtered_ready) begin
  endpoint=48'(dut.frontend.filtered_tag[47:0]-base[47:0]+48'd20);
  if(endpoint>=@STRIDE@ || dut.frontend.filtered_data!==filtered_gold[j*@STRIDE@+endpoint]) $fatal(1,"filtered oracle %0d endpoint%0d",j,endpoint);
  if(kind==0 && endpoint>=commits[j] && dut.frontend.filtered_context!=2) $fatal(1,"new history context");
  rrc_outputs=rrc_outputs+1;
 end
end
initial begin
$readmemh("adc.hex",adc);$readmemh("mixed.hex",mixed_gold);$readmemh("filtered.hex",filtered_gold);
@INIT@
@CASES@
for(j=0;j<10;j=j+1) begin
 rst=1;repeat(4) @(negedge clk);rst=0;f=fixtures[j];kind=kinds[j];base={16'(j+30),48'hfffffffff000};
 ct=base;cp=phases[f];cs=nsteps[f];sense=senses[j];cv=1;ordinal=0;stop_feed=0;wait_seen=0;ready=0;
 fork
  begin @(posedge clk);while(!cr) @(posedge clk);@(negedge clk);cv=0;sense=~sense;cp=~cp;cs=~cs;ct=~ct;end
  begin
   for(k=0;k<lengths[f]+1100 && !stop_feed;k=k+1) begin
    it={base[63:48],48'(base[47:0]+k)};data=adc[offsets[f]+(k<lengths[f]?k:lengths[f]-1)];
    if(senses[j]) begin data[31:16]=-$signed(data[31:16]);data[63:48]=-$signed(data[63:48]);end
    iv=1;@(posedge clk);while(!ir && !stop_feed) @(posedge clk);@(negedge clk);iv=0;ordinal=k+1;
    repeat(j%2?11:7) @(negedge clk);
   end
   iv=0;
  end
  begin
   while(ordinal<=known_at[f]) @(negedge clk);
   jid=1234+j;jc=ctx;jg=hgen;saved_context=ctx;saved_generation=hgen;je=base[63:48];jp={base[47:0],32'd0}+positions[f];js=tsteps[f];target=targets[j];origin=origins[j];jv=1;
   @(posedge clk);while(!jr) @(posedge clk);@(negedge clk);jv=0;target=0;origin=0;jp=0;js=0;
   if(kind==3) begin
    while(dut.state!=5) @(negedge clk);repeat(31) @(negedge clk);stop_feed=1;cancel=1;repeat(2) @(negedge clk);cancel=0;
    repeat(20) begin @(negedge clk);if(ov || committed) $fatal(1,"cancel published");end revoked=revoked+1;
   end else begin
    while(!ov) @(negedge clk);
    if(oid!=1234+j || oc!=saved_context || og!=saved_generation || oe!=base[63:48] || !sm) $fatal(1,"identity");
    if(kind==0) begin
     if(error || detail || !committed || !admitted || commit_context!=2 || ctx!=2 || final_tag!==targets[j] || final_step!==final_steps[j] || final_phase!==final_phases[j]) $fatal(1,"commit result %0d err%0d:%0d",j,error,detail);
     success=success+1;
    end else begin
     if(committed || admitted || ctx!=1 || (kind==4?(error!=4 || detail!=2):(error!=6 || detail!=4 || config_error!=(kind==1?1:2)))) $fatal(1,"rejection %0d err%0d:%0d cfg%0d",j,error,detail,config_error);
     rejected=rejected+1;
    end
    held_step=final_step;held_phase=final_phase;held_committed=committed;
    repeat(33) begin @(negedge clk);if(!ov || final_step!==held_step || final_phase!==held_phase || committed!==held_committed || jr) $fatal(1,"held");end
    ready=1;@(negedge clk);ready=0;
   end
  end
 join
 repeat(50) @(negedge clk);
end
if(success!=6 || rejected!=3 || revoked!=1 || configs!=16) $fatal(1,"coverage %0d/%0d/%0d/%0d",success,rejected,revoked,configs);
$display("G0_AUTOCOMMIT_PASS success=%0d rejected=%0d revoked=%0d configurations=%0d nco_outputs=%0d rrc_outputs=%0d minimum_ready_lead_samples=%0d",success,rejected,revoked,configs,nco_outputs,rrc_outputs,min_lead);$finish;
end
initial begin #100000000;$fatal(1,"timeout");end
endmodule
'''
 if a.continuous:
  tb=tb.replace('reg rst=1,cancel=0', 'reg rearm=0;wire ingress_cancel,ingress_running,front_valid,front_ready;wire [63:0] front_tag,front_data;wire [31:0] ingress_generation,ingress_faults;wire [2:0] ingress_reason;wire [7:0] ingress_occupancy,ingress_peak;integer observed_peak=0;\nreg rst=1,cancel=0')
  insert=tb.index('training_frontend_cost_probe #')
  tb=tb[:insert]+"""continuous_adc_ingress_cost_probe ingress(.clk(clk),.rst(rst || cancel),.rearm(rearm),.raw_valid(iv),.raw_tag(it),.raw_data(data),.upstream_loss(1'b0),
.out_valid(front_valid),.out_ready(front_ready),.out_tag(front_tag),.out_data(front_data),.out_start(),.receiver_cancel(ingress_cancel),.running(ingress_running),
.generation(ingress_generation),.fault_count(ingress_faults),.last_fault(ingress_reason),.occupancy(ingress_occupancy),.high_water(ingress_peak),.admitted(),.dropped());
"""+tb[insert:]
  tb=tb.replace('.cancel(cancel),.cfg_valid', '.cancel(cancel || ingress_cancel),.cfg_valid')
  tb=tb.replace('.in_valid(iv),.in_ready(ir),.in_tag(it),.in_data(data)', '.in_valid(front_valid),.in_ready(front_ready),.in_tag(front_tag),.in_data(front_data)')
  tb=tb.replace('if(!rst && !cancel) begin', 'if(!rst && !cancel && !ingress_cancel) begin')
  tb=tb.replace('it!==targets[j]', 'front_tag!==targets[j]')
  tb=tb.replace('rst=0;f=fixtures[j]', 'rst=0;rearm=1;@(negedge clk);rearm=0;while(!ingress_running) @(negedge clk);f=fixtures[j]')
  tb=tb.replace('@(posedge clk);while(!ir && !stop_feed) @(posedge clk);', '@(posedge clk);')
  tb=tb.replace('repeat(j%2?11:7)', 'repeat(11+k%2)')
  tb=tb.replace('while(dut.state!=5) @(negedge clk);repeat(31) @(negedge clk);stop_feed=1;cancel=1;repeat(2) @(negedge clk);cancel=0;', """while(dut.state!=5) @(negedge clk);force front_ready=0;
    @(posedge clk);while(!ingress_cancel) @(posedge clk);stop_feed=1;@(negedge clk);release front_ready;
    if(ingress_faults!=1 || ingress_reason!=2) $fatal(1,"overflow failed to revoke receiver count=%0d reason=%0d state=%0d pending=%0d",ingress_faults,ingress_reason,system.ingress.state,system.ingress.overflow_pending);""")
  # Bounded backpressure in all other cases; fixed raw producer keeps running.
  tb=tb.replace(' fork\n  begin @(posedge clk)', """ fork
  begin
   if(kind!=3) begin
    while(ordinal<=known_at[f]+30) @(negedge clk);force front_ready=0;
    repeat(240) @(negedge clk);release front_ready;
   end
  end
  begin @(posedge clk)""")
  tb=tb.replace('configs=configs+1;', 'configs=configs+1;')
  tb=tb.replace(' cycle=cycle+1;', ' cycle=cycle+1;')
  tb=tb.replace(' join\n repeat(50)', ' join\n if(ingress_peak>observed_peak) observed_peak=ingress_peak;\n if(kind!=3 && ingress_faults) $fatal(1,"unexpected ingress fault");\n repeat(50)')
  tb=tb.replace('minimum_ready_lead_samples=%0d",success', 'minimum_ready_lead_samples=%0d maximum_ingress_words=%0d",success').replace('rrc_outputs,min_lead)', 'rrc_outputs,min_lead,observed_peak)')
 if a.continuous:
  a0=tb.index('continuous_adc_ingress_cost_probe ingress(');b0=tb.index('training_frontend_cost_probe #',a0)
  tb=tb[:a0]+tb[b0:]
  a0=tb.index('training_frontend_cost_probe #');b0=tb.index(' dut(',a0)
  tb=tb[:a0]+'continuous_training_frontend_cost_probe system'+tb[b0+4:]
  tb=tb.replace('.cancel(cancel || ingress_cancel)', '.cancel(cancel)')
  tb=tb.replace('.in_valid(front_valid),.in_ready(front_ready),.in_tag(front_tag),.in_data(front_data)', '.in_valid(iv),.raw_admitted(),.in_tag(it),.in_data(data)')
  tb=tb.replace('.out_committed(committed),.out_commit_context(commit_context));', """.out_committed(committed),.out_commit_context(commit_context),
.ingress_rearm(rearm),.ingress_upstream_loss(1'b0),.ingress_cancel(ingress_cancel),.ingress_running(ingress_running),
.ingress_generation(ingress_generation),.ingress_faults(ingress_faults),.ingress_reason(ingress_reason),.ingress_occupancy(ingress_occupancy),.ingress_peak(ingress_peak));
assign front_tag=system.buffer_tag;""")
  tb=tb.replace('dut.', 'system.core.').replace('force front_ready=0', 'force system.buffer_ready=0').replace('release front_ready', 'release system.buffer_ready')
 if a.continuous:
  tb=tb.replace('commit_context!=2 || ctx!=2', 'commit_context!=saved_context+1 || ctx!=saved_context+1').replace('ctx!=1', 'ctx!=saved_context').replace('filtered_context!=2', 'filtered_context!=saved_context+1')
 if a.continuous:
  tb=tb.replace('observed_peak=0;', 'observed_peak=0,held_results_revoked=0;')
  tb=tb.replace('ready=1;@(negedge clk);ready=0;', """if(j==0) begin
     force system.buffer_ready=0;
     @(posedge clk);while(!ingress_cancel) @(posedge clk);
     if(ov) $fatal(1,"held result escaped fault");
     stop_feed=1;@(negedge clk);release system.buffer_ready;
     repeat(20) begin @(negedge clk);if(ov) $fatal(1,"revoked result returned");end
     held_results_revoked=held_results_revoked+1;
    end else begin ready=1;@(negedge clk);ready=0;end""")
  tb=tb.replace('if(kind!=3 && ingress_faults)', 'if(j!=0 && kind!=3 && ingress_faults)')
  tb=tb.replace('maximum_ingress_words=%0d', 'maximum_ingress_words=%0d held_results_revoked=%0d').replace('min_lead,observed_peak)', 'min_lead,observed_peak,held_results_revoked)')
 if a.continuous:
  tb=tb.replace('repeat(33) begin @(negedge clk);if(', 'repeat(33) begin @(posedge clk);if(').replace('$fatal(1,"held");end', '$fatal(1,"held");end @(negedge clk);')
 for key,value in {'DECL':decl,'LAST':n-1,'TRAIN_LAST':nt-1,'GOLDLAST':len(ng)-1,'STRIDE':stride,'INIT':init,'CASES':'\n'.join(initial)}.items():tb=tb.replace('@'+key+'@',str(value))
 (out/'tb.sv').write_text(tb);sources=[]
 for name in ['fine_nco_config_cost_probe','nco_cost_probe','rrc_cost_probe','timing_request_cost_probe','interpolation_cost_probe','frontend_chain_cost_probe','phase_angle_cost_probe','training_channel_cost_probe','training_frontend_cost_probe']:
  src=out/(name+'.sv');shutil.copyfile(project/'sim/g0'/src.name,src);sources.append(src)
 if a.continuous:
  for name in ['continuous_adc_ingress_cost_probe.sv','compact_stream_fifo_cost_probe.sv','rf_rx_elastic_fifo.v','continuous_training_frontend_cost_probe.sv']:
   src=out/name;shutil.copyfile(project/'sim/g0'/name,src);sources.append(src)
 bp=a.vivado_bin.resolve()
 def run(name,args):
  with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 extra=[]
 if a.continuous:
  xpm=bp.parent/'data/ip/xpm';extra=[str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv')]
 run('compile',[str(bp/'xvlog'),'--sv',*extra,*map(str,sources),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','auto_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'auto_tb','-runall'])
 log=(out/'simulate.log').read_text();assert 'G0_AUTOCOMMIT_PASS' in log and 'Fatal:' not in log,log[-2000:]
 for name,old,new in [('boundary','wire own_commit=automatic_config && frontend_cfg_applied','wire own_commit=1\'b0'),('phase','automatic_config?out_final_phase:cfg_phase','automatic_config?(out_final_phase^32\'d1):cfg_phase')]:
  src=out/(name+'.sv');src.write_text((out/'training_frontend_cost_probe.sv').read_text().replace('module training_frontend_cost_probe','module '+name+'_frontend').replace(old,new))
  fault=out/(name+'_tb.sv');fault.write_text(tb.replace('module tb;','module '+name+'_tb;').replace('training_frontend_cost_probe #',name+'_frontend #'))
  extra_fault=[]
  if a.continuous:
   wrapper=out/(name+'_wrapper.sv');wrapper.write_text((out/'continuous_training_frontend_cost_probe.sv').read_text().replace('module continuous_training_frontend_cost_probe', 'module '+name+'_wrapper').replace('training_frontend_cost_probe #',name+'_frontend #'))
   fault.write_text(fault.read_text().replace('continuous_training_frontend_cost_probe system',name+'_wrapper system'));extra_fault=[str(wrapper)]
  run(name+'-compile',[str(bp/'xvlog'),'--sv',str(src),*extra_fault,str(fault)]);run(name+'-elaborate',[str(bp/'xelab'),name+'_tb','-s',name+'_tb','-mt','off']);run(name+'-simulate',[str(bp/'xsim'),name+'_tb','-runall'])
  l=(out/(name+'-simulate.log')).read_text();assert 'Fatal:' in l and 'G0_AUTOCOMMIT_PASS' not in l
 receipt={'continuous_no_ready_input':a.continuous,'raw_cadence_cycles':[12,13] if a.continuous else [8,12],'scope':__doc__,'simulation':log[log.index('G0_AUTOCOMMIT_PASS'):].splitlines()[0],'negative_controls':['own commit treated as foreign context change','phase output bit'],'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'routed_OOC':None,'g0_passed':False}
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if not a.sim_only:
  tcl='set_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(p)+'}\n' for p in sources)+'''synth_design -top training_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic {ENABLE_STARTUP=1 ENABLE_FINAL_CONFIG=1 ENABLE_CONJUGATE=1 ENABLE_TIMED_CONFIG=1 ENABLE_AUTOCOMMIT=1}
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
write_checkpoint -force autocommit.dcp
'''
  if a.continuous:
   tcl=tcl.replace('-top training_frontend_cost_probe','-top continuous_training_frontend_cost_probe').replace(' -generic {ENABLE_STARTUP=1 ENABLE_FINAL_CONFIG=1 ENABLE_CONJUGATE=1 ENABLE_TIMED_CONFIG=1 ENABLE_AUTOCOMMIT=1}', '')
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY XPM_CDC} [current_project]\n'+tcl
  (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fixture-directory',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--continuous',action='store_true');p.add_argument('--sim-only',action='store_true');main(p.parse_args())
