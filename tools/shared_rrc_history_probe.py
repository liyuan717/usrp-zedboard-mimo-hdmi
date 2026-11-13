#!/usr/bin/env python3
"""Actual4096 XPM history shared by coherent metrics and the SAME F=1 sampler.
Insertion-ordered deep-history oracle plus simultaneous direct C/N/E and
weighted interpolation oracles. No duplicate sampler arithmetic implementation.
"""
import argparse,hashlib,json,re,shutil,subprocess
from pathlib import Path
from interpolation_cost_probe import TB as INTERPOLATION_TB
from coherent_window_probe import TB as COHERENT_TB,prepare
from workspace_storage_guard import check as disk_check,GIB

SOURCES=['coherent_window_cost_probe.sv','interpolation_cost_probe.sv']

def deep_tb():
 t=INTERPOLATION_TB.replace('@FRACTION@','1').replace('@FAULT@',"W'(0)").replace('@ACTUAL@','')
 for a,b in [('511','4095'),('512','4096'),('513','4097')]:t=re.sub(r'\b'+a+r'\b',b,t)
 t=t.replace('0000000200','0000001000').replace('0000000201','0000001001').replace('k<600;','k<4200;')
 t=t.replace("count!==10'(qcount)","count!==13'(qcount)")
 t=t.replace('wire [9:0] count;','wire [12:0] count;wire [9:0] legacy_count;wire pool_ready,pool_restart,rden;wire [11:0] raddr;wire [79:0] rword;wire [31:0] pool_generation;wire [63:0] pool_latest;')
 t=t.replace('.OUT_FRACTION_BITS(F))','.OUT_FRACTION_BITS(F),.EXTERNAL_HISTORY(1))').replace('.history_count(count),','.history_count(legacy_count),')
 end='.latest_tag(latest),.history_count(legacy_count),.fence_count(fences));'
 assert end in t
 t=t.replace(end,'''.latest_tag(latest),.history_count(legacy_count),.fence_count(fences),
 .shared_ready(pool_ready),.shared_generation(pool_generation),.shared_latest(pool_latest),.shared_count(count),.shared_restart(pool_restart),
 .shared_read_enable(rden),.shared_read_address(raddr),.shared_read_word(rword));
coherent_window_cost_probe pool(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(iv),.in_ready(pool_ready),.in_start(istart),.in_tag(it),.in_data(idata),
 .external_read_enable(rden),.external_read_address(raddr),.read_word(rword),.cfg_valid(1'b0),.cfg_ready(),.cfg_id(32'd0),.cfg_generation(32'd0),.cfg_first_tag(64'd0),.cfg_points(7'd0),
 .out_valid(),.out_ready(1'b1),.out_id(),.out_generation(),.out_tag(),.out_point(),.out_last(),.out_error(),.out_c(),.out_n(),.out_e(),
 .history_generation(pool_generation),.latest_tag(pool_latest),.history_count(count),.history_restart(pool_restart),.busy());
always @(posedge clk)begin #2;if(legacy_count!==(count>512?10'd512:count[9:0]))$fatal(1,"legacy count view");end''')
 return t

SAMPLER=r'''
reg srv=0,sready=0;reg [31:0] sgen=0,sid=0;reg [63:0] stag=0;reg [15:0] smu=0;
wire srr,sov,sidle;wire [31:0] sog,soid;wire [63:0] sot;wire [15:0] somu;wire [2:0] serr;wire [83:0] sdata;wire [9:0] legacy_count;
interpolation_cost_probe #(.EXTERNAL_HISTORY(1)) sampler(
 .clk(clk),.rst(rst),.cancel(cancel),.in_valid(iv),.in_ready(),.in_start(start),.in_tag(tag),.in_data(data),
 .req_valid(srv),.req_ready(srr),.req_generation(sgen),.req_id(sid),.req_tag(stag),.req_mu(smu),
 .out_valid(sov),.out_ready(sready),.out_generation(sog),.out_id(soid),.out_tag(sot),.out_mu(somu),.out_error(serr),.out_data(sdata),
 .history_generation(),.latest_tag(),.history_count(legacy_count),.fence_count(),.request_cancel(1'b0),.request_idle(sidle),.history_restart(),
 .shared_ready(ir),.shared_generation(hgen),.shared_latest(latest),.shared_count(hc),.shared_restart(restart),
 .shared_read_enable(ext),.shared_read_address(addr),.shared_read_word(word));
integer requests=0,responses=0,sample_errors=0,revoked=0,r=0,target_index,request_age,depth_reads=0,coincident=0;
reg sp=0,seen=0,sheld=0,forced_hold=0;reg [31:0] eg,eid;reg [63:0] et;reg [15:0] emu;reg [83:0] expected_sample;reg [230:0] shold;
wire [230:0] sview={sog,soid,sot,somu,serr,sdata};
reg signed [63:0] a,b,v;integer lane,index0;
always @(posedge clk)begin
 if(rst||cancel)begin if(sp)revoked=revoked+1;sp=0;seen=0;sheld=0;end
 else begin
  if(sheld&&(!sov||sview!==shold))$fatal(1,"shared sampler held response");
  if(sov&&!seen)begin
   if(!sp || sog!=eg || soid!=eid || sot!==et || somu!==emu)$fatal(1,"shared sampler identity");
   if(serr==0)begin if(sdata!==expected_sample)$fatal(1,"shared sampler data");end
   else begin if(serr!=1 || sog==hgen || sdata)$fatal(1,"shared sampler error");sample_errors=sample_errors+1;end
   seen=1;responses=responses+1;
  end
  sheld=sov&&!sready;if(sheld)shold=sview;
  if(sov&&sready)begin sp=0;seen=0;end
  if(srv&&srr)begin
   if(sp)$fatal(1,"shared sampler capacity");sp=1;eg=sgen;eid=sid;et=stag;emu=smu;requests=requests+1;
   index0=48'(stag[47:0]-BASE);if(index0<0||index0>=12499)$fatal(1,"shared oracle bounds");
   request_age=48'(latest[47:0]-stag[47:0]);if(request_age>512)depth_reads=depth_reads+1;
   for(lane=0;lane<4;lane=lane+1)begin
    a=$signed(words[index0][lane*20+:20]);b=$signed(words[index0+1][lane*20+:20]);
    v=(2*(a*(65536-smu)+b*smu)+32768)>>>16;expected_sample[lane*21+:21]=v[20:0];
   end
  end
  if(ext&&dut.own_request)coincident=coincident+1;
 end
end
initial begin
 while(rst)@(negedge clk);
 while(!source_done&&!cancel)begin
  while(hc<4&&!source_done&&!cancel)@(negedge clk);
  if(!source_done&&!cancel)begin
   request_age=3+r%3070;if(request_age>hc-2)request_age=hc-2;
   stag={latest[63:48],latest[47:0]-48'(request_age)};sgen=hgen;sid=r;smu=16'(7919*r);srv=1;
   @(posedge clk);while(!srr&&!cancel)@(posedge clk);@(negedge clk);srv=0;
   while(!sov&&!cancel)@(negedge clk);
   if(!cancel)begin
    if(!forced_hold && 48'(latest[47:0]-BASE)>=6980 && 48'(latest[47:0]-BASE)<7000)begin forced_hold=1;repeat(200)@(negedge clk);end
    else if(r%23==0)repeat(41)@(negedge clk);
    sready=1;@(negedge clk);sready=0;r=r+1;
   end
  end
 end
end
'''

def concurrent_tb():
 t=COHERENT_TB.replace('ready=1,ext=0','ready=1').replace('reg [11:0] addr=0;','wire ext;wire [11:0] addr;')
 start=t.index('always @(negedge clk) begin\n ext=');end=t.index('always @(posedge clk) begin',start);t=t[:start]+SAMPLER+'\n'+t[end:]
 t=t.replace("lookup=48'(latest[47:0]-BASE-48'd3);","lookup=48'(sampler.work_tag[47:0]-BASE+(sampler.state==2?48'd1:48'd0));")
 old='if(metrics!=67 || errors!=6 || external_reads<1000 || max_age>=4096 || ov)'
 assert old in t;t=t.replace(old,'if(requests<1000 || responses+revoked!=requests || depth_reads<100 || coincident<100 || metrics!=67 || errors!=6 || external_reads<1000 || max_age>=4096 || ov)')
 t=t.replace('$display("G0_COHERENT_WINDOW_PASS', '$display("G0_SHARED_SAMPLER_PASS requests=%0d responses=%0d revoked=%0d deep_reads=%0d contested_reads=%0d",requests,responses,revoked,depth_reads,coincident);\n $display("G0_COHERENT_WINDOW_PASS')
 return t

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();project=Path(__file__).resolve().parents[1]
 before=disk_check('before-shared-RRC-history');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  concurrent=out/'concurrent';concurrent.mkdir();prepare(concurrent);deep=out/'deep';deep.mkdir();shutil.copyfile(concurrent/'coherent_signs.hex',deep/'coherent_signs.hex')
  def execute(d,tb,mutate=None):
   sources=[]
   for name in SOURCES:
    src=d/name;shutil.copyfile(project/'sim/g0'/name,src);sources.append(src)
   if mutate:
    name,old,new=mutate;s=d/name;text=s.read_text();assert text.count(old)==1;s.write_text(text.replace(old,new))
   (d/'tb.sv').write_text(tb);xpm=bp.parent/'data/ip/xpm'
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*map(str,sources),str(d/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','shared_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'shared_tb','-runall'])
   return (d/'simulate.log').read_text()
  deep_log=execute(deep,deep_tb());assert 'G0_INTERPOLATION_PASS' in deep_log and 'Fatal:' not in deep_log,deep_log[-2500:]
  log=execute(concurrent,concurrent_tb());assert 'G0_SHARED_SAMPLER_PASS' in log and 'G0_COHERENT_WINDOW_PASS' in log and 'Fatal:' not in log,log[-2500:]
  controls=[]
  if a.negative_controls:
   mutations={'priority':('coherent_window_cost_probe.sv','own_request && !external_read_enable && !owner_lost','own_request && !owner_lost'),
    'address_width':('interpolation_cost_probe.sv','assign shared_read_address=read_address;',"assign shared_read_address={3'd0,read_address[8:0]};")}
   for name,change in mutations.items():
    d=out/name;d.mkdir()
    for f in ['words.hex','expected.hex','coherent_signs.hex']:shutil.copyfile(concurrent/f,d/f)
    try:bad=execute(d,concurrent_tb(),change)
    except subprocess.CalledProcessError:bad=(d/'simulate.log').read_text() if (d/'simulate.log').exists() else ''
    witnesses=['Fatal: coherent metric mismatch','Fatal: shared sampler data','Fatal: priority sampler read'];found=next((x for x in witnesses if x in bad),None);assert found and 'G0_SHARED_SAMPLER_PASS' not in bad,(name,bad[-2000:]);controls.append({'name':name,'detected_by':found})
  result={'scope':__doc__,'deep_simulation':next(s for s in deep_log.splitlines() if s.startswith('G0_INTERPOLATION_PASS')),'concurrent_simulation':[s for s in log.splitlines() if s.startswith(('G0_SHARED_SAMPLER_PASS','G0_COHERENT_WINDOW_PASS'))],
   'source_sha256':{n:hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest() for n in SOURCES},'negative_controls':controls,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-shared-RRC-history'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--negative-controls',action='store_true');main(p.parse_args())
