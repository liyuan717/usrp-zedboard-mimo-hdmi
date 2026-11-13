#!/usr/bin/env python3
"""Eight queued FEC owners survive a new range/ninth block and resume after bounded SOURCE hold."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
import struct
import subprocess
import zlib

from crc_fec_pipeline_probe import SOURCES
from workspace_storage_guard import check, GIB

TB = r'''`timescale 1ns/1ps
module tb;
localparam [31:0] BASE=32'hfffffffc;
reg clk=0;always #10 clk=~clk;reg rst=1,pa=0,cv=0,iv=0;reg [7:0] data=0;
wire cr,ir,sv,last,recovered,ab,ev;wire [7:0] od;wire [31:0] ss,sb,es,eb,accepted,erased,unmapped,descs,baddesc,busy,drops,expired;
wire [4:0] si;wire [8:0] so;wire [2:0] ec;wire [19:0] em,ed;reg sr=0,er=0,resume_output=0;
crc_fec_pipeline_cost_probe dut(clk,rst,pa,cv,cr,32'h56334601,32'd31,16'd548,iv,ir,data,
 sv,sr,od,ss,sb,si,so,last,recovered,ab,ev,er,es,eb,ec,em,ed,accepted,erased,unmapped,descs,baddesc,busy,drops,expired);
reg [7:0] bytes[0:89323],golden[0:92159];integer cycle=0,checked=0,held=0,j,k,terminals=0,start;
integer position[0:8][0:19];reg [19:0] seen[0:8];reg source_held=0,event_held=0;
reg [87:0] source_view;reg [106:0] event_view;
wire [87:0] current_source={ss,sb,si,so,last,recovered,od};wire [106:0] current_event={es,eb,ec,em,ed};
wire [31:0] logical_block=sb-BASE;
always @(negedge clk)begin sr=resume_output&&cycle%17>3;er=resume_output&&cycle%13>2;end
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(ab)$fatal(1,"capacity unexpected source abort");
  if(source_held&&(!sv||current_source!==source_view))$fatal(1,"capacity held source changed");
  if(event_held&&(!ev||current_event!==event_view))$fatal(1,"capacity held event changed");
  if(sv&&sr)begin
   if(ss!=32'h56334601||logical_block>=9||si>=20||so!=position[logical_block][si]||seen[logical_block][si]||od!==golden[logical_block*10240+si*512+so]||last!==(so==511)||recovered)$fatal(1,"capacity source oracle block%h row%0d byte%0d",sb,si,so);
   position[logical_block][si]=position[logical_block][si]+1;checked=checked+1;if(last)seen[logical_block][si]=1;
  end
  if(dut.ingress.core_status_valid)begin
   if(dut.ingress.core_code!=0||dut.ingress.core_missing||dut.ingress.core_delivered!=20'hfffff)$fatal(1,"capacity terminal data mismatch");
   terminals=terminals+1;
  end
  source_held=sv&&!sr;event_held=ev&&!er;
  if(source_held)begin source_view=current_source;held=held+1;end
  if(event_held)begin event_view=current_event;held=held+1;end
 end
end
task send(input integer packet);
 begin
  while(!cr)@(negedge clk);cv=1;@(negedge clk);cv=0;
  for(integer p=0;p<548;p=p+1)begin data=bytes[packet*548+p];iv=1;@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;end
  while(!cr||dut.ingress.state!=0)@(negedge clk);repeat(2)@(negedge clk);
 end
endtask
task owners;
 begin
  if(dut.ingress.bank_live!=8'hff)$fatal(1,"capacity queued bank owner lost");
  for(integer b=0;b<8;b=b+1)begin
   if(dut.ingress.bank_block[(b+4)%8]!==BASE+32'(b)||dut.ingress.fec.bank_job[(b+4)%8]!==BASE+32'(b))$fatal(1,"capacity queued owner overwritten");
   if(dut.ingress.fec.bank_state[(b+4)%8]!=2&&dut.ingress.fec.bank_state[(b+4)%8]!=3)$fatal(1,"capacity full-k owner improperly retired");
  end
 end
endtask
initial begin
 $readmemh("bytes.hex",bytes);$readmemh("source.hex",golden);
 for(j=0;j<9;j=j+1)begin seen[j]=0;for(k=0;k<20;k=k+1)position[j][k]=0;end
 repeat(5)@(negedge clk);rst=0;send(0);
 for(j=1;j<=160;j=j+1)send(j);owners();
 send(161);owners();send(161);owners();
 send(162);owners();
 if(busy!=16||unmapped!=1||checked||terminals||expired||erased||baddesc)$fatal(1,"capacity ninth task changed full owners");
 resume_output=1;start=cycle;
 while(terminals!=8)begin @(negedge clk);if(cycle-start>600000)$fatal(1,"capacity queued drain service bound");end
 repeat(32)@(negedge clk);
 if(checked!=81920||dut.ingress.bank_live)$fatal(1,"capacity old queue incomplete");
 send(161);send(162);start=cycle;
 while(!seen[8][0])begin @(negedge clk);if(cycle-start>10000)$fatal(1,"capacity resumed ninth block timeout");end
 if(checked!=82432||expired||erased||baddesc||unmapped!=1||busy!=16)$fatal(1,"capacity resumed census");
 $display("G0_FEC_WINDOW_CAPACITY_PASS queued_banks=8 blocked_declarations=%0d rejected_ninth=1 resumed_ninth_bytes=512 exact_bytes=%0d terminal_jobs=%0d held_checks=%0d cycles=%0d",busy,checked,terminals,held,cycle);$finish;
end
initial begin #30000000;$fatal(1,"capacity bounded global timeout");end
endmodule
'''


def main(a):
    v = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = check('before-FEC-window-capacity')
    (out/'disk-before.json').write_text(json.dumps(before, indent=2)+'\n')
    assert before['disk']['available_bytes'] >= 5*GIB
    try:
        rng = random.Random(0x71756575)
        truth = [bytes(rng.randrange(256) for _ in range(10240)) for _ in range(9)]
        packets = []
        def packet(kind, block, index, payload):
            header = struct.pack('<4sBBHIIIIIHH', b'VRL3', 1, kind, 0, 0x56334601,
                                 31, len(packets), 0xffffffff, block&0xffffffff, index, 512)
            body = header+payload
            packets.append(body+struct.pack('<I', zlib.crc32(body)))
        def descriptor(first, serial):
            payload = struct.pack('<4sBBHBBHHHHHIBBHI', b'VRS3', 1, 0xf0, 0,
                                  20, 32, 512, 1024, 600, 20, 1, first, 8, 0, 0, serial)+bytes(480)
            packet(3, first, 0, payload)
        descriptor(0xfffffffc, 0xfffffffe)
        for b in range(8):
            for row in range(20):
                packet(1, 0xfffffffc+b, row, truth[b][row*512:(row+1)*512])
        descriptor(4, 0xffffffff)
        packet(1, 4, 0, truth[8][:512])
        assert len(packets) == 163
        (out/'bytes.hex').write_text(''.join(f'{x:02x}\n' for p in packets for x in p))
        (out/'source.hex').write_text(''.join(f'{x:02x}\n' for p in truth for x in p))
        (out/'tb.sv').write_text(TB)
        for name in SOURCES:
            shutil.copyfile(v/'sim/g0'/name, out/name)
        for name in ('generator.hex', 'inverse.hex'):
            shutil.copyfile(a.reference/name, out/name)
        bp = a.vivado_bin.resolve()
        for step, args in [('compile', [bp/'xvlog', '--sv', *SOURCES, 'tb.sv']),
                           ('elaborate', [bp/'xelab', 'tb', '-s', 'capacity_tb', '-mt', 'off']),
                           ('simulate', [bp/'xsim', 'capacity_tb', '-runall'])]:
            with (out/(step+'.log')).open('w') as log:
                subprocess.run([str(x) for x in args], cwd=out, stdout=log, stderr=subprocess.STDOUT, check=True)
        log = (out/'simulate.log').read_text()
        assert 'G0_FEC_WINDOW_CAPACITY_PASS' in log and 'Fatal:' not in log, log[-2200:]
        result = {'scope': __doc__, 'source_sha256': {n: hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},
                  'simulation': next(line for line in log.splitlines() if line.startswith('G0_FEC_WINDOW_CAPACITY_PASS')),
                  'full_PHY_DMA_RF_video_or_G0_passed': False}
        (out/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result), flush=True)
    finally:
        (out/'disk-after.json').write_text(json.dumps(check('after-FEC-window-capacity'), indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'reference', 'vivado-bin'):
        p.add_argument('--'+name, type=Path, required=True)
    main(p.parse_args())
