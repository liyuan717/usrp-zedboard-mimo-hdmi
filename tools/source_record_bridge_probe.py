#!/usr/bin/env python3
"""Measure candidate bounded SOURCE ordering/atomic AXIS records, not full DMA/G0."""
import argparse,hashlib,json,re,shutil,struct,subprocess
from pathlib import Path
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB

SOURCE='source_record_bridge_cost_probe.sv'
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,fence=0,sv=0,sl=0,srec=0,sa=0,ev=0;
reg [31:0] epoch=1,ss=32'h12345678,sb=0,es=32'h12345678,eb=4;
reg [4:0] si=0;reg [8:0] so=0;reg [7:0] sd=0;reg [2:0] ec=5;
reg [19:0] em=20'hffffc,ed=3;
wire sr,er,tv,tl;wire [63:0] td;wire [7:0] tk;
reg tr=0;integer ready_mode=1;
wire [31:0] committed,dropped,bad,aborted,late,edrop,completed;
source_record_bridge_cost_probe dut(
 .clk(clk),.rst(rst),.fence(fence),.epoch(epoch),.source_valid(sv),.source_ready(sr),.source_data(sd),
 .source_session(ss),.source_block(sb),.source_index(si),.source_offset(so),.source_last(sl),.source_recovered(srec),.source_abort(sa),
 .event_valid(ev),.event_ready(er),.event_session(es),.event_block(eb),.event_code(ec),.event_missing(em),.event_delivered(ed),
 .m_axis_tdata(td),.m_axis_tkeep(tk),.m_axis_tvalid(tv),.m_axis_tready(tr),.m_axis_tlast(tl),
 .sources_committed(committed),.sources_dropped(dropped),.sources_bad(bad),.sources_aborted(aborted),
 .sources_late_or_duplicate(late),.events_dropped(edrop),.records_completed(completed),.control_valid(1'b0));
integer cycle=0,checked=0,held=0,wait_max=0,records=0,fd,start;
reg hold_valid=0;reg [72:0] hold_word;
always @(negedge clk)tr=ready_mode==2||(ready_mode==1&&cycle%17>3);
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(hold_valid&&(!tv||{td,tk,tl}!==hold_word))$fatal(1,"atomic AXIS held beat changed");
  if(tv&&tr)begin
   if(tk!==8'hff)$fatal(1,"bridge unexpected byte keep");
   $fwrite(fd,"%016h %0d\n",td,tl);checked=checked+8;if(tl)records=records+1;
  end
  hold_valid=tv&&!tr;if(hold_valid)begin hold_word={td,tk,tl};held=held+1;end
 end else hold_valid=0;
end
function automatic [7:0] pattern(input [31:0] s,b,input integer i,j);
 begin pattern=(s>>((j%4)*8))^(b+j*17)^(i*29)^j;end
endfunction
task send(input [31:0] session,b,input integer idx,bytes,mode);
 integer j,w;begin
  ss=session;sb=b;si=idx;srec=idx[0];
  for(j=0;j<bytes;j=j+1)begin
   so=(mode==1&&j==17)?18:j;sl=(j==bytes-1)&&(mode!=2);sd=pattern(session,b,idx,j);sv=1;
   @(posedge clk);w=0;while(!sr)begin w=w+1;if(w>=32768)$fatal(1,"unbounded source admission");@(posedge clk);end
   if(w>wait_max)wait_max=w;
   @(negedge clk);sv=0;
  end
 end
endtask
task drain;
 begin start=cycle;
  while(dut.state!=0||dut.slot_state[0]!=0||dut.slot_state[1]!=0||dut.slot_state[2]!=0||dut.event_pending||dut.loss_generation!=dut.reported_generation)begin
   @(negedge clk);if(cycle-start>40000)$fatal(1,"bridge bounded drain failed");
  end
  repeat(3)@(negedge clk);
 end
endtask
task pulse_event;
 begin ev=1;@(negedge clk);ev=0;end
endtask
initial begin
 fd=$fopen("axis.txt","w");repeat(5)@(negedge clk);rst=0;
 send(32'h12345678,32'hfffffffe,3,512,0);
 send(32'h12345678,32'hfffffffe,1,512,0);drain();
 send(32'h12345678,32'hfffffffe,3,512,0);drain();
 if(late!=1)$fatal(1,"duplicate/late source not ignored");
 send(32'h12345678,32'hfffffffe,2,512,0);drain();
 send(32'h12345678,32'hffffffff,0,512,0);send(32'h12345678,0,0,512,0);drain();
 send(32'h12345678,1,1,512,1);send(32'h12345678,1,2,512,0);drain();
 send(32'h12345678,2,0,64,2);sa=1;@(negedge clk);sa=0;
 send(32'h12345678,2,1,512,0);drain();
 send(32'h12345678,3,0,32,2);repeat(50010)@(negedge clk);
 send(32'h12345678,3,1,512,0);drain();
 ready_mode=0;@(negedge clk);
 send(32'h12345678,4,0,512,0);send(32'h12345678,4,2,512,0);
 while(!tv)@(negedge clk);send(32'h12345678,4,1,512,0);
 if(dropped!=0||dut.slot_state[2]==0)$fatal(1,"third SOURCE did not fit in existing BRAM");
 send(32'h12345678,4,1,512,0);
 if(dropped!=0||late!=3)$fatal(1,"duplicate of third SOURCE was not ignored");
 send(32'h12345678,4,3,512,0);
 pulse_event();eb=5;pulse_event();eb=6;pulse_event();
 fence=1;@(negedge clk);fence=0;sa=1;@(negedge clk);sa=0;
 repeat(1000)@(negedge clk);
 if(dropped!=1||edrop!=2||dut.rx_busy)$fatal(1,"blocked DMA stopped bounded input drain");
 ready_mode=1;drain();
 epoch=2;send(32'h89abcdef,0,0,512,0);drain();
 ready_mode=2;
 for(integer i=0;i<20;i=i+1)send(32'h89abcdef,1,i,512,0);
 drain();send(32'h89abcdef,2,0,256,0);drain();
 if(committed!=32||dropped!=1||bad!=2||aborted!=2||late!=3||edrop!=2||completed!=records||held==0||wait_max==0)$fatal(1,"bridge final census c%0d d%0d b%0d a%0d l%0d e%0d",committed,dropped,bad,aborted,late,edrop);
 $fclose(fd);
 $display("G0_SOURCE_RECORD_BRIDGE_PASS source_records=%0d payload_bytes=%0d dropped_sources=%0d malformed=%0d aborted=%0d late_retained_or_duplicate_ignored=%0d dropped_events=%0d AXIS_records=%0d held_checks=%0d max_admission_wait=%0d",committed,committed*512,dropped,bad,aborted,late,edrop,records,held,wait_max);$finish;
end
initial begin #10000000;$fatal(1,"bridge global timeout");end
endmodule
'''

def inspect_axis(path):
    records=[];current=bytearray()
    for row in path.read_text().splitlines():
        word,last=row.split();current.extend(int(word,16).to_bytes(8,'little'))
        if int(last):records.append(bytes(current));current.clear()
    assert not current,'partial AXIS record'
    source=[];gaps=[];events=[]
    for seq,data in enumerate(records):
        assert data[:4]==b'V3RX' and struct.unpack_from('<HH',data,4)==(1,64)
        size,kind,flags,session,epoch=struct.unpack_from('<IHHII',data,8)
        sequence,sample,capture=struct.unpack_from('<QQQ',data,24)
        block,index,payload,status,reserved=struct.unpack_from('<IHHII',data,48)
        assert size==len(data)==64+payload and sequence==seq and sample==capture==reserved==0
        assert flags&6==6
        if kind==1:
            assert payload==512 and status==0 and flags&7==6|(index&1) and flags&~15==0
            assert bool(flags&8)==(epoch==1 and block==0xfffffffe and index==2)
            expected=bytes(((session>>((j%4)*8))^(block+j*17)^(index*29)^j)&255 for j in range(512))
            assert data[64:]==expected,(epoch,session,block,index)
            source.append((epoch,session,block,index))
        elif kind==2:
            assert payload==0 and index==65535
            events.append((session,epoch,block,status>>20,status&0xfffff))
        else:
            assert kind==4 and payload==16 and index==65535 and session==block==0
            gaps.append(struct.unpack_from('<IIII',data,64))
    s=0x12345678;n=0x89abcdef
    expected=[(1,s,0xfffffffe,1),(1,s,0xfffffffe,3),(1,s,0xfffffffe,2),(1,s,0xffffffff,0),(1,s,0,0),
              (1,s,1,2),(1,s,2,1),(1,s,3,1),(1,s,4,0),(1,s,4,1),(1,s,4,2),(2,n,0,0)]+[(2,n,1,i) for i in range(20)]
    assert source==expected,(source,expected)
    assert events==[(s,1,4,5,0xffffc)],events
    assert gaps and gaps[-1]==(1,2,2,2),gaps
    return {'SOURCE_records':len(source),'independent_exact_payload_bytes':len(source)*512,
            'ERASURE_records':len(events),'DISCONTINUITY_records':len(gaps),'AXIS_records':len(records),
            'final_gap_counters':dict(zip(['dropped_sources','malformed_sources','aborted_sources','dropped_events'],gaps[-1]))}

def main(a):
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
    before=disk_check('before-source-record-bridge');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
    try:
        shutil.copyfile(project/'sim/g0'/SOURCE,out/SOURCE);(out/'tb.sv').write_text(TB)
        def run(label,args):
            with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
        run('compile',[str(bp/'xvlog'),'--sv',SOURCE,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','bridge_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'bridge_tb','-runall'])
        log=(out/'simulate.log').read_text();assert 'G0_SOURCE_RECORD_BRIDGE_PASS' in log and 'Fatal:' not in log,log[-4000:]
        result={'scope':__doc__,'simulation':next(x for x in log.splitlines() if x.startswith('G0_SOURCE_RECORD_BRIDGE_PASS')),
                'independent_AXIS_validation':inspect_axis(out/'axis.txt'),'source_sha256':hashlib.sha256((out/SOURCE).read_bytes()).hexdigest(),
                'routed_OOC':None,'full_R09_R10_DMA_or_G0_passed':False}
        if not a.sim_only:
            before_ooc=disk_check('before-source-record-bridge-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(before_ooc,indent=2)+'\n');assert before_ooc['disk']['available_bytes']>=5*GIB
            try:
                (out/'ooc.tcl').write_text('set_param general.maxThreads 4\nread_verilog -sv {'+str(out/SOURCE)+'}\n'+'''synth_design -top source_record_bridge_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force bridge.dcp
''')
                run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
            finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-source-record-bridge-OOC'),indent=2)+'\n')
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-source-record-bridge'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--sim-only',action='store_true');main(p.parse_args())
