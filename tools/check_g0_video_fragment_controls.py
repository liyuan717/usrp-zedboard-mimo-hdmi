#!/usr/bin/env python3
"""Compile actual early-publish, AU-gating and cache-budget faults against the real-video oracle."""
import argparse,hashlib,json,subprocess
from pathlib import Path

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 source=(v/'ps/g0_video_fragment.c').read_text();positive=json.loads((a.reference/'receipt.json').read_text())
 assert positive['host_ASan_UBSan_actual_C_passed'] and positive['source_sha256']['ps/g0_video_fragment.c']==hashlib.sha256(source.encode()).hexdigest()
 tests=[('premature_partial_unit','if(u->received==u->count){','if(u->received>=1){','clean','byteexact'),
 ('gate_non_AU_end_units','if(deliver&&deliver(opaque,&u->info,u->storage)>0)','if((u->info.flags&2U)&&deliver&&deliver(opaque,&u->info,u->storage)>0)','clean','byteexact'),
 ('disable_global_cache_limit','c->stats.current_bytes+allocation>V3VF_CACHE_MAX_BYTES','0','bounded_pressure_session_reentry','budget'),
 ('bypass_ordered_delivery','if(!c->ordered)publish(c,u,deliver,opaque);','if(1)publish(c,u,deliver,opaque);','ordered_clean_reordered','byteexact'),
 ('disable_unknown_hole_wait','local-nearest->first_seen<V3VF_REORDER_WAIT_US','0','ordered_clean_reordered','byteexact'),
 ('disable_partial_HOL_release','if(!later_due){','if(!later_due || nearest->received!=nearest->count){','ordered_partial_HOL_deadline_late_drop','byteexact')]
 rows=[]
 for name,before,after,case,oracle in tests:
  assert source.count(before)==1,before
  folder=out/name;folder.mkdir();mutant=folder/'mutant.c';mutant.write_text(source.replace(before,after));binary=folder/'probe'
  subprocess.run(['gcc','-std=c11','-Wall','-Wextra','-Werror','-O2','-I',str(v/'ps'),str(mutant),str(v/'ps/g0_video_fragment_probe.c'),'-o',str(binary)],check=True)
  r=subprocess.run([str(binary),str((a.reference/case/'events.bin').resolve()),str(folder/'actual.units')],capture_output=True,text=True)
  (folder/'stdout').write_text(r.stdout);(folder/'stderr').write_text(r.stderr);assert r.returncode==0,'Need a concrete functional witness, not a crash/timeout'
  stats=json.loads(r.stdout)
  if oracle=='byteexact':
   actual=(folder/'actual.units').read_bytes();expected=(a.reference/case/'normal.units').read_bytes();assert actual!=expected
   witness={'expected_output_bytes':len(expected),'actual_output_bytes':len(actual),'first_byte_difference':next((i for i,(x,y) in enumerate(zip(actual,expected)) if x!=y),min(len(actual),len(expected))),'units_delivered':stats['units_delivered']}
  else:
   assert stats['peak_bytes']>2097152;witness={'peak_bytes':stats['peak_bytes'],'required_global_bound':2097152,'final_bytes':stats['current_bytes']}
  rows.append({'fault':name,'compiled':True,'rejected':True,'functional_witness':witness,'mutant_sha256':hashlib.sha256(mutant.read_bytes()).hexdigest()})
 result={'scope':__doc__,'positive_source_sha256':hashlib.sha256(source.encode()).hexdigest(),'controls':rows,'native_or_RF_video_or_G0_passed':False}
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reference',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
