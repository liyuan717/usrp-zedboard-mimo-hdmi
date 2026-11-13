#!/usr/bin/env python3
"""Exact signed16 differential-input RTL functional probe; no synthesis/OOC.

Python integers form each lag4 complex product and directly sum at most127
stride4 powers. No RTL output, RAM address, energy recurrence or pipeline
register supplies the expected values. All inputs here are synthetic ADC
vectors, including physical signed16 rails; actual captures are joint tests.
"""
import argparse
from collections import deque
import json
from pathlib import Path
import random
import re
import shutil
import subprocess

LOW_MASK = (1 << 48) - 1
MASK32 = (1 << 32) - 1


def pack(values, bits):
    return sum((int(x) & ((1 << bits) - 1)) << (bits * k) for k, x in enumerate(values))


def next_tag(tag, increment=1):
    return (tag & ~LOW_MASK) | ((tag + increment) & LOW_MASK)


class DirectOracle:
    def __init__(self):
        self.generation = self.fences = 0
        self.previous = None
        self.adc = deque(maxlen=5)
        self.z = deque(maxlen=505)
        self.ordinal = 0

    def control(self, reset, cycles=2):
        if reset:
            self.generation = self.fences = 0
        else:
            self.generation = (self.generation + cycles) & MASK32
        self.previous = None
        self.adc.clear()
        self.z.clear()
        self.ordinal = 0

    def accept(self, tag, lanes, start):
        if len(lanes) != 4 or any(not -32768 <= int(x) <= 32767 for x in lanes):
            raise ValueError('expected four physical signed16 ADC lanes')
        restart = self.previous is None or start or tag != next_tag(self.previous)
        if restart:
            self.fences = (self.fences + int(self.previous is not None)) & MASK32
            self.generation = (self.generation + 1) & MASK32
            self.adc.clear()
            self.z.clear()
            self.ordinal = 0
        self.previous = tag
        self.adc.append(tuple(map(int, lanes)))
        self.ordinal += 1
        result = None
        if len(self.adc) == 5:
            old, now = self.adc[0], self.adc[-1]
            z = []
            for rx in range(2):
                a, b = now[2 * rx:2 * rx + 2]
                c, d = old[2 * rx:2 * rx + 2]
                z.extend((a * c + b * d, b * c - a * d))
            self.z.append(z)
            # Direct finite convolution: deliberately no four-phase recurrence.
            energy = sum(component * component for v in list(self.z)[::-4] for component in v)
            assert all(-(1 << 32) <= x < (1 << 32) for x in z)
            assert 0 <= energy <= 127 * (1 << 63)
            # Packet low-to-high: window_valid1, energy70, z132, generation32,
            # endpoint tag64, start1; matches the TB's named-field concatenation.
            result = (int(self.ordinal == 5) << 299) | (tag << 235)
            result |= self.generation << 203 | pack(z, 33) << 71
            result |= energy << 1 | int(self.ordinal >= 509)
        return int(restart), self.generation, self.fences, self.ordinal, result


def make_vectors(out):
    rng = random.Random(710926)
    oracle = DirectOracle()
    rows, commands, segments = [], [], []
    group = 0

    def control(reset, stage=0):
        commands.append((3, 2 if reset else 1, stage, 2))
        oracle.control(reset)

    def mode(value):
        commands.append((2, value, 0, 0))

    def drain():
        commands.append((1, 0, 0, 0))

    def emit(tag, lanes, start=False, period=0):
        event, generation, fences, ordinal, result = oracle.accept(tag, lanes, start)
        rows.append((tag, pack(lanes, 16), int(start), event, generation, fences,
                     ordinal, int(result is not None), result or 0))
        commands.append((0, period, group, 0))

    def segment(name, first, count, values=None, start=True, cadence=False):
        nonlocal group
        group += 1
        offset = len(rows)
        for k in range(count):
            lanes = values(k) if values else [rng.randrange(-32768, 32768) for _ in range(4)]
            emit(next_tag(first, k), lanes, start=start and k == 0,
                 period=12 + k % 2 if cadence else 0)
        segments.append({'name': name, 'offset': offset, 'inputs': count, 'first_tag': first,
                         'mode': '12/13 clocks' if cadence else 'back-to-back requests'})

    # Both RX real components reach +2^31; p reaches +2^63, E reaches127*2^63.
    segment('all_negative_rails', (1 << 48) | 3, 1536, lambda k: [-32768] * 4)
    drain()
    rails = (-32768, -32767, -1, 0, 1, 32766, 32767)
    segment('varying_rails', (2 << 48) | 511, 1536,
            lambda k: [rails[(k // (7 ** lane)) % 7] for lane in range(4)])
    drain()
    mode(1)
    segment('random_with_output_backpressure', (3 << 48) | 151, 4096)
    drain()
    mode(0)
    segment('low48_wrap_cadence', (4 << 48) | (LOW_MASK - 699), 1400, cadence=True)
    drain()
    segment('zero_and_impulses', (5 << 48) | 39, 1050,
            lambda k: [0, 0, 0, 0] if k not in (0, 4, 508, 512, 700) else [-32768, 32767, 1, -1])
    drain()

    # Old output remains held while a first/start/gap/epoch/duplicate ADC is
    # accepted into a new window and the new work slot subsequently fills.
    for variant in range(4):
        control(True)
        mode(2)
        old_first = ((20 + variant) << 48) | 101
        segment('held_old_' + str(variant), old_first, 5)
        commands.append((5, 0, 0, 0))
        if variant == 0:
            new_first, start = next_tag(old_first, 5), True
        elif variant == 1:
            new_first, start = next_tag(old_first, 17), False
        elif variant == 2:
            new_first, start = ((30 + variant) << 48) | 106, False
        else:
            new_first, start = next_tag(old_first, 4), False
        segment('held_new_start_gap_epoch_repeat_' + str(variant), new_first, 5, start=start)
        commands.append((4, 24, 0, 0))
        mode(0)
        drain()

    # Exercise every active arithmetic/read/publication state under both
    # two-cycle cancel and reset, followed by a complete new energy window.
    for reset in (False, True):
        for stage in range(1, 8):
            control(True)
            mode(0)
            segment('before_control_' + str(int(reset)) + '_' + str(stage),
                    ((50 + stage) << 48) | (stage * 73 + 1), 5)
            control(reset, stage)
            segment('recovery_' + str(int(reset)) + '_' + str(stage),
                    ((70 + stage) << 48) | (LOW_MASK - 253 + stage), 520)
            drain()

    for reset in (False, True):
        control(True)
        mode(2)
        segment('held_control_' + str(int(reset)), (91 << 48) | 9, 5)
        control(reset, 8)
        mode(0)
        segment('held_recovery_' + str(int(reset)), (92 << 48) | 511, 520)
        drain()

    commands.append((9, 0, 0, 0))
    (out / 'vectors.txt').write_text(''.join(
        f'{t:016x} {d:016x} {s} {r} {g:08x} {f:08x} {o} {h} {v:075x}\n'
        for t, d, s, r, g, f, o, h, v in rows))
    (out / 'commands.txt').write_text(''.join(' '.join(map(str, row)) + '\n' for row in commands))
    return {'inputs': len(rows), 'segments': segments, 'seed': 710926,
            'expected_cancel_state_mask': 254, 'expected_reset_state_mask': 254,
            'input_provenance': 'synthetic physical signed16 ADC rails/random/zeros/impulses; no actual capture',
            'oracle': 'Python exact integer lag4 conjugate product; direct127-term stride4 sum, with no RTL recurrence',
            'maximum_power': 1 << 63, 'maximum_energy': 127 * (1 << 63),
            'first_z_ADC_count': 5, 'first_complete_window_ADC_count': 509}


TB = r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,iv=0,istart=0,ready=1;
 reg [63:0] tag=0,data=0;
 wire ir,ov,ostart,wvalid,restart;wire [63:0] otag;wire [31:0] ogen,agen,fences;
 wire [131:0] raw_z;wire [69:0] raw_energy;
 wire [131:0] z=raw_z ^ @ZFAULT@;
 wire [69:0] energy=raw_energy ^ @EFAULT@;
 wire [299:0] packet={ostart,otag,ogen,z,energy,wvalid};
 differential_input_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),
 .in_valid(iv),.in_ready(ir),.in_start(istart),.in_tag(tag),.in_data(data),
 .out_valid(ov),.out_ready(ready),.out_start(ostart),.out_tag(otag),
 .out_generation(ogen),.out_z(raw_z),.out_energy(raw_energy),.out_window_valid(wvalid),
 .fences(fences),.active_generation(agen),.history_restart(restart));
 integer cycle=0,consumer_mode=0,group=0,period=0,last_group=-1,last_accept=-100;
 integer input_fd,command_fd,n,kind,a,b,c,finished=0;
 integer gstart,grestart,gordinal,ghas;reg [31:0] ggen,gfences;
 reg [299:0] golden;
 reg [299:0] queue[0:@INPUTS@];integer ordinals[0:@INPUTS@];
 integer head=0,tail=0,accepted=0,expected_results=0,produced=0,consumed=0;
 integer active_retracted=0,held_retracted=0,input_stalls=0,output_stalls=0;
 integer restart_events=0,held_restarts=0,warmup_inputs=0,first_z=0,first_complete=0;
 integer fullscale_z=0,fullscale_power=0,fullscale_energy=0;
 integer intervals8=0,min8=999,max8=0,intervals1213=0,min1213=999,max1213=0;
 integer cancel_cycles=0,reset_cycles=0,cancel_mask=0,reset_mask=0,held_controls=0;
 integer metadata_checks=0,read_stalls=0,delta;
 reg [31:0] expected_generation=0,expected_fences=0;
 reg seen=0,held=0,input_held=0;
 reg [299:0] held_packet;
 reg [128:0] held_input;
 always @(negedge clk) begin
  #1;
  if(consumer_mode==2) ready=0;
  else if(consumer_mode==1) ready=!((cycle%97<31)||(cycle%113<17));
  else ready=1;
 end
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst||cancel) begin
   active_retracted=active_retracted+(tail-head)-(seen?1:0);
   if(seen) held_retracted=held_retracted+1;
   head=tail;seen=0;held=0;input_held=0;last_group=-1;
   if(rst) begin expected_generation=0;expected_fences=0;reset_cycles=reset_cycles+1;end
   else begin expected_generation=expected_generation+32'd1;cancel_cycles=cancel_cycles+1;end
   if(ov||ir||restart) $fatal(1,"cancel/reset did not retract handshake");
  end else begin
   if(held && (!ov || packet!==held_packet)) $fatal(1,"held differential response changed");
   if(input_held && (!iv || {istart,tag,data}!==held_input)) $fatal(1,"stalled ADC input changed");
   if(restart && !(iv&&ir)) $fatal(1,"restart without accepted ADC");
   if(ov&&!seen) begin
    if(head>=tail || packet!==queue[head])
     $fatal(1,"differential mismatch output%0d ordinal%0d packet%h expected%h",produced,ordinals[head],packet,queue[head]);
    produced=produced+1;seen=1;
    if(ostart) begin
     if(ordinals[head]!=5) $fatal(1,"first z not fifth ADC");
     first_z=first_z+1;
    end
    if(wvalid&&ordinals[head]==509) first_complete=first_complete+1;
    if(z[32:0]==33'h080000000 && z[98:66]==33'h080000000) fullscale_z=fullscale_z+1;
    if(energy==(70'd1<<63)) fullscale_power=fullscale_power+1;
    if(energy==(70'd127<<63)) fullscale_energy=fullscale_energy+1;
   end
   held=ov&&!ready;
   if(held) begin held_packet=packet;output_stalls=output_stalls+1;end
   if(ov&&ready) begin head=head+1;seen=0;consumed=consumed+1;end
   input_held=iv&&!ir;
   if(input_held) begin held_input={istart,tag,data};input_stalls=input_stalls+1;end
   if(iv&&ir) begin
    if(restart!==1'(grestart)) $fatal(1,"same-edge history restart differs from oracle");
    expected_generation=ggen;expected_fences=gfences;
    if(grestart) begin restart_events=restart_events+1;if(held) held_restarts=held_restarts+1;end
    accepted=accepted+1;
    if(ghas) begin queue[tail]=golden;ordinals[tail]=gordinal;tail=tail+1;expected_results=expected_results+1;end
    else warmup_inputs=warmup_inputs+1;
    if(group==last_group && gordinal>5 && consumer_mode==0) begin
     delta=cycle-last_accept;
     if(period==0) begin
      if(delta!=8) $fatal(1,"saturated ADC interval %0d",delta);
      intervals8=intervals8+1;if(delta<min8) min8=delta;if(delta>max8) max8=delta;
     end else begin
      if(delta!=period) $fatal(1,"cadenced ADC interval %0d wanted%0d",delta,period);
      intervals1213=intervals1213+1;
      if(delta<min1213) min1213=delta;if(delta>max1213) max1213=delta;
     end
    end
    last_group=group;last_accept=cycle;
   end
  end
  #1;
  metadata_checks=metadata_checks+1;
  if(agen!==expected_generation || fences!==expected_fences)
   $fatal(1,"window metadata mismatch generation%h/%h fences%h/%h",agen,expected_generation,fences,expected_fences);
 end
 task send_input(input integer requested_period,input integer requested_group);
 begin
  n=$fscanf(input_fd,"%h %h %d %d %h %h %d %d %h\n",tag,data,gstart,grestart,ggen,gfences,gordinal,ghas,golden);
  if(n!=9) $fatal(1,"ADC oracle vector parse");
  istart=1'(gstart);period=requested_period;group=requested_group;
  if(period!=0 && group==last_group)
   while(cycle-last_accept<period-1) @(negedge clk);
  iv=1;@(posedge clk);while(!ir) @(posedge clk);
  @(negedge clk);iv=0;
 end
 endtask
 task drain;
 begin
  while(head!=tail || ov || dut.state!=0) @(negedge clk);
  repeat(2) @(negedge clk);
 end
 endtask
 task control(input integer which,input integer stage,input integer count);
 begin
  if(stage==8) begin
   while(!ov) @(negedge clk);
   repeat(3) @(negedge clk);
   if(ready) $fatal(1,"held control without output backpressure");
   held_controls=held_controls+1;
  end else if(stage!=0) begin
   while(dut.state!=stage) @(negedge clk);
   if(which==1) cancel_mask=cancel_mask|(1<<stage);
   else reset_mask=reset_mask|(1<<stage);
  end
  if(which==1) cancel=1;else rst=1;
  repeat(count) @(negedge clk);
  cancel=0;rst=0;
 end
 endtask
 initial begin
  input_fd=$fopen("vectors.txt","r");command_fd=$fopen("commands.txt","r");
  repeat(3) @(negedge clk);rst=0;
  while(!finished) begin
   n=$fscanf(command_fd,"%d %d %d %d\n",kind,a,b,c);
   if(n!=4) $fatal(1,"command parse");
   case(kind)
    0:send_input(a,b);
    1:drain();
    2:consumer_mode=a;
    3:control(a,b,c);
    4:repeat(a) @(negedge clk);
    5:begin while(!ov) @(negedge clk);repeat(3) @(negedge clk);end
    9:finished=1;
    default:$fatal(1,"unknown command");
   endcase
  end
  consumer_mode=0;drain();repeat(4) @(negedge clk);
  if(accepted!=@INPUTS@ || expected_results!=produced+active_retracted ||
     produced!=consumed+held_retracted || active_retracted!=14 || held_retracted!=2 ||
     cancel_mask!=254 || reset_mask!=254 || held_controls!=2 || held_restarts!=4 ||
     min8!=8 || max8!=8 || intervals8<8000 || intervals1213<1300 ||
     min1213!=12 || max1213!=13 || first_complete<20 || first_z<25 ||
     fullscale_z<1000 || fullscale_power<4 || fullscale_energy<500 ||
     input_stalls==0 || output_stalls<1000 || cancel_cycles!=16)
   $fatal(1,"coverage count accepted%0d results%0d produced%0d consumed%0d active%0d held%0d masks%0d/%0d restartheld%0d firstcomplete%0d",accepted,expected_results,produced,consumed,active_retracted,held_retracted,cancel_mask,reset_mask,held_restarts,first_complete);
  $display("G0_DIFFERENTIAL_INPUT_PASS inputs=%0d expected_results=%0d produced=%0d consumed=%0d active_retracted=%0d held_retracted=%0d warmup_inputs=%0d first_z=%0d first_complete=%0d restart_events=%0d held_restarts=%0d fullscale_z=%0d fullscale_power=%0d fullscale_energy=%0d service_intervals=%0d min_ii=%0d max_ii=%0d cadence_intervals=%0d cadence_min=%0d cadence_max=%0d input_stalls=%0d output_stalls=%0d cancel_cycles=%0d reset_cycles=%0d cancel_state_mask=%0d reset_state_mask=%0d metadata_checks=%0d",accepted,expected_results,produced,consumed,active_retracted,held_retracted,warmup_inputs,first_z,first_complete,restart_events,held_restarts,fullscale_z,fullscale_power,fullscale_energy,intervals8,min8,max8,intervals1213,min1213,max1213,input_stalls,output_stalls,cancel_cycles,reset_cycles,cancel_mask,reset_mask,metadata_checks);
  $finish;
 end
 initial begin #100000000;$fatal(1,"simulation timeout");end
endmodule
'''


def main(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    vectors = make_vectors(out)
    (out / 'vectors.json').write_text(json.dumps(vectors, indent=2) + '\n')
    project = Path(__file__).resolve().parents[1]
    source = out / 'differential_input_cost_probe.sv'
    shutil.copyfile(project / 'sim/g0' / source.name, source)
    tb = TB.replace('@INPUTS@', str(vectors['inputs'])).replace('@ZFAULT@', "132'd0").replace('@EFAULT@', "70'd0")
    (out / 'tb.sv').write_text(tb)
    bp = args.vivado_bin.resolve()
    commands = []

    def run(name, argv):
        commands.append(argv)
        (out / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        with (out / (name + '.log')).open('w') as log:
            subprocess.run(argv, cwd=out, stdout=log, stderr=subprocess.STDOUT, check=True)

    run('compile', [str(bp / 'xvlog'), '--sv', str(source), str(out / 'tb.sv')])
    run('elaborate', [str(bp / 'xelab'), 'tb', '-s', 'differential_input_tb', '-mt', 'off'])
    run('simulate', [str(bp / 'xsim'), 'differential_input_tb', '-runall'])
    log = (out / 'simulate.log').read_text()
    marker = 'G0_DIFFERENTIAL_INPUT_PASS'
    if marker not in log or 'Fatal:' in log:
        raise ValueError('differential input functional simulation failed')
    measured = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', log.split(marker)[-1].split('\n')[0])}
    for name, old, new in [('z', "raw_z ^ 132'd0", "raw_z ^ 132'd1"),
                            ('energy', "raw_energy ^ 70'd0", "raw_energy ^ 70'd1")]:
        module = name + '_fault_tb'
        fault = tb.replace('module tb;', 'module ' + module + ';').replace(old, new)
        assert fault != tb and new in fault
        (out / (module + '.sv')).write_text(fault)
        run(name + '-fault-compile', [str(bp / 'xvlog'), '--sv', str(out / (module + '.sv'))])
        run(name + '-fault-elaborate', [str(bp / 'xelab'), module, '-s', module, '-mt', 'off'])
        run(name + '-fault-simulate', [str(bp / 'xsim'), module, '-runall'])
        negative = (out / (name + '-fault-simulate.log')).read_text()
        if 'Fatal: differential mismatch' not in negative or marker in negative:
            raise ValueError(name + ' bit-flip negative control escaped')
    receipt = {'scope': 'Standalone exact signed16 ADC differential and bounded stride4 energy, functional simulation only',
               'clock_hz': 50_000_000, 'measured': measured, 'vectors': vectors,
               'negative_controls_rejected': ['z bit', 'energy bit'], 'routed_OOC': None,
               'hardware_touched': False, 'new_RF_transmissions': False, 'g0_passed': False}
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({k: v for k, v in receipt.items() if k != 'vectors'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vivado-bin', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
