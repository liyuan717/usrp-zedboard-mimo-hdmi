#!/usr/bin/env python3
"""Finite calibrated-input frontend/two-column training integration RTL/OOC.

Input fixtures are actual observations AFTER floating calibration/notch/IQ sense
and signed16 quantization. They are not unchanged raw ADC. All expected NCO,
RRC and interpolation values are recomputed from input/public coefficients;
training output gold is independently checked against saved fixture results.
"""
import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import zlib
import numpy as np
from fec_inverse_cost_probe import routed_measurements
from g0_sounder import rrc, TRAIN_START
from g0_packet_probe import public_prefix
from g0_fixed_training_model import estimate_training, training_signs, TrainingInvalid

MASK48=(1<<48)-1
MASK80=(1<<80)-1


def pack(a,bits):
    return sum((int(v)&((1<<bits)-1))<<(bits*i) for i,v in enumerate(a))


def make_vectors(out,paths):
    taps=[int(x) for x in np.rint(rrc()*131072)]
    cos=[round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)]
    sin=[round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)]
    public=public_prefix();metadata=[];initial=[];all_adc=[];all_nco=[];all_rrc=[];all_train=[]
    (out/'rrc.hex').write_text(''.join(f'{x&0x3ffff:05x}\n' for x in taps[:21]))
    (out/'sine.hex').write_text(''.join(f'{x:05x}\n' for x in sin[:1024]))
    signs=[]
    for tx in (0,1):signs += [int(v.real<0)+2*int(v.imag<0) for v in public[TRAIN_START[tx]:TRAIN_START[tx]+512,tx]]
    (out/'training_signs.hex').write_text(''.join(f'{x:x}\n' for x in signs))
    for number,path in enumerate(list(paths)+[None]):
        synthetic=path is None
        if synthetic:
            path=paths[0].resolve()
        else:path=path.resolve()
        m=json.loads(path.with_suffix('.json').read_text())
        if m['schema']!='g0-training-frontend-fixture-v2' or not m['passed']:raise ValueError('requires passed v2 fixture')
        with np.load(path,allow_pickle=False) as archive:f={k:archive[k].copy() for k in archive.files}
        if synthetic:
            for key in ('input_iq','expected_nco_s18','expected_rrc_s20','training_iq','expected_H_s26','expected_step_s32','expected_energy_u50','expected_selected_rx'):f[key]=np.zeros_like(f[key])
        raw=f['input_iq'];phase=int(m['nco_config']['phase_u32']);step=int(m['nco_config']['step_u32'])
        offset=len(all_adc);mixed=[];filtered=[]
        for ordinal,row in enumerate(raw):
            if any(not -32768<=int(v)<32768 for v in row):raise ValueError('input not signed16')
            b=((phase+0x80000)&0xffffffff)>>20;co,si=cos[b],sin[b];mrow=[]
            for rx in (0,1):
                i,q=map(int,row[2*rx:2*rx+2])
                mrow += [(i*co-q*si+16384)//32768,(i*si+q*co+16384)//32768]
            mixed.append(mrow);all_adc.append(pack(row,16));all_nco.append(pack(mrow,18))
            if ordinal>=40:
                rr=[(sum(taps[k]*mixed[ordinal-k][lane] for k in range(41))+65536)//131072 for lane in range(4)]
                filtered.append(rr);all_rrc.append(pack(rr,20))
            else:all_rrc.append(0)
            phase=(phase+step)&0xffffffff
        if not np.array_equal(mixed,f['expected_nco_s18']) or not np.array_equal(filtered,f['expected_rrc_s20']):raise AssertionError('independent full-circle/direct RRC differs')
        positions=[int(x) for x in f['timing_first_position_relative_q32']]
        timing_step=int(m['timing']['step_q32_u35']);train=[];reports=[]
        for tx in (0,1):
            rowset=[]
            for k in range(448):
                q16=(positions[tx]+k*timing_step+32768)//65536;left,mu=divmod(q16,65536)
                a,b=filtered[left-20],filtered[left-19]
                row=[(2*(int(a[l])* (65536-mu)+int(b[l])*mu)+32768)//65536 for l in range(4)]
                rowset.append(row);all_train.append(pack(row,21))
            train.append(rowset)
            _,sgn=training_signs(public,tx)
            try:report=estimate_training(np.array(rowset,dtype=np.int64),sgn,tx)
            except TrainingInvalid as exc:
                if not synthetic:raise
                report=dict(exc.diagnostics,H_raw_s26=[0]*4,symbol_step_s32=0,expected_error=2)
            reports.append(report)
            if report['H_raw_s26']!=list(map(int,f['expected_H_s26'][tx])) or report['symbol_step_s32']!=int(f['expected_step_s32'][tx]) or report['energy_u50']!=list(map(int,f['expected_energy_u50'][tx])) or report['selected_rx']!=int(f['expected_selected_rx'][tx]):raise AssertionError('fixture training gold differs')
        if not np.array_equal(train,f['training_iq']):raise AssertionError('independent weighted interpolation differs')
        first_input_index=int(m['nco_config']['first_tag_u64'])&MASK48
        h=pack([v for r in reports for v in r['H_raw_s26']],26)
        st=pack([r['symbol_step_s32'] for r in reports],32)
        en=pack([v for r in reports for v in r['energy_u50']],50)
        se=pack([r['selected_rx'] for r in reports],1)
        milestones={k:((int(v['required_right_RRC_tag_u64'])&MASK48)-first_input_index)&MASK48 for k,v in m['milestones'].items()}
        initial.append(f"offsets[{number}]={offset};lengths[{number}]={len(raw)};known_at[{number}]={m['timing']['known_at_input_ordinal']};phases[{number}]=32'h{m['nco_config']['phase_u32']:08x};nsteps[{number}]=32'h{step:08x};tsteps[{number}]=35'd{timing_step};positions[{number}]=80'h{positions[0]&MASK80:020x};gold_h[{number}]=208'h{h:052x};gold_step[{number}]=64'h{st:016x};gold_energy[{number}]=200'h{en:050x};gold_selected[{number}]=2'd{se};header_offset[{number}]=48'd{milestones['HEADER_START']};data_offset[{number}]=48'd{milestones['DATA_START']};")
        metadata.append({'synthetic_zero_input':synthetic,'fixture':str(path),'fixture_crc32':f'{zlib.crc32(path.read_bytes()):08x}','inputs':len(raw),'known_at_input_ordinal':m['timing']['known_at_input_ordinal'],'provenance':m['source'],'milestones':m['milestones'],'training_columns':reports})
        if not synthetic:
            shutil.copyfile(path,out/path.name);shutil.copyfile(path.with_suffix('.json'),out/path.with_suffix('.json').name)
    for filename,values,width in [('adc.hex',all_adc,16),('nco-oracle.hex',all_nco,18),('rrc-oracle.hex',all_rrc,20),('training-oracle.hex',all_train,21)]:
        (out/filename).write_text(''.join(f'{v:0{width}x}\n' for v in values))
    (out/'vectors.json').write_text(json.dumps(metadata,indent=2)+'\n')
    return len(all_adc),len(all_train),'\n'.join(initial),metadata


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,cv=0,iv=0,jv=0,ready=0;
 reg [63:0] ct=0,it=0,data=0;reg [31:0] cp=0,cs=0,jid=0,jc=0,jg=0;
 reg [15:0] je=0;reg [79:0] jp=0;reg [34:0] js=0;
 wire cr,ir,jr,ov,armed,wr,idle,restart;wire [31:0] oid,oc,og,ctx,hgen;
 wire [15:0] oe;wire [207:0] raw_h;wire [63:0] raw_steps;wire [1:0] selected;
 wire [199:0] energies;wire [2:0] error,detail;wire [63:0] latest;wire [9:0] hcount;
 wire [207:0] h=@H_EXPR@;
 wire [63:0] steps=raw_steps ^ @STEP_FAULT@;
 training_frontend_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),
 .cfg_valid(cv),.cfg_ready(cr),.cfg_tag(ct),.cfg_phase(cp),.cfg_step(cs),
 .in_valid(iv),.in_ready(ir),.in_tag(it),.in_data(data),
 .job_valid(jv),.job_ready(jr),.job_id(jid),.job_context(jc),.job_generation(jg),
 .job_epoch(je),.job_first_position(jp),.job_step(js),
 .job_startup(1'b0),.job_count(9'd448),.job_first_symbol(10'd416),
 .out_valid(ov),.out_ready(ready),.out_id(oid),.out_context(oc),.out_generation(og),.out_epoch(oe),
 .out_h(raw_h),.out_phase_steps(raw_steps),.out_selected_rx(selected),.out_energies(energies),
 .out_error(error),.out_detail(detail),.armed(armed),.window_ready(wr),.request_idle(idle),
 .active_context(ctx),.history_generation(hgen),.latest_tag(latest),.history_count(hcount),.history_restart(restart));
 reg [63:0] adc[0:@LAST@];reg [71:0] nco_gold[0:@LAST@];reg [79:0] rrc_gold[0:@LAST@];
 reg [83:0] training_gold[0:@TRAIN_LAST@];
 integer offsets[0:3],lengths[0:3],known_at[0:3];
 reg [31:0] phases[0:3],nsteps[0:3];reg [34:0] tsteps[0:3];reg [79:0] positions[0:3];
 reg [207:0] gold_h[0:3];reg [63:0] gold_step[0:3];reg [199:0] gold_energy[0:3];reg [1:0] gold_selected[0:3];
 reg [47:0] header_offset[0:3],data_offset[0:3];
 reg [136:0] mix_queue[0:32767];reg [176:0] filter_queue[0:32767];
 integer mh=0,mt=0,fh=0,ft=0,cycle=0,source=0,source_ord=0,fixture=0,case_no=0,mode=0;
 integer current_cadence=8,last_adc_cycle=0,segment_inputs=0,stop_feed=0,feed_done=0;
 reg [63:0] base=0,expected_next=0;reg [31:0] model_context=0,model_generation=0;
 reg model_armed=0;reg [63:0] model_latest=0;integer model_count=0;
 integer accepted=0,mixed=0,filtered=0,samples=0,requests=0,responses=0;
 integer cfgs=0,jobs=0,terminal=0,success=0,errors=0,local_cancels=0,local_cancel_writes=0;
 integer cancelled_active=0,cancelled_held=0,global_cancel_cycles=0,reset_cycles=0;
 integer adc8_intervals=0,adc1213_intervals=0,output_hold_cycles=0,held_after_cfg=0;
 integer col0_observed=0,full_real_jobs=0,input_gap_publication=0,cfg_publication=0;
 integer training_columns=0,input_gaps=0,request_cancel_pending=0;
 integer request_pending=0,local_discarded_requests=0,global_discarded_requests=0,terminal_consumed=0;
 integer generated_rrc=0,discarded_rrc=0,discarded_mixed=0,held_input_writes=0,held_window_restarts=0,global_publication_revokes=0,zero_training_errors=0,zero_training_recoveries=0;
 integer job_requests=0,job_responses=0,job_samples=0,job_column_outputs=0;
 integer job_req_next0=0,job_req_next1=0;
 integer first_column_cycle=-1,matrix_cycle=-1,header_cycle=-1,payload_cycle=-1;
 integer wait_start=0,timeout_cycles=0,local_recoveries=0,stale_held_jobs=0;
 integer mf,measurement_rows=0;
 reg [79:0] expected_position=0;reg [34:0] expected_timing_step=0;
 wire [79:0] oracle_request_position=expected_position+{45'd0,expected_timing_step}*{48'd0,dut.frontend.req_id};
 wire [80:0] oracle_request_round={1'b0,oracle_request_position}+81'd32768;
 wire [79:0] oracle_response_position=expected_position+{45'd0,expected_timing_step}*{48'd0,dut.sample_id};
 wire [80:0] oracle_response_round={1'b0,oracle_response_position}+81'd32768;
 reg [31:0] expected_id=0,expected_ctx=0,expected_gen=0;reg [15:0] expected_epoch=0;
 reg [2:0] expected_error=0,expected_detail=0;reg [207:0] expected_h=0;
 reg [63:0] expected_steps=0;reg [1:0] expected_selected=0;reg [199:0] expected_energies=0;
 reg job_active=0,seen=0,held=0;reg [591:0] held_bits;
 wire [591:0] all_output={oid,oc,og,oe,h,steps,selected,energies,error,detail};
 reg [176:0] expect_filter;reg [63:0] center;
 integer tx_index,ordinal_index,step_cycles;
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst||cancel) begin
   if(job_active) begin
    if(seen) cancelled_held=cancelled_held+1;else cancelled_active=cancelled_active+1;
   end
   global_discarded_requests=global_discarded_requests+request_pending;request_pending=0;
   discarded_rrc=discarded_rrc+(ft-fh);discarded_mixed=discarded_mixed+(mt-mh);
   job_active=0;seen=0;held=0;mh=0;mt=0;fh=0;ft=0;model_count=0;model_latest=0;model_armed=0;last_adc_cycle=0;
   if(rst) begin model_context=0;model_generation=0;reset_cycles=reset_cycles+1;end
   else begin model_context=model_context+1;model_generation=model_generation+1;global_cancel_cycles=global_cancel_cycles+1;end
  end else begin
   if(ctx!==model_context || hgen!==model_generation) $fatal(1,"context/generation mismatch");
   if(held && (!ov || all_output!==held_bits)) $fatal(1,"held atomic result changed");
   if(ov&&!seen) begin
    if(!job_active || {oid,oc,og,oe,error,detail,h,steps,selected,energies}!==
       {expected_id,expected_ctx,expected_gen,expected_epoch,expected_error,expected_detail,expected_h,expected_steps,expected_selected,expected_energies})
      $fatal(1,"matrix mismatch case%0d id%h/%h err%0d:%0d/%0d:%0d H%h/%h steps%h/%h",case_no,oid,expected_id,error,detail,expected_error,expected_detail,h,expected_h,steps,expected_steps);
    if(error==0 && (job_samples!=896 || job_requests!=896 || job_responses!=896 || job_column_outputs!=2))
      $fatal(1,"atomic matrix before both complete %0d/%0d/%0d/%0d",job_samples,job_requests,job_responses,job_column_outputs);
    if(error==0) begin success=success+1;matrix_cycle=cycle;end else begin errors=errors+1;if(error==4) zero_training_errors=zero_training_errors+1;end
    terminal=terminal+1;seen=1;
    if(mode==4) begin timeout_cycles=cycle-wait_start;if(timeout_cycles<4097) $fatal(1,"future timeout early");end
   end
   held=ov&&!ready;if(held) begin held_bits=all_output;output_hold_cycles=output_hold_cycles+1;end
   if(ov&&ready) begin terminal_consumed=terminal_consumed+1;job_active=0;seen=0;held=0;end
   if(cv&&cr) begin
    if(held) held_after_cfg=held_after_cfg+1;
    if(dut.state==3 && dut.column && dut.train_valid) cfg_publication=cfg_publication+1;
    model_context=model_context+1;expected_next=ct;model_armed=1;cfgs=cfgs+1;last_adc_cycle=0;segment_inputs=0;
   end
   if(iv&&ir) begin
    if(it!=expected_next) begin
     input_gaps=input_gaps+1;model_armed=0;
     if(dut.state==3 && dut.column && dut.train_valid) input_gap_publication=input_gap_publication+1;
    end else begin
     mix_queue[mt]={1'(source_ord==0),it,nco_gold[source]};mt=mt+1;
     if(source_ord>=40) begin
      center={it[63:48],it[47:0]-48'd20};
      filter_queue[ft]={model_context,1'(source_ord==40),center,rrc_gold[source]};ft=ft+1;generated_rrc=generated_rrc+1;
     end
     if(last_adc_cycle) begin
      if(current_cadence==8) begin
       if(cycle-last_adc_cycle!=8) $fatal(1,"ADC8 service difference%0d",cycle-last_adc_cycle);
       adc8_intervals=adc8_intervals+1;
      end else if(current_cadence==12) begin
       step_cycles=12+((source_ord-1)%2);
       if(cycle-last_adc_cycle!=step_cycles) $fatal(1,"ADC12/13 cadence");
       adc1213_intervals=adc1213_intervals+1;
      end
     end
     last_adc_cycle=cycle;expected_next={expected_next[63:48],expected_next[47:0]+48'd1};
     segment_inputs=segment_inputs+1;accepted=accepted+1;if(held) held_input_writes=held_input_writes+1;
    end
   end
   if(dut.frontend.mixed_valid && dut.frontend.mixed_ready) begin
    if(mh>=mt || {dut.frontend.mixed_start,dut.frontend.mixed_tag,dut.frontend.mixed_data}!==mix_queue[mh]) $fatal(1,"NCO oracle mismatch");
    mh=mh+1;mixed=mixed+1;
   end
   if(dut.frontend.filtered_valid && dut.frontend.filtered_ready) begin
    if(fh>=ft || {dut.frontend.filtered_context,dut.frontend.filtered_start,dut.frontend.filtered_tag,dut.frontend.filtered_data}!==filter_queue[fh]) $fatal(1,"RRC oracle mismatch");
    expect_filter=filter_queue[fh];fh=fh+1;filtered=filtered+1;
    if(held && expect_filter[144]) held_window_restarts=held_window_restarts+1;
    if(expect_filter[144] || model_count==0 || expect_filter[143:80]!={model_latest[63:48],model_latest[47:0]+48'd1}) begin model_generation=model_generation+1;model_count=1;end
    else if(model_count<512) model_count=model_count+1;
    model_latest=expect_filter[143:80];
    if(dut.request_cancel) local_cancel_writes=local_cancel_writes+1;
    if(expect_filter[143:80]=={base[63:48],base[47:0]+header_offset[fixture]} && header_cycle<0) header_cycle=cycle;
    if(expect_filter[143:80]=={base[63:48],base[47:0]+data_offset[fixture]} && payload_cycle<0) payload_cycle=cycle;
   end
   if(jv&&jr) begin
    jobs=jobs+1;job_active=1;seen=0;job_requests=0;job_responses=0;job_samples=0;job_column_outputs=0;
    job_req_next0=0;job_req_next1=0;
    expected_id=jid;expected_ctx=jc;expected_gen=jg;expected_epoch=je;expected_position=jp;expected_timing_step=js;wait_start=cycle;
   end
   if(dut.frontend.req_valid && dut.frontend.req_ready) begin
    if(!job_active) $fatal(1,"request without active job");
    if(dut.frontend.req_generation!==expected_gen || dut.frontend.req_tag!=={expected_epoch,oracle_request_round[79:32]} || dut.frontend.req_mu!==oracle_request_round[31:16]) $fatal(1,"request closed-form position/identity");
    if(dut.frontend.req_id<448) begin
     if(dut.frontend.req_id!=job_req_next0) $fatal(1,"TX0 request sequence");job_req_next0=job_req_next0+1;
    end else begin
     if(dut.frontend.req_id!=512+job_req_next1 || job_req_next0!=448) $fatal(1,"TX1 request sequence");job_req_next1=job_req_next1+1;
    end
    requests=requests+1;job_requests=job_requests+1;request_pending=request_pending+1;
   end
   if(dut.sample_valid && dut.sample_ready) begin
    if(dut.sample_generation!==expected_gen || dut.sample_tag!=={expected_epoch,oracle_response_round[79:32]} || dut.sample_mu!==oracle_response_round[31:16]) $fatal(1,"response position/identity echo");
    responses=responses+1;job_responses=job_responses+1;request_pending=request_pending-1;if(request_pending<0) $fatal(1,"response without outstanding request");
    if(dut.sample_error==0 && dut.state==2 && !dut.owner_lost) begin
     tx_index=dut.sample_id>=512;ordinal_index=dut.sample_id-(tx_index?512:0);
     if(ordinal_index<0 || ordinal_index>=448 || dut.sample_data!==training_gold[fixture*896+tx_index*448+ordinal_index]) $fatal(1,"F1 input oracle mismatch case%0d col%0d index%0d",case_no,tx_index,ordinal_index);
     if(!dut.train_ready) $fatal(1,"accepted sample without training ready");
    end
   end
   if(dut.train_input_valid && dut.train_ready) begin
    samples=samples+1;job_samples=job_samples+1;
   end
   if(dut.train_valid) begin
    if(dut.train_error!==(fixture==3?3'd2:3'd0) || dut.train_id!==expected_id || dut.train_h!==gold_h[fixture][dut.train_tx*104+:104] || dut.train_step!==gold_step[fixture][dut.train_tx*32+:32] ||
       {dut.train_energy1,dut.train_energy0}!==gold_energy[fixture][dut.train_tx*100+:100] || dut.train_selected!==gold_selected[fixture][dut.train_tx]) $fatal(1,"training column oracle mismatch");
    training_columns=training_columns+1;job_column_outputs=job_column_outputs+1;
    if(!dut.train_tx) begin first_column_cycle=cycle;col0_observed=col0_observed+1;end
   end
   if(dut.request_cancel) begin local_cancels=local_cancels+1;if(request_pending>0) request_cancel_pending=request_cancel_pending+1;local_discarded_requests=local_discarded_requests+request_pending;request_pending=0;end
  end
  #1;if((rst||cancel)&&(ov||jr||ir||cr)) $fatal(1,"global reset/cancel gate");
 end
 task clear_all;
  begin
   iv=0;cv=0;jv=0;ready=0;rst=1;repeat(2) @(negedge clk);rst=0;
   first_column_cycle=-1;matrix_cycle=-1;header_cycle=-1;payload_cycle=-1;stop_feed=0;feed_done=0;
  end
 endtask
 task configure;
  begin
   ct=base;cp=phases[fixture];cs=nsteps[fixture];cv=1;
   @(posedge clk);while(!cr) @(posedge clk);
   @(negedge clk);cv=0;
  end
 endtask
 task feed(input integer limit);
  integer k,d;
  begin
   feed_done=0;
   for(k=0;k<limit && !stop_feed;k=k+1) begin
    source_ord=k;source=offsets[fixture]+k;it={base[63:48],base[47:0]+48'(k)};data=adc[source];iv=1;
    @(posedge clk);while(!ir && !stop_feed) @(posedge clk);
    @(negedge clk);iv=0;
    d=current_cadence==12?12+(k%2):8;
    if(!stop_feed) repeat(d-1) @(negedge clk);
   end
   iv=0;feed_done=1;
  end
 endtask
 task issue(input integer code,input integer det,input integer bad);
  begin
   expected_error=3'(code);expected_detail=3'(det);
   expected_h=code==0?gold_h[fixture]:208'd0;expected_steps=code==0?gold_step[fixture]:64'd0;
   expected_selected=code==0?gold_selected[fixture]:2'd0;expected_energies=code==0?gold_energy[fixture]:200'd0;
   jid=32'hfffffff0+jobs*13;jc=model_context;jg=model_generation;je=base[63:48];
   jp=({base[47:0],32'd0}+positions[fixture]);js=tsteps[fixture];
   if(bad==1) jp=jp-(80'd600<<32);
   if(bad==2) jp=jp+(80'd100000<<32);
   if(bad==3) js=0;
   if(bad==4) jc=jc-1;
   jv=1;@(posedge clk);while(!jr) @(posedge clk);
   @(negedge clk);jv=0;
  end
 endtask
 task accept_terminal;
  begin
   while(!ov) @(negedge clk);
   repeat(19) @(negedge clk);ready=1;
   @(posedge clk);if(!ov) $fatal(1,"terminal absent");
   @(negedge clk);ready=0;
  end
 endtask
 task revoke(input integer reset_it);
  begin
   stop_feed=1;while(!feed_done) @(negedge clk);iv=0;jv=0;cv=0;
   if((mode-10)%5==3) begin
    while(!dut.train_valid) @(negedge clk);
    if(!dut.column || dut.state!=3) $fatal(1,"global publication revoke target");
    global_publication_revokes=global_publication_revokes+1;
   end
   if(reset_it) rst=1;else cancel=1;
   repeat(2) @(negedge clk);rst=0;cancel=0;
   repeat(5) begin @(negedge clk);if(ov) $fatal(1,"stale output after revoke");end
  end
 endtask
 task mark_error(input integer code,input integer det);
  begin expected_error=3'(code);expected_detail=3'(det);expected_h=0;expected_steps=0;expected_selected=0;expected_energies=0;end
 endtask
 task run_case(input integer f,input integer m,input integer cad,input integer wrap);
  integer before_terminal,before_cancel;
  begin
   clear_all;fixture=f;mode=m;current_cadence=cad;case_no=case_no+1;
   base={16'(10+case_no),48'(wrap?48'hffffffffff80:1024)};
   configure;before_terminal=terminal;before_cancel=cancelled_active+cancelled_held;
   fork
    feed(lengths[fixture]);
    begin
     while(segment_inputs<=known_at[fixture]) @(negedge clk);
     if(m==3) issue(3,2,1);
     else if(m==4) issue(3,3,2);
     else if(m==5) begin
      issue(2,2,3);accept_terminal;
      while(!idle || !jr) @(negedge clk);
      local_recoveries=local_recoveries+1;issue(0,0,0);
     end else if(m==6) begin
      issue(1,3,4);accept_terminal;
      while(!idle || !jr) @(negedge clk);
      local_recoveries=local_recoveries+1;issue(0,0,0);
     end else issue(0,0,0);
     if(m==7) begin
      while(!(dut.state==2 && !dut.column && dut.received>=20)) @(negedge clk);
      stop_feed=1;while(!feed_done) @(negedge clk);
      current_cadence=0;last_adc_cycle=0;mark_error(1,4);it={expected_next[63:48],expected_next[47:0]+48'd1};iv=1;
      @(posedge clk);if(!ir) $fatal(1,"gap not accepted");@(negedge clk);iv=0;
     end else if(m==8 || m==9) begin
      while(!(dut.state==3 && dut.column)) @(negedge clk);
      stop_feed=1;while(!feed_done) @(negedge clk);
      while(!dut.train_valid) @(negedge clk);
      current_cadence=0;last_adc_cycle=0;
      if(m==8) begin
       mark_error(1,4);it={expected_next[63:48],expected_next[47:0]+48'd1};iv=1;
       @(posedge clk);if(!ir || !dut.train_valid) $fatal(1,"publication gap overlap missing");@(negedge clk);iv=0;
      end else begin
       mark_error(1,1);base=base+64'd10000;ct=base;cp=phases[fixture];cs=nsteps[fixture];cv=1;
       @(posedge clk);if(!cr || !dut.train_valid) $fatal(1,"publication cfg overlap missing");@(negedge clk);cv=0;
      end
     end else if(m>=10 && m<=19) begin
      if((m-10)%5==0) while(!(dut.state==2 && !dut.column && dut.received>=20)) @(negedge clk);
      if((m-10)%5==1) while(!(dut.state==3 && !dut.column)) @(negedge clk);
      if((m-10)%5==2) while(!(dut.state==2 && dut.column && dut.received>=20)) @(negedge clk);
      if((m-10)%5==3) while(!(dut.state==3 && dut.column)) @(negedge clk);
      if((m-10)%5==4) begin while(!ov) @(negedge clk);repeat(20) @(negedge clk);end
      revoke(m>=15);
     end
     if(m<10) accept_terminal;
    end
   join
   if(m<10) begin
    if(terminal-before_terminal!=(m==5||m==6?2:1)) $fatal(1,"terminal count");
    if(m<3 || m==5 || m==6) begin
     if(first_column_cycle<0 || matrix_cycle<0 || header_cycle<0 || payload_cycle<0 || first_column_cycle>=header_cycle || matrix_cycle>=payload_cycle) $fatal(1,"actual milestone deadline");
     $fwrite(mf,"%0d %0d %0d %0d %0d %0d %0d\n",case_no,fixture,current_cadence,first_column_cycle,matrix_cycle,header_cycle,payload_cycle);
     measurement_rows=measurement_rows+1;full_real_jobs=full_real_jobs+1;
    end
   end else if(cancelled_active+cancelled_held-before_cancel!=1) $fatal(1,"revoke accounting");
   repeat(20) @(negedge clk);
  end
 endtask
 task held_reconfigure(input integer bad);
  integer prior;
  begin
   clear_all;fixture=0;mode=20+bad;current_cadence=8;case_no=case_no+1;base={16'(10+case_no),48'd1024};configure;
   prior=terminal;
   fork
    feed(lengths[fixture]);
    begin
     while(segment_inputs<=known_at[fixture]) @(negedge clk);
     if(bad) issue(2,2,3);else issue(0,0,0);
     while(!ov) @(negedge clk);repeat(40) @(negedge clk);
     stop_feed=1;while(!feed_done) @(negedge clk);
    end
   join
   base={base[63:48],base[47:0]+48'd100000};configure;
   stop_feed=0;feed(256);repeat(20) @(negedge clk);
   if(!ov || jr || ctx==expected_ctx || hgen==expected_gen) $fatal(1,"held result/new window isolation");
   accept_terminal;
   if(terminal-prior!=1) $fatal(1,"held result terminal count");
  end
 endtask
 task stale_descriptor_across_cfg;
  integer prior;
  begin
   clear_all;fixture=1;mode=22;current_cadence=8;case_no=case_no+1;base={16'(10+case_no),48'd1024};configure;
   feed(known_at[fixture]+1);repeat(24) @(negedge clk);
   mark_error(1,3);jid=32'hfffffff0+jobs*13;jc=model_context;jg=model_generation;je=base[63:48];
   jp={base[47:0],32'd0}+positions[fixture];js=tsteps[fixture];
   // Old descriptor is asserted while cfg_valid withdraws window_ready, then
   // remains held until the newly configured RRC window permits admission.
   base={base[63:48],base[47:0]+48'd100000};ct=base;cp=phases[fixture];cs=nsteps[fixture];cv=1;jv=1;
   @(posedge clk);if(!cr || jr) $fatal(1,"stale held descriptor cfg overlap");
   @(negedge clk);cv=0;prior=terminal;stop_feed=0;
   fork
    feed(256);
    begin
     @(posedge clk);while(!jr) @(posedge clk);
     @(negedge clk);jv=0;stale_held_jobs=stale_held_jobs+1;
     accept_terminal;
    end
   join
   if(terminal-prior!=1 || job_requests!=0 || job_samples!=0) $fatal(1,"stale descriptor launched work");
  end
 endtask
 task zero_training_then_recover;
  reg [31:0] saved_context,saved_generation;
  integer prior;
  begin
   clear_all;fixture=3;mode=23;current_cadence=8;case_no=case_no+1;base={16'(10+case_no),48'd1024};configure;
   prior=terminal;
   fork
    feed(lengths[fixture]);
    begin
     while(segment_inputs<=known_at[fixture]) @(negedge clk);
     saved_context=model_context;saved_generation=model_generation;
     issue(4,2,0);accept_terminal;
    end
   join
   if(ctx!=saved_context || hgen!=saved_generation || !armed || !wr || !idle || job_samples!=448 || job_requests!=448 || job_column_outputs!=1) $fatal(1,"zero training local abort changed history or launchedTX1");
   fixture=0;base={base[63:48],base[47:0]+48'd100000};configure;stop_feed=0;
   fork
    feed(lengths[fixture]);
    begin
     while(segment_inputs<=known_at[fixture]) @(negedge clk);
     issue(0,0,0);accept_terminal;
    end
   join
   if(terminal-prior!=2) $fatal(1,"zero training recovery terminal count");
   zero_training_recoveries=zero_training_recoveries+1;
  end
 endtask
 initial begin
  @INIT@
  $readmemh("adc.hex",adc);$readmemh("nco-oracle.hex",nco_gold);$readmemh("rrc-oracle.hex",rrc_gold);$readmemh("training-oracle.hex",training_gold);
  mf=$fopen("milestones.txt","w");repeat(2) @(negedge clk);
  run_case(0,0,12,0);run_case(1,1,8,0);run_case(2,2,8,1);
  run_case(0,3,8,0);run_case(0,4,8,0);run_case(0,5,8,0);run_case(1,6,8,0);
  run_case(0,7,8,0);run_case(0,8,8,0);run_case(0,9,8,0);
  for(mode=10;mode<20;mode=mode+1) run_case(0,mode,8,0);
  held_reconfigure(0);held_reconfigure(1);stale_descriptor_across_cfg;zero_training_then_recover;
  repeat(30) @(negedge clk);
  if(success!=9 || errors!=10 || local_recoveries!=2 || input_gap_publication!=1 || cfg_publication!=1 || cancelled_active!=8 || cancelled_held!=2 ||
     adc8_intervals<30000 || adc1213_intervals<5000 || samples<8000 || local_cancels!=10 || zero_training_errors!=1 || zero_training_recoveries!=1 || local_cancel_writes<1 || held_after_cfg!=2 || held_window_restarts!=2 || stale_held_jobs!=1 || global_publication_revokes!=2 || measurement_rows!=5 || output_hold_cycles<100 || request_cancel_pending<1)
    $fatal(1,"coverage success%0d errors%0d local%0d revokes%0d/%0d samples%0d pending%0d",success,errors,local_cancels,cancelled_active,cancelled_held,samples,request_cancel_pending);
  if(requests!=responses+local_discarded_requests+global_discarded_requests || request_pending!=0 || jobs!=terminal+cancelled_active || terminal!=terminal_consumed+cancelled_held || accepted!=mixed+discarded_mixed+(mt-mh) || generated_rrc!=filtered+discarded_rrc+(ft-fh)) $fatal(1,"conservation failed");
  $display("G0_TRAINING_FRONTEND_PASS cases=%0d jobs=%0d terminal=%0d success=%0d errors=%0d accepted=%0d mixed=%0d filtered=%0d requests=%0d responses=%0d samples=%0d training_columns=%0d local_cancels=%0d local_cancel_writes=%0d local_recoveries=%0d cancelled_active=%0d cancelled_held=%0d adc8_intervals=%0d adc1213_intervals=%0d output_hold_cycles=%0d input_gap_publication=%0d cfg_publication=%0d timeout_cycles=%0d measurement_rows=%0d request_cancel_pending=%0d local_discarded_requests=%0d global_discarded_requests=%0d terminal_consumed=%0d discarded_rrc=%0d discarded_mixed=%0d held_after_cfg=%0d held_window_restarts=%0d held_input_writes=%0d stale_held_jobs=%0d global_publication_revokes=%0d zero_training_errors=%0d zero_training_recoveries=%0d",case_no,jobs,terminal,success,errors,accepted,mixed,filtered,requests,responses,samples,training_columns,local_cancels,local_cancel_writes,local_recoveries,cancelled_active,cancelled_held,adc8_intervals,adc1213_intervals,output_hold_cycles,input_gap_publication,cfg_publication,timeout_cycles,measurement_rows,request_cancel_pending,local_discarded_requests,global_discarded_requests,terminal_consumed,discarded_rrc,discarded_mixed,held_after_cfg,held_window_restarts,held_input_writes,stale_held_jobs,global_publication_revokes,zero_training_errors,zero_training_recoveries);
  $finish;
 end
 initial begin #1000000000;$fatal(1,"timeout");end
endmodule
'''


def main(a):
    if a.final_config and not a.enable_startup:raise ValueError('--final-config requires --enable-startup')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    paths=sorted(a.fixture_directory.glob('packet-air-g12-long-r*.npz'))
    if len(paths)!=3:raise ValueError('three actual-source fixtures required')
    n,nt,init,metadata=make_vectors(out,paths)
    tb=TB.replace('@LAST@',str(n-1)).replace('@TRAIN_LAST@',str(nt-1)).replace('@INIT@',init).replace('@H_EXPR@','raw_h').replace('@STEP_FAULT@',"64'd0")
    if a.enable_startup:tb=tb.replace('training_frontend_cost_probe dut', 'training_frontend_cost_probe #(.ENABLE_STARTUP(1)) dut')
    if a.final_config:tb=tb.replace('.ENABLE_STARTUP(1)', '.ENABLE_STARTUP(1),.ENABLE_FINAL_CONFIG(1)')
    (out/'tb.sv').write_text(tb);project=Path(__file__).resolve().parents[1]
    names=['fine_nco_config_cost_probe.sv','nco_cost_probe.sv','rrc_cost_probe.sv','timing_request_cost_probe.sv','interpolation_cost_probe.sv','frontend_chain_cost_probe.sv','phase_angle_cost_probe.sv','training_channel_cost_probe.sv','training_frontend_cost_probe.sv']
    sources=[]
    for name in names:
        path=out/name;shutil.copyfile(project/'sim/g0'/name,path);sources.append(path)
    bp=a.vivado_bin.resolve();commands=[]
    def run(name,args):
        commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',*[str(p) for p in sources],str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','training_frontend_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'training_frontend_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_TRAINING_FRONTEND_PASS' not in sim or 'Fatal:' in sim:raise ValueError('joint simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',sim.split('G0_TRAINING_FRONTEND_PASS')[-1])}
    milestone_rows=[]
    for line in (out/'milestones.txt').read_text().splitlines():
        case,fixture,cadence,c0,matrix,header,payload=map(int,line.split())
        milestone_rows.append(dict(case=case,fixture=fixture,ADC_cadence=cadence,column0_cycle=c0,matrix_cycle=matrix,header_right_neighbor_cycle=header,payload_right_neighbor_cycle=payload,
         column0_margin_to_header_cycles=header-c0,matrix_margin_to_header_cycles=header-matrix,matrix_margin_to_payload_cycles=payload-matrix))
    if len(milestone_rows)!=measured['measurement_rows'] or len(milestone_rows)!=5:raise AssertionError('missing normal milestone measurements')
    (out/'normal-milestones.json').write_text(json.dumps(milestone_rows,indent=2)+'\n')
    for kind in ('phase','H','column'):
        name=kind.lower()+'_fault_tb';fault=tb.replace('module tb;',f'module {name};').replace('milestones.txt',name+'-milestones.txt')
        if kind=='phase':fault=fault.replace("raw_steps ^ 64'd0","raw_steps ^ (ov && error==0 ? 64'd1 : 64'd0)")
        elif kind=='H':fault=fault.replace('h=raw_h;','h=raw_h ^ (ov && error==0 ? 208\'d1 : 208\'d0);')
        else:fault=fault.replace('h=raw_h;','h={raw_h[103:0],raw_h[207:104]};')
        fp=out/(name+'.sv');fp.write_text(fault)
        run(name+'-compile',[str(bp/'xvlog'),'--sv',str(fp)])
        run(name+'-elaborate',[str(bp/'xelab'),name,'-s',name,'-mt','off'])
        run(name+'-simulate',[str(bp/'xsim'),name,'-runall'])
        log=(out/(name+'-simulate.log')).read_text()
        if 'Fatal: matrix mismatch' not in log or 'G0_TRAINING_FRONTEND_PASS' in log:raise ValueError(kind+' negative not rejected')
    receipt={'final_configuration_enabled':a.final_config,'startup_enabled_normal_mode_regression':a.enable_startup,'scope':__doc__,'clock_hz':50000000,'actual_source_fixtures':3,'measured':measured,'milestones':milestone_rows,
     'milestone_contract':'Actual matching RRC filtered-history right-neighbor handshake; column0 observed internally, no header/payload consumer implemented',
     'independent_oracle':'input-derived full-circle NCO/direct41tap RRC/weighted F1 plus model and fixture H,phase,energy checks; no DUT output used as sole expected data',
     'negative_controls_rejected':['phase bit','H bit','swapped H columns'],'routed_OOC':None,'g0_passed':False,'new_RF_transmissions':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:
        (out/'ooc.tcl').write_text('set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{p}}}\n' for p in sources)+'''synth_design -top training_frontend_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2.0 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
write_checkpoint -force training_frontend_cost_probe.dcp
''')
        if a.enable_startup:
            p=out/'ooc.tcl';p.write_text(p.read_text().replace('-mode out_of_context','-mode out_of_context -generic ENABLE_STARTUP=1'))
        if a.final_config:
            p=out/'ooc.tcl';p.write_text(p.read_text().replace('-generic ENABLE_STARTUP=1','-generic {ENABLE_STARTUP=1 ENABLE_FINAL_CONFIG=1}'))
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=routed_measurements(out);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True)
    p.add_argument('--final-config',action='store_true')
    p.add_argument('--enable-startup',action='store_true')
    p.add_argument('--sim-only',action='store_true');main(p.parse_args())
