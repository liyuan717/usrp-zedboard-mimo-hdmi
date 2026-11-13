#!/usr/bin/env python3
"""Actual CRC/fixed8 PL FEC consumes a bounded scheduled video block range.
No ADC front-end, physical DMA, real-time service or RF video acceptance.
"""
import argparse,hashlib,json,shutil,struct,subprocess
from pathlib import Path
import numpy as np
from g0_video_air_stream import VideoSchedule
from crc_fec_pipeline_probe import SOURCES
from fec_inverse_cost_probe import vandermonde_generator
from fec_oracle import inverse
from workspace_storage_guard import check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,cv=0,iv=0;reg [31:0] sid=0,sf=0;reg [7:0] data=0;
wire cr,ir,sv,last,recovered,abort_source,ev;wire [7:0] value;wire [31:0] ss,sb,es,eb;
wire [4:0] si;wire [8:0] offset;wire [2:0] ec;wire [19:0] missing,delivered;
wire [31:0] accepted,erased,unmapped,descs,baddesc,busy,drops,expired;
reg ready=0;longint unsigned cycle=0,last_progress=0;integer checked=0,restored=0,holds=0,sending_packet=-1;
crc_fec_pipeline_cost_probe dut(clk,rst,1'b0,cv,cr,sid,sf,16'd548,iv,ir,data,
 sv,ready,value,ss,sb,si,offset,last,recovered,abort_source,ev,1'b1,es,eb,ec,missing,delivered,
 accepted,erased,unmapped,descs,baddesc,busy,drops,expired);
reg [7:0] bytes[0:@LAST@],golden[0:@BLOCKS@*10240-1];integer position[0:@BLOCKS@-1][0:19];reg [19:0] seen[0:@BLOCKS@-1];
longint unsigned opened_at[0:@BLOCKS@-1],max_open_to_complete=0;
integer opens=0,completed_blocks=0,bank_owners,peak_bank_owners=0;
integer partial_events=0;
localparam integer PARTIAL_BLOCK=@PARTIAL_BLOCK@;
localparam [19:0] PARTIAL_SOURCE_MASK=20'hfe000;
reg opened_successor=0;
reg held=0;reg [87:0] held_value;wire [87:0] view={ss,sb,si,offset,last,recovered,value};
always @(negedge clk)ready=cycle%19>4;
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  bank_owners=0;
  for(integer b=0;b<8;b=b+1)
   if(dut.ingress.fec.bank_state[b]!=0)bank_owners=bank_owners+1;
  if(bank_owners>peak_bank_owners)peak_bank_owners=bank_owners;
  if(dut.ingress.open_valid&&dut.ingress.open_ready)begin
   if(dut.ingress.declare_block==@BLOCKS@ && @ALLOW_SUCCESSOR@)begin
    if(opened_successor)$fatal(1,"duplicate lookahead admission");
    opened_successor=1;
   end else begin
    if(dut.ingress.declare_block>=@BLOCKS@) $fatal(1,"unexpected block admission");
    if(opened_at[dut.ingress.declare_block]!=0) $fatal(1,"duplicate block admission");
    opened_at[dut.ingress.declare_block]=cycle;
    opens=opens+1;
   end
  end
  if(ev && ec==1)begin
   if(es!==32'h56334601||eb!=PARTIAL_BLOCK||missing!==~PARTIAL_SOURCE_MASK||delivered!==PARTIAL_SOURCE_MASK)
    $fatal(1,"unexpected partial FEC event");
   partial_events=partial_events+1;
  end
  if(held&&!abort_source&&(!sv||view!==held_value))$fatal(1,"held video SOURCE changed");
  if(abort_source)$fatal(1,"unexpected SOURCE abort");
  if(sv&&ready)begin
   if(ss!==32'h56334601||sb>=@BLOCKS@||si>=20)$fatal(1,"SOURCE identity");
   if(seen[sb][si]||offset!=position[sb][si]||last!==(offset==511))$fatal(1,"SOURCE boundary/duplicate");
   if(sb==PARTIAL_BLOCK&&si<13)$fatal(1,"unrecoverable row was emitted");
   if(value!==golden[sb*10240+si*512+offset]||recovered!==(sb!=PARTIAL_BLOCK&&si<12))$fatal(1,"actual video byte/recovery mismatch");
   position[sb][si]=position[sb][si]+1;checked=checked+1;last_progress=cycle;if(recovered)restored=restored+1;
   if(last)begin
    seen[sb][si]=1;
    if(seen[sb]==(sb==PARTIAL_BLOCK?PARTIAL_SOURCE_MASK:20'hfffff))begin
     if(!opened_at[sb])$fatal(1,"completed block with no open handshake");
     if(cycle-opened_at[sb]>max_open_to_complete)max_open_to_complete=cycle-opened_at[sb];
     if(@PERIOD_CYCLES@ && cycle-opened_at[sb]>25000000)$fatal(1,"500ms deadline block%0d",sb);
     completed_blocks=completed_blocks+1;
    end
   end
  end
  held=sv&&!ready;if(held)begin held_value=view;holds=holds+1;end
  if(@PERIOD_CYCLES@==0 && cycle-last_progress>10000000)
   $fatal(1,"accelerated no-progress packet%0d bytes%0d accepted%0d erased%0d unmapped%0d desc%0d bad%0d busy%0d drop%0d expired%0d ingress%0d fec%0d",sending_packet,checked,accepted,erased,unmapped,descs,baddesc,busy,drops,expired,dut.ingress.state,dut.ingress.fec.state);
 end else held=0;
end
task send(input integer packet);
 integer base,packet_sf;begin
  sending_packet=packet;base=packet*548;
  packet_sf={bytes[base+15],bytes[base+14],bytes[base+13],bytes[base+12]};
  while(cycle<packet_sf*64'd@PERIOD_CYCLES@)@(negedge clk);
  while(!cr)@(negedge clk);
  sid={bytes[base+11],bytes[base+10],bytes[base+9],bytes[base+8]};
  sf={bytes[base+15],bytes[base+14],bytes[base+13],bytes[base+12]};
  cv=1;@(negedge clk);cv=0;
  for(integer p=0;p<548;p=p+1)begin
   data=bytes[base+p];iv=1;@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
  end
 end
endtask
initial begin
 $readmemh("wire.hex",bytes);$readmemh("video.hex",golden);
 for(integer b=0;b<@BLOCKS@;b=b+1)begin seen[b]=0;opened_at[b]=0;for(integer i=0;i<20;i=i+1)position[b][i]=0;end
 repeat(5)@(negedge clk);rst=0;
 for(integer p=0;p<@PACKETS@;p=p+1)send(p);
 while(checked<@BYTES@||partial_events<@PARTIAL_EVENTS@)@(negedge clk);repeat(200)@(negedge clk);
 if(checked!=@BYTES@||restored!=@RESTORED@||accepted!=@ACCEPTED@||erased!=@ERASED@||unmapped!=@CONTROLS@||descs!=@DESCS@||baddesc||busy||drops||expired||holds==0)$fatal(1,"final video census bytes%0d restored%0d erased%0d bad%0d busy%0d drop%0d expired%0d",checked,restored,erased,baddesc,busy,drops,expired);
 for(integer b=0;b<@BLOCKS@;b=b+1)
  if(seen[b]!==(b==PARTIAL_BLOCK?PARTIAL_SOURCE_MASK:20'hfffff))$fatal(1,"unexpected video source row census");
 if(partial_events!=@PARTIAL_EVENTS@)$fatal(1,"partial FEC event census");
 if(@PARTIAL_EVENTS@)$display("G0_VIDEO_FEC_PARTIAL block=%0d known_sources=7 missing_sources=13 terminal_events=%0d",PARTIAL_BLOCK,partial_events);
 if(opens!=@BLOCKS@||completed_blocks!=@BLOCKS@||peak_bank_owners<1)$fatal(1,"missing block owner lifecycle");
 if(opened_successor!=@ALLOW_SUCCESSOR@)$fatal(1,"lookahead admission census");
 $display("G0_VIDEO_FEC_SERVICE blocks=%0d peak_banks=%0d max_open_to_complete_cycles=%0d lookahead=%0d",completed_blocks,peak_bank_owners,max_open_to_complete,opened_successor);
 $display("G0_VIDEO_CRC_FEC_PASS bytes=%0d restored=%0d CRC_erased=%0d control_skipped=%0d descriptors=%0d held=%0d",checked,restored,erased,unmapped,descs,holds);$finish;
end
initial begin #(64'd@TIMEOUT@);$fatal(1,"bounded video CRC/FEC timeout bytes%0d",checked);end
endmodule
'''

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 before=check('before-video-CRC-FEC-simulation');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 result={'completed':False,'physical_RF_DMA_video_passed':False,'PL_resources_changed':False,'fixed_banks':8}
 try:
  shutil.copyfile(Path(__file__),out/'helper.py')
  s=VideoSchedule(a.symbols,0x56334601,a.availability);packets=[];done=False
  blocks=s.plan['source_blocks'] if a.blocks==0 else a.blocks
  if not 1<=blocks<=min(256,s.plan['source_blocks']):s.close();raise ValueError('bounded1..256 available source blocks required')
  partial=a.partial_block
  if partial is not None and not 0<partial<blocks-1:
   s.close();raise ValueError('partial block must have a complete predecessor and successor')
  for sf in range(s.plan['periods']):
   wires,_=s.period(sf)
   for wire in wires:
    b=bytearray(wire);kind=b[5];block,index=struct.unpack_from('<IH',b,24)
    if kind==1 and index<(13 if block==partial else 12):b[-1]^=1
    packets.append(bytes(b))
    if kind==2 and block==blocks-1 and index==31:done=True;break
   if done:break
  s.close();assert done
  if a.availability:
   result['camera_readiness_manifest_sha256']=hashlib.sha256(a.availability.read_bytes()).hexdigest()
   result['camera_ready_prefix_us']=s.actual_ready[:blocks]
  (out/'wire.hex').write_text(''.join(f'{b:02x}\n' for p in packets for b in p))
  with a.symbols.open('rb') as f:golden=f.read(blocks*10240)
  assert len(golden)==blocks*10240;(out/'video.hex').write_text(''.join(f'{b:02x}\n' for b in golden))
  text=TB.replace('@LAST@',str(len(packets)*548-1)).replace('@PACKETS@',str(len(packets)))
  ranges=[struct.unpack_from('<IB',p,52) for p in packets if p[5]==3]
  last_declared=max(first+count-1 for first,count in ranges)
  if not blocks-1<=last_declared<=blocks:raise ValueError('unexpected bounded prefix lookahead')
  text=text.replace('@ALLOW_SUCCESSOR@',str(int(last_declared==blocks)))
  text=text.replace('@CONTROLS@',str(sum(p[5]==5 for p in packets))).replace('@DESCS@',str(sum(p[5]==3 for p in packets)))
  if a.period_cycles not in (0,1000000):raise ValueError('only accelerated or actual20ms packet-period gate is supported')
  timeout=max(blocks*80000000,struct.unpack_from('<I',packets[-1],12)[0]*a.period_cycles*20+500000000)
  partial_count=int(partial is not None)
  delivered_bytes=(blocks*20-13*partial_count)*512
  expected=dict(BLOCKS=blocks,BYTES=delivered_bytes,RESTORED=(blocks-partial_count)*6144,
                ACCEPTED=blocks*20-partial_count,ERASED=blocks*12+partial_count,
                PARTIAL_BLOCK=-1 if partial is None else partial,PARTIAL_EVENTS=partial_count,
                TIMEOUT=timeout,PERIOD_CYCLES=a.period_cycles)
  for key,value in expected.items():text=text.replace('@'+key+'@',str(value))
  (out/'tb.sv').write_text(text)
  matrix=np.array(vandermonde_generator(),dtype=np.uint8);rom=np.zeros((32,32),dtype=np.uint8);rom[:,:20]=matrix
  (out/'generator.hex').write_text(''.join(f'{int(x):02x}\n' for x in rom.ravel()))
  (out/'inverse.hex').write_text(''.join(f'{inverse(x) if x else 0:02x}\n' for x in range(256)))
  for n in SOURCES:shutil.copyfile(v/'sim/g0'/n,out/n)
  result.update(source_blocks=blocks,expected_source_bytes=delivered_bytes,partial_block=partial,packet_period_cycles=a.period_cycles,
    full_source_fixture_in_input=blocks==s.plan['source_blocks'],packets=len(packets),
    source_sha256={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},
    testbench_sha256=hashlib.sha256((out/'tb.sv').read_bytes()).hexdigest(),helper_sha256=hashlib.sha256((out/'helper.py').read_bytes()).hexdigest())
  commands=[('compile',[str(a.vivado_bin/'xvlog'),'--sv',*SOURCES,'tb.sv']),('elaborate',[str(a.vivado_bin/'xelab'),'tb','-s','video_tb','-mt','off']),('simulate',[str(a.vivado_bin/'xsim'),'video_tb','-runall'])]
  (out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
  for name,command in commands:
   with (out/(name+'.log')).open('w') as log:subprocess.run(command,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_VIDEO_CRC_FEC_PASS' in log and 'Fatal:' not in log,log[-4000:]
  result.update(completed=True,simulation=next(x for x in log.splitlines() if x.startswith('G0_VIDEO_CRC_FEC_PASS')),
    bank_service=next(x for x in log.splitlines() if x.startswith('G0_VIDEO_FEC_SERVICE')),
    partial_delivery=next((x for x in log.splitlines() if x.startswith('G0_VIDEO_FEC_PARTIAL')),None),
    packets=len(packets),source_sha256={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},
    video_source_prefix_sha256=hashlib.sha256(golden).hexdigest(),source_blocks=blocks,source_bytes=blocks*10240,
    full_source_fixture_covered=blocks==s.plan['source_blocks'],packet_period_cycles=a.period_cycles,
    note=('packet-stage ideal50MHz/20ms cadence; not sample-timed ADC, whole-board service, physical DMA or RF video signoff'
          if a.period_cycles else 'packet-stage accelerated playback, not sample-timed ADC/50MHz service, physical DMA or RF video signoff'))
 finally:
  if (out/'simulate.log').exists():result['terminal_simulator_lines']=[x for x in (out/'simulate.log').read_text().splitlines() if x.startswith(('Fatal:','G0_VIDEO_CRC_FEC_PASS'))]
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-video-CRC-FEC-simulation'),indent=2)+'\n')
 print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--availability',type=Path,help='actual camera SOURCE readiness and identity manifest for finite replay')
 p.add_argument('--blocks',type=int,default=5,help='bounded1..256; 0 selects all available blocks, still fixed8 physical banks')
 p.add_argument('--partial-block',type=int,help='one interior block with 13 CRC erasures, requiring partial delivery and continued reception')
 p.add_argument('--period-cycles',type=int,choices=(0,1000000),default=0,help='0 accelerated; 1000000 gates each sf at20ms/50MHz, still no ADC')
 p.add_argument('--vivado-bin',type=Path,default=Path('/vivado/2025.2/Vivado/bin'));main(p.parse_args())
