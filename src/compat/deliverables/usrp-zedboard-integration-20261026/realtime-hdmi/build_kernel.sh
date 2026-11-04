#!/usr/bin/env bash
set -euo pipefail
TASK_BASE=/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3
TASK_ROOT="$TASK_BASE/deliverables/usrp-zedboard-integration-20261026"
TASK_SRC="$TASK_BASE/hardware-review/project-identity-fix-20261018/linux"
TASK_O="$TASK_ROOT/realtime-hdmi/kernel-o"
TASK_CROSS="$TASK_BASE/hardware-review/boot-fix-build/toolchain/bin/arm-buildroot-linux-gnueabihf-"
export PATH="$TASK_BASE/hardware-review/boot-fix-build/host-tools/usr/bin:$PATH"
export M4="$TASK_BASE/hardware-review/boot-fix-build/host-tools/usr/bin/m4"
export BISON_PKGDATADIR="$TASK_BASE/hardware-review/boot-fix-build/host-tools/usr/share/bison"
python3 - "$TASK_SRC" "$TASK_O" "$TASK_ROOT" <<'PY'
from pathlib import Path
import re,subprocess,sys
source,out,root=map(Path,sys.argv[1:])
fragment=root/'zedboard-source/board/zedboard/linux/config/hdmi_pipeline.fragment'
for line in fragment.read_text().splitlines():
 m=re.fullmatch(r'CONFIG_(\w+)=(y|m)',line)
 if m:subprocess.run([str(source/'scripts/config'),'--file',str(out/'.config'),'--enable' if m[2]=='y' else '--module',m[1]],check=True)
 m=re.fullmatch(r'# CONFIG_(\w+) is not set',line)
 if m:subprocess.run([str(source/'scripts/config'),'--file',str(out/'.config'),'--disable',m[1]],check=True)
subprocess.run([str(source/'scripts/config'),'--file',str(out/'.config'),'--set-str','LOCALVERSION','-xilinx-usrp-hdmi','--disable','LOCALVERSION_AUTO'],check=True)
PY
make -C "$TASK_SRC" O="$TASK_O" ARCH=arm CROSS_COMPILE="$TASK_CROSS" olddefconfig
make -C "$TASK_SRC" O="$TASK_O" ARCH=arm CROSS_COMPILE="$TASK_CROSS" -j8 zImage modules
