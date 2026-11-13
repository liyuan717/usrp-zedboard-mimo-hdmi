#!/usr/bin/env python3
"""Real VRL3 bytes/CRC -> in-band range descriptor -> fixed8 FEC RAM/inverse.
The protected PHY boundary/session is the only configuration input; source
truth is monitor-only. This is not the ADC PHY, R09/DMA or full G0 signoff.
"""
import argparse,hashlib,json,re,shutil,struct,subprocess,zlib
from pathlib import Path
import numpy as np
from g0_packet_fec import GENERATOR,encode
from fec_inverse_cost_probe import vandermonde_generator,routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['link_packet_cost_probe.sv','fec_inverse_cost_probe.sv','fec_memory_cost_probe.sv',
         'fec_stream_recovery_cost_probe.sv','link_fec_ingress_cost_probe.sv','crc_fec_pipeline_cost_probe.sv']
SID=0x89abcdef;NEW=0xc1234567;BASE=0xfffffffc

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,abort_packet=0,cv=0,iv=0;
reg [31:0] sid=0,sf=32'h80000001;reg [15:0] size=548;reg [7:0] data=0;
wire cr,ir,sov,slast,recovered,sabort,ev;wire [7:0] od;wire [31:0] ss,sb,es,eb;
wire [4:0] si;wire [8:0] so;wire [2:0] ec;wire [19:0] em,ed;
wire [31:0] accepted,erased,unmapped,descs,baddesc,busy,drops,expired;
reg sr=0,er=1;integer ready_mode=1,event_mode=1;
crc_fec_pipeline_cost_probe dut(clk,rst,abort_packet,cv,cr,sid,sf,size,iv,ir,data,
 sov,sr,od,ss,sb,si,so,slast,recovered,sabort,ev,er,es,eb,ec,em,ed,
 accepted,erased,unmapped,descs,baddesc,busy,drops,expired);
reg [7:0] bytes[0:@BYTES_LAST@],golden[0:12*10240-1];
integer packet_offset[0:@PACKETS_LAST@],cycle=0,checked=0,restored=0,holds=0,terminals=0,events=0,revoked=0;
integer counts[0:11],positions[0:11][0:19];reg [19:0] seen[0:11];reg ended[0:11];
integer which,j,k,n,start,first_deadline;reg [31:0] expected_original[0:11];
reg source_held=0,event_held=0;reg [87:0] source_view;reg [106:0] event_view;
wire [87:0] sv={ss,sb,si,so,slast,recovered,od};wire [106:0] eview={es,eb,ec,em,ed};
function automatic integer slot(input [31:0] s,input [31:0] b);
 reg [31:0] delta;begin delta=b-32'hfffffffc;slot=-1;
 if(s==32'h89abcdef&&delta<10)slot=integer'(delta);
 if(s==32'hc1234567&&b==4)slot=10;
 if(s==32'hc1234567&&b==5)slot=11;end
endfunction
always @(negedge clk)begin sr=ready_mode==2||(ready_mode==1&&cycle%17>3)||(ready_mode==3&&so<128);er=event_mode&&cycle%13>2;end
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst)begin source_held=0;event_held=0;end else begin
  if(source_held&&!sabort&&(!sov||sv!==source_view))$fatal(1,"ingress held source changed");
  if(event_held&&(!ev||eview!==event_view))$fatal(1,"ingress held event changed");
  if(sov&&sr)begin
   which=slot(ss,sb);
   if(which<0||ended[which]||seen[which][si]||so!=positions[which][si]||od!==golden[which*10240+si*512+so]||slast!==(so==511)||recovered!==!expected_original[which][si])$fatal(1,"ingress source oracle session%h block%h row%0d offset%0d",ss,sb,si,so);
   positions[which][si]=positions[which][si]+1;counts[which]=counts[which]+1;checked=checked+1;if(recovered)restored=restored+1;if(slast)seen[which][si]=1;
  end
  if(sabort)begin if(counts[9]!=128||seen[9])$fatal(1,"ingress session partial abort");revoked=revoked+1;ended[9]=1;end
  if(dut.ingress.core_status_valid)begin
   which=slot(dut.ingress.session_id,dut.ingress.core_status_job);
   if(which<0||ended[which]||dut.ingress.core_delivered!==seen[which]||dut.ingress.core_missing!==~seen[which]||dut.ingress.core_code!=(which>=2&&which<=7?1:0))$fatal(1,"ingress terminal oracle block%h slot%0d code%0d missing%h seen%h",dut.ingress.core_status_job,which,dut.ingress.core_code,dut.ingress.core_missing,seen[which]);
   ended[which]=1;terminals=terminals+1;
  end
  if(ev&&er)begin
   which=slot(es,eb);
   if(which<0||ec!=(which>=2&&which<=7?1:0)||em!==~seen[which]||ed!==seen[which])$fatal(1,"ingress published event oracle");events=events+1;
  end
  source_held=sov&&!sr;if(source_held)begin source_view=sv;holds=holds+1;end
  event_held=ev&&!er;if(event_held)begin event_view=eview;holds=holds+1;end
 end
end
task send(input integer packet);
 begin
  while(!cr)@(negedge clk);sid={bytes[packet_offset[packet]+11],bytes[packet_offset[packet]+10],bytes[packet_offset[packet]+9],bytes[packet_offset[packet]+8]};cv=1;@(negedge clk);cv=0;
  for(integer p=0;p<548;p=p+1)begin
   data=bytes[packet_offset[packet]+p];iv=1;@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
  end
 end
endtask
task idle_packet;
 begin while(!cr||dut.ingress.state!=0)@(negedge clk);repeat(2)@(negedge clk);end
endtask
task wait_complete(input integer s);
 begin start=cycle;while(!ended[s])begin @(negedge clk);if(cycle-start>1000000)$fatal(1,"ingress complete block service bound slot%0d",s);end
 if(counts[s]!=10240||seen[s]!=20'hfffff)$fatal(1,"ingress whole source census");end
endtask
initial begin
 $readmemh("bytes.hex",bytes);$readmemh("offsets.hex",packet_offset);$readmemh("source.hex",golden);
 for(j=0;j<12;j=j+1)begin counts[j]=0;seen[j]=0;ended[j]=0;expected_original[j]=32'h000fffff;for(k=0;k<20;k=k+1)positions[j][k]=0;end
 expected_original[1]=32'hfffff000;expected_original[2]=32'hffffe000;expected_original[10]=32'hfff000ff;
 repeat(5)@(negedge clk);rst=0;
 send(@PRE_DATA@);idle_packet();if(unmapped!=1||dut.ingress.bank_live||checked)$fatal(1,"ingress undeclared data admitted");
 send(@BAD_DESC@);idle_packet();if(erased!=1||descs||dut.ingress.bank_live)$fatal(1,"ingress CRC-bad descriptor admitted");
 send(@DESC@);idle_packet();if(dut.ingress.bank_live!=8'hff||descs!=1)$fatal(1,"ingress in-band eight block declaration");first_deadline=dut.ingress.fec.bank_deadline[4];
 send(@DESC@);idle_packet();if(dut.ingress.fec.bank_deadline[4]!==first_deadline||descs!=2)$fatal(1,"ingress repeated descriptor reset deadline");
 send(@EARLY@);while(!seen[0][7])@(negedge clk);if(dut.ingress.fec.bank_count[4]!=1)$fatal(1,"ingress before-k original missing");
 send(@DUPLICATE@);idle_packet();if(dut.ingress.fec.bank_count[4]!=1||counts[0]!=512)$fatal(1,"ingress duplicate source not ignored");
 send(@BAD_DATA@);idle_packet();if(erased!=2||dut.ingress.fec.bank_count[4]!=1||dut.ingress.fec.bank_count[7])$fatal(1,"ingress bad header assigned an erasure bank");
 send(@ABORT@);while(!(dut.packet.out_valid&&dut.packet.out_index==128))@(negedge clk);abort_packet=1;#1;
 if(dut.ingress.fec.commit_packet||dut.ingress.fec.byte_ready)$fatal(1,"ingress current abort fence");@(negedge clk);abort_packet=0;
 if(dut.ingress.fec.packet_active||dut.ingress.fec.bank_mask[4][8])$fatal(1,"ingress current packet abort retained");
 for(j=@BLOCK0_BEGIN@;j<@BLOCK0_END@;j=j+1)send(j);wait_complete(0);idle_packet();
 for(j=@BLOCK1_BEGIN@;j<@BLOCK1_END@;j=j+1)send(j);
 for(j=@BLOCK2_BEGIN@;j<@BLOCK2_END@;j=j+1)send(j);
 wait_complete(1);while(seen[2]!=20'hfe000)@(negedge clk);idle_packet();
 // Replacement admission closes old loading blocks. Hold the event consumer
 // before that change, while the FEC terminal/release path must still drain.
 event_mode=0;
 send(@NEXT_DESC@);idle_packet();if(dut.ingress.range_first!=4)$fatal(1,"ingress next range declaration");
 for(j=@BLOCK8_BEGIN@;j<@BLOCK8_END@;j=j+1)send(j);wait_complete(8);idle_packet();
 send(@NEXT_DESC@);idle_packet();if(dut.ingress.bank_live[4])$fatal(1,"ingress completed block reopened by repeat");
 send(@OLD_DESC@);idle_packet();if(dut.ingress.range_first!=4||baddesc!=1)$fatal(1,"ingress stale range replay accepted");
 send(@WRAP_DESC@);idle_packet();if(dut.ingress.range_serial!=0)$fatal(1,"ingress range serial wrap");
 while(terminals<9)@(negedge clk);repeat(256)@(negedge clk);
 if((dut.ingress.bank_live&8'hcf)!=0||terminals!=9||drops!=6||!ev)$fatal(1,"ingress terminal backpressure blocked release terminals%0d live%h drops%0d",terminals,dut.ingress.bank_live,drops);
 event_mode=1;while(ev)@(negedge clk);
 ready_mode=3;send(@PARTIAL@);while(counts[9]<128||!sov)@(negedge clk);
 send(@NEW_SESSION@);idle_packet();if(revoked!=1||dut.ingress.session_id!=32'hc1234567)$fatal(1,"ingress new session fence absent");ready_mode=1;
 send(@RETIRED_DESC@);idle_packet();if(dut.ingress.session_id!=32'hc1234567||baddesc!=2)$fatal(1,"ingress previous session replay accepted");
 for(j=@BLOCK10_BEGIN@;j<@BLOCK10_END@;j=j+1)send(j);wait_complete(10);idle_packet();
 send(@NEW_RANGE@);idle_packet();for(j=@BLOCK11_BEGIN@;j<@BLOCK11_END@;j=j+1)send(j);wait_complete(11);idle_packet();
 if(terminals!=11||restored!=12288||checked!=54912||revoked!=1||busy||expired!=0)$fatal(1,"ingress final census checked%0d restored%0d terminals%0d revoked%0d busy%0d",checked,restored,terminals,revoked,busy);
 $display("G0_CRC_FEC_PIPELINE_PASS terminal_jobs=%0d exact_bytes=%0d restored_bytes=%0d revoked_partial_bytes=128 events=%0d dropped_terminal_events=%0d held_checks=%0d CRC_erasures=%0d descriptor_rejects=%0d",terminals,checked,restored,events,drops,holds,erased,baddesc);$finish;
end
initial begin #2000000000;$fatal(1,"ingress global timeout state%0d packet%0d cycle%0d",dut.ingress.state,j,cycle);end
endmodule
'''

def make_vectors(out):
 rng=np.random.default_rng(0x7c1168);sources=[rng.integers(0,256,(20,512),dtype=np.uint8) for _ in range(12)];coded=[encode(s) for s in sources]
 packets=[];marks={};seq=0xffffffd0
 def add(name,slot=0,index=0,desc=None,session=SID,corrupt=False,mutate=False):
  nonlocal seq
  block=(BASE+slot)&0xffffffff if session==SID else 4+(slot-10)
  payload=bytes(coded[slot][index]) if desc is None else desc+bytes(512-len(desc))
  if mutate:payload=bytes(x^0x55 for x in payload)
  header=struct.pack('<4sBBHIIIIIHH',b'VRL3',1,3 if desc is not None else 1 if index<20 else 2,0,session,0x80000001,seq&0xffffffff,0xffffffff,block,index,512);seq+=7
  body=header+payload;packet=bytearray(body+struct.pack('<I',zlib.crc32(body)))
  if corrupt:packet[24]^=0x37
  marks[name]=len(packets);packets.append(packet)
 def desc(first=BASE,count=8,serial=0xfffffffe):
  return struct.pack('<4sBBHBBHHHHHI BBHI',b'VRS3',1,0xf0,0,20,32,512,1024,600,20,1,first&0xffffffff,count,0,0,serial)
 add('PRE_DATA');add('BAD_DESC',desc=desc(),corrupt=True);add('DESC',desc=desc());add('EARLY',index=7);add('DUPLICATE',index=7,mutate=True);add('BAD_DATA',index=8,corrupt=True);add('ABORT',index=8)
 for slot,indices in [(0,[i for i in range(20) if i!=7]),(1,range(12,32)),(2,range(13,32))]:
  marks[f'BLOCK{slot}_BEGIN']=len(packets)
  for i in indices:add(f'b{slot}_{i}',slot,i)
  marks[f'BLOCK{slot}_END']=len(packets)
 add('NEXT_DESC',desc=desc(4,2,0xffffffff));marks['BLOCK8_BEGIN']=len(packets)
 for i in range(20):add(f'b8_{i}',8,i)
 marks['BLOCK8_END']=len(packets);add('OLD_DESC',desc=desc(serial=0xfffffffd));add('WRAP_DESC',desc=desc(4,2,0));add('PARTIAL',9,0)
 add('NEW_SESSION',10,desc=desc(4,1,0),session=NEW);add('RETIRED_DESC',desc=desc());marks['BLOCK10_BEGIN']=len(packets)
 for i in [*range(8),*range(20,32)]:add(f'b10_{i}',10,i,session=NEW)
 marks['BLOCK10_END']=len(packets);add('NEW_RANGE',10,desc=desc(4,2,1),session=NEW);marks['BLOCK11_BEGIN']=len(packets)
 for i in range(20):add(f'b11_{i}',11,i,session=NEW)
 marks['BLOCK11_END']=len(packets)
 (out/'bytes.hex').write_text(''.join(f'{b:02x}\n' for p in packets for b in p));(out/'offsets.hex').write_text(''.join(f'{i*548:08x}\n' for i in range(len(packets))))
 (out/'source.hex').write_text(''.join(f'{int(b):02x}\n' for s in sources for b in s.ravel()))
 text=TB.replace('@BYTES_LAST@',str(len(packets)*548-1)).replace('@PACKETS_LAST@',str(len(packets)-1))
 for k,v in marks.items():text=text.replace('@'+k+'@',str(v))
 assert not re.search(r'@[A-Z_0-9]+@',text);(out/'tb.sv').write_text(text)
 return len(packets)

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 before=disk_check('before-crc-fec-pipeline');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  count=make_vectors(out);generator=np.array(vandermonde_generator(),dtype=np.uint8)
  assert np.array_equal(generator,GENERATOR)
  rom=np.zeros((32,32),dtype=np.uint8);rom[:,:20]=generator
  from fec_oracle import inverse
  (out/'generator.hex').write_text(''.join(f'{int(v):02x}\n' for v in rom.ravel()));(out/'inverse.hex').write_text(''.join(f'{inverse(v) if v else 0:02x}\n' for v in range(256)))
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','crc_fec_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'crc_fec_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_CRC_FEC_PIPELINE_PASS' in log and 'Fatal:' not in log,log[-5000:]
  result={'scope':__doc__,'simulation':next(x for x in log.splitlines() if x.startswith('G0_CRC_FEC_PIPELINE_PASS')),'packets':count,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'routed_OOC':None,'g0_passed':False}
  if not a.sim_only:
   before_ooc=disk_check('before-crc-fec-ingress-OOC')
   (out/'disk-before-ooc.json').write_text(json.dumps(before_ooc,indent=2)+'\n');assert before_ooc['disk']['available_bytes']>=5*GIB
   try:
    tcl='set_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in SOURCES)+'''synth_design -top link_fec_ingress_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force ingress.dcp
'''
    (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
   finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-crc-fec-ingress-OOC'),indent=2)+'\n')
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-crc-fec-pipeline'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ('output','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--sim-only',action='store_true');main(p.parse_args())
