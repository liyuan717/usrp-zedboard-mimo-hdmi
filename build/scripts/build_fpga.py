#!/usr/bin/env python3
"""Create a fresh offline Vivado build; never open_hw or program a device."""
from pathlib import Path
import argparse,os,subprocess,json
p=argparse.ArgumentParser();p.add_argument('--workspace',type=Path,required=True);p.add_argument('--name',default='submission_rebuild');p.add_argument('--action',choices=['validate','synth','implement'],default='validate');p.add_argument('--vivado',default='/vivado/2025.2/Vivado/bin/vivado');a=p.parse_args()
if not a.name.replace('_','').isalnum():raise SystemExit('name must be alphanumeric/underscore')
w=a.workspace.resolve();root=w/'deliverables/usrp-zedboard-integration-20261026/zedboard-source';out=root/'build'/a.name
if out.exists():raise SystemExit('build output must be new')
out.mkdir(parents=True);spi=out/'zedboard_spi_mode1_budget.xdc'
subprocess.run(['/usr/bin/python3',str(root/'pl/scripts/bootstrap_hdmi_pipeline_candidate.py'),'--build',str(out),'--spi-budget',str(spi)],check=True)
env=dict(os.environ,ADI_HDL_DIR=str(w/'vendor/adi_hdl_verified'),ADI_ALLOW_VERSION_MISMATCH='YES',MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW='YES',MIMO_RF_EXTERNAL_BUDGET_XDC=str(root/'pl/constraints/zedboard_rf_lvds_video_candidate_budget.xdc'),MIMO_RF_SPI_BUDGET_XDC=str(spi),MIMO_RF_BUILD_PYTHON='/usr/bin/python3')
argv=[a.vivado,'-mode','batch','-nojournal','-log',str(out/'vivado.log'),'-source',str(root/'pl/scripts/create_zedboard_hdmi_pipeline_system.tcl'),'-tclargs',str(out),a.action]
with (out/'stdout.txt').open('xb') as log:r=subprocess.run(argv,env=env,cwd=out,stdout=log,stderr=subprocess.STDOUT)
(out/'build_result.json').write_text(json.dumps(dict(argv=argv,returncode=r.returncode,action=a.action,hardware_test=False),indent=2)+'\n')
raise SystemExit(r.returncode)
