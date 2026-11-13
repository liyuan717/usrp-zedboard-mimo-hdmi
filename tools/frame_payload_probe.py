#!/usr/bin/env python3
"""Actual shared4096/F1 header, pilots, SG queues and full-width business ML.
Inputs are real integer RRC rows and normal-H descriptors. No hidden phase or
business-sample truth enters the DUT. Not live ADC, PL linkCRC or full G0.
"""
import argparse, hashlib, json, os, shutil, subprocess
from pathlib import Path
import numpy as np
from frame_replay_probe import SOURCES as FRAME
from payload_datapath_probe import SOURCES as PAYLOAD
from payload_reference import packet_results
from workspace_storage_guard import check as disk_check, GIB

SOURCES = FRAME + PAYLOAD + ['payload_schedule_cost_probe.sv', 'frame_payload_replay_cost_probe.sv']
TB = r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,rv=0,start=0,window_ready=1,hr=0,ready=0,sr=0,hold_data=0;
reg [31:0] ci=0,cc=0,cg=0,ctx=0;reg [15:0] ep=0,ce=0;reg [47:0] base=0;reg [63:0] tag=0;
reg [79:0] data=0,position=0;reg [34:0] step=0;reg [207:0] h=0;reg [63:0] steps=0;
wire pool_ready,restart,re,reservation,active,cr,hv,ov,sv,fabort;wire [11:0] ra;wire [79:0] word;
wire [31:0] gen,hi,hc,hg,session,sf,oi,oc,og,si,sc,sg;wire [63:0] latest;wire [12:0] count;
wire [15:0] he,l0,l1,oe,se;wire [2:0] herr,hd,serr,sd;wire [191:0] raw;wire [14:0] symbol;wire [3:0] decision;wire [61:0] metric;wire [8:0] sp;
coherent_window_cost_probe #(.EXTERNAL_RESERVATION(1)) pool(.clk(clk),.rst(rst),.cancel(1'b0),.in_valid(rv),.in_ready(pool_ready),.in_start(start),.in_tag(tag),.in_data(data),.external_read_reservation(reservation),
 .cfg_valid(1'b0),.cfg_ready(),.cfg_id(32'd0),.cfg_generation(32'd0),.cfg_first_tag(64'd0),.cfg_points(7'd1),
 .out_valid(),.out_ready(1'b1),.out_id(),.out_generation(),.out_tag(),.out_point(),.out_c(),.out_n(),.out_e(),.out_error(),.out_last(),.busy(),
 .history_generation(gen),.latest_tag(latest),.history_count(count),.history_restart(restart),.external_read_enable(re),.external_read_address(ra),.read_word(word));
frame_payload_replay_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_id(ci),.cfg_context(cc),.cfg_generation(cg),.cfg_epoch(ce),.cfg_training_position(position),.cfg_symbol_step(step),.cfg_h(h),.cfg_phase_steps(steps),
 .window_ready(window_ready),.current_context(ctx),.rrc_valid(rv),.rrc_start(start),.shared_ready(pool_ready),.rrc_tag(tag),.rrc_data(data),.history_generation(gen),.latest_tag(latest),.history_count(count),.history_restart(restart),
 .reader_active(active),.read_enable(re),.read_reservation(reservation),.read_address(ra),.read_word(word),.header_valid(hv),.header_ready(hr),.header_id(hi),.header_context(hc),.header_generation(hg),.header_epoch(he),.header_error(herr),.header_detail(hd),.header_raw(raw),.header_session(session),.header_superframe(sf),.header_stream0_bytes(l0),.header_stream1_bytes(l1),
 .out_valid(ov),.out_ready(ready),.out_id(oi),.out_context(oc),.out_generation(og),.out_epoch(oe),.out_symbol(symbol),.out_decision(decision),.out_metric(metric),
 .frame_abort(fabort),.status_valid(sv),.status_ready(sr),.status_id(si),.status_context(sc),.status_generation(sg),.status_epoch(se),.status_error(serr),.status_detail(sd),.status_point(sp));
reg [79:0] rrc[0:@RLAST@];reg [83:0] samples[0:368255];reg [449:0] gold[0:368255];reg [59:0] phase_gold[0:11507];reg [83:0] legacy_samples[0:19571];
integer off[0:20],lo[0:20],pre[0:20],last[0:20],last_data[0:20];reg [79:0] training[0:20],peak[0:20];reg [34:0] st[0:20];reg [207:0] ch[0:20];reg [63:0] ph[0:20];reg [191:0] headers[0:20];
integer j=0,model=0,mode=0,k,n,cycle=0,phase_count=0,data_count=0,consumed=0,heads=0,hhwait=0,fd;
integer frame_start_cycle=0,frame_service_cycles=0;
integer total_data=0,total_samples=0,total_legacy=0,total_phases=0,total_heads=0,total_status=0,cancelled=0,held_checks=0,write_count=0,max_read_age=0,max_response_age=0,max_nodes=0,idx,age,writes_before;
reg [31:0] owner,owner_context,owner_gen;reg [15:0] owner_epoch;reg seen=0,held=0,hseen=0,hheld=0;reg [195:0] held_view;reg [405:0] header_hold;
wire [195:0] view={oe,og,oc,oi,symbol,3'd0,decision,metric};wire [405:0] header_view={hi,hc,hg,he,herr,hd,raw,session,sf,l0,l1};
reg [79:0] coordinate;reg [80:0] rounded;reg [126:0] status_hold;
always @(negedge clk)begin
 // Producer also drives tag/valid on this edge. Observe settled combinational
 // ownership, rather than a delta-cycle restart while those fields change.
 #1;
 ready=!hold_data && cycle%19>4;
 if(!hv||rst||cancel)begin hr=0;hhwait=0;end else if(hhwait==37)hr=1;else hhwait=hhwait+1;
end
always @(posedge clk)begin
 cycle=cycle+1;if(rv&&pool_ready)write_count=write_count+1;
 if(!rst&&!cancel&&held&&!ov&&!fabort)$fatal(1,"business held result withdrawn without abort");
 if(!rst&&!cancel&&hheld&&!hv&&!fabort)$fatal(1,"business held header withdrawn without abort");
 if(rst||cancel||fabort)begin seen=0;held=0;hheld=0;end
 else begin
  if((dut.frame.timing_cfg&&!dut.frame.timing_ready)||(dut.frame.math_cfg&&!dut.frame.math_ready))$fatal(1,"business job launched into busy configuration slot");
  if(re&&!reservation || re&&pool.own_read)$fatal(1,"business pool reservation contract");
  if(dut.payload.count0>8||dut.payload.count1>8)$fatal(1,"business node queue overflow");
  if(dut.payload.count0>max_nodes)max_nodes=dut.payload.count0;if(dut.payload.count1>max_nodes)max_nodes=dut.payload.count1;
  if(re&&dut.frame.business_owner&&dut.frame.sampler.state==1)begin
   age=48'(latest[47:0]-dut.frame.sampler.work_tag[47:0]);if(age>=4096)$fatal(1,"business actual pool read exceeded retention");if(age>max_read_age)max_read_age=age;
  end
  if(dut.sample_valid&&dut.sample_ready)begin
   idx=model*17536+dut.sample_id;coordinate={base,32'd0}+peak[model]+80'(1538+66*(dut.sample_id/64)+dut.sample_id%64)*st[model];rounded={1'b0,coordinate}+81'd32768;
   if(dut.sample_error||dut.sample_data!==samples[idx]||dut.sample_tag!=={ep,rounded[79:32]}||dut.sample_mu!==rounded[31:16]||dut.sample_generation!==owner_gen)$fatal(1,"business actual shared F1 oracle frame%0d index%0d",j,dut.sample_id);
   if(dut.payload.math.phases!==gold[idx][63:0])$fatal(1,"business actual SG interpolation oracle");
   total_samples=total_samples+1;age=48'(latest[47:0]-dut.sample_tag[47:0]);if(age>max_response_age)max_response_age=age;
  end
  if(dut.frame.sample_valid&&dut.frame.sample_ready&&dut.frame.sampling&&dut.frame.sample_error==0)begin
   idx=model*932+(dut.frame.piloting?384+2*dut.frame.point+dut.frame.tx:dut.frame.received);
   if(dut.frame.sample_bad||dut.frame.sample_data!==legacy_samples[idx])$fatal(1,"business shared header/pilot F1 oracle");total_legacy=total_legacy+1;
  end
  if(dut.phase_valid&&dut.phase_ready)begin
   if(phase_count>=548||{dut.phase_tx,dut.phase_known,dut.phase_point,dut.phase_value}!==phase_gold[model*548+phase_count])$fatal(1,"business live SG node oracle frame%0d index%0d",j,phase_count);
   phase_count=phase_count+1;total_phases=total_phases+1;
  end
  if(hv&&!hseen)begin
   if({hi,hc,hg,he}!=={owner,owner_context,owner_gen,owner_epoch})$fatal(1,"business header identity");
   if(mode==1)begin if(herr!=1||{session,sf,l0,l1}!==0)$fatal(1,"business bad header metadata");end
   else if(herr||raw!==headers[model]||l0!=4384||l1!=4384)$fatal(1,"business header oracle");
   hseen=1;heads=heads+1;total_heads=total_heads+1;
  end
  if(mode==1&&(dut.req_valid||ov||dut.phase_valid))$fatal(1,"business rejected header admitted data");
  if(hheld&&(!hv||header_view!==header_hold))$fatal(1,"business held header changed");hheld=hv&&!hr;if(hheld)begin header_hold=header_view;held_checks=held_checks+1;end
  if(ov&&!seen)begin
   idx=model*17536+symbol;
   if({oi,oc,og,oe}!=={owner,owner_context,owner_gen,owner_epoch}||symbol!=data_count||{metric,decision}!==gold[idx][449:384])$fatal(1,"business actual ML oracle frame%0d symbol%0d expected%0d",j,symbol,data_count);
   $fdisplay(fd,"%0d %0d %x",j,symbol,decision);seen=1;data_count=data_count+1;total_data=total_data+1;
  end
  if(held&&(!ov||view!==held_view))$fatal(1,"business held result changed");
  if(ov&&ready)begin seen=0;consumed=consumed+1;if(consumed==17536)frame_service_cycles=cycle-frame_start_cycle;end
  held=ov&&!ready;if(held)begin held_view=view;held_checks=held_checks+1;end
 end
end
task put(input integer r);begin
 rv=1;start=r==0;tag={ep,base+48'(lo[model]+r)};data=rrc[off[model]+r];@(posedge clk);while(!pool_ready)@(posedge clk);@(negedge clk);rv=0;start=0;
end endtask
task run_case(input integer number,input integer source,input integer kind);
 integer stop_at;begin
 j=number;model=source;mode=kind;ctx=32'h80000000^number;cc=ctx;ep=16'hffff+number;base=48'hffffffff0000+48'(111*number);window_ready=1;hold_data=0;sr=0;phase_count=0;data_count=0;consumed=0;heads=0;hseen=0;seen=0;held=0;hheld=0;
 for(k=0;k<pre[model];k=k+1)put(k);
 ci=32'hfffffff0+number;cg=gen;ce=ep;position={base,32'd0}+training[model];step=st[model];h=ch[model];steps=ph[model];
 if(mode==1)for(integer v=0;v<4;v=v+1)h[v*26+:26]=-$signed(h[v*26+:26]);
 owner=ci;owner_context=cc;owner_gen=cg;owner_epoch=ce;cv=1;@(posedge clk);while(!cr)@(posedge clk);frame_start_cycle=cycle;frame_service_cycles=0;@(negedge clk);cv=0;ci=~ci;cc=~cc;cg=~cg;ce=~ce;position=~position;step=~step;h=~h;steps=~steps;
 stop_at=mode==2?last_data[model]-32:last[model];
 fork
  begin for(n=pre[model];n<=stop_at;n=n+1)begin put(n);repeat(j%2?11+n%2:7)@(negedge clk);end end
  begin
   if(mode==3)begin while(!(dut.payload.pending&&dut.frame.sampler.state==5))@(negedge clk);ctx=~ctx;end
   if(mode==4)begin while(!ov)@(negedge clk);hold_data=1;writes_before=write_count;while(write_count-writes_before<4600)@(negedge clk);hold_data=0;end
   if(mode==5)begin while(!dut.phase_valid)@(negedge clk);force dut.phase_generation=32'h80000000;while(!fabort)@(negedge clk);release dut.phase_generation;end
   if(mode==6)begin while(!dut.sample_valid)@(negedge clk);force dut.sample_id=32'h80000000;while(!fabort)@(negedge clk);release dut.sample_id;end
   if(mode==7)begin hold_data=1;while(!sv)@(negedge clk);hold_data=0;end
   if(mode==8)begin while(!(dut.frame.business_owner&&dut.frame.sampler.state==5))@(negedge clk);force start=1;@(posedge clk);while(!rv)@(posedge clk);@(negedge clk);release start;end
   if(mode>=10)begin
    case(mode)
     10:while(!(dut.frame.business_owner&&dut.frame.sampler.state==2))@(negedge clk);
     11:while(!(dut.frame.business_owner&&dut.frame.sampler.state==5))@(negedge clk);
     12:while(!(dut.payload.math.phasor.state==2&&dut.payload.math.phasor.iteration==20))@(negedge clk);
     13:while(dut.payload.math.math.rotation.state!=4)@(negedge clk);
     14:while(!(dut.payload.math.math.detector.metric_valid&&dut.payload.math.math.detector.metric_candidate==7))@(negedge clk);
     15:begin hold_data=1;while(!ov)@(negedge clk);repeat(7)@(negedge clk);end
    endcase
    cancel=1;#1;if(ov||hv||sv||re||cr)$fatal(1,"business cancel edge");repeat(2)@(negedge clk);cancel=0;hold_data=0;cancelled=cancelled+1;
   end
  end
 join
 if(mode>=10)begin repeat(64)begin @(negedge clk);if(ov||hv||sv||active||re||!cr)$fatal(1,"business cancelled work revived");end end
 else begin
  while(!sv)@(negedge clk);
  if({si,sc,sg,se}!=={owner,owner_context,owner_gen,owner_epoch})$fatal(1,"business terminal identity");
  case(mode)
   0:if(serr||sd||data_count!=17536||consumed!=17536||phase_count!=548||heads!=1||!dut.payload_done)$fatal(1,"business complete census error%0d:%0d data%0d phases%0d",serr,sd,data_count,phase_count);
   1:if(serr!=1||sd!=1||data_count||phase_count)$fatal(1,"business header rejection gate");
   2:if(serr!=7||sd!=3||data_count>=17536)$fatal(1,"business missing tail error%0d:%0d data%0d",serr,sd,data_count);
   3:if(serr!=4||sd)$fatal(1,"business context fence");
   4:if(!((serr==7||serr==2)&&sd==2))$fatal(1,"business expired history error%0d:%0d",serr,sd);
   5:if(serr!=7||sd!=5)$fatal(1,"business SG identity error%0d:%0d",serr,sd);
   6:if(serr!=7||sd!=4)$fatal(1,"business sample identity error%0d:%0d",serr,sd);
   7:if(serr!=3||sd!=3)$fatal(1,"business bounded held deadline error%0d:%0d",serr,sd);
   8:if(serr!=4||sd)$fatal(1,"business actual source restart error%0d:%0d",serr,sd);
  endcase
  if(mode==0)begin if(frame_service_cycles<=0||(!(j%2)&&frame_service_cycles>674461))$fatal(1,"business shared scheduling service below1.3Msymbol/s cycles%0d",frame_service_cycles);$display("FRAME_PAYLOAD_SERVICE case=%0d symbols=17536 cycles=%0d source_clock_period=%0d clock_Hz=50000000",j,frame_service_cycles,j%2?0:8);end
  status_hold={si,sc,sg,se,serr,sd,sp};repeat(31)begin @(negedge clk);if(!sv||{si,sc,sg,se,serr,sd,sp}!==status_hold||ov||active||re)$fatal(1,"business held terminal changed");end
  sr=1;@(negedge clk);sr=0;repeat(4)@(negedge clk);if(!cr||hv||ov||sv||active||re)$fatal(1,"business terminal not idle");total_status=total_status+1;
 end
 $display("FRAME_PAYLOAD_CASE case=%0d model=%0d mode=%0d samples=%0d phases=%0d error=%0d:%0d",j,model,mode,data_count,phase_count,serr,sd);
end endtask
initial begin
 $readmemh("rrc_rows.hex",rrc);$readmemh("samples.hex",samples);$readmemh("gold.hex",gold);$readmemh("phase_gold.hex",phase_gold);$readmemh("vectors.hex",legacy_samples);
 @INIT@
 fd=$fopen("observed.txt","w");repeat(4)@(negedge clk);rst=0;
 @CASE_RUNS@
 if(total_status!=@STATUS@||cancelled!=@CANCELS@||total_data<@COMPLETE@*17536||total_samples<@COMPLETE@*17536||total_phases<@COMPLETE@*548||max_read_age>=4096||max_read_age<512||max_nodes<5)$fatal(1,"business coverage status%0d cancel%0d data%0d samples%0d phases%0d age%0d nodes%0d",total_status,cancelled,total_data,total_samples,total_phases,max_read_age,max_nodes);
 $fclose(fd);$display("G0_FRAME_PAYLOAD_PASS complete_frames=@COMPLETE@ terminal_status=%0d cancelled=%0d exact_business_samples=%0d exact_decisions=%0d exact_header_pilot_samples=%0d exact_SG_nodes=%0d headers=%0d maximum_business_read_age=%0d maximum_cached_response_age=%0d maximum_nodes_per_TX=%0d held_checks=%0d",total_status,cancelled,total_samples,total_data,total_legacy,total_phases,total_heads,max_read_age,max_response_age,max_nodes,held_checks);$finish;
end
initial begin #2000000000;$fatal(1,"business timeout case%0d mode%0d frame_state%0d point%0d tx%0d payload_state%0d group%0d sent%0d joined%0d count%0d/%0d",j,mode,dut.frame.state,dut.frame.point,dut.frame.tx,dut.payload.state,dut.payload.group,dut.payload.sent,dut.payload.joined,dut.payload.count0,dut.payload.count1);end
endmodule
'''

def main(a):
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    before=disk_check('before-frame-payload');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
    try:
        fixture=a.fixtures.resolve();legacy=a.frame_reference.resolve();numeric=a.payload_reference.resolve();bp=a.vivado_bin.resolve()
        meta=json.loads((fixture/'fixture_metadata.json').read_text());filtered=np.memmap(fixture/'filtered.hex',dtype='S21',mode='r')
        headers=json.loads((a.header_reference.resolve()/'receipt.json').read_text())['actual_capture_headers']
        init=[];rows=[]
        for m,model in enumerate(meta['models']):
            for w in range(1,4):
                n=m*3+w-1;e=model['windows'][w]
                left=(e['peak']+1152*e['step'])//(1<<32)-512
                first=(e['peak']+1536*e['step']+32768)//(1<<32)
                tail=(e['peak']+19619*e['step']+32768)//(1<<32)
                right=tail+80;offset=len(rows)
                rows.extend(int(v,16) for v in filtered[model['offset']+left+20:model['offset']+right+21])
                hdr=next(v for v in headers if v['model']==m and v['window']==w)
                init.append(f"off[{n}]={offset};lo[{n}]={left};pre[{n}]={first+9-left};last[{n}]={right-left};last_data[{n}]={tail-left};training[{n}]=80'h{e['training']:020x};peak[{n}]=80'h{e['peak']:020x};st[{n}]=35'd{e['step']};ch[{n}]=208'h{e['H']:052x};ph[{n}]=64'h{e['phase_steps']:016x};headers[{n}]=192'h{int.from_bytes(bytes.fromhex(hdr['raw_header']),'little'):048x};")
        (out/'rrc_rows.hex').write_text(''.join(f'{v:020x}\n' for v in rows))
        for name,ref in [('samples.hex',numeric),('gold.hex',numeric),('vectors.hex',legacy),('phase_gold.hex',legacy),('sine.hex',legacy),('coherent_signs.hex',legacy)]:os.link(ref/name,out/name)
        text=TB.replace('@RLAST@',str(len(rows)-1)).replace('@INIT@','\n'.join(init))
        calls=''
        if not a.boundaries_only:calls='for(integer f=0;f<21;f=f+1)run_case(f,f,0);\n'
        calls+='for(integer f=1;f<=8;f=f+1)run_case(20+f,0,f);\nfor(integer f=10;f<=15;f=f+1)run_case(19+f,0,f);\nrun_case(35,0,0);'
        if a.service_only:calls='run_case(0,0,0);run_case(36,0,0);'
        text=text.replace('@CASE_RUNS@',calls).replace('@STATUS@','2' if a.service_only else ('9' if a.boundaries_only else '30')).replace('@COMPLETE@','2' if a.service_only else ('1' if a.boundaries_only else '22')).replace('@CANCELS@','0' if a.service_only else '6')
        (out/'tb.sv').write_text(text)
        for name in SOURCES:shutil.copyfile(project/'sim/g0'/name,out/name)
        xpm=bp.parent/'data/ip/xpm'
        def run(label,args):
            with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
        run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*[str(out/n) for n in SOURCES],str(out/'tb.sv')])
        run('elaborate',[str(bp/'xelab'),'tb','-s','frame_payload_tb','-mt','off'])
        run('simulate',[str(bp/'xsim'),'frame_payload_tb','-runall'])
        log=(out/'simulate.log').read_text();assert 'G0_FRAME_PAYLOAD_PASS' in log and 'Fatal:' not in log,log[-5000:]
        observed=[s.split() for s in (out/'observed.txt').read_text().splitlines()];packets=[]
        selected=[(0,0),(36,0)] if a.service_only else ([(35,0)] if a.boundaries_only else [(j,j) for j in range(21)])
        for j,model_index in selected:
            data=[v for v in observed if int(v[0])==j];assert len(data)==17536 and all(int(v[1])==i for i,v in enumerate(data))
            h=next(v for v in headers if v['model']==model_index//3 and v['window']==model_index%3+1);raw=bytes.fromhex(h['raw_header']);session=int.from_bytes(raw[4:8],'little');sf=int.from_bytes(raw[8:12],'little')
            result,_=packet_results(np.array([int(v[2],16) for v in data]),session,sf)
            packets.append({'model':model_index//3,'window':model_index%3+1,'accepted_packets':sum(v['accepted'] for v in result)})
        assert sum(v['accepted_packets'] for v in packets)==(30 if a.service_only else (15 if a.boundaries_only else 310))
        result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_FRAME_PAYLOAD_PASS')),
                'cases':[s for s in log.splitlines() if s.startswith('FRAME_PAYLOAD_CASE')],
                'service':[s for s in log.splitlines() if s.startswith('FRAME_PAYLOAD_SERVICE')],
                'packets':packets,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},
                'boundaries_only':a.boundaries_only,'service_only':a.service_only,'g0_passed':False,'live_ADC_integration_verified':False,'PL_link_CRC_implemented':False}
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(result['simulation'],flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-frame-payload'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('output','fixtures','frame-reference','payload-reference','header-reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
    modes=p.add_mutually_exclusive_group()
    modes.add_argument('--boundaries-only',action='store_true',help='Run the8error boundaries,6cancels and fresh recovery frame; full21fixture coverage is a separate required receipt')
    modes.add_argument('--service-only',action='store_true',help='Two exact full frames at continuous8clock RRC cadence, including header/pilot/SG/F1/ML service')
    main(p.parse_args())
