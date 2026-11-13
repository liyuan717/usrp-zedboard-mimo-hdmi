#!/usr/bin/env python3
"""G0 bit-exact arithmetic and OOC implementation on XC7Z020, not PHY acceptance."""
import argparse
import json
from pathlib import Path
import random
import subprocess


def ml(y, h):
    metrics = []
    for bits in range(16):
        x = [complex(-1 if bits & (1 << j) else 1, -1 if bits & (1 << (j + 1)) else 1) for j in (0, 2)]
        predicted = [h[0] * x[0] + h[1] * x[1], h[2] * x[0] + h[3] * x[1]]
        metrics.append(sum(int((v.real - p.real)**2 + (v.imag - p.imag)**2) for v, p in zip(y, predicted)))
    return min(range(16), key=metrics.__getitem__), min(metrics)


def pack(values, width):
    return sum((int(v) & ((1 << width) - 1)) << (i * width) for i, v in enumerate(values))


def main(a):
    root = Path(__file__).resolve().parents[1]
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    rng = random.Random(9361310)
    cases = [([0j, 0j], [0j] * 4), ([complex(-32768,-32768)] * 2, [complex(-8192,-8192)] * 4),
             ([complex(32767,32767)] * 2, [complex(-8192,8191)] * 4)]
    for _ in range(1000):
        cases.append(([complex(rng.randrange(-32768,32768), rng.randrange(-32768,32768)) for _ in range(2)],
                      [complex(rng.randrange(-8192,8192), rng.randrange(-8192,8192)) for _ in range(4)]))
    with (out / "vectors.txt").open("w") as f:
        for y,h in cases:
            d,m = ml(y,h)
            f.write(f"{pack([v for z in y for v in (z.real,z.imag)],16):016x} {pack([v for z in h for v in (z.real,z.imag)],14):028x} {d:x} {m:09x}\n")
    (out / "tb.sv").write_text('''`timescale 1ns/1ps
module tb;
 reg clk=0; always #10 clk=~clk;
 reg rst=1, iv=0, ready=1;
 reg [63:0] y; reg [111:0] h;
 wire ir, ov; wire [3:0] d; wire [35:0] m;
 reg [3:0] ed; reg [35:0] em;
 integer fd, n, count=0, cycles=0, last_accept=-100, min_ii=100;
 ml_cost_probe dut(clk,rst,iv,ir,y,h,ov,ready,d,m);
 always @(posedge clk) cycles <= cycles+1;
 initial begin
  repeat(3) @(negedge clk); rst=0;
  fd=$fopen("vectors.txt","r");
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%h %h %h %h\\n",y,h,ed,em);
   if(n!=4) $fatal(1,"vector read");
   iv=1;
   while(!ir) @(negedge clk);
   if(cycles-last_accept<min_ii) min_ii=cycles-last_accept;
   last_accept=cycles;
   @(negedge clk); iv=0;
   while(!ov) @(negedge clk);
   if(d!==ed || m!==em) $fatal(1,"ML mismatch case %d got %h/%h expected %h/%h",count,d,m,ed,em);
   // Exercise backpressure without consuming or changing the result.
   if(count%7==0) begin
    ready=0;
    repeat(5) begin
     @(negedge clk);
     if(!ov || d!==ed || m!==em || ir) $fatal(1,"output changed under stall");
    end
    ready=1;
   end
   count=count+1;
   @(negedge clk);
  end
  $display("G0_ML_PASS cases=%0d min_ii=%0d",count,min_ii); $finish;
 end
 initial begin #1000000; $fatal(1,"timeout"); end
endmodule
''')
    commands=[]
    def run(name, argv):
        commands.append(argv)
        (out / "commands.json").write_text(json.dumps(commands,indent=2)+"\n")
        with (out/(name+".log")).open("w") as f:
            subprocess.run(argv,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
    binpath = a.vivado_bin.resolve()
    rtl = root / "sim/g0/ml_cost_probe.sv"
    run("compile", [str(binpath/"xvlog"),"--sv",str(rtl),str(out/"tb.sv")])
    run("elaborate", [str(binpath/"xelab"),"tb","-s","ml_tb","-mt","off"])
    run("simulate", [str(binpath/"xsim"),"ml_tb","-runall"])
    if "G0_ML_PASS cases=1003" not in (out/"simulate.log").read_text():
        raise ValueError("simulation pass marker missing")
    (out/"ooc.tcl").write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{rtl}}}
synth_design -top ml_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports {{rst in_valid y_in[*] h_in[*] out_ready}}]
set_output_delay 2.0 -clock processing [get_ports {{in_ready out_valid decision[*] metric_out[*]}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force ml_cost_probe.dcp
''')
    run("ooc",[str(binpath/"vivado"),"-mode","batch","-source",str(out/"ooc.tcl"),"-nolog","-nojournal"])
    (out/"receipt.json").write_text(json.dumps({"arithmetic_cases":len(cases),"output_stall_test":True,
        "scope":"G0 isolated arithmetic probe; no acquisition, channel tracker or complete receiver",
        "part":"xc7z020clg484-1","clock_period_ns":20,"g0_passed":False},indent=2)+"\n")
    print("G0 isolated ML simulation and OOC completed; inspect timing/utilization reports")


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vivado-bin",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    main(p.parse_args())
