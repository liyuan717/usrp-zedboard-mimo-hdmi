#!/usr/bin/env python3
"""Derive SOURCE-only identity DTB from the matched diagnostic IO/HDMI DTB."""
import argparse,hashlib,json,subprocess
from pathlib import Path
from workspace_storage_guard import check
def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);dtc=a.dtc.resolve()
 (out/'disk-before.json').write_text(json.dumps(check('before-SOURCE-DTB-build'),indent=2))
 try:
  with (out/'base.dts').open('w') as f,(out/'decompile.log').open('w') as log:subprocess.run([str(dtc),'-I','dtb','-O','dts',str(a.base_dtb.resolve())],stdout=f,stderr=log,check=True)
  (out/'source.dts').write_text('/include/ "base.dts"\n/include/ "'+str(r/'board/g0_source.dtsi')+'"\n')
  with (out/'compile.log').open('w') as f:subprocess.run([str(dtc),'-I','dts','-O','dtb','-o',str(out/'g0_source.dtb'),str(out/'source.dts')],stdout=f,stderr=subprocess.STDOUT,check=True)
  s=subprocess.check_output([str(dtc),'-I','dtb','-O','dts',str(out/'g0_source.dtb')],stderr=subprocess.DEVNULL).decode();assert 'compatible = "seu,video-radio-v3-g0-source-batch";' in s and 'compatible = "seu,video-radio-v3-g0-adc";' not in s and 'adi,expect-s-axis-tlast;' not in s
  receipt={'base_DTB_sha256':hashlib.sha256(a.base_dtb.read_bytes()).hexdigest(),'SOURCE_DTB_sha256':hashlib.sha256((out/'g0_source.dtb').read_bytes()).hexdigest(),'provider_byte_count_contract_retained':True,'SOURCE_compatible_distinct':True,'hardware_boot_tested':False};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(receipt)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-SOURCE-DTB-build'),indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','base-dtb','dtc']:p.add_argument('--'+n,type=Path,required=True)
 main(p.parse_args())
