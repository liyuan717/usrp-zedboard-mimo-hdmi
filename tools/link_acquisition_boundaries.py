#!/usr/bin/env python3
"""Actual ADC packet-stage source loss/held CRC result -> fresh-origin recovery.
Preserve old numeric/deadline oracles. A canceled validated partial packet is
revoked, never a complete FEC commit; a held unaccepted result owns no payload.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from payload_acquisition_boundaries import boundary_tb
from header_acquisition_boundaries import replace
from link_acquisition_probe import SOURCES
from workspace_storage_guard import check as disk_check,GIB

def packet_boundary(original,held):
 kind='held_ML' if held else 'active_business_source_loss'
 text=boundary_tb(original,kind,('lready=cycle%31>6;','lready=!(j==7&&stage==0)&&cycle%31>6;'))
 if held:
  text=replace(text,'dut.receiver.job_pending&&dut.frame.reader_active&&data_seen','dut.receiver.job_pending&&dut.frame.reader_active&&lv')
  text=replace(text,'boundary_data_prefix!=1||data_revoked!=1','boundary_data_prefix<2192||data_revoked!=1')
 else:
  text=replace(text,'dut.frame.business_owner&&dut.frame.sampler.state==5&&data_results>=96&&!dvalid','packet_fault_fired')
  text=replace(text,'task run_samples','reg packet_fault_fired=0;\nalways @(negedge clk)if(rst)packet_fault_fired=0;else if(j==6&&stage==0&&ldv&&link_di==128&&!packet_fault_fired)begin loss=1;packet_fault_fired=1;end\ntask run_samples')
  text=replace(text,'boundary_data_prefix<96||data_revoked!=0','boundary_data_prefix<2192||data_revoked>1')
  text=replace(text,'if(update||nv||hv||pvalid||dvalid||dut.read_enable||dut.receiver.startup_commit)','if(update||nv||hv||pvalid||dvalid||lv||ldv||dut.read_enable||dut.receiver.startup_commit)')
 prefix='boundary_data_prefix=data_results;boundary_sample_prefix=data_inputs;boundary_phase_prefix=phase_points;'
 text=replace(text,prefix,prefix+'boundary_link_good=link_good;boundary_link_bad=link_bad;boundary_link_checked=link_checked;')
 extra=r'''
integer boundary_link_good=0,boundary_link_bad=0,boundary_link_checked=0,revoked_link_metadata=0,revoked_link_result=0;
reg link_owned_q=0,link_result_q=0;
always @(posedge clk)begin
 if(rst||cancel)begin link_owned_q<=0;link_result_q<=0;end
 else if(fence&&!boundary_fence_q)begin
  if(link_owned_q)revoked_link_metadata=revoked_link_metadata+1;
  if(link_result_q)revoked_link_result=revoked_link_result+1;
  link_owned_q<=0;link_result_q<=0;
 end else begin link_owned_q<=link_metadata;link_result_q<=lv&&!lready;end
 if(!rst&&frame_abort&&(lv||ldv))$fatal(1,"link public output survived abort");
end
'''
 text=replace(text,'task run_samples',extra+'\ntask run_samples')
 old=' if(link_packets!=432||link_good!=402||link_bad!=30||link_checked!=402*512)$fatal(1,"live link global census %0d/%0d/%0d/%0d",link_packets,link_good,link_bad,link_checked);'
 conditions='boundary_link_good!=0||boundary_link_bad!=0||boundary_link_checked!=0||revoked_link_metadata!=0||revoked_link_result!=1' if held else 'boundary_link_good!=1||boundary_link_bad!=0||boundary_link_checked!=128||revoked_link_metadata!=1||revoked_link_result!=0'
 new=' if(link_packets!=48+boundary_link_good+boundary_link_bad||link_good!=46+boundary_link_good||link_bad!=2+boundary_link_bad||link_checked!=46*512+boundary_link_checked||'+conditions+')$fatal(1,"link recovery census packets%0d good%0d bad%0d bytes%0d prefix%0d/%0d/%0d revoked%0d/%0d",link_packets,link_good,link_bad,link_checked,boundary_link_good,boundary_link_bad,boundary_link_checked,revoked_link_metadata,revoked_link_result);'
 text=replace(text,old,new)
 text=replace(text,' $display("G0_LIVE_LINK_ACQUISITION_PASS',' $display("G0_LINK_BOUNDARY_OWNERSHIP prefix_good=%0d prefix_bad=%0d prefix_payload_bytes=%0d revoked_partial_packet=%0d revoked_unaccepted_result=%0d",boundary_link_good,boundary_link_bad,boundary_link_checked,revoked_link_metadata,revoked_link_result);\n $display("G0_LINK_BOUNDARY_PASS')
 text=text.replace('G0_LINK_BOUNDARY_PASS complete_frames=27','G0_LINK_BOUNDARY_PASS complete_frames=3')
 return text

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-link-boundaries');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  hashes={}
  for n in SOURCES:
   h=hashlib.sha256((ref/n).read_bytes()).hexdigest();assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h;hashes[n]=h;shutil.copyfile(ref/n,out/n)
  rows=[];xpm=bp.parent/'data/ip/xpm'
  for held in [False,True]:
   label='held_packet_result_job_expiry' if held else 'partial_packet_source_loss';d=out/label;d.mkdir()
   for p in ref.glob('*.hex'):os.link(p,d/p.name)
   for n in SOURCES:shutil.copyfile(out/n,d/n)
   (d/'tb.sv').write_text(packet_boundary((ref/'tb.sv').read_text(),held))
   for step,args in [('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*SOURCES,'tb.sv']),('elaborate',[str(bp/'xelab'),'tb','-s','link_boundary_tb','-mt','off']),('simulate',[str(bp/'xsim'),'link_boundary_tb','-runall'])]:
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   log=(d/'simulate.log').read_text();assert 'G0_LINK_BOUNDARY_PASS' in log and 'Fatal:' not in log,log[-5000:]
   row={'name':label,**{k:next(s for s in log.splitlines() if s.startswith(v)) for k,v in [('link','G0_LINK_BOUNDARY_PASS'),('link_ownership','G0_LINK_BOUNDARY_OWNERSHIP'),('business_ownership','G0_PAYLOAD_BOUNDARY_OWNERSHIP'),('payload','G0_PAYLOAD_BOUNDARY_PASS'),('phase','G0_PAYLOAD_BOUNDARY_PHASE_PASS'),('header','G0_PAYLOAD_BOUNDARY_HEADER_PASS'),('tracking','G0_PAYLOAD_BOUNDARY_TRACKING_PASS')]}};rows.append(row);print(json.dumps(row),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'source_sha256':hashes,'boundaries':rows,'g0_passed':False},indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-link-boundaries'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 main(p.parse_args())
