#!/usr/bin/env python3
"""Independent ADC->NCO->RRC->configured timing/history/interpolation probe.

The input is raw signed16 ADC, not the measured RRC output. Expected NCO uses
a full-circle table and expected RRC uses direct41-tap convolution. Configured
CFO/timing are test inputs; no acquisition estimator or complete PHY claim.
"""
import argparse
from collections import deque
import json
from pathlib import Path
import re
import shutil
import subprocess
import zlib
import numpy as np
from fec_inverse_cost_probe import routed_measurements
from g0_sounder import rrc

MASK48 = (1 << 48) - 1


def pack(values, width):
    return sum((int(value) & ((1 << width) - 1)) << (width * lane) for lane, value in enumerate(values))


def make_vectors(out, captures):
    taps = np.rint(rrc() * 131072).astype(np.int64)
    angle = np.arange(4096) * (2 * np.pi / 4096)
    cosine = np.rint(65536 * np.cos(angle)).astype(np.int64)
    sine = np.rint(65536 * np.sin(angle)).astype(np.int64)
    (out / 'sine.hex').write_text(''.join(f'{int(x):05x}\n' for x in sine[:1024]))
    (out / 'rrc.hex').write_text(''.join(f'{int(x)&0x3ffff:05x}\n' for x in taps[:21]))
    rng = np.random.default_rng(783006)
    segments = []
    adc, expected, metadata = [], [], []

    def add(name, values, tag, phase, step, provenance=None):
        offset = len(adc)
        history = deque(maxlen=41)
        original_tag = tag
        for ordinal, row in enumerate(values):
            values16 = [int(x) for x in row]
            if min(values16) < -32768 or max(values16) > 32767:
                raise ValueError('ADC interface overflow')
            adc.append((tag << 64) | pack(values16, 16))
            bin_index = ((phase + 0x80000) & 0xffffffff) >> 20
            co, si = int(cosine[bin_index]), int(sine[bin_index])
            mixed = []
            for lane in range(2):
                i, q = values16[2*lane:2*lane+2]
                mixed += [(i*co-q*si+16384)//32768, (i*si+q*co+16384)//32768]
            history.append(mixed)
            if len(history) == 41:
                result = (taps @ np.asarray(history, dtype=np.int64)[::-1] + 65536) // 131072
                if np.any(result < -(1 << 19)) or np.any(result >= 1 << 19):
                    raise ValueError('RRC oracle range')
                center = (tag & ~MASK48) | ((tag - 20) & MASK48)
                expected.append((int(ordinal == 40) << 144) | (center << 80) | pack(result, 20))
            else:
                expected.append(0)
            phase = (phase + step) & 0xffffffff
            tag = (tag & ~MASK48) | ((tag + 1) & MASK48)
        segments.append((offset, len(values), original_tag))
        metadata.append({'name': name, 'offset': offset, 'inputs': len(values),
                         'first_tag': original_tag, 'provenance': provenance})

    def synthetic(length):
        v = rng.integers(-32768, 32768, (length, 4))
        v[::17] = [32767, -32768, -32768, 32767]
        return v

    # Segment0 is intentionally long enough to exercise many ring wraps.
    phases = [0x12345678, 0x81234567, 0xfff80000, 0x007fffff]
    steps = [0x23456789, 0xf1234567, 0x00345678, 0xfe765432]
    base = (1 << 48) + 1024
    add('saturated', synthetic(4096), base, phases[0], steps[0])
    add('center_contiguous_restart', synthetic(768), base+4096-40, phases[1], steps[1])
    add('overlapping_restart', synthetic(768), base+4096-40+100, phases[2], steps[2])
    add('index_wrap', synthetic(1100), (4 << 48) | (MASK48-127), phases[3], steps[3])
    for number, path in enumerate(captures):
        if path.stat().st_size % 16:
            raise ValueError('truncated ADC capture')
        raw = np.memmap(path, dtype=np.dtype([('tag', '<u8'), ('iq', '<i2', (4,))]), mode='r')
        crc = f'{zlib.crc32(raw):08x}'
        if crc != json.loads((path.parent/'receipt.json').read_text())['capture']['crc32']:
            raise ValueError('ADC capture differs from transport receipt')
        fragment = raw[154000:156048]
        if len(fragment) != 2048 or np.any(np.diff(fragment['tag']) != 1):
            raise ValueError('actual ADC fragment is not contiguous2048')
        phase, step = 0x81234567, round(236000*(1 << 32)/4000000) & 0xffffffff
        phases.append(phase); steps.append(step)
        add(f'actual_ADC_{number}', fragment['iq'], int(fragment[0]['tag']), phase, step,
            {'path': str(path.resolve()), 'crc32': crc, 'slice': [154000, 156048],
             'CFO_for_cost_probe_only_hz': -236000})
    phases.append(0x81234567); steps.append(0xfedcba98)
    add('cancel_and_rearm', synthetic(400), (9 << 48) + 4000, phases[-1], steps[-1])
    (out/'adc.hex').write_text(''.join(f'{value:032x}\n' for value in adc))
    (out/'oracle.hex').write_text(''.join(f'{value:037x}\n' for value in expected))
    (out/'vectors.json').write_text(json.dumps(metadata, indent=2)+'\n')
    initial = []
    for index, (offset, length, first) in enumerate(segments):
        initial.append(f"offsets[{index}]={offset};lengths[{index}]={length};bases[{index}]=64'h{first:016x};"
                       f"phases[{index}]=32'h{phases[index]:08x};steps[{index}]=32'h{steps[index]:08x};")
    return len(adc), metadata, '\n'.join(initial)


TB = r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,cv=0,iv=0,tv=0,ready=1;
 wire cr,ir,tr,done,ov,armed,wr;wire [1:0] terr;wire [15:0] emitted;
 reg [63:0] ct=0,it=0,idata=0;reg [31:0] cp=0,cs=0,tc=0,tid=0;
 reg [15:0] te=0,tn=0;reg [79:0] tp=0;reg [34:0] ts=0;
 wire [31:0] og,oid,active_ctx,bound,hgen,nf,rf,hf;wire [63:0] ot,latest;
 wire [15:0] omu;wire [2:0] error;wire [83:0] raw_result;
 wire [83:0] result=raw_result ^ @FAULT@;
 wire [9:0] hcount;
 frontend_chain_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.request_cancel(1'b0),.request_idle(),.history_restart(),.cfg_valid(cv),.cfg_ready(cr),
 .cfg_tag(ct),.cfg_phase(cp),.cfg_step(cs),.in_valid(iv),.in_ready(ir),.in_tag(it),.in_data(idata),
 .timing_cfg_valid(tv),.timing_cfg_ready(tr),.timing_cfg_epoch(te),.timing_cfg_context(tc),
 .timing_cfg_first_position(tp),.timing_cfg_step(ts),.timing_cfg_count(tn),.timing_cfg_first_id(tid),
 .timing_done(done),.timing_cfg_error(terr),.timing_emitted_count(emitted),
 .out_valid(ov),.out_ready(ready),.out_generation(og),.out_id(oid),.out_tag(ot),.out_mu(omu),
 .out_error(error),.out_data(raw_result),.armed(armed),.window_ready(wr),.active_context(active_ctx),
 .bound_generation(bound),.history_generation(hgen),.latest_tag(latest),.history_count(hcount),
 .nco_fences(nf),.rrc_fences(rf),.history_fences(hf));
 reg [127:0] adc[0:@LAST@];reg [144:0] oracle[0:@LAST@];
 integer offsets[0:@SEG_LAST@],lengths[0:@SEG_LAST@];reg [63:0] bases[0:@SEG_LAST@];
 reg [31:0] phases[0:@SEG_LAST@],steps[0:@SEG_LAST@];
 integer src=0,ordinal=0,cycle=0,last_adc=-1,cadence=0,old_cadence=0,adc_group=0;
 integer accepted=0,filtered=0,requests=0,responses=0,good=0,e1=0,e2=0,e3=0;
 integer adc8=0,adc8min=999,adc8max=0,adc1213=0,req_intervals=0,req_min=999,req_max=0;
 integer last_service=-1,service_group=0;reg service=0;
 integer response_intervals=0,response_min=999,response_max=0,last_response=-1,response_group=0;
 integer timeout_latency=0,reset_with_held=0;
 integer cfg_tail=0,old_context_outputs=0,old_start_ignored=0,contiguous_starts=0,overlap_starts=0;
 integer windows=0,history_wraps=0,held_out_cycles=0,held_req_cycles=0,held_window_changes=0;
 integer cancelled=0,cancelled_pending=0,stale_cfg=0,invalid_cfg=0,configs=0;
 integer fp_head=0,fp_tail=0;
 reg [144:0] filter_queue[0:32767];reg [31:0] filter_context[0:32767];
 reg [31:0] mcontext=0,mg=0,mbound=0;reg established=0;
 reg [63:0] htag[0:511],mlatest=0;reg [79:0] hdata[0:511];integer head=0,count=0;
 reg pending=0,seen=0,held=0,reqheld=0,known=0;
 reg [31:0] eg=0,eid=0;reg [63:0] et=0;reg [15:0] emu=0;reg [83:0] expected=0;
 integer eerror=0,override_error=-1,accepted_at=0;
 reg [31:0] heldg,heldid,rhg,rhi;reg [63:0] heldt,rht;
 reg [15:0] heldmu,rhm;reg [2:0] helderr;reg [83:0] helddata;
 reg [79:0] desc_first=0;reg [34:0] desc_step=0;reg [15:0] desc_epoch=0;
 reg [31:0] desc_gen=0,desc_id=0;integer desc_count=0,desc_sent=0;
 reg desc_active=0;reg [80:0] rounded;reg [79:0] position;
 reg [63:0] expected_req_tag,ftag,nexttag;reg [144:0] fexpected;
 reg [47:0] age;reg newwindow;integer j,k,ix,found0,found1,interval,expected_cfg_error;
 function automatic [63:0] plus_tag(input [63:0] t,input integer amount);
  plus_tag={t[63:48],t[47:0]+48'(amount)};
 endfunction
 function automatic [83:0] mix(input [79:0] a,b,input [15:0] u);
  longint signed x,y,numerator,value;integer lane;begin
   for(lane=0;lane<4;lane=lane+1) begin
    x=$signed(a[lane*20+:20]);y=$signed(b[lane*20+:20]);
    // Direct weighted sum differs from the RTL difference-product schedule.
    numerator=2*(x*(65536-longint'(u))+y*longint'(u));
    value=(numerator+32768)>>>16;mix[lane*21+:21]=value[20:0];
   end
  end
 endfunction
 task lookup;
  begin
   found0=-1;found1=-1;
   for(k=0;k<count;k=k+1) begin
    ix=(head-k-1+512)%512;
    if(htag[ix]==et) found0=ix;
    if(htag[ix]==plus_tag(et,1)) found1=ix;
   end
   if(found0>=0&&found1>=0) begin
    expected=mix(hdata[found0],hdata[found1],emu);known=1;
   end
  end
 endtask
 always @(posedge clk) begin
  cycle=cycle+1;expected_cfg_error=0;
  if(rst) begin
   if(ov) reset_with_held=reset_with_held+1;
   mcontext=0;mg=0;mbound=0;established=0;head=0;count=0;mlatest=0;
   fp_head=0;fp_tail=0;pending=0;seen=0;held=0;reqheld=0;desc_active=0;
   adc_group=0;last_adc=-1;service_group=0;
  end else if(cancel) begin
   if(pending||seen) cancelled_pending=cancelled_pending+1;
   mcontext=mcontext+1;mg=mg+1;mbound=0;established=0;head=0;count=0;
   fp_head=fp_tail;pending=0;seen=0;held=0;reqheld=0;desc_active=0;
   cancelled=cancelled+1;adc_group=0;last_adc=-1;service_group=0;
  end else begin
   if(held&&(!ov||{og,oid,ot,omu,error,result}!=={heldg,heldid,heldt,heldmu,helderr,helddata}))
    $fatal(1,"held chain response mutated");
   if(reqheld&&!(cv&&cr)&&(!dut.req_valid||{dut.req_generation,dut.req_id,dut.req_tag,dut.req_mu}!=={rhg,rhi,rht,rhm}))
    $fatal(1,"held configured request mutated");
   if(ov&&!seen) begin
    if(!pending||{og,oid,ot,omu}!=={eg,eid,et,emu}||error!==3'(eerror)||
       result!==(eerror==0?expected:84'd0)||(eerror==0&&!known))
     $fatal(1,"chain response mismatch id%0d error%0d expected%0d data%h expected%h known%0d",oid,error,eerror,result,expected,known);
    responses=responses+1;pending=0;seen=1;
    if(error==3) begin
     timeout_latency=cycle-accepted_at;
     if(timeout_latency!=4097) $fatal(1,"joint future timeout latency%0d expected4097",timeout_latency);
    end
    if(service) begin
     if(response_group>2) begin
      interval=cycle-last_response;response_intervals=response_intervals+1;
      if(interval<response_min) response_min=interval;if(interval>response_max) response_max=interval;
      if(interval>38||cycle-last_adc>9) $fatal(1,"joint response service lacks1.3x during ADC traffic");
     end
     response_group=response_group+1;last_response=cycle;
    end
    case(error) 0:good=good+1;1:e1=e1+1;2:e2=e2+1;3:e3=e3+1;endcase
   end
   held=ov&&!ready;
   if(held) begin
    heldg=og;heldid=oid;heldt=ot;heldmu=omu;helderr=error;helddata=result;held_out_cycles=held_out_cycles+1;
   end
   if(ov&&ready) seen=0;
   reqheld=dut.req_valid&&!dut.req_ready;
   if(reqheld) begin rhg=dut.req_generation;rhi=dut.req_id;rht=dut.req_tag;rhm=dut.req_mu;held_req_cycles=held_req_cycles+1;end
   if(dut.req_valid&&dut.req_ready) begin
    if(!desc_active||pending) $fatal(1,"unsolicited or concurrent configured request");
    position=desc_first+80'(desc_sent)*80'(desc_step);
    rounded={1'b0,position}+81'd32768;
    expected_req_tag={desc_epoch,rounded[79:32]};
    if({dut.req_generation,dut.req_id,dut.req_tag,dut.req_mu}!==
       {desc_gen,(desc_id+32'(desc_sent)),expected_req_tag,rounded[31:16]})
     $fatal(1,"configured position/active_ctx mismatch");
    desc_sent=desc_sent+1;if(desc_sent==desc_count) desc_active=0;
    pending=1;known=0;eg=dut.req_generation;eid=dut.req_id;et=dut.req_tag;emu=dut.req_mu;
    expected=0;eerror=override_error<0?0:override_error;accepted_at=cycle;
    age=mlatest[47:0]-et[47:0];
    if(eerror==0) begin
     if(eg!=mg||(count!=0&&et[63:48]!=mlatest[63:48])) eerror=1;
     else if(count!=0&&!age[47]&&age>=count) eerror=2;
     else lookup();
    end
    requests=requests+1;
    if(service) begin
     if(service_group>2) begin
      interval=cycle-last_service;req_intervals=req_intervals+1;
      if(interval<req_min) req_min=interval;if(interval>req_max) req_max=interval;
      if(interval>38) $fatal(1,"joint symbol service exceeds1.3x %0d",interval);
      if(cycle-last_adc>9) $fatal(1,"service measured outside active ADC traffic");
     end
     service_group=service_group+1;last_service=cycle;
    end
   end
   if(iv&&ir) begin
    if(ordinal>=40) begin filter_queue[fp_tail]=oracle[src];filter_context[fp_tail]=mcontext;fp_tail=fp_tail+1;end
    accepted=accepted+1;interval=cycle-last_adc;
    if(adc_group>3&&cadence==8&&old_cadence==8) begin
     if(interval!=8) $fatal(1,"joint ADC8 service missed %0d",interval);
     adc8=adc8+1;if(interval<adc8min) adc8min=interval;if(interval>adc8max) adc8max=interval;
    end
    if(adc_group>3&&cadence==12&&old_cadence==12) begin
     if(interval!=12&&interval!=13) $fatal(1,"actual ADC12/13 cadence missed %0d",interval);
     adc1213=adc1213+1;
    end
    adc_group=adc_group+1;last_adc=cycle;old_cadence=cadence;
   end
   if(dut.filtered_valid&&dut.filtered_ready) begin
    if(fp_head==fp_tail) $fatal(1,"unsolicited filtered output");
    fexpected=filter_queue[fp_head];
    if({dut.filtered_start,dut.filtered_tag,dut.filtered_data}!==fexpected||dut.filtered_context!==filter_context[fp_head])
     $fatal(1,"independent ADC convolution mismatch ordinal%0d tag%h expected%h active_ctx%0d/%0d",filtered,dut.filtered_tag,fexpected[143:80],dut.filtered_context,filter_context[fp_head]);
    fp_head=fp_head+1;filtered=filtered+1;ftag=fexpected[143:80];
    if(dut.filtered_context!=mcontext) begin
     old_context_outputs=old_context_outputs+1;
     if(dut.filtered_start) old_start_ignored=old_start_ignored+1;
    end
    nexttag=plus_tag(mlatest,1);newwindow=count==0||fexpected[144]||ftag!=nexttag;
    if(newwindow) begin
     if(count!=0&&ftag==nexttag) contiguous_starts=contiguous_starts+1;
     if(count!=0&&ftag[63:48]==mlatest[63:48]&&ftag[47:0]<=mlatest[47:0]) overlap_starts=overlap_starts+1;
     if(held) held_window_changes=held_window_changes+1;
     head=0;count=0;mg=mg+1;windows=windows+1;
    end else if(head==0&&count==512) history_wraps=history_wraps+1;
    htag[head]=ftag;hdata[head]=fexpected[79:0];head=(head+1)%512;if(count<512) count=count+1;
    mlatest=ftag;
    if(pending&&eg!=mg) eerror=1;
    if(fexpected[144]&&dut.filtered_context==mcontext&&!(cv&&cr)) begin mbound=mg;established=1;end
   end
   if(pending&&eerror==0&&!known) lookup();
   if(cv&&cr) begin
    if(dut.rrc.phase!=0||dut.filtered_valid) cfg_tail=cfg_tail+1;
    mcontext=mcontext+1;established=0;desc_active=0;reqheld=0;configs=configs+1;adc_group=0;
   end
   if(tv&&tr) begin
    if(tc!=mcontext) begin expected_cfg_error=3;stale_cfg=stale_cfg+1;end
    else if(tn==0||tn>20000) begin expected_cfg_error=1;invalid_cfg=invalid_cfg+1;end
    else if(ts<35'd17176433210||ts>35'd17183305158) begin expected_cfg_error=2;invalid_cfg=invalid_cfg+1;end
    else begin
     desc_active=1;desc_first=tp;desc_step=ts;desc_epoch=te;desc_count=tn;
     desc_id=tid;desc_gen=mbound;desc_sent=0;
    end
   end
  end
  #1;
  if(active_ctx!==mcontext||hgen!==mg||hcount!==10'(count)||bound!==mbound)
   $fatal(1,"chain active_ctx/window metadata mismatch active_ctx%0d/%0d hgen%0d/%0d count%0d/%0d bound%0d/%0d",active_ctx,mcontext,hgen,mg,hcount,count,bound,mbound);
  if(wr!==(!rst&&!cancel&&!cv&&armed&&established)) $fatal(1,"window opened before its own RRCstart");
  if(terr!==2'(expected_cfg_error)) $fatal(1,"configuration error mismatch %0d/%0d",terr,expected_cfg_error);
 end
 task configure(input integer segment);
  begin
   ct=bases[segment];cp=phases[segment];cs=steps[segment];cv=1;
   @(posedge clk);while(!cr) @(posedge clk);
   @(negedge clk);cv=0;
  end
 endtask
 task feed(input integer segment,first,last,spacing);
  integer i,period;begin
   cadence=spacing;
   for(i=first;i<last;i=i+1) begin
    period=spacing==12?12+(i%2):spacing;
    if(period>0&&adc_group>0) while(cycle-last_adc<period-1) @(negedge clk);
    src=offsets[segment]+i;ordinal=i;it=adc[src][127:64];idata=adc[src][63:0];iv=1;
    @(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;
   end
   cadence=0;
  end
 endtask
 task descriptor(input [63:0] tag,input [31:0] fraction,input [34:0] step,
                 input [15:0] number,input [31:0] identifier,expected_context);
  begin
   te=tag[63:48];tp={tag[47:0],fraction};ts=step;tn=number;tid=identifier;tc=expected_context;tv=1;
   @(posedge clk);while(!tr) @(posedge clk);
   @(negedge clk);tv=0;
  end
 endtask
 task drain;
  begin while(desc_active||pending||ov||dut.req_valid) @(negedge clk);end
 endtask
 task settled;
  begin while(fp_head!=fp_tail||dut.nco.state!=0||dut.mixed_valid||dut.rrc.phase!=0||dut.filtered_valid) @(negedge clk);end
 endtask
 task clear;
  begin
   iv=0;cv=0;tv=0;ready=1;cancel=1;repeat(2) @(negedge clk);cancel=0;
   repeat(25) @(negedge clk);
   if(ov||wr||hcount||armed||fp_head!=fp_tail) $fatal(1,"cancel allowed old tail to rebuild history");
  end
 endtask
 task abort_interp(input integer state_number);
  begin
   configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,100,8);settled();
   descriptor(plus_tag(latest,-20),32'h81230000,35'h400000000,1,32'h5000+state_number,active_ctx);
   while(dut.interp.state!=state_number) @(negedge clk);
   clear();
  end
 endtask
 integer i,saved_context;reg [63:0] saved_latest;
 initial begin
  $readmemh("adc.hex",adc);$readmemh("oracle.hex",oracle);
  @SEG_INIT@
  repeat(3) @(negedge clk);rst=0;
  // Old active_ctx's FIRST valid RRC output drains after a new NCO config.
  configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,41,8);configure(0);
  service=1;service_group=0;
  fork
   feed(0,0,4096,8);
   begin while(!wr) @(negedge clk);descriptor(plus_tag(bases[0],100),32'hffff8000,35'h400000000,900,32'hfffffff0,active_ctx);drain();end
  join
  service=0;settled();drain();
  // A held old response remains immutable while a contiguous-center restart writes.
  ready=0;descriptor(plus_tag(latest,-20),32'h81230000,35'h400000000,1,32'h1000,active_ctx);
  while(!ov) @(negedge clk);
  configure(1);feed(1,0,768,8);settled();repeat(13) @(negedge clk);ready=1;drain();
  descriptor(plus_tag(latest,-100),32'hffff8000,35'd17183305158,10,32'h2000,active_ctx);drain();
  // An accepted future request survives config until the new history fence.
  descriptor(plus_tag(latest,10000),0,35'h400000000,100,32'h3000,active_ctx);
  while(!pending||!dut.req_valid||dut.req_ready) @(negedge clk);
  saved_context=active_ctx;
  fork
   descriptor(plus_tag(latest,-30),0,35'h400000000,1,32'h3010,saved_context);
   begin repeat(5) @(negedge clk);configure(2);feed(2,0,768,8);settled();end
  join
  drain();
  descriptor(plus_tag(latest,-100),32'h8000,35'd17176433210,10,32'h3100,active_ctx);drain();
  configure(3);
  fork
   feed(3,0,1024,8);
   begin while(!wr) @(negedge clk);descriptor(plus_tag(bases[3],100),32'hffff8000,35'h400000000,200,32'hfffffffe,active_ctx);drain();end
  join
  settled();drain();
  // mu0 requires the neighbor, which arrives after the request was accepted.
  saved_latest=latest;
  fork
   begin descriptor(saved_latest,0,35'h400000000,1,32'h4000,active_ctx);drain();end
   begin repeat(20) @(negedge clk);feed(3,1024,1025,0);end
  join
  settled();
  descriptor(plus_tag(bases[3],20),0,35'h400000000,1,32'h4100,active_ctx);drain();
  descriptor(latest^(64'h1<<48),0,35'h400000000,1,32'h4200,active_ctx);drain();
  override_error=3;descriptor(latest,0,35'h400000000,1,32'h4300,active_ctx);drain();override_error=-1;
  descriptor(latest,0,0,1,32'h4400,active_ctx);
  descriptor(latest,0,35'h400000000,0,32'h4401,active_ctx);
  for(i=4;i<@ABORT_SEG@;i=i+1) begin
   configure(i);
   fork
    feed(i,0,2048,12);
    begin while(!wr) @(negedge clk);descriptor(plus_tag(bases[i],60),32'h81230000,
        i%2?35'd17176433210:35'd17183305158,450,32'h6000+i*1000,active_ctx);drain();end
   join
   settled();drain();
  end
  // Every active arithmetic phase, plus NCO/RRC work and a held response.
  for(i=1;i<=7;i=i+1) abort_interp(i);
  configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,1,0);clear();
  configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,1,0);
  while(dut.rrc.phase!=4) @(negedge clk);clear();
  configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,100,8);settled();ready=0;
  descriptor(plus_tag(latest,-30),0,35'h400000000,1,32'h7000,active_ctx);
  while(!ov) @(negedge clk);saved_context=active_ctx;clear();
  configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,120,8);settled();
  descriptor(plus_tag(latest,-20),0,35'h400000000,1,32'h7100,saved_context);
  descriptor(plus_tag(latest,-20),0,35'h400000000,1,32'h7101,active_ctx);drain();
  // Global reset is a separate all-owner boundary, including a held response.
  ready=0;descriptor(plus_tag(latest,-20),0,35'h400000000,1,32'h7200,active_ctx);
  while(!ov) @(negedge clk);rst=1;repeat(2) @(negedge clk);rst=0;ready=1;
  repeat(25) @(negedge clk);
  if(active_ctx||hgen||wr||ov||hcount) $fatal(1,"global reset retained prior ownership");
  configure(@ABORT_SEG@);feed(@ABORT_SEG@,0,120,8);settled();
  descriptor(plus_tag(latest,-20),0,35'h400000000,1,32'h7201,active_ctx);drain();
  repeat(20) @(negedge clk);
  if(fp_head!=fp_tail||adc8<4000||adc8max>9||adc1213<@ACTUAL_INTERVALS@||req_intervals<850||req_max>38||
     response_intervals<850||response_max>38||timeout_latency!=4097||reset_with_held<1||
     cfg_tail<1||old_context_outputs<1||old_start_ignored<1||contiguous_starts<1||overlap_starts<1||
     history_wraps<8||held_out_cycles<100||held_req_cycles<100||held_window_changes<1||cancelled<10||
     cancelled_pending<7||stale_cfg<2||invalid_cfg<2||e1<2||e2<1||e3<1)
    $fatal(1,"coverage failure inputs%0d filtered%0d good%0d errors%0d/%0d/%0d ctxold%0d oldstart%0d starts%0d/%0d heldwindow%0d cancel%0d pending%0d stalecfg%0d reqii%0d/%0d",accepted,filtered,good,e1,e2,e3,old_context_outputs,old_start_ignored,contiguous_starts,overlap_starts,held_window_changes,cancelled,cancelled_pending,stale_cfg,req_min,req_max);
  $display("G0_FRONTEND_CHAIN_PASS inputs=%0d filtered=%0d requests=%0d responses=%0d good=%0d err1=%0d err2=%0d err3=%0d adc8_intervals=%0d adc8_min=%0d adc8_max=%0d actual_cadence_intervals=%0d joint_request_intervals=%0d request_min=%0d request_max=%0d cfg_with_RRC_tail=%0d old_context_outputs=%0d old_start_ignored=%0d contiguous_starts=%0d overlap_starts=%0d history_wraps=%0d held_output_cycles=%0d held_request_cycles=%0d held_window_changes=%0d cancel_cycles=%0d cancelled_pending=%0d stale_cfg=%0d invalid_cfg=%0d",accepted,filtered,requests,responses,good,e1,e2,e3,adc8,adc8min,adc8max,adc1213,req_intervals,req_min,req_max,cfg_tail,old_context_outputs,old_start_ignored,contiguous_starts,overlap_starts,history_wraps,held_out_cycles,held_req_cycles,held_window_changes,cancelled,cancelled_pending,stale_cfg,invalid_cfg);
  $display("joint_response_intervals=%0d response_min=%0d response_max=%0d timeout_latency=%0d reset_with_held=%0d",response_intervals,response_min,response_max,timeout_latency,reset_with_held);
  $finish;
 end
 initial begin #100000000;$fatal(1,"chain timeout");end
endmodule
'''


def main(a):
    project = Path(__file__).resolve().parents[1]
    out = a.output.resolve();out.mkdir(parents=True, exist_ok=False)
    receipt = {'scope': 'configured ADC/NCO/RRC/Q32/history/interpolation joint probe only',
               'status': 'started', 'g0_passed': False, 'new_RF_transmissions': False}
    def save():
        (out/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    save()
    try:
        count, metadata, initial = make_vectors(out, a.capture)
        source_names = ['nco_cost_probe.sv', 'rrc_cost_probe.sv', 'timing_request_cost_probe.sv',
                        'interpolation_cost_probe.sv', 'frontend_chain_cost_probe.sv']
        sources = []
        for name in source_names:
            dest = out/name;shutil.copyfile(project/'sim/g0'/name, dest);sources.append(str(dest))
        tb = TB.replace('@LAST@', str(count-1)).replace('@SEG_LAST@', str(len(metadata)-1))
        tb = tb.replace('@ABORT_SEG@', str(len(metadata)-1)).replace('@SEG_INIT@', initial)
        tb = tb.replace('@ACTUAL_INTERVALS@', str(len(a.capture)*2040)).replace('@FAULT@', "84'd0")
        (out/'tb.sv').write_text(tb)
        bp = a.vivado_bin.resolve();commands = []
        def run(name, args):
            commands.append(args);(out/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')
            with (out/(name+'.log')).open('w') as log:
                subprocess.run(args, cwd=out, stdout=log, stderr=subprocess.STDOUT, check=True)
        run('compile', [str(bp/'xvlog'), '--sv', *sources, str(out/'tb.sv')])
        run('elaborate', [str(bp/'xelab'), 'tb', '-s', 'frontend_chain_tb', '-mt', 'off'])
        run('simulate', [str(bp/'xsim'), 'frontend_chain_tb', '-runall'])
        log = (out/'simulate.log').read_text()
        if 'G0_FRONTEND_CHAIN_PASS' not in log or 'Fatal:' in log:
            raise ValueError('joint data/service/control simulation failed; see simulate.log')
        measured = {k:int(v) for k,v in re.findall(r'(\w+)=(\d+)', log.split('G0_FRONTEND_CHAIN_PASS')[-1])}
        negative = tb.replace('module tb;', 'module fault_tb;').replace("raw_result ^ 84'd0", "raw_result ^ 84'd1")
        (out/'fault_tb.sv').write_text(negative)
        run('fault-compile', [str(bp/'xvlog'), '--sv', str(out/'fault_tb.sv')])
        run('fault-elaborate', [str(bp/'xelab'), 'fault_tb', '-s', 'frontend_chain_fault_tb', '-mt', 'off'])
        run('fault-simulate', [str(bp/'xsim'), 'frontend_chain_fault_tb', '-runall'])
        log = (out/'fault-simulate.log').read_text()
        if 'Fatal: chain response mismatch' not in log or 'G0_FRONTEND_CHAIN_PASS' in log:
            raise ValueError('single-bit chain output fault was not rejected')
        receipt.update({'status': 'simulation_passed', 'measured': measured,
            'oracle': 'raw signed16 ADC -> full-circle NCO -> direct41tap RRC -> insertion-ordered512 history -> direct weighted F1 interpolation',
            'ADC_segments': metadata, 'output_bits': 21, 'output_scale': 4,
            'single_bit_output_fault_rejected': True,
            'clock_hz': 50000000, 'joint_ADC_service_MSPS': 50/measured['adc8_max'],
            'joint_symbol_service_Msymbols_per_s': 50/measured['response_max'],
            'timing_CFO_estimator_service_or_complete_PHY_verified': False,
            'remaining_limitations': ['no LVDS/CDC/whole-board acceptance', 'configured CFO/timing only',
                                     'no G0/video/feedback acceptance', 'only output-bit mutation negative control in this joint probe']})
        save()
        if not a.sim_only:
            tcl = 'set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{p}}}\n' for p in sources)+'''
synth_design -top frontend_chain_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force frontend_chain_cost_probe.dcp
'''
            (out/'ooc.tcl').write_text(tcl)
            run('ooc', [str(bp/'vivado'), '-mode', 'batch', '-source', str(out/'ooc.tcl'), '-nolog', '-nojournal'])
            receipt['routed_OOC'] = routed_measurements(out)
        else:
            receipt['routed_OOC'] = None
        receipt['status'] = 'passed';save();print(json.dumps(receipt), flush=True)
    except BaseException as error:
        receipt.update({'status': 'failed', 'error': repr(error)});save();raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vivado-bin', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--capture', type=Path, action='append', default=[])
    parser.add_argument('--sim-only', action='store_true')
    main(parser.parse_args())
