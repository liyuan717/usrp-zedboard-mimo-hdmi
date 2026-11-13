#!/usr/bin/env python3
"""Actual CRC/FEC -> shared SOURCE/SESSION BRAM -> DMA finite prototype.
Synthetic protected-frame coordinates at this isolated test boundary are not
full PHY coordinate or hardware clock proof. No hardware/RF access.
"""
import argparse,hashlib,json,shutil,struct,subprocess,zlib
from pathlib import Path
from crc_fec_pipeline_probe import SOURCES
from g0_fec_session_waveform import descriptor
from g0_packet_fec import source_pattern
from dma_batch_stream_probe import decode_batch
from workspace_storage_guard import check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,abort_packet=0,cv=0,iv=0,run=0,gate=1,ready=0,ref_ok=1;
reg [31:0] sid=32'h89abcdef,sf=1;reg [7:0] data=0;
reg [95:0] refpos=96'h0007123456789abc40000000;
wire cr,ir,sv,sr,sl,srec,sa,ev,er;wire [7:0] sd;wire [31:0] ss,sb,es,eb;
wire [4:0] si;wire [8:0] so;wire [2:0] ec;wire [19:0] em,ed;
wire [31:0] accepted,erased,unmapped,descs,baddesc,rangebusy,eventsdrop,expired;
crc_fec_pipeline_cost_probe dut(clk,rst,abort_packet,cv,cr,sid,sf,16'd548,iv,ir,data,
 sv,sr,sd,ss,sb,si,so,sl,srec,sa,ev,er,es,eb,ec,em,ed,
 accepted,erased,unmapped,descs,baddesc,rangebusy,eventsdrop,expired);
// This existing isolated CRC wrapper ties coordinates off. Drive only its
// trusted-frame reference test boundary; packet bytes/CRC/FEC are actual RTL.
wire mr;wire mv=dut.ingress.control_valid,ml=dut.ingress.control_last;
wire [63:0] md=dut.ingress.control_data;wire [1:0] mw=dut.ingress.control_word;
wire [31:0] ms=dut.ingress.control_session,mf=dut.ingress.control_superframe;
wire [95:0] mp=dut.ingress.control_position;
initial begin
 force dut.ingress.reference_valid=ref_ok;
 force dut.ingress.reference_position=refpos;
 force dut.ingress.control_ready=mr;
end
wire [63:0] rd,bd;wire [7:0] rk,bk;wire rv,rr,rl,bv,bl,busy,br;
wire [31:0] committed,dropped,bad,aborted,late,edrop,records,mc,mb,batches,batchrecords,batchbad,batchtimeouts;
source_record_bridge_cost_probe bridge(
 .clk(clk),.rst(rst),.fence(1'b0),.epoch(32'd1),.source_valid(sv),.source_ready(sr),.source_data(sd),
 .source_session(ss),.source_block(sb),.source_index(si),.source_offset(so),.source_last(sl),.source_recovered(srec),.source_abort(sa),
 .event_valid(ev),.event_ready(er),.event_session(es),.event_block(eb),.event_code(ec),.event_missing(em),.event_delivered(ed),
 .m_axis_tdata(rd),.m_axis_tkeep(rk),.m_axis_tvalid(rv),.m_axis_tready(rr),.m_axis_tlast(rl),
 .sources_committed(committed),.sources_dropped(dropped),.sources_bad(bad),.sources_aborted(aborted),
 .sources_late_or_duplicate(late),.events_dropped(edrop),.records_completed(records),
 .control_valid(mv),.control_ready(mr),.control_data(md),.control_word(mw),.control_last(ml),
 .control_session(ms),.control_superframe(mf),.control_position(mp),.controls_committed(mc),.controls_dropped(mb));
assign rr=br&&gate;
dma_batch_stream_cost_probe batch(clk,rst,run,rd,rk,rv&&gate,br,rl,bd,bk,bv,ready,bl,busy,batches,batchrecords,batchbad,batchtimeouts);
reg [7:0] bytes[0:@LAST@];integer offsets[0:11],cycle=0,fd,held=0;reg hold=0;reg [72:0] oldbeat;
always @(negedge clk)ready=cycle%19>4;
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(hold&&(!rv||{rd,rk,rl}!==oldbeat))$fatal(1,"control record changed under backpressure");
  hold=rv&&!rr;if(hold)begin oldbeat={rd,rk,rl};held=held+1;end
  if(bv&&ready)begin if(bk!==8'hff)$fatal(1,"batch keep");$fwrite(fd,"%016h %0d\n",bd,bl);if(bl)run<=0;end
 end else hold=0;
end
task send(input integer which,input bit valid_ref,input [31:0] session);
 integer k,w;begin
  ref_ok=valid_ref;sid=session;cv=1;@(posedge clk);w=0;
  while(!cr)begin @(posedge clk);w=w+1;if(w>10000)$fatal(1,"packet cfg blocked");end
  @(negedge clk);cv=0;
  for(k=0;k<548;k=k+1)begin
   iv=1;data=bytes[offsets[which]+k];@(posedge clk);w=0;
   while(!ir)begin @(posedge clk);w=w+1;if(w>10000)$fatal(1,"packet bytes blocked");end
   @(negedge clk);iv=0;
  end
  w=0;while(!cr||dut.ingress.state!=0)begin @(negedge clk);w=w+1;if(w>20000)$fatal(1,"metadata blocked RF");end
  repeat(10)@(negedge clk);
 end
endtask
task source_count(input integer wanted);
 integer w;begin w=0;
  while(committed<wanted)begin
   @(negedge clk);w=w+1;
   if(w>12000)$fatal(1,"metadata consumed reserved SOURCE slot c%0d d%0d m%0d",committed,dropped,mc);
  end
 end
endtask
task drain;
 integer w;begin w=0;
  while(bridge.slot_state[0]!=0||bridge.slot_state[1]!=0||bridge.slot_state[2]!=0||bridge.state!=0)begin
   @(negedge clk);w=w+1;if(w>15000)$fatal(1,"record bounded drain");
  end
 end
endtask
initial begin
 fd=$fopen("batches.txt","w");$readmemh("bytes.hex",bytes);$readmemh("offsets.hex",offsets);
 repeat(5)@(negedge clk);rst=0;run=1;
 gate=0;send(0,1,32'h89abcdef);send(7,1,32'h89abcdef);source_count(1);
 if(mc!=1||dropped)$fatal(1,"SESSION slot aliased SOURCE duplicate identity");
 gate=1;drain();send(1,1,32'h89abcdef);drain();
 if(mc!=2)$fatal(1,"valid control records missing");
 send(2,1,32'h89abcdef);send(3,1,32'h89abcdef);send(4,1,32'h76543210);
 send(5,1,32'h89abcdef);send(6,0,32'h89abcdef);
 if(mc!=2||erased!=1||dut.ingress.controls_dropped!=4)$fatal(1,"untrusted timebase promoted");
 gate=0;send(8,1,32'h89abcdef);refpos=96'h0008fedcba98765480000000;
 send(9,1,32'h89abcdef);send(10,1,32'h89abcdef);
 source_count(2);
 if(mc!=3||committed!=2||dropped||mb)$fatal(1,"metadata consumed reserved SOURCE slot");
 abort_packet=1;repeat(3)@(negedge clk);abort_packet=0;send(11,1,32'h89abcdef);
 if(dut.ingress.controls_emitted!=3||dut.ingress.controls_dropped!=6)$fatal(1,"busy metadata not locally dropped");
 gate=1;drain();while(batches==0)@(negedge clk);
 repeat(5)@(negedge clk);$fclose(fd);
 if(batches!=1||batchrecords!=5||batchbad||batchtimeouts||records!=5||held==0)$fatal(1,"control batch census");
 $display("G0_FORWARD_CONTROL_PASS controls=%0d sources=%0d local_metadata_drops=%0d held=%0d",mc,committed,dut.ingress.controls_dropped,held);$finish;
end
initial begin #5000000;$fatal(1,"forward control timeout");end
endmodule
'''

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 before=check('before-forward-control');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 assert before['disk']['available_bytes']>=5*GIB
 names=[*SOURCES,'source_record_bridge_cost_probe.sv','dma_batch_stream_cost_probe.sv']
 for n in names:shutil.copyfile(v/'sim/g0'/n,out/n)
 for n in ['generator.hex','inverse.hex']:shutil.copyfile(a.rom_reference/n,out/n)
 def vrt(low=-123456,high=-123000,profile=240):return struct.pack('<4sBBHQqq',b'VRT3',1,profile,1,17,low,high)
 desc=bytearray(descriptor(1,1));struct.pack_into('<H',desc,6,2)
 bodies=[(3,bytes(desc)),(5,vrt()),(5,vrt()),(5,vrt(profile=241)),(5,vrt()),(5,vrt(10,9)),(5,vrt()),
         (1,source_pattern(1)[0].tobytes()),(5,vrt()),(5,vrt()),(1,source_pattern(1)[1].tobytes()),(5,vrt())]
 packed=bytearray();offsets=[]
 for i,(kind,body) in enumerate(bodies):
  sid=0x76543210 if i==4 else 0x89abcdef
  head=struct.pack('<4sBBHIIIIIHH',b'VRL3',1,kind,0,sid,1,i,0xffffffff,1 if kind==1 else 0xffffffff,1 if i==10 else 0,512)
  raw=head+body+bytes(512-len(body));wire=bytearray(raw+struct.pack('<I',zlib.crc32(raw)))
  if i==2:wire[-1]^=1
  offsets.append(len(packed));packed.extend(wire)
 (out/'bytes.hex').write_text(''.join(f'{x:02x}\n' for x in packed))
 (out/'offsets.hex').write_text(''.join(f'{x:08x}\n' for x in offsets))
 (out/'tb.sv').write_text(TB.replace('@LAST@',str(len(packed)-1)))
 bp=a.vivado_bin.resolve()
 def run(label,args):
  with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
 result=dict(completed=False,RF_started=False,physical_DMA_or_source_clock_or_G0_passed=False)
 try:
  run('compile',[str(bp/'xvlog'),'--sv',*names,'tb.sv'])
  run('elaborate',[str(bp/'xelab'),'tb','-s','forward_control_tb','-mt','off'])
  run('simulate',[str(bp/'xsim'),'forward_control_tb','-runall'])
  log=(out/'simulate.log').read_text();line=next(x for x in log.splitlines() if x.startswith('G0_FORWARD_CONTROL_PASS'))
  data=b''.join(int(row.split()[0],16).to_bytes(8,'little') for row in (out/'batches.txt').read_text().splitlines())
  assert len(data)==32768
  records,meta=decode_batch(data,0);assert len(records)==5
  controls=[r for r in records if struct.unpack_from('<H',r,12)[0]==3]
  sources=[r for r in records if struct.unpack_from('<H',r,12)[0]==1]
  assert [r[64:] for r in controls]==[bytes(desc),vrt(),vrt()]
  for r in controls:
   assert len(r)==96 and struct.unpack_from('<H',r,14)[0]==4
   assert struct.unpack_from('<Q',r,32)[0]==0x0007123456789abc
   assert struct.unpack_from('<I',r,56)[0]==0x40000000
  assert [r[64:] for r in sources]==[source_pattern(1)[i].tobytes() for i in range(2)]
  (out/'batches.bin').write_bytes(data)
  result.update(completed=True,simulation=line,source_sha256={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names},
   DMA_batch_sha256=hashlib.sha256(data).hexdigest(),decoded_batch_metadata=meta,
   exact_SESSION_records=len(controls),exact_SOURCE_records=len(sources),shared_payload_RAM_bytes=1536,
   full_PHY_reference_coordinate_verified=False)
 finally:
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-forward-control'),indent=2)+'\n')
 print(line)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['vivado-bin','output','rom-reference']:p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
