#!/usr/bin/env python3
"""Replay tagged raw through qualified55RTL copies; optionally test an explicit gate-only variant."""
import argparse,hashlib,json,os,re,shutil,struct,subprocess
from pathlib import Path
import numpy as np
from g0_source_receiver_probe import TB
from dma_batch_stream_probe import decode_batch
from workspace_storage_guard import check,GIB
from g0_coarse_gate_implementation_probe import gate10

MONITOR=r'''
`define F dut.receiver.core.core.core
`define A `F.receiver.acquisition
integer coarse_events=0,start_good=0,start_bad=0,header_good=0,header_bad=0,normal_h=0,link_frames=0,link_good_total=0,link_bad_total=0,timing_events=0;
reg link_was_done=0;
always @(posedge clk)if(!resetn)link_was_done=0;else begin
 if(`A.coarse_valid && `A.coarse_accept)begin
  coarse_events=coarse_events+1;
  if(coarse_events<=12)$display("D_COARSE n%0d err%0d first%h peak%h step%h sense%0d",coarse_events,`A.candidate_error,`A.candidate_first,`A.candidate_peak,`A.candidate_step,`A.candidate_sense);
 end
 if(`A.status_valid && `A.status_ready)begin
  if(`A.status_error==0)start_good=start_good+1;else start_bad=start_bad+1;
  if(start_good+start_bad<=12)$display("D_START good%0d bad%0d err%0d detail%0d",start_good,start_bad,`A.status_error,`A.status_detail);
 end
 if(`F.header_valid && `F.header_sink_ready)begin
  if(`F.header_error==0)header_good=header_good+1;else header_bad=header_bad+1;
  if(header_good+header_bad<=12)$display("D_HEADER good%0d bad%0d err%0d detail%0d raw%h",header_good,header_bad,`F.header_error,`F.header_detail,`F.header_raw);
 end
 if(`F.core_normal_valid && `F.core_normal_ready)normal_h=normal_h+1;
 if(`F.link_done && !link_was_done)begin link_frames=link_frames+1;link_good_total=link_good_total+`F.link_good;link_bad_total=link_bad_total+`F.link_erased;end
 link_was_done=`F.link_done;
 if(`F.timing_update)timing_events=timing_events+1;
end
'''

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False);base=a.baseline.resolve();proof=json.loads((base/'receipt.json').read_text())
 before=check('before-fresh-raw55-diagnostic');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  if a.qualified_raw_regression:
   n=2097152;first=None;os.link(base/'adc.hex',out/'adc.hex')
  else:
   raw=np.fromfile(a.capture,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]));n=len(raw);assert 100000<=n<=2097152 and np.all(np.diff(raw['tag'])==1)
   first=int(raw['tag'][0]);data=raw.view('<u8').reshape(-1,2)[:,1];np.savetxt(out/'adc.hex',data,fmt='%016x')
  names=list(proof['source_sha256']);assert len(names)==55
  source_variants={}
  for name,sha in proof['source_sha256'].items():
   # Compile the exact qualified snapshot. The only permitted current-source
   # difference is its already joined gate10 change; old-gate A/B evidence
   # must not silently start compiling the new gate as its baseline.
   snapshot=base/name;assert hashlib.sha256(snapshot.read_bytes()).hexdigest()==sha,name
   src=v/('pl/g0' if name=='g0_source_receiver.v' else 'sim/g0')/name
   allowed={sha}
   if name=='differential_score_cost_probe.sv':allowed.add(hashlib.sha256(gate10(snapshot.read_text()).encode()).hexdigest())
   assert hashlib.sha256(src.read_bytes()).hexdigest() in allowed,name
   shutil.copyfile(snapshot,out/name)
   if name=='differential_score_cost_probe.sv' and a.coarse_hit_denominator==10:
    original=(out/name).read_text();changed=gate10(original)
    (out/name).write_text(changed);source_variants[name]={'original_sha256':sha,'diagnostic_sha256':hashlib.sha256(changed.encode()).hexdigest(),'change':'strict coarse candidate score>1/10 instead of>1/5; all startup quality, PHY/link CRC and FEC checks unchanged'}
  for p in base.glob('*.hex'):
   if p.name!='adc.hex':os.link(p,out/p.name)
  body=r'''$readmemh("adc.hex",adc);phase=1;csr_write(44,0,0,15);csr_write(8,1,1,15);
for(k=0;k<@COUNT@;k=k+1)begin pd={BASE+64'(k),adc[k]};pv=1;@(negedge clk);pv=0;repeat(k%2?12:11)@(negedge clk);end
$display("D_END_INPUT coarse%0d startup_good%0d startup_bad%0d header_good%0d header_bad%0d normal_h%0d link_frames%0d link_good%0d link_bad%0d timing%0d retry%0d reacquire%0d SOURCE%0d faults%0d cal%0d",coarse_events,start_good,start_bad,header_good,header_bad,normal_h,link_frames,link_good_total,link_bad_total,timing_events,`F.receiver.retry_count,dut.reacquisitions,dut.sources,dut.ingress_faults,dut.cal_faults);
repeat(1000000)@(negedge clk);csr_write(8,0,0,15);idle;
$display("G0_RAW_DIAGNOSTIC_DONE SOURCE%0d fecaccepted%0d fec_erased%0d expired%0d drops%0d",dut.counters[0],dut.counters[4],dut.counters[5],dut.counters[12],dut.counters[13]);
'''.replace('@COUNT@',str(n))
  if a.qualified_raw_regression:
   tb=(base/'tb.sv').read_text();needle='initial begin\n fd='
   assert tb.count(needle)==1 and 'dut.counters[0]!=136' in tb
   tb=tb.replace(needle,MONITOR+'\n'+needle,1).replace('G0_SOURCE_BOARD_BRIDGE_PASS','G0_RAW_TESTBENCH_COMPLETED')
  else:
   declaration=f'reg [63:0] adc[0:{n-1}];integer k;localparam [63:0] BASE=64\'h{first:016x};\n'+MONITOR
   tb=TB.replace('@RAW_DECL@',declaration).replace('@BODY@',body).replace('G0_SOURCE_BOARD_BRIDGE_PASS','G0_RAW_TESTBENCH_COMPLETED')
  (out/'tb.sv').write_text(tb)
  bp=a.vivado_bin.resolve();xpm=bp.parent/'data/ip/xpm'
  calls=[('compile',[bp/'xvlog','--sv',xpm/'xpm_cdc/hdl/xpm_cdc.sv',xpm/'xpm_memory/hdl/xpm_memory.sv',*names,'tb.sv']),('elaborate',[bp/'xelab','tb','-s','fresh_raw_tb','-mt','off']),('simulate',[bp/'xsim','fresh_raw_tb','-runall'])]
  for step,args in calls:
   with (out/(step+'.log')).open('w') as log:subprocess.run([str(x) for x in args],cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
  log=(out/'simulate.log').read_text();assert 'G0_RAW_TESTBENCH_COMPLETED' in log and 'Fatal:' not in log
  data=bytearray();batch=0;records=[]
  with (out/'batches.txt').open() as f:
   for line in f:
    phase,word,last=line.split();data.extend(int(word,16).to_bytes(8,'little'))
    if int(last):rs,meta=decode_batch(bytes(data),batch);records.extend(rs);batch+=1;data.clear();assert not(meta['flags']&~6)
  assert not data
  sources={};restored=0
  for record in records:
   kind,flags=struct.unpack_from('<HH',record,12)
   if kind==1:
    block,index=struct.unpack_from('<IH',record,48);expected=bytes((block*71+index*29+i*17+i//11+(block+1)*(i%13))&255 for i in range(512));assert record[64:]==expected and (block,index) not in sources;sources[block,index]=True;restored+=512*(flags&1)
  result={'scope':__doc__,'capture':str(base/'adc.hex') if a.qualified_raw_regression else str(a.capture),'input_records':n,'real_first_tag':f'{first:016x}' if first is not None else None,'actual_tag_domain_not_fixture_rebased':not a.qualified_raw_regression,'qualified55RTL_sources_unchanged':not source_variants,'source_sha256':proof['source_sha256'],'explicit_diagnostic_source_variants':source_variants,'coarse_hit_denominator':a.coarse_hit_denominator,'testbench_completed_not_reception_acceptance':True,'SOURCE_records':len(sources),'SOURCE_bytes_exact':len(sources)*512,'restored_bytes_exact':restored,'all_emitted_SOURCE_pattern_bytes_equal':bool(sources),'outcome':'observed_SOURCE' if sources else 'zero_SOURCE_reproduced','batches':batch,'records':len(records),'diagnostics':[s for s in log.splitlines() if s.startswith(('D_','G0_RAW_','G0_SOURCE_'))],'g0_or_physical_RF_throughput_or_video_passed':False}
  if a.qualified_raw_regression:
   assert len(sources)==136 and len(records)==142 and batch==491 and restored==13312
   result['original_136_SOURCE_142_records_491_batches_gate_unchanged']=True
   result['complete_accepted_beat_stream_equal']=hashlib.sha256((out/'batches.txt').read_bytes()).digest()==hashlib.sha256((base/'batches.txt').read_bytes()).digest()
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
  if a.qualified_raw_regression:assert result['complete_accepted_beat_stream_equal'],'original accepted beat stream differs'
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-fresh-raw55-diagnostic'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ['baseline','output','vivado-bin']:p.add_argument('--'+name,type=Path,required=True)
 p.add_argument('--capture',type=Path)
 p.add_argument('--qualified-raw-regression',action='store_true')
 p.add_argument('--coarse-hit-denominator',type=int,choices=(5,10),default=5)
 a=p.parse_args()
 if not a.qualified_raw_regression and a.capture is None:p.error('--capture required unless --qualified-raw-regression')
 main(a)
