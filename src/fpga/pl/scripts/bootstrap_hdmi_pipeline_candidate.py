"""Freeze HDMI pipeline candidate inputs before Vivado."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from generate_zedboard_spi_timing import build_xdc
ROOT=Path(__file__).resolve().parents[2]
SOURCES=("pl/scripts/create_zedboard_hdmi_pipeline_system.tcl","pl/scripts/bootstrap_hdmi_pipeline_candidate.py","pl/scripts/run_hdmi_pipeline_candidate.ps1","pl/scripts/hdmi_pipeline_provenance.py","pl/scripts/create_zedboard_continuous_video_health_system.tcl","pl/scripts/add_hdmi_presentation_observer.tcl","pl/scripts/hdmi_pipeline_post_synth_audit.tcl","pl/scripts/hdmi_pipeline_release_gate.tcl","pl/rtl/hdmi_presentation_observer.v","pl/rtl/rf_axis_status_monitor.v","pl/constraints/hdmi_presentation_observer_cdc.xdc","pl/constraints/zedboard_rf_lvds_video_candidate_budget.xdc","pl/scripts/add_rf_rx_continuous_transport.tcl","pl/scripts/add_mhgd_interleaved_candidate_module.tcl","pl/scripts/rf_signoff_checks.tcl","pl/scripts/capture_zedboard_rf_inputs.tcl","pl/scripts/generate_zedboard_spi_timing.py","board/zedboard/rf_spi_timing_budget.json")
def main():
 p=argparse.ArgumentParser();p.add_argument('--build',required=True,type=Path);p.add_argument('--spi-budget',required=True,type=Path);a=p.parse_args();b=a.build.resolve()
 if not b.is_relative_to(ROOT/'build') or any(b.iterdir()): raise SystemExit('fresh build/ directory required')
 spi_budget=a.spi_budget.resolve()
 if spi_budget.parent != b or spi_budget.name != 'zedboard_spi_mode1_budget.xdc':
  raise SystemExit('SPI budget must be build/zedboard_spi_mode1_budget.xdc')
 profile=json.loads((ROOT/'board/zedboard/rf_spi_timing_budget.json').read_text(encoding='utf-8'))
 spi_budget.write_text(build_xdc(profile),encoding='utf-8')
 rows={s:hashlib.sha256((ROOT/s).read_bytes()).hexdigest() for s in SOURCES}
 generated_sha=hashlib.sha256(spi_budget.read_bytes()).hexdigest()
 (b/'hdmi_pipeline_bootstrap.json').write_text(json.dumps({'status':'FROZEN_BEFORE_VIVADO','hardware_tested':False,'source_sha256':rows,'generated_sha256':{'zedboard_spi_mode1_budget.xdc':generated_sha}},indent=2)+'\n')
 print('HDMI_PIPELINE_BOOTSTRAP_PASS',b)
if __name__=='__main__': main()
