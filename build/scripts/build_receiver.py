#!/usr/bin/env python3
"""Build the frozen 5120-block ARM receiver; no board/UART/RF/SD access."""
import argparse,hashlib,json,os,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--workspace',type=Path,required=True);p.add_argument('--compiler',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();w=a.workspace.resolve();out=a.output.resolve()
if out.exists():raise SystemExit('output must be new')
out.mkdir(parents=True);s=w/'reception-display-optimization/20261030/candidates/capture5120-r1/source';c=a.compiler or w/'hardware-review/boot-fix-build/toolchain/bin/arm-buildroot-linux-gnueabihf-g++'
flags=['-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread','-DMRF1_NF_ENABLE_DIAG_DC_BLOCKER']
inc=['-I'+str(s/x) for x in ['rf_native_frontend/include','rf_native_runtime/include','rf_native_transport/include','kernel/uapi']]
src=[str(s/x) for x in ['rf_native_runtime/src/mrf1_native_rx_backend.cpp','rf_native_frontend/src/mrf1_native_frontend.cpp','rf_native_transport/src/rf_native_transport.cpp']]
cmd=[str(c),*flags,*inc,*src,'-o',str(out/'live-rx-arm')]
start=time.monotonic()
with (out/'compiler.txt').open('xb') as f:r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=180,env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'))
if r.returncode:raise SystemExit('build failed; inspect compiler.txt')
h=hashlib.sha256((out/'live-rx-arm').read_bytes()).hexdigest();expected='09fd7222ec31434d5643c9235171add705c747e5e9b853da2182f99228d83eec'
report=dict(build_pass=True,sha256=h,frozen_binary_sha256=expected,byte_identical=h==expected,command=cmd,elapsed_s=time.monotonic()-start,hardware_test=False)
(out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
