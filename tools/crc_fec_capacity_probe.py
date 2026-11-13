#!/usr/bin/env python3
"""Actual CRC-good range collision must preserve all8 owners and resume service."""
import argparse,hashlib,json,os,shutil,struct,subprocess,zlib
from pathlib import Path
from crc_fec_pipeline_probe import SOURCES,SID,BASE
from workspace_storage_guard import check as disk_check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,pa=0,cv=0,iv=0;reg [7:0] data=0;
wire cr,ir,sv,last,recovered,ab,ev;wire [7:0] od;wire [31:0] ss,sb,es,eb,accepted,erased,unmapped,descs,baddesc,busy,drops,expired;
wire [4:0] si;wire [8:0] so;wire [2:0] ec;wire [19:0] em,ed;reg sr=0,er=0;
crc_fec_pipeline_cost_probe dut(clk,rst,pa,cv,cr,32'h89abcdef,32'h80000001,16'd548,iv,ir,data,
 sv,sr,od,ss,sb,si,so,last,recovered,ab,ev,er,es,eb,ec,em,ed,accepted,erased,unmapped,descs,baddesc,busy,drops,expired);
reg [7:0] bytes[0:2739],golden[0:511];integer cycle=0,checked=0,held=0,j,k,age;
reg event_held=0;reg [106:0] event_view;wire [106:0] current_event={es,eb,ec,em,ed};
always @(negedge clk)sr=cycle%13>2;
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(ab)$fatal(1,"capacity unrelated source abort");
  if(event_held&&(!ev||current_event!==event_view))$fatal(1,"capacity held event changed");
  if(sv&&sr)begin
   if(ss!=32'h89abcdef||sb!=32'hfffffffc||si||so!=checked||od!==golden[checked]||last!==(checked==511)||recovered)$fatal(1,"capacity resumed source oracle");checked=checked+1;
  end
  event_held=ev&&!er;if(event_held)begin event_view=current_event;held=held+1;end
 end else event_held=0;
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
  if(dut.ingress.bank_live!=8'hff)$fatal(1,"capacity old bank lost");
  for(integer b=0;b<8;b=b+1)if(dut.ingress.bank_block[(b+4)%8]!==32'hfffffffc+32'(b)||dut.ingress.fec.bank_job[(b+4)%8]!==32'hfffffffc+32'(b))$fatal(1,"capacity live owner overwritten");
 end
endtask
initial begin
 $readmemh("bytes.hex",bytes);$readmemh("source.hex",golden);repeat(5)@(negedge clk);rst=0;
 send(0);owners();age=dut.ingress.fec.bank_age[4];
 send(1);owners();if(busy!=8||drops!=7||!ev||es!=32'h89abcdef||eb!=4||ec!=6||em!=20'hfffff||ed)$fatal(1,"capacity collision event census");
 send(1);owners();if(busy!=16||drops!=15||dut.ingress.fec.bank_age[4]<=age)$fatal(1,"capacity duplicate blocked declaration lifetime");
 send(2);send(3);owners();if(unmapped!=2||checked||dut.ingress.fec.bank_count[4])$fatal(1,"capacity wrong-window bytes committed");
 send(4);owners();if(dut.ingress.range_serial!=0||dut.ingress.range_first!=32'hfffffffc)$fatal(1,"capacity serial wrap restore");
 send(3);while(checked<512)@(negedge clk);owners();
 if(busy!=16||drops!=15||erased||baddesc||descs!=4||expired||dut.ingress.fec.bank_count[4]!=1)$fatal(1,"capacity final census");
 er=1;while(ev)@(negedge clk);
 $display("G0_CRC_FEC_CAPACITY_PASS banks=8 blocked_declarations=16 held_events=1 dropped_events=15 wrong_window_packets=2 resumed_exact_bytes=%0d held_checks=%0d",checked,held);$finish;
end
initial begin #10000000;$fatal(1,"capacity local service timeout");end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];ref=a.reference.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 before=disk_check('before-crc-fec-capacity');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  positive=json.loads((ref/'receipt.json').read_text());hashes=positive['source_sha256'];assert positive['simulation'].startswith('G0_CRC_FEC_PIPELINE_PASS')
  for n in SOURCES:assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==hashes[n];shutil.copyfile(ref/n,out/n)
  for n in ['generator.hex','inverse.hex']:os.link(ref/n,out/n)
  source=bytes(int(x,16) for x in (ref/'source.hex').read_text().splitlines()[:512]);packets=[]
  def descriptor(first,serial):return struct.pack('<4sBBHBBHHHHHIBBHI',b'VRS3',1,0xf0,0,20,32,512,1024,600,20,1,first,8,0,0,serial)
  for kind,block,payload in [(3,0,descriptor(BASE,0xfffffffe)),(3,0,descriptor(4,0xffffffff)),(1,4,source),(1,BASE,source),(3,0,descriptor(BASE,0))]:
   payload=payload+bytes(512-len(payload));body=struct.pack('<4sBBHIIIIIHH',b'VRL3',1,kind,0,SID,0x80000001,len(packets)*19,0xffffffff,block,0,512)+payload;packets.append(body+struct.pack('<I',zlib.crc32(body)))
  (out/'bytes.hex').write_text(''.join(f'{b:02x}\n' for p in packets for b in p));(out/'source.hex').write_text(''.join(f'{b:02x}\n' for b in source));(out/'tb.sv').write_text(TB)
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','capacity_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'capacity_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_CRC_FEC_CAPACITY_PASS' in log and 'Fatal:' not in log,log[-3500:]
  result={'scope':__doc__,'simulation':next(x for x in log.splitlines() if x.startswith('G0_CRC_FEC_CAPACITY_PASS')),'source_sha256':hashes,'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-crc-fec-capacity'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
