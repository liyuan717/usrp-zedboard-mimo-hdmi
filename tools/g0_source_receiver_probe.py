#!/usr/bin/env python3
"""Actual55RTL SOURCE board-bridge simulation: CSR, STOP and autonomous raw RX.
No stub receiver, physical IO/DDR/RF or SD/production acceptance.
"""
import argparse,hashlib,json,os,shutil,struct,subprocess
from pathlib import Path
from dma_batch_stream_probe import decode_batch,SOURCE as DMA_BATCH_SOURCE,validate_dma_replacement
from workspace_storage_guard import check,GIB
from correlation_implementation_probe import validate_replacement
from fec_deadline_proof import NAME as FEC_STREAM_SOURCE,validate_deadline_replacement
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg resetn=0,pv=0,fence=0,boundary=1,capture=1,rfready=1,hold=0,ready=0;
reg [127:0] pd=0;reg [15:0] awaddr=0,araddr=0;reg awv=0,wv=0,arv=0;
reg [31:0] wd=0;reg [3:0] ws=15;wire awr,wr,bv,arr,rrv,flush_ack,overflow;
wire [1:0] bp,rp;wire [31:0] rd;wire [63:0] od;wire [7:0] keep;wire ov,last;
g0_source_receiver dut(.aclk(clk),.aresetn(resetn),
.s_axi_awaddr(awaddr),.s_axi_awvalid(awv),.s_axi_awready(awr),.s_axi_wdata(wd),.s_axi_wstrb(ws),.s_axi_wvalid(wv),.s_axi_wready(wr),.s_axi_bresp(bp),.s_axi_bvalid(bv),.s_axi_bready(1'b1),
.s_axi_araddr(araddr),.s_axi_arvalid(arv),.s_axi_arready(arr),.s_axi_rdata(rd),.s_axi_rresp(rp),.s_axi_rvalid(rrv),.s_axi_rready(1'b1),
.packed_data(pd),.packed_valid(pv),.boundary_resetn(boundary),.capture_enable(capture),.rf_path_ready(rfready),.capture_epoch_fence(fence),.flush_ack(flush_ack),.source_overflow(overflow),
.m_axis_tdata(od),.m_axis_tkeep(keep),.m_axis_tvalid(ov),.m_axis_tready(ready),.m_axis_tlast(last));
integer cycles=0,phase=0,fd,beats=0,batches=0,held=0;reg was_held=0;reg [72:0] held_view;reg [31:0] value;
always @(negedge clk)ready=!hold && cycles%19>4;
always @(posedge clk)begin
 cycles=cycles+1;
 if(resetn)begin
  if(was_held && (!ov || {od,keep,last}!==held_view))$fatal(1,"SOURCE bridge revoked held DMA beat");
  was_held=ov&&!ready;if(was_held)begin held_view={od,keep,last};held=held+1;end
  if(ov&&ready)begin
   if(keep!=255 || last!=(beats%4096==4095))$fatal(1,"SOURCE batch boundary");
   $fwrite(fd,"%0d %016h %0d\n",phase,od,last);beats=beats+1;if(last)batches=batches+1;
  end
 end
end
task csr_write(input [15:0] addr,input [31:0] data,input integer reverse,input [3:0] strobe);
 integer w;
 begin
  awaddr=addr;wd=data;ws=strobe;
  if(reverse)begin wv=1;@(posedge clk);while(!wr)@(posedge clk);@(negedge clk);wv=0;end
  else begin awv=1;@(posedge clk);while(!awr)@(posedge clk);@(negedge clk);awv=0;end
  repeat(3)@(negedge clk);
  if(reverse)begin awv=1;@(posedge clk);while(!awr)@(posedge clk);@(negedge clk);awv=0;end
  else begin wv=1;@(posedge clk);while(!wr)@(posedge clk);@(negedge clk);wv=0;end
  w=0;while(!bv)begin @(negedge clk);w=w+1;if(w>50)$fatal(1,"CSR write wait");end
  if(bp!=0)$fatal(1,"CSR write response");@(negedge clk);
 end
endtask
task csr_read(input [15:0] addr);
 integer w;
 begin araddr=addr;arv=1;@(posedge clk);while(!arr)@(posedge clk);@(negedge clk);arv=0;
 w=0;while(!rrv)begin @(negedge clk);w=w+1;if(w>50)$fatal(1,"CSR read wait");end
 if(rp!=0)$fatal(1,"CSR read response");value=rd;@(negedge clk);end
endtask
task idle;
 integer n;
 begin n=0;while(dut.run || dut.batch_busy)begin @(negedge clk);n=n+1;if(n>120000)$fatal(1,"unbounded SOURCE STOP");end
 repeat(4)@(negedge clk);csr_read(12);if(value&6)$fatal(1,"SOURCE owner not quiescent");end
endtask
@RAW_DECL@
initial begin
 fd=$fopen("batches.txt","w");repeat(6)@(negedge clk);resetn=1;repeat(3)@(negedge clk);
 csr_read(0);if(value!=32'h50533356)$fatal(1,"SOURCE identity");
 csr_read(4);if(value!=32'h20261103)$fatal(1,"SOURCE version");
 @BODY@
 $fclose(fd);$display("G0_SOURCE_BOARD_BRIDGE_PASS batches=%0d held_checks=%0d",batches,held);$finish;
end
initial begin #2000000000;$fatal(1,"SOURCE bridge timeout");end
endmodule
'''
CONTROL=r'''
rfready=0;csr_write(8,1,1,15);csr_read(8);if(value)$fatal(1,"START without RF path");rfready=1;
csr_write(44,2,0,15);csr_write(44,99,1,1);csr_read(44);if(value!=2)$fatal(1,"partial LIMIT write");
phase=1;csr_write(8,1,1,15);csr_write(44,7,0,15);csr_read(44);if(value!=2)$fatal(1,"active LIMIT changed");idle;csr_read(16);if(value!=2)$fatal(1,"finite batch count");
csr_read(48);if(value)$fatal(1,"finite batch remaining");
phase=2;csr_write(44,0,1,15);hold=1;csr_write(8,1,0,15);
while(!ov)@(negedge clk);repeat(4)@(negedge clk);
// The RF reset fence flushes the actual single CPACK pipeline register.
capture=0;fence=1;pv=1;pd=128'h1234;@(negedge clk);
if(!flush_ack || dut.pipe_valid)$fatal(1,"CPACK real flush acknowledgement");
fence=0;repeat(3)@(negedge clk);if(flush_ack)$fatal(1,"stale flush acknowledgement");if(dut.pipe_valid)$fatal(1,"capture guard admitted stale input");pv=0;
csr_write(8,0,1,15);repeat(4)@(negedge clk);if(!ov)$fatal(1,"STOP killed held output");
hold=0;idle;capture=1;csr_read(16);if(value!=1)$fatal(1,"STOP final batch");
phase=3;csr_write(44,2,0,15);csr_write(8,1,0,15);idle;csr_read(16);if(value!=2)$fatal(1,"reentry count");
if(batches!=5)$fatal(1,"total finite batch census");
'''
RAW=r'''
$readmemh("adc.hex",adc);phase=1;csr_write(44,0,0,15);csr_write(8,1,1,15);
for(k=0;k<2097152;k=k+1)begin
 pd={16'h8011,BASE+48'(k-83644),adc[k]};pv=1;@(negedge clk);pv=0;repeat(k%2?12:11)@(negedge clk);
end
if(!dut.search_armed || dut.phase_origin!==BASE-48'd83644 || dut.cal_faults || dut.cal_range)$fatal(1,"autonomous raw phase/search/calibration");
repeat(1000000)@(negedge clk);
if(dut.cal_faults!=1 || dut.cal_ready)$fatal(1,"autonomous raw idle fence");
if(dut.counters[0]!=136 || dut.counters[1] || dut.counters[2] || dut.counters[3] || !dut.counters[4] || !dut.counters[5])$fatal(1,"raw SOURCE CSR census good%0d drop%0d crc%0d/%0d",dut.counters[0],dut.counters[1],dut.counters[4],dut.counters[5]);
csr_write(8,0,0,15);idle;
$display("G0_SOURCE_AUTONOMOUS_RAW_PASS SOURCE=%0d dropped=%0d crc_good=%0d crc_bad=%0d",dut.counters[0],dut.counters[1],dut.counters[4],dut.counters[5]);
'''
CLOCK=r'''
csr_read(128);if(value!=32'h43543356)$fatal(1,"clock tag capability");
phase=4;csr_write(44,0,0,15);csr_write(8,1,0,15);
pd={64'h1234abcdffffffff,64'd0};pv=1;@(negedge clk);pv=0;repeat(3)@(negedge clk);
csr_read(132);if(value!=32'hffffffff)$fatal(1,"clock tag low word");
// Change both high and low between reads; the high word must stay atomic.
pd={64'h1235abce00000005,64'd0};pv=1;@(negedge clk);pv=0;repeat(3)@(negedge clk);
csr_read(136);if(value!=32'h1234abcd)$fatal(1,"clock tag torn high word");
csr_read(140);if(!value[0])$fatal(1,"fresh clock tag unavailable");
csr_read(132);if(value!=5)$fatal(1,"clock tag next low word");
csr_read(136);if(value!=32'h1235abce)$fatal(1,"clock tag next high word");
repeat(65)@(negedge clk);csr_read(132);csr_read(140);
if(value[0])$fatal(1,"stale CPACK tag considered current");
pd={64'h1235abce00000006,64'd0};pv=1;@(negedge clk);pv=0;repeat(3)@(negedge clk);
fence=1;csr_read(132);csr_read(140);if(value[0])$fatal(1,"fenced clock tag valid");fence=0;
csr_write(8,0,0,15);idle;
csr_read(132);csr_read(140);if(value[0])$fatal(1,"stopped clock tag valid");
if(batches!=6)$fatal(1,"clock test batch census");
'''
COUNTER_SHARING=r'''
// Inject counter outputs at the bridge boundary with the real receiver
// instantiated. Search control and AXI/AXIS continue to use the actual RTL.
phase=5;hold=1;csr_write(8,1,0,15);
force dut.cal_ready=1'b1;
force dut.ingress_running=1'b1;
force dut.search_ready=1'b1;
pd={64'h1235abce00000020,64'd0};pv=1;@(negedge clk);pv=0;
repeat(6)@(negedge clk);
if(!dut.search_armed || dut.search_id!=32'h80000001 || overflow)$fatal(1,"initial search or false overflow");
force dut.ingress_faults=32'h80000001;
@(negedge clk);if(!overflow)$fatal(1,"ingress overflow missed");
@(negedge clk);if(overflow)$fatal(1,"ingress overflow repeated");
csr_read(24);if(value!=32'h80000001)$fatal(1,"ingress CSR width");
force dut.cal_range=32'h40000002;
@(negedge clk);if(!overflow)$fatal(1,"calibration range overflow missed");
@(negedge clk);if(overflow)$fatal(1,"calibration range overflow repeated");
csr_read(100);if(value!=32'h40000002)$fatal(1,"range CSR width");
force dut.cal_faults=32'h20000001;fence=1;
@(negedge clk);if(dut.search_armed || dut.search_pending || dut.have_origin || !flush_ack || overflow)$fatal(1,"calibration loss did not clear search");
fence=0;pd={64'h1236abcf00000080,64'd0};pv=1;@(negedge clk);pv=0;
repeat(6)@(negedge clk);
if(!dut.search_armed || dut.search_id!=32'h80000002 ||
   dut.phase_origin!=48'habcf00000080 || dut.search_tag!=64'h1236abcf00000084)$fatal(1,"new epoch did not rearm search");
csr_read(32);if(value!=32'h20000001)$fatal(1,"calibration CSR width");
csr_write(8,0,0,15);
if(!ov || dut.core_reset)$fatal(1,"STOP abandoned held batch");
force dut.ingress_faults=32'hffffffff;
@(negedge clk);if(!overflow)$fatal(1,"fault tracking stopped before drain");
force dut.ingress_faults=32'h00000000;
@(negedge clk);if(!overflow)$fatal(1,"counter wrap missed");
@(negedge clk);if(overflow)$fatal(1,"counter wrap repeated");
hold=0;idle;
force dut.ingress_faults=32'hdeadbeef;
repeat(4)@(negedge clk);
if(overflow)$fatal(1,"idle overflow");
csr_read(24);if(value)$fatal(1,"idle ingress CSR changed");
csr_read(32);if(value!=32'h20000001)$fatal(1,"STOP lost calibration CSR");
csr_read(100);if(value!=32'h40000002)$fatal(1,"STOP lost range CSR");
release dut.ingress_faults;release dut.cal_faults;release dut.cal_range;
release dut.cal_ready;release dut.ingress_running;release dut.search_ready;
phase=6;csr_write(44,1,0,15);
fork
 csr_write(8,1,0,15);
 begin
  while(!dut.run)@(negedge clk);
  if(dut.counters[6] || dut.counters[8] || dut.counters[9])$fatal(1,"START retained previous-session counts on the first active cycle");
 end
join
if(overflow || dut.search_armed || dut.search_pending)$fatal(1,"START reused stale fault state");
csr_read(24);if(value)$fatal(1,"START ingress CSR clear");
csr_read(32);if(value)$fatal(1,"START calibration CSR clear");
csr_read(100);if(value)$fatal(1,"START range CSR clear");
idle;
if(batches!=8)$fatal(1,"counter sharing batch census");
$display("G0_SOURCE_COUNTER_SHARING_PASS overflow_wrap STOP_restart search_epoch CSR_retention");
'''

def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 c=check('before-SOURCE-bridge-probe');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
 try:
  proof=json.loads((ref/'receipt.json').read_text());assert proof['original52RTL_sources_unchanged'];names=[*proof['source_sha256'],'g0_source_receiver.v'];assert len(names)==55
  replacements=[]
  control_proof=json.loads(a.forward_control_proof.read_text()) if a.forward_control_proof else None
  control_files={'link_fec_ingress_cost_probe.sv','source_record_bridge_cost_probe.sv',
                 'frame_acquiring_frontend_cost_probe.sv','record_acquiring_frontend_cost_probe.sv'}
  if control_proof:assert control_proof['completed'] and control_proof['simulation'].startswith('G0_FORWARD_CONTROL_PASS')
  for n in names:
   p=r/('pl/g0' if n=='g0_source_receiver.v' else 'sim/g0')/n
   if n in proof['source_sha256']:
    if control_proof and n in control_files:
     current=hashlib.sha256(p.read_bytes()).hexdigest()
     if n in control_proof['source_sha256']:assert current==control_proof['source_sha256'][n]
     # These two adapter files are deliberately verified by THIS full
     # frontend compilation/replay, not claimed unchanged or proven by
     # the isolated synthetic-reference control test.
     replacement={'name':n,'baseline_sha256':proof['source_sha256'][n],'compiled_sha256':current,
                  'new_forward_control_metadata':True,'same_logic_transform_claimed':False,
                  'independent_full_frontend_replay_required':True}
    elif n==DMA_BATCH_SOURCE and a.dma_batch_proof:
     replacement=validate_dma_replacement(proof['source_sha256'][n],p,a.dma_batch_proof)
    elif n==FEC_STREAM_SOURCE and a.fec_deadline_proof:
     replacement=validate_deadline_replacement(proof['source_sha256'][n],p,a.fec_deadline_proof)
    else:replacement=validate_replacement(n,proof['source_sha256'][n],p,a.correlation_proof,a.rrc_proof,a.window_proof,a.coarse_proof)
    if replacement:replacements.append(replacement)
   shutil.copyfile(p,out/n)
  for f in ref.glob('*.hex'):
   if f.name!='adc.hex':os.link(f,out/f.name)
  if any(x['name']=='rrc_cost_probe.sv' for x in replacements):assert hashlib.sha256((out/'rrc.hex').read_bytes()).hexdigest()==json.loads(a.rrc_proof.read_text())['coefficient_ROM_sha256']
  if a.raw:
   os.link(r/'build/g0-raw-PL-DC-air-r2/adc.hex',out/'adc.hex')
  assert not (a.raw and a.clock_snapshot) and (not a.counter_sharing or a.clock_snapshot)
  body=RAW if a.raw else CONTROL+(CLOCK if a.clock_snapshot else '')+(COUNTER_SHARING if a.counter_sharing else '')
  tb=TB.replace('@RAW_DECL@',"reg [63:0] adc[0:2097151];integer k;localparam [47:0] BASE=48'hfffffffeeaa8;" if a.raw else '').replace('@BODY@',body);(out/'tb.sv').write_text(tb)
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm';run('compile',[bp/'xvlog','--sv',xpm/'xpm_cdc/hdl/xpm_cdc.sv',xpm/'xpm_memory/hdl/xpm_memory.sv',*names,'tb.sv']);run('elaborate',[bp/'xelab','tb','-s','source_tb','-mt','off']);run('simulate',[bp/'xsim','source_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_SOURCE_BOARD_BRIDGE_PASS' in log and 'Fatal:' not in log,log[-3000:]
  if a.counter_sharing:assert 'G0_SOURCE_COUNTER_SHARING_PASS' in log
  records=[];data=bytearray();sessions={};batch_meta=[]
  for line in (out/'batches.txt').read_text().splitlines():
   phase,word,last=line.split();phase=int(phase);data.extend(int(word,16).to_bytes(8,'little'))
   if int(last):
    rs,meta=decode_batch(bytes(data),sessions.get(phase,0));records.extend(rs);sessions[phase]=sessions.get(phase,0)+1;batch_meta.append(meta);data.clear()
  assert not data
  if a.raw:
   source=[];restored=0
   for record in records:
    kind,flags=struct.unpack_from('<HH',record,12)
    if kind==1:
     block,index=struct.unpack_from('<IH',record,48);expected=bytes((block*71+index*29+o*17+o//11+(block+1)*(o%13))&255 for o in range(512));assert record[64:]==expected;source.append((block,index));restored+=512*(flags&1)
   assert len(source)==len(set(source))==136 and restored==13312 and len(records)==142
  else:assert len(batch_meta)==(8 if a.counter_sharing else 6 if a.clock_snapshot else 5) and not records
  receipt={'scope':__doc__,'raw':a.raw,'verified_source_replacements':replacements,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names},'simulation':[s for s in log.splitlines() if s.startswith('G0_SOURCE_')],'batches':len(batch_meta),'records':len(records),'independent_SOURCE_exact_bytes':69632 if a.raw else 0,'atomic_CPACK_tag_CSR_freshness_fence_STOP_test_passed':a.clock_snapshot,'counter_sharing_fault_and_restart_test_passed':a.counter_sharing,'physical_IO_DDR_RF_or_G0_passed':False};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps({k:v for k,v in receipt.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-SOURCE-bridge-probe'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--coarse-proof',type=Path);p.add_argument('--window-proof',type=Path);p.add_argument('--rrc-proof',type=Path);p.add_argument('--correlation-proof',type=Path)
 p.add_argument('--forward-control-proof',type=Path,help='actual CRC/control/shared-BRAM prototype proof; requires new full frontend replay, never an unchanged-source claim')
 p.add_argument('--dma-batch-proof',type=Path,help='boundary proof for bounded DMA accounting replacement')
 p.add_argument('--fec-deadline-proof',type=Path,help='byte and deadline proof for the fixed-8 FEC replacement')
 p.add_argument('--clock-snapshot',action='store_true',help='actual CSR atomic CPACK-tag/freshness/STOP test; not ADC-to-clock latency or source-age proof')
 p.add_argument('--counter-sharing',action='store_true',help='inject ingress/calibration faults and verify CSR retention and search across STOP/restart')
 p.add_argument('--raw',action='store_true');main(p.parse_args())
