#!/usr/bin/env python3
"""G0 true-port RAM + GF-MAC data comparison and XC7Z020 cost probe.

This does NOT implement matrix inversion, erasure/block scheduling, deadlines
or the complete PL FEC receiver. Those retain separate resource/latency budgets.
"""
import argparse
import json
from pathlib import Path
import subprocess
import numpy as np
from fec_oracle import multiply,inverse
from g0_packet_fec import encode,K,N,SIZE


def recovery_coefficients(source_index,selected):
    # Independent interpolation at source node (index+1), not the decoder's
    # Gaussian-elimination implementation or a copied inverse matrix.
    result=[]
    for col in selected:
        numerator=denominator=1
        for j in selected:
            if j!=col:
                numerator=multiply(numerator,(source_index+1)^(j+1))
                denominator=multiply(denominator,(col+1)^(j+1))
        result.append(multiply(numerator,inverse(denominator)))
    return result


def main(a):
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    rng=np.random.default_rng(3109361);commands=0;restored=0;readbacks=0
    with (out/'operations.txt').open('w') as f:
        def emit(kind,address,data=0,coefficient=0,first=0,last=0,expected=0):
            nonlocal commands
            f.write(f'{kind:x} {address:x} {int(data):02x} {coefficient:02x} {first:x} {last:x} {int(expected):02x}\n');commands+=1
        jobs=[]
        for bank in range(a.banks):
            source=rng.integers(0,256,(K,SIZE),dtype=np.uint8);coded=encode(source)
            if bank==0:erased=list(range(12))
            else:erased=sorted(rng.choice(N,bank%13,replace=False).tolist())
            valid=[i for i in range(N) if i not in erased];rng.shuffle(valid);selected=valid[:K]
            image=coded.copy()
            for idx in erased:image[idx]=0xa5
            for pos,value in enumerate(image.ravel()):emit(0,bank*N*SIZE+pos,value)
            jobs.append((source,coded,erased,selected))
        for bank,(source,coded,erased,selected) in enumerate(jobs):
            offsets=list(range(SIZE)) if bank==0 else sorted(set(list(range(0,SIZE,8))+[SIZE-1]))
            for row in (i for i in erased if i<K):
                coeffs=recovery_coefficients(row,selected)
                for byte in offsets:
                    for j,(idx,coefficient) in enumerate(zip(selected,coeffs)):
                        emit(1,bank*N*SIZE+idx*SIZE+byte,coded[idx,byte],coefficient,j==0,j==K-1,source[row,byte])
                    # DUT's observed result, not expected bytes, is written
                    # back by the testbench. Then read back via the real port.
                    address=bank*N*SIZE+row*SIZE+byte
                    emit(2,address)
                    emit(3,address,coefficient=1,first=1,last=1,expected=source[row,byte])
                    restored+=1;readbacks+=1
            # Check every bank's first/last received coordinates as well.
            for idx in (selected[0],selected[-1]):
                for byte in (0,SIZE-1):
                    emit(3,bank*N*SIZE+idx*SIZE+byte,coefficient=1,first=1,last=1,expected=coded[idx,byte]);readbacks+=1
    with (out/'gf.txt').open('w') as f:
        for x in range(256):
            for y in range(256):f.write(f'{x:02x} {y:02x} {multiply(x,y):02x}\n')
    aw=(a.banks*N*SIZE-1).bit_length()
    (out/'tb.sv').write_text(f'''`timescale 1ns/1ps
module tb;
 reg clk=0; always #10 clk=~clk;
 reg rst=1,load_we=0,iv=0,ow=0,first=0,last=0,ready=1;
 reg [{aw-1}:0] la=0,oa=0;
 reg [7:0] ld=0,od=0,coef=0;
 wire ir,ov;wire [7:0] result;
 fec_memory_cost_probe #(.BANKS({a.banks})) dut(clk,rst,load_we,la,ld,iv,ir,ow,oa,od,coef,first,last,ov,ready,result);
 integer fd,n,kind,address,data,c,fi,ls,expected,count=0,results=0,collisions=0;
 integer cycles=0,last_read=-100,min_ii=100,hold_cycles,i,gx,gy,ge,gf_count=0;
 reg [7:0] last_result;
 always @(posedge clk) cycles<=cycles+1;
 initial begin
  fd=$fopen("gf.txt","r");
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%h %h %h\\n",gx,gy,ge);
   if(n!=3 || dut.gf_mul(gx,gy)!==ge[7:0]) $fatal(1,"GF polynomial mismatch");
   gf_count=gf_count+1;
  end
  $fclose(fd);
  repeat(3) @(negedge clk);rst=0;
  fd=$fopen("operations.txt","r");
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%h %h %h %h %h %h %h\\n",kind,address,data,c,fi,ls,expected);
   if(n!=7) $fatal(1,"operation parse");
   if(kind==0) begin
    la=address;ld=data;load_we=1;
    @(negedge clk);load_we=0;
   end else begin
    oa=address;ow=(kind==2);od=last_result;coef=c;first=fi;last=ls;iv=1;
    if(ls) ready=0;
    // A same-address write is a real ownership hazard: request must stall.
    // Only ORIGINAL received basis bytes are rewritten. Readback checks
    // (kind3) never inject the expected reconstructed value into memory.
    if(kind==1 && count%31==0) begin
     la=address;ld=data;load_we=1;
     #1;if(ir) $fatal(1,"same-address collision accepted");
     @(negedge clk);load_we=0;collisions=collisions+1;
    end
    #1;while(!ir) @(negedge clk);
    if(kind==1) begin
     if(cycles-last_read<min_ii) min_ii=cycles-last_read;
     last_read=cycles;
    end
    @(negedge clk);iv=0;
    if(ls) begin
     while(!ov) @(negedge clk);
     if(result!==expected[7:0]) $fatal(1,"result mismatch op%0d addr%h got%h expected%h",count,address,result,expected);
     last_result=result;
     hold_cycles=(results%7==0)?4:0;
     repeat(hold_cycles) begin
      @(negedge clk);
      if(!ov || result!==last_result || ir) $fatal(1,"output changed under backpressure");
     end
     ready=1;@(negedge clk);results=results+1;
    end
   end
   count=count+1;
  end
  if(gf_count!=65536 || count!={commands} || collisions==0 || min_ii!=2) $fatal(1,"coverage/II mismatch");
  $display("G0_FEC_MEMORY_PASS operations=%0d results=%0d collisions=%0d min_read_ii=%0d gf=%0d",count,results,collisions,min_ii,gf_count);
  $finish;
 end
 initial begin #500000000;$fatal(1,"timeout");end
endmodule
''')
    executed=[]
    def run(name,argv):
        executed.append(argv);(out/'commands.json').write_text(json.dumps(executed,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(argv,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    binpath=a.vivado_bin.resolve();rtl=project/'sim/g0/fec_memory_cost_probe.sv'
    run('compile',[str(binpath/'xvlog'),'--sv',str(rtl),str(out/'tb.sv')])
    run('elaborate',[str(binpath/'xelab'),'tb','-s','fec_memory_tb','-mt','off'])
    run('simulate',[str(binpath/'xsim'),'fec_memory_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_FEC_MEMORY_PASS' not in sim:raise ValueError('data comparison did not pass')
    (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{rtl}}}
synth_design -top fec_memory_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic BANKS={a.banks}
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force fec_memory_cost_probe.dcp
''')
    run('ooc',[str(binpath/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    report={'scope':'G0 RAM+GF-MAC cost probe ONLY, not full FEC/controller/PL throughput acceptance',
        'banks':a.banks,'storage_bytes':a.banks*N*SIZE,'clock_hz':50000000,'operations':commands,
        'missing_source_bytes_reconstructed_and_written_back':restored,'RAM_readback_results':readbacks,
        'GF_independent_polynomial_cases':65536,'output_stall_and_same_address_hazard_test':True,
        'minimum_read_MAC_II_cycles':2,'matrix_inversion_and_block_control_implemented':False,
        'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--banks',type=int,choices=[8,16],default=8);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
