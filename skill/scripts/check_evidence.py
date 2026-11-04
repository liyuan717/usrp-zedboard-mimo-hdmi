#!/usr/bin/env python3
import argparse,json
from pathlib import Path

def check(j,visual=None,expected_hash=None):
 for k in ['technical_pass','new_RF','new_ADC','original_file_byte_identical','LO_closed','ADC_closed']:
  if j.get(k) is not True:raise ValueError('not confirmed: '+k)
 if j.get('historical_redisplay') is not False:raise ValueError('fresh RF identity missing')
 if not j.get('run_id') or not j.get('boot_id'):raise ValueError('identity missing')
 if j.get('kernel_errors')!=[]:raise ValueError('kernel errors or missing health receipt')
 if j.get('CRC_correct_packets')!=2*j.get('RF_frames',0):raise ValueError('two-stream CRC counts')
 if j.get('HDMI_frames')!=25 or j.get('ADC_blocks')!=5120:raise ValueError('profile count mismatch')
 h=j.get('original_file_sha256','')
 if len(h)!=64 or expected_hash and h!=expected_hash:raise ValueError('source SHA mismatch')
 v='not_observed'
 if visual is not None:
  if visual.get('run_id')!=j['run_id']:raise ValueError('visual feedback belongs to a different run')
  v=visual.get('choice','not_observed')
 return dict(technical_pass=True,visual=v,uniform_5fps=j.get('presentation_cadence',{}).get('nominal_5fps_grid_followed') is True,continuous_realtime_verified=j.get('continuous_realtime_verified') is True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('result',type=Path);p.add_argument('--visual',type=Path);p.add_argument('--expected-sha256');a=p.parse_args();print(json.dumps(check(json.loads(a.result.read_text()),json.loads(a.visual.read_text()) if a.visual else None,a.expected_sha256),indent=2))
