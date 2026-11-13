#!/usr/bin/env python3
"""Measure worst-phase SOURCE admission across a low-traffic32KiB DMA batch closure."""
import argparse,hashlib,json,shutil,struct,subprocess
from pathlib import Path
from dma_batch_stream_probe import decode_batch
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['source_record_bridge_cost_probe.sv','dma_batch_stream_cost_probe.sv']
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,run=0,sv=0,sl=0;reg [4:0] index=0;reg [8:0] offset=0;reg [7:0] data=0;
wire sr,rv,rr,rl;wire [63:0] rd;wire [7:0] rk;reg ready=0;wire bv,bl,busy;wire [63:0] bd;wire [7:0] bk;
wire [31:0] committed,dropped,bad,aborted,late,edrop,done,batches,records,rejected,timeouts;
source_record_bridge_cost_probe #(.ADMISSION_WAIT_CYCLES(@CAP@)) source(
clk,rst,1'b0,32'd1,sv,sr,data,32'h12345678,32'd0,index,offset,sl,index[0],1'b0,
1'b0,,32'd0,32'd0,3'd0,20'd0,20'd0,rd,rk,rv,rr,rl,committed,dropped,bad,aborted,late,edrop,done,1'b0,,64'd0,2'd0,1'b0,32'd0,32'd0,96'd0,,);
dma_batch_stream_cost_probe batch(clk,rst,run,rd,rk,rv,rr,rl,bd,bk,bv,ready,bl,busy,batches,records,rejected,timeouts);
integer cycle=0,max_wait=0,wait_cycles=0,fd,start;
always @(negedge clk)ready=cycle%19>4 && (cycle<48000 || cycle>=48000+@HOLD@);
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(sv&&!sr)begin wait_cycles=wait_cycles+1;if(wait_cycles>max_wait)max_wait=wait_cycles;end else wait_cycles=0;
  if(bv&&ready)$fwrite(fd,"%016h %0d\n",bd,bl);
 end
end
task send(input integer which);
 begin index=which;
  for(integer p=0;p<512;p=p+1)begin
   offset=p;data=(which*29+p*17)&255;sl=p==511;sv=1;
   @(posedge clk);while(!sr)@(posedge clk);@(negedge clk);sv=0;repeat(2)@(negedge clk);
  end
 end
endtask
initial begin
 fd=$fopen("batches.txt","w");repeat(5)@(negedge clk);rst=0;run=1;
 // Three SOURCE records arrive while padding owns the DMA output.
 // The third must fit without waiting for the stalled DMA.
 repeat(48000)@(negedge clk);send(0);send(1);send(2);
 start=cycle;while(done!=3)begin @(negedge clk);if(cycle-start>100000)$fatal(1,"coupled source drain");end
 run=0;while(busy)@(negedge clk);
 if(committed!=3||dropped||bad||aborted||edrop||rejected||timeouts||records!=3||max_wait!=0)$fatal(1,"coupled source batch capacity committed%0d dropped%0d max_wait%0d",committed,dropped,max_wait);
 $fclose(fd);$display("G0_SOURCE_BATCH_CAPACITY_PASS SOURCE=3 exact_bytes=1536 max_admission_wait=%0d cap=@CAP@ batches=%0d",max_wait,batches);$finish;
end
initial begin #5000000;$fatal(1,"coupled capacity global timeout");end
endmodule
'''

def main(a):
    if not 0<=a.hold_dma_cycles<=35000:raise ValueError('bounded DMA stall required')
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
    check=disk_check('before-SOURCE-batch-capacity');(out/'disk-before.json').write_text(json.dumps(check,indent=2)+'\n');assert check['disk']['available_bytes']>=5*GIB
    try:
        for n in SOURCES:shutil.copyfile(v/'sim/g0'/n,out/n)
        (out/'tb.sv').write_text(TB.replace('@CAP@',str(a.cap)).replace('@HOLD@',str(a.hold_dma_cycles)))
        def run(label,args):
            with (out/(label+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
        run('compile',[str(bp/'xvlog'),'--sv',*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','capacity_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'capacity_tb','-runall'])
        log=(out/'simulate.log').read_text();assert 'G0_SOURCE_BATCH_CAPACITY_PASS' in log and 'Fatal:' not in log,log[-3000:]
        records=[];data=bytearray();seq=0
        for row in (out/'batches.txt').read_text().splitlines():
            word,last=row.split();data.extend(int(word,16).to_bytes(8,'little'))
            if int(last):rs,_=decode_batch(bytes(data),seq);seq+=1;records.extend(rs);data.clear()
        assert not data and len(records)==3
        for i,r in enumerate(records):
            assert len(r)==576 and struct.unpack_from('<H',r,52)[0]==i
            assert r[64:]==bytes((i*29+p*17)&255 for p in range(512))
        receipt={'scope':__doc__,'simulation':next(x for x in log.splitlines() if x.startswith('G0_SOURCE_BATCH_CAPACITY_PASS')),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'input_byte_spacing_cycles':3,'DMA_ready_pattern':'14of19cycles','forced_DMA_stall_cycles':a.hold_dma_cycles,'no_SOURCE_loss':True,'independent_exact_bytes':1536,'physical_DMA_or_G0_passed':False}
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-SOURCE-batch-capacity'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--cap',type=int,default=32768)
    p.add_argument('--hold-dma-cycles',type=int,default=0)
    main(p.parse_args())
