#!/usr/bin/env python3
"""Full-precision TX0 BPSK/repeat2 header arithmetic and bounded metadata oracle.
Independent direct integer dot/rotation, actual captured RRC/training fixtures,
full signed ranges, CRC/profile corruption, backpressure and pipeline revocation.
Finite G0 profile0xf0 only; no production profile or full receiver acceptance.
"""
import argparse,hashlib,json,math,random,shutil,struct,subprocess,zlib
from pathlib import Path
import numpy as np
from g0_packet_probe import phy_header,parse_phy,STREAM_BYTES
from workspace_storage_guard import check as disk_check,GIB
from fec_inverse_cost_probe import routed_measurements

MASK32=(1<<32)-1
SINE=[round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)]
COS=[round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)]
def signed(v,b):return (int(v)+(1<<(b-1)))%(1<<b)-(1<<(b-1))
def unpack(v,b,n=4):return [signed(int(v)>>(k*b),b) for k in range(n)]
def pack(v,b):return sum((int(x)% (1<<b))<<(k*b) for k,x in enumerate(v))
def math_oracle(lanes,h,step):
 rows=[];soft=[]
 for i,x in enumerate(lanes):
  real=sum(h[2*r]*x[2*r]+h[2*r+1]*x[2*r+1] for r in (0,1))
  imag=sum(h[2*r]*x[2*r+1]-h[2*r+1]*x[2*r] for r in (0,1))
  phase=(-step*(1152+i))&MASK32;bin=((phase+0x80000)&MASK32)>>20
  value=real*COS[bin]-imag*SINE[bin]
  assert -(1<<48)<=real<1<<48 and -(1<<48)<=imag<1<<48 and -(1<<67)<=value<1<<67
  rows.append((real%(1<<49))+((imag%(1<<49))<<49)+((value%(1<<68))<<98));soft.append(value)
 bits=[int(a+b<0) for a,b in zip(soft[::2],soft[1::2])]
 raw=np.packbits(bits,bitorder='big').tobytes();info,error=parse_phy(raw)
 return rows,raw,0 if info is not None else 1 if error=='CRC' else 2

def signal_rows(raw,h,step):
 bits=np.repeat(np.unpackbits(np.frombuffer(raw,dtype=np.uint8),bitorder='big'),2)
 rows=[]
 for i,bit in enumerate(bits):
  bin=(((step*(1152+i)&MASK32)+0x80000)&MASK32)>>20;c,s=COS[bin],SINE[bin];sgn=1-2*int(bit)
  row=[]
  for r in (0,1):
   row.extend([(sgn*(h[r*2]*c-h[r*2+1]*s)+(1<<19))//(1<<20),(sgn*(h[r*2]*s+h[r*2+1]*c)+(1<<19))//(1<<20)])
  assert all(-(1<<20)<=x<1<<20 for x in row);rows.append(row)
 return rows

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,cv=0,iv=0,ie=0,ready=0;reg [31:0] ci=0,step=0,ii=0;reg [103:0] h=0;reg [8:0] ix=0;reg [83:0] data=0;
wire cr,ir,valid;wire [31:0] oi,sid,sf;wire [2:0] error;wire [191:0] raw;wire [15:0] l0,l1;
bpsk_header_cost_probe dut(clk,rst,cancel,cv,cr,ci,h,step,iv,ir,ii,ix,ie,data,valid,ready,oi,error,raw,sid,sf,l0,l1,1'b0,32'd0,,,);
reg [83:0] vectors[0:@LAST@];reg [165:0] gold[0:@LAST@];
integer cycle=0,base=0,current=0,accepted=0,math_checks=0,results=0,revoked=0,held_checks=0,source_stalls=0,previous=-1,min_ii=10000,max_ii=0,products=0;
reg [322:0] view;
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst&&!cancel)begin
  if(iv&&!ir)source_stalls=source_stalls+1;
  if(iv&&ir&&!ie&&ii==dut.owner&&ix==dut.index)begin current=base+ix;accepted=accepted+1;if(previous>=0)begin if(cycle-previous<min_ii)min_ii=cycle-previous;if(cycle-previous>max_ii)max_ii=cycle-previous;end previous=cycle;end
  if(dut.multiply_enable)products=products+1;
  if(dut.state==3&&dut.issued==0)begin
   if({dut.zimag,dut.zreal}!==gold[current][97:0])$fatal(1,"header full dot oracle vec%0d",current);
  end
  if(dut.state==4)begin if(dut.soft_value!==gold[current][165:98])$fatal(1,"header full rotation oracle vec%0d",current);math_checks=math_checks+1;end
 end
end
task run_job(input integer number,input integer offset,input [103:0] ch,input [31:0] phase_step,input [191:0] expected,input [2:0] expected_error,input integer mode,input integer target,input integer cadence);
 integer k,limit;reg [31:0] id;begin
 id=32'hffffff00+32'(number);base=offset;previous=-1;ready=0;ci=id;h=ch;step=phase_step;cv=1;
 @(posedge clk);while(!cr)@(posedge clk);@(negedge clk);cv=0;ci=32'hdeadbeef;h=~ch;step=~phase_step;
 limit=ch==0?0:384;if(mode>=10&&mode<50)limit=mode==10?0:1;if(mode>=50&&mode<70)limit=16;
 for(k=0;k<limit;k=k+1)begin
  iv=1;ii=id;ix=9'(k);ie=0;data=vectors[offset+k];
  if((mode==1||mode==2||mode==3)&&k==target)begin ie=mode==1;ii=mode==2?id^32'h80000000:id;ix=mode==3?9'(k)^9'h100:9'(k);end
  @(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
  if(mode>=1&&mode<=3&&k==target)break;
  if(!(mode>=10&&mode<70))repeat(cadence>0?cadence-1:0)@(negedge clk);
 end
 if(mode>=10&&mode<70)begin
  if(mode==11||mode==12)while(!(dut.state==(mode==11?2:3)&&dut.issued==target))@(negedge clk);
  if(mode==13)while(dut.state!=4)@(negedge clk);
  if(mode>=50)while(!(dut.crc_busy&&dut.crc_left==target))@(negedge clk);
  if(mode>=60)rst=1;else cancel=1;
  repeat(2)@(negedge clk);rst=0;cancel=0;iv=0;revoked=revoked+1;
  repeat(32)begin @(negedge clk);if(valid||!cr||dut.product_valid)$fatal(1,"header cancelled pipeline revival");end
 end else begin
  while(!valid)@(negedge clk);
  if(oi!==id||error!==expected_error||raw!==expected)$fatal(1,"header result oracle job%0d error%0d/%0d raw%h/%h",number,error,expected_error,raw,expected);
  if(error==0)begin if(sid!==expected[63:32]||sf!==expected[95:64]||l0!=@STREAMBYTES@||l1!=@STREAMBYTES@)$fatal(1,"header trusted fields");end
  else if({sid,sf,l0,l1}!==96'd0)$fatal(1,"header untrusted metadata escaped");
  results=results+1;view={oi,error,raw,sid,sf,l0,l1};cv=1;
  repeat(33+number%17)begin @(negedge clk);if(!valid||cr||{oi,error,raw,sid,sf,l0,l1}!==view)$fatal(1,"header held result changed");held_checks=held_checks+1;end
  cv=0;
  if(mode==90)begin cancel=1;@(negedge clk);cancel=0;revoked=revoked+1;end
  else begin ready=1;@(negedge clk);ready=0;end
  repeat(2)@(negedge clk);if(valid||!cr)$fatal(1,"header result not drained");
 end
 end endtask
initial begin
 $readmemh("vectors.hex",vectors);$readmemh("math.hex",gold);repeat(3)@(negedge clk);rst=0;
 @JOBS@
 if(results!=@RESULTS@||revoked!=@REVOKED@||math_checks<@MINCHECKS@||min_ii!=26)$fatal(1,"header coverage result%0d revoke%0d math%0d ii%0d",results,revoked,math_checks,min_ii);
 $display("G0_BPSK_HEADER_PASS results=%0d revoked=%0d input_vectors=%0d exact_soft_checks=%0d products=%0d held_checks=%0d source_stalls=%0d minimum_input_II=%0d maximum_input_II=%0d",results,revoked,accepted,math_checks,products,held_checks,source_stalls,min_ii,max_ii);$finish;
end
initial begin #1000000000;$fatal(1,"header unit timeout");end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-bpsk-header');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rng=random.Random(0x5633c012);jobs=[];vectors=[];gold=[];actual=[]
  def add(lanes,h,step,kind,mode=0,target=0,cadence=1):
   offset=len(vectors);mathrows,raw,error=math_oracle(lanes,h,step);vectors.extend(pack(x,21) for x in lanes);gold.extend(mathrows)
   expected=raw;expected_error=error
   if mode in (1,2,3):expected=b'\0'*24;expected_error=3
   if not any(h):expected=b'\0'*24;expected_error=5
   jobs.append({'number':len(jobs),'offset':offset,'H':pack(h,26),'step':step&MASK32,'expected':int.from_bytes(expected,'little'),'error':expected_error,'kind':kind,'mode':mode,'target':target,'cadence':cadence})
   return raw,error
  meta=json.loads((ref/'fixture_metadata.json').read_text());filtered=np.memmap(ref/'filtered.hex',dtype='S21',mode='r')
  for m,model in enumerate(meta['models']):
   for w,window in enumerate(model['windows']):
    if not w:continue
    lanes=[]
    for i in range(384):
     p=window['peak']+(1152+i)*window['step'];tag,mu=divmod((p+32768)//65536,65536)
     row0=unpack(int(filtered[model['offset']+tag+20],16),20);row1=unpack(int(filtered[model['offset']+tag+21],16),20)
     lanes.append([(2*(x*(65536-mu)+y*mu)+32768)//65536 for x,y in zip(row0,row1)])
    h=unpack(window['H'],26);step=signed(window['phase_steps'],32);raw,error=add(lanes,h,step,'capture',cadence=1 if m%2==0 else 32)
    actual.append({'model':m,'window':w,'vector_offset':jobs[-1]['offset'],'raw_header':raw.hex(),'error':error})
  for k in range(24):
   h=[rng.randrange(-(1<<22),1<<22) for _ in range(4)];h[k%4]=0 if k%3==0 else h[k%4]
   step=rng.randrange(-(1<<28),1<<28);raw=phy_header(rng.getrandbits(32),MASK32 if k==0 else rng.getrandbits(32));lanes=signal_rows(raw,h,step)
   got,error=add(lanes,h,step,'synthetic_valid',cadence=[1,32,50][k%3]);assert got==raw and error==0
  base_h=[(1<<22)-1,-(1<<22),-1717,431];base_step=-0x111113;raw=phy_header(0xffffffff,0)
  for b in [0,1,2,3,12,13,14,15,16,17,18,19]:
   data=bytearray(raw[:20]);data[b]^=1;data=bytes(data);bad=data+struct.pack('<I',zlib.crc32(data));got,error=add(signal_rows(bad,base_h,base_step),base_h,base_step,'bad_profile');assert got==bad and error==2
  bad=bytearray(raw);bad[5]^=1;got,error=add(signal_rows(bad,base_h,base_step),base_h,base_step,'bad_crc');assert error==1
  bad=raw[:20]+raw[20:][::-1];got,error=add(signal_rows(bad,base_h,base_step),base_h,base_step,'CRC_byte_order');assert error==1
  for k in range(24):
   h=[rng.randrange(-(1<<25),1<<25) for _ in range(4)];h[k%4]=-(1<<25) if k%2 else (1<<25)-1
   lanes=[[rng.choice([-(1<<20),(1<<20)-1,rng.randrange(-(1<<20),1<<20)]) for _ in range(4)] for i in range(384)]
   add(lanes,h,rng.getrandbits(32),'full_signed_range')
  add([[0]*4 for _ in range(384)],[1,2,3,4],0x80000,'zero_soft_tie')
  lanes=[[123,-456,789,-987],[-123,456,-789,987]]*192;add(lanes,[1,2,3,4],0,'repeat_sum_tie')
  base_lanes=signal_rows(raw,base_h,base_step)
  for mode in [1,2,3]:
   for target in [0,177,383]:add(base_lanes,base_h,base_step,'bad_input',mode,target)
  add(base_lanes,[0]*4,base_step,'zero_H')
  add(base_lanes,base_h,base_step,'cancel_collect',10)
  for target in range(16):add(base_lanes,base_h,base_step,'cancel_dot_pipeline',11,target)
  for target in range(6):add(base_lanes,base_h,base_step,'cancel_rotation_pipeline',12,target)
  add(base_lanes,base_h,base_step,'cancel_decision',13)
  for target in range(1,9):add(base_lanes,base_h,base_step,'cancel_crc',50,target);add(base_lanes,base_h,base_step,'reset_crc',60,target)
  add(base_lanes,base_h,base_step,'held_result_cancel',90)
  add(base_lanes,base_h,base_step,'post_dirty_recovery')
  for name,values,width in [('vectors',vectors,21),('math',gold,42)]: (out/(name+'.hex')).write_text(''.join(f'{x:0{width}x}\n' for x in values))
  shutil.copyfile(ref/'sine.hex',out/'sine.hex');src=out/'bpsk_header_cost_probe.sv';shutil.copyfile(project/'sim/g0'/src.name,src)
  calls=[]
  for j in jobs:calls.append(f"run_job({j['number']},{j['offset']},104'h{j['H']:026x},32'h{j['step']:08x},192'h{j['expected']:048x},3'd{j['error']},{j['mode']},{j['target']},{j['cadence']});")
  results=sum(not 10<=j['mode']<70 for j in jobs);revoked=sum(10<=j['mode']<70 or j['mode']==90 for j in jobs)
  text=TB.replace('@STREAMBYTES@',str(STREAM_BYTES)).replace('@LAST@',str(len(vectors)-1)).replace('@JOBS@','\n'.join(calls)).replace('@RESULTS@',str(results)).replace('@REVOKED@',str(revoked)).replace('@MINCHECKS@',str((results-11)*384))
  (out/'tb.sv').write_text(text);(out/'actual_headers.json').write_text(json.dumps(actual,indent=2)+'\n')
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',str(src),str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','header_unit','-mt','off']);run('simulate',[str(bp/'xsim'),'header_unit','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_BPSK_HEADER_PASS' in log and 'Fatal:' not in log,log[-3500:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_BPSK_HEADER_PASS')),'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'actual_capture_headers':actual,'case_counts':{kind:sum(j['kind']==kind for j in jobs) for kind in sorted({j['kind'] for j in jobs})},'routed_OOC':None,'g0_passed':False}
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_param general.maxThreads 4\nread_verilog -sv {'+str(src)+'}\nsynth_design -top bpsk_header_cost_probe -part xc7z020clg484-1 -mode out_of_context\ncreate_clock -name processing -period 20 [get_ports clk]\nset_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]\nset_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]\nopt_design\nplace_design\nroute_design\nreport_utilization -file utilization.rpt\nreport_timing_summary -file timing.rpt -report_unconstrained\nreport_drc -file drc.rpt\n'
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);assert result['routed_OOC']['DSP']==1
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-bpsk-header'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
