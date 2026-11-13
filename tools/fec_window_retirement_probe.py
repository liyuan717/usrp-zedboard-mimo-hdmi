#!/usr/bin/env python3
"""Characterize fixed8 admission progress across16 damaged blocks using real CRC/FEC RTL."""
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
localparam [31:0] BASE=32'h@BASE@;
reg clk=0;always #10 clk=~clk;reg rst=1,pa=0,cv=0,iv=0;reg [7:0] data=0;
wire cr,ir,sv,last,recovered,ab,ev;wire [7:0] od;wire [31:0] ss,sb,es,eb,accepted,erased,unmapped,descs,baddesc,busy,drops,expired;
wire [4:0] si;wire [8:0] so;wire [2:0] ec;wire [19:0] em,ed;reg sr=0,er=0,force_hold=0;
crc_fec_pipeline_cost_probe dut(clk,rst,pa,cv,cr,32'h56334601,32'd31,16'd548,iv,ir,data,
 sv,sr,od,ss,sb,si,so,last,recovered,ab,ev,er,es,eb,ec,em,ed,accepted,erased,unmapped,descs,baddesc,busy,drops,expired);
reg [7:0] bytes[0:19179],golden[0:8191];integer cycle=0,checked=0,held=0,j,k,terminals=0;
integer position[0:15];reg [15:0] seen=0;reg source_held=0,event_held=0;
reg [87:0] source_view;reg [106:0] event_view;
wire [87:0] current_source={ss,sb,si,so,last,recovered,od};wire [106:0] current_event={es,eb,ec,em,ed};
wire [31:0] logical_block=sb-BASE,terminal_block=dut.ingress.core_status_job-BASE;
always @(negedge clk)begin sr=!force_hold&&cycle%13>2;er=cycle%20000>18000;end
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(ab)$fatal(1,"window unrelated source abort");
  if(source_held&&(!sv||current_source!==source_view))$fatal(1,"window held source changed");
  if(event_held&&(!ev||current_event!==event_view))$fatal(1,"window held event changed");
  if(sv&&sr)begin
   if(ss!=32'h56334601||logical_block>=16||si||so!=position[logical_block]||seen[logical_block]||od!==golden[logical_block*512+so]||last!==(so==511)||recovered)$fatal(1,"window source oracle block%0d offset%0d",sb,so);
   position[logical_block]=position[logical_block]+1;checked=checked+1;if(last)seen[logical_block]=1;
  end
  if(dut.ingress.core_status_valid)begin
   if(terminal_block>=16||dut.ingress.core_code!=1||dut.ingress.core_delivered!==20'd1||dut.ingress.core_missing!==20'hffffe)$fatal(1,"window terminal partial-source contract block%0d code%0d",dut.ingress.core_status_job,dut.ingress.core_code);
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
initial begin
 $readmemh("bytes.hex",bytes);$readmemh("source.hex",golden);for(k=0;k<16;k=k+1)position[k]=0;
 repeat(5)@(negedge clk);rst=0;
 for(j=0;j<16;j=j+1)begin
  send(j*2);
  if(j==1)begin
   repeat(256)@(negedge clk);
   if(!sv||so!=128||!dut.ingress.bank_live[BASE[2:0]]||dut.ingress.bank_block[BASE[2:0]]!=BASE)$fatal(1,"window held source/owner lost sv%0d offset%0d live%h owner%0d",sv,so,dut.ingress.bank_live,dut.ingress.bank_block[BASE[2:0]]);
   force_hold=0;
  end
  send(j*2+1);
  if(j==0)begin
   k=cycle;while(!sv||so!=128)begin @(negedge clk);if(cycle-k>4000)$fatal(1,"window missing early source");end
   force_hold=1;sr=0;
   send(33);send(34);
   if(dut.ingress.range_first!=BASE||dut.ingress.bank_live!=8'hff||baddesc!=1||erased!=1)$fatal(1,"window bad/stale descriptor changed admission");
  end
  repeat(6000)@(negedge clk);
 end
 send(32);repeat(16000)@(negedge clk);
 if(checked!=@EXPECTED@*512||$countones(seen)!=@EXPECTED@||erased!=1||baddesc!=1||expired)$fatal(1,"window progress census bytes%0d seen%h accepted%0d unmapped%0d terminals%0d",checked,seen,accepted,unmapped,terminals);
 if(@EXPECTED@==16&&terminals!=16)$fatal(1,"window retire terminal census");
 $display("G0_FEC_WINDOW_CENSUS sources=%0d exact_bytes=%0d terminals=%0d accepted=%0d unmapped=%0d range_busy=%0d event_drop=%0d held=%0d cycles=%0d",$countones(seen),checked,terminals,accepted,unmapped,busy,drops,held,cycle);$finish;
end
initial begin #10000000;$fatal(1,"window bounded progress timeout");end
endmodule
'''


def main(a):
    v = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = check('before-fec-window-progress')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    assert before['disk']['available_bytes'] >= 5 * GIB
    try:
        rng = random.Random(0x7365616c)
        truth = [bytes(rng.randrange(256) for _ in range(512)) for _ in range(16)]
        packets = []
        for b in range(17):
            descriptor = struct.pack('<4sBBHBBHHHHHIBBHI', b'VRS3', 1, 0xf0, 0,
                                     20, 32, 512, 1024, 600, 20, 1, (a.base+b)&0xffffffff, 8, 0, 0, (a.base+b)&0xffffffff)
            bodies = [(3, descriptor + bytes(480))]
            if b < 16:
                bodies.append((1, truth[b]))
            for kind, payload in bodies:
                header = struct.pack('<4sBBHIIIIIHH', b'VRL3', 1, kind, 0,
                                     0x56334601, 31, len(packets), 0xffffffff, (a.base+b)&0xffffffff, 0, 512)
                body = header + payload
                packets.append(body + struct.pack('<I', zlib.crc32(body)))
        for serial, corrupt in (((a.base+1)&0xffffffff, True), ((a.base-1)&0xffffffff, False)):
            descriptor = struct.pack('<4sBBHBBHHHHHIBBHI', b'VRS3', 1, 0xf0, 0,
                                     20, 32, 512, 1024, 600, 20, 1, (a.base+32)&0xffffffff, 8, 0, 0, serial)
            header = struct.pack('<4sBBHIIIIIHH', b'VRL3', 1, 3, 0, 0x56334601, 31,
                                 len(packets), 0xffffffff, (a.base+32)&0xffffffff, 0, 512)
            body = header + descriptor + bytes(480)
            wire = bytearray(body + struct.pack('<I', zlib.crc32(body)))
            if corrupt:
                wire[-1] ^= 1
            packets.append(wire)
        (out / 'bytes.hex').write_text(''.join(f'{x:02x}\n' for p in packets for x in p))
        (out / 'source.hex').write_text(''.join(f'{x:02x}\n' for p in truth for x in p))
        (out / 'tb.sv').write_text(TB.replace('@EXPECTED@', str(a.expected_sources)).replace('@BASE@', f'{a.base:08x}'))
        for name in SOURCES:
            source = a.ingress_source if name == 'link_fec_ingress_cost_probe.sv' else v / 'sim/g0' / name
            shutil.copyfile(source, out / name)
        for name in ('generator.hex', 'inverse.hex'):
            shutil.copyfile(a.reference / name, out / name)
        bp = a.vivado_bin.resolve()
        for label, args in [('compile', [bp / 'xvlog', '--sv', *SOURCES, 'tb.sv']),
                            ('elaborate', [bp / 'xelab', 'tb', '-s', 'window_tb', '-mt', 'off']),
                            ('simulate', [bp / 'xsim', 'window_tb', '-runall'])]:
            with (out / (label + '.log')).open('w') as log:
                subprocess.run([str(x) for x in args], cwd=out, stdout=log, stderr=subprocess.STDOUT, check=True)
        log = (out / 'simulate.log').read_text()
        assert 'G0_FEC_WINDOW_CENSUS' in log and 'Fatal:' not in log
        result = {'scope': __doc__, 'expected_sources': a.expected_sources, 'base': a.base,
                  'all16_partial_blocks_continue': a.expected_sources == 16,
                  'simulation': next(x for x in log.splitlines() if x.startswith('G0_FEC_WINDOW_CENSUS')),
                  'source_sha256': {n: hashlib.sha256((out / n).read_bytes()).hexdigest() for n in SOURCES},
                  'full_PHY_DMA_RF_video_or_G0_passed': False}
        (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    finally:
        (out / 'disk-after.json').write_text(json.dumps(check('after-fec-window-progress'), indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'reference', 'ingress-source', 'vivado-bin'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--expected-sources', type=int, choices=(8, 16), required=True)
    p.add_argument('--base', type=lambda x: int(x, 0), choices=(0, 0xfffffff8), default=0)
    main(p.parse_args())
