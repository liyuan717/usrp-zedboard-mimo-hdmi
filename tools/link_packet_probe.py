#!/usr/bin/env python3
"""VRL3 PL CRC/bounds/private-buffer proof; no FEC/DMA or full G0 claim.
Independent zlib CRC and byte vectors include every single-bit corruption of a
maximum data packet, empty/control packets, semantic failures and dirty aborts.
"""
import argparse,hashlib,json,random,shutil,struct,subprocess,zlib
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,cancel=0,cv=0,iv=0,rr=0,ready=0;
reg [31:0] sid=0,sf=0;reg [15:0] size=0;reg [7:0] data=0;
wire cr,ir,rv,ov,last,active;wire [2:0] err;wire [255:0] header;wire [15:0] payload,oi;wire [7:0] od;
link_packet_cost_probe #(.SYMBOL_BYTES(@SIZE@),.MAX_PAYLOAD_BYTES(@SIZE@)) dut(clk,rst,cancel,cv,cr,sid,sf,size,iv,ir,data,rv,rr,err,header,payload,ov,ready,od,oi,last,active);
reg [7:0] bytes[0:@LAST@];integer fd,j,k,off,len,cfglen,expected,mode,target,r,cycle=0;
integer accepted=0,rejected=0,aborted=0,checked=0,held=0,di=0;reg metadata=0;
reg [255:0] wanted,previous_header;reg [2:0] previous_error;reg [15:0] previous_size;reg was_result_held=0,was_data_held=0;
reg [24:0] previous_data;reg [31:0] known;
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst||cancel)begin metadata=0;was_result_held=0;was_data_held=0;end
 else begin
  if(was_result_held&&(!rv||{err,header,payload}!=={previous_error,previous_header,previous_size}))$fatal(1,"link held metadata changed");
  if(was_data_held&&(!ov||{last,oi,od}!==previous_data))$fatal(1,"link held payload changed");
  if(rv)begin
   if(err!=expected||header!==(expected==3?256'd0:wanted)||payload!=(expected==0?cfglen-36:0))$fatal(1,"link CRC/bounds oracle job%0d got%0d expected%0d size%0d",j,err,expected,payload);
   if(rr)metadata=1;
  end
  if(ov)begin
   if(!metadata||expected!=0||oi!=di||od!==bytes[off+32+di]||last!==(di==len-37))$fatal(1,"link payload oracle job%0d byte%0d",j,di);
   if(ready)begin di=di+1;checked=checked+1;end
  end
  was_result_held=rv&&!rr;if(was_result_held)begin previous_error=err;previous_header=header;previous_size=payload;held=held+1;end
  was_data_held=ov&&!ready;if(was_data_held)begin previous_data={last,oi,od};held=held+1;end
 end
end
task revoke(input bit reset_it);
 begin iv=0;cv=0;ready=0;rr=0;if(reset_it)rst=1;else cancel=1;#1;
 if(cr||ir||rv||ov)$fatal(1,"link publication fence late");@(negedge clk);rst=0;cancel=0;
 if(active||rv||ov)$fatal(1,"link canceled ownership survived edge");aborted=aborted+1;
 end
endtask
initial begin
 $readmemh("bytes.hex",bytes);fd=$fopen("jobs.txt","r");repeat(4)@(negedge clk);rst=0;
 known=32'hffffffff;for(k=0;k<9;k=k+1)known=dut.crc_byte(known,8'h31+8'(k));if(~known!==32'hcbf43926)$fatal(1,"ISO-HDLC check value");
 for(j=0;j<@JOBS@;j=j+1)begin
  r=$fscanf(fd,"%d %d %d %d %d %d\n",off,len,cfglen,expected,mode,target);if(r!=6)$fatal(1,"link vector file");
  wanted=0;for(k=0;k<32&&k<len;k=k+1)wanted[k*8+:8]=bytes[off+k];di=0;metadata=0;rr=0;ready=0;
  while(!cr)@(negedge clk);sid=32'h89abcdef;sf=32'h80000001;size=16'(cfglen);cv=1;@(negedge clk);cv=0;sid=~sid;sf=~sf;size=0;
  for(k=0;k<len;k=k+1)begin
   if(mode%10==1&&k==target)break;
   if(k%17==3)@(negedge clk);
   iv=1;data=bytes[off+k];@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
  end
  if(mode%10==1)begin revoke(mode>=10);continue;end
  while(!rv)@(negedge clk);repeat(3+j%5)@(negedge clk);
  if(mode%10==2)begin revoke(mode>=10);continue;end
  rr=1;@(negedge clk);rr=0;
  if(mode%10==3)begin if(dut.state!=3)$fatal(1,"link cancel read not armed");revoke(mode>=10);continue;end
  if(expected==0&&cfglen>36)begin
   while(active)begin
    ready=mode%10==4?(di<target):(cycle%11>3);
    if(mode%10==4&&ov&&di==target)break;@(negedge clk);
   end
   if(mode%10==4)begin revoke(mode>=10);continue;end
   if(di!=len-36)$fatal(1,"link output census job%0d",j);
  end
  ready=0;if(active||rv||ov)$fatal(1,"link terminal owner held");
  if(expected==0)accepted=accepted+1;else rejected=rejected+1;
 end
 if(accepted!=@GOOD@||rejected!=@BAD@||aborted!=@ABORT@)$fatal(1,"link global census");
 $display("G0_LINK_PACKET_PASS symbol_bytes=@SIZE@ accepted=%0d rejected=%0d revoked=%0d exact_payload_bytes=%0d held_checks=%0d all_single_bit_positions=@BITS@",accepted,rejected,aborted,checked,held);$finish;
end
initial begin #2000000000;$fatal(1,"link timeout job%0d state%0d",j,dut.state);end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();n=a.symbol_bytes
 before=disk_check('before-link-packet');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rng=random.Random(0x43524332);vectors=[];jobs=[];cases=[]
  def packet(kind=1,index=0,payload=None,**kw):
   if payload is None:payload=rng.randbytes(n)
   fields=[b'VRL3',1,kind,kw.get('flags',0),kw.get('session',0x89abcdef),kw.get('sf',0x80000001),kw.get('seq',0xffffffff),kw.get('hint',0x12345678),kw.get('block',0x80000000),index,len(payload)]
   raw=struct.pack('<4sBBHIIIIIHH',*fields)+payload
   return raw+struct.pack('<I',zlib.crc32(raw))
  def add(raw,err=0,mode=0,target=0,cfg=None,label=''):
   jobs.append((len(vectors),len(raw),len(raw) if cfg is None else cfg,err,mode,target));vectors.extend(raw);cases.append({'label':label,'error':err,'mode':mode,'target':target})
  for index in [0,19,20,31]:add(packet(1 if index<20 else 2,index),label='data-index-boundary')
  for size in [0,1,7,n-1,n]:add(packet(5,payload=rng.randbytes(size),flags=0x8000),label='control-length/noncritical-flag')
  add(packet(3,payload=b''),label='empty-session');add(packet(payload=bytes(n)),label='zero-payload-data')
  for _ in range(16):add(packet(1,rng.randrange(20),seq=rng.getrandbits(32),block=rng.getrandbits(32)),label='independent-seq-block-identity')
  maximum=packet()
  for bit in range(len(maximum)*8):
   raw=bytearray(maximum);raw[bit//8]^=1<<(bit%8);add(raw,1,label='single-bit-'+str(bit))
  add(maximum[:-4]+maximum[-4:][::-1],1,label='CRC-endianness')
  for offset,value in [(0,0),(4,2),(5,4),(5,0),(8,0),(15,0),(30,1),(31,0xff),(28,32)]:
   raw=bytearray(maximum[:-4]);raw[offset]=value;add(raw+struct.pack('<I',zlib.crc32(raw)),2,label='CRC-valid-bad-header-'+str(offset))
  add(packet(1,20),2,label='source-index-k');add(packet(2,19),2,label='repair-index-below-k')
  add(packet(payload=bytes(n-1)),2,label='data-wrong-symbol-size');add(packet(5,payload=b''),3,cfg=35,label='descriptor-too-short')
  # Invalid descriptors publish immediately and consume no bytes.
  jobs[-1]=(jobs[-1][0],0,35,3,0,0)
  add(b'',3,cfg=n+37,label='descriptor-too-long')
  for mode,target in [(1,5),(1,200),(1,n+34),(2,0),(3,0),(4,0),(4,127),(4,n-1),(11,100),(12,0),(14,0),(14,n-1)]:add(maximum,mode=mode,target=target,label='dirty-cancel/reset')
  add(maximum,label='fresh-after-all-aborts')
  (out/'bytes.hex').write_text(''.join(f'{v:02x}\n' for v in vectors));(out/'jobs.txt').write_text(''.join(' '.join(map(str,j))+'\n' for j in jobs));(out/'cases.json').write_text(json.dumps(cases,indent=2)+'\n')
  good=sum(e==0 and m==0 for _,_,_,e,m,_ in jobs);bad=sum(e!=0 and m==0 for _,_,_,e,m,_ in jobs);aborts=sum(m!=0 for *_,m,_ in jobs)
  tb=TB
  for k,v in {'SIZE':n,'LAST':len(vectors)-1,'JOBS':len(jobs),'GOOD':good,'BAD':bad,'ABORT':aborts,'BITS':len(maximum)*8}.items():tb=tb.replace('@'+k+'@',str(v))
  (out/'tb.sv').write_text(tb);name='link_packet_cost_probe.sv';shutil.copyfile(project/'sim/g0'/name,out/name)
  for label,args in [('compile',[str(bp/'xvlog'),'--sv',name,'tb.sv']),('elaborate',[str(bp/'xelab'),'tb','-s','link_packet_tb','-mt','off']),('simulate',[str(bp/'xsim'),'link_packet_tb','-runall'])]:
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_LINK_PACKET_PASS' in log and 'Fatal:' not in log,log[-4000:]
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_LINK_PACKET_PASS')),'jobs':len(jobs),'source_sha256':{name:hashlib.sha256((out/name).read_bytes()).hexdigest()},'symbol_bytes':n,'CRC_reference':'Python zlib plus ISO-HDLC123456789 check value','g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(result['simulation'],flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-link-packet'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 p.add_argument('--symbol-bytes',type=int,choices=[512,1024],default=512);main(p.parse_args())
