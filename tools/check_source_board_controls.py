#!/usr/bin/env python3
"""Actual compiled CSR/DMA/reset faults must fail the55RTL control test."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB
SOURCE='g0_source_receiver.v'
FAULTS=[
 ('reset_held_DMA','wire core_reset=!aresetn || (!run && !batch_busy);','wire core_reset=!aresetn || !run;'),
 ('old_ADC_limit_units','remaining<=limit_batches;','remaining<=limit_batches*2048;'),
 ('constant_flush_ACK','flush_ack<=boundary_resetn && capture_epoch_fence;',"flush_ack<=1'b1;"),
 ('bypass_capture_guard','boundary_resetn && capture_enable && packed_valid && !capture_epoch_fence','boundary_resetn && packed_valid && !capture_epoch_fence'),
 ('active_limit_mutation','if(awaddr[15:2]==11 && !run && !batch_busy && wstrb==15)','if(awaddr[15:2]==11 && wstrb==15)'),
 ('ignore_RF_ready','!run && !batch_busy && rf_path_ready','!run && !batch_busy'),
 ('commit_partial_AXI','aw_pending && w_pending && !s_axi_bvalid','(aw_pending || w_pending) && !s_axi_bvalid')]
def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 c=check('before-SOURCE-board-fault-controls');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
 try:
  positive=json.loads((ref/'receipt.json').read_text());assert not positive['raw'];text=(r/'pl/g0'/SOURCE).read_text();assert hashlib.sha256(text.encode()).hexdigest()==positive['source_sha256'][SOURCE]
  results=[];xpm=bp.parent/'data/ip/xpm'
  for fault,old,new in FAULTS:
   assert text.count(old)==1,(fault,text.count(old));job=out/fault;job.mkdir()
   for n in positive['source_sha256']:shutil.copyfile(ref/n,job/n)
   (job/SOURCE).write_text(text.replace(old,new));shutil.copyfile(ref/'tb.sv',job/'tb.sv')
   for f in ref.glob('*.hex'):shutil.copyfile(f,job/f.name)
   for label,args in [('compile',[bp/'xvlog','--sv',xpm/'xpm_cdc/hdl/xpm_cdc.sv',xpm/'xpm_memory/hdl/xpm_memory.sv',*positive['source_sha256'],'tb.sv']),('elaborate',[bp/'xelab','tb','-s','source_tb','-mt','off']),('simulate',[bp/'xsim','source_tb','-runall'])]:
    with (job/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=job,stdout=f,stderr=subprocess.STDOUT,check=True)
   s=(job/'simulate.log').read_text();assert 'Fatal:' in s and 'G0_SOURCE_BOARD_BRIDGE_PASS' not in s,(fault,s[-2000:]);results.append({'fault':fault,'compiled':True,'rejected':True,'reason':next(x for x in s.splitlines() if x.startswith('Fatal:'))})
  receipt={'scope':__doc__,'source_sha256':positive['source_sha256'][SOURCE],'controls':results,'full_G0_passed':False};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-SOURCE-board-fault-controls'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
