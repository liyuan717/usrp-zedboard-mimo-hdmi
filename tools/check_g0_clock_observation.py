#!/usr/bin/env python3
"""Exact-rational clock interval controls, synthetic only; no hardware/age pass."""
import argparse,copy,json
from fractions import Fraction
from pathlib import Path
from g0_clock_observation import full_ack_age,tail_age

def full_census_controls():
 board0=10**12;host0=10**13
 observations=[dict(board_monotonic_ns=board0+x,host_before_ns=host0+x-1000,
                    host_after_ns=host0+x+1000,DRM_timestamp_monotonic=1) for x in (0,10**10)]
 capture=[0]+[3000000+50000*i for i in range(64)]
 timestamps=[board0+3200000000]+[board0+t*1000+400000000 for t in capture[1:]]
 availability=dict(capture_epoch_host_monotonic_us=host0//1000,capture_times_us=capture)
 native=dict(development_ACKs=[[(7<<32)|i,t*1000,ts] for i,(t,ts) in enumerate(zip(capture,timestamps))],
             development_ACK_monotonic=True,development_ACK_omitted=0,HDMI_physical_frames=65)
 tail=[dict(source_session_id=7,source_frame_id=i,source_pts_ns=capture[i]*1000,
            drm_tv_sec=timestamps[i]//10**9,drm_tv_usec=timestamps[i]%10**9//1000) for i in range(1,65)]
 proof=dict(records=tail,new_frames=65,proof_records_total=65)
 assert tail_age(availability,proof,observations,7)['conditional_all_retained_tail_under3s']
 result=full_ack_age(availability,proof,native,observations,7)
 assert not result['conditional_all_source_frames_under3s'] and result['maximum_age_upper_ns']>3200000000
 native['development_ACKs'][0][2]=board0+500000000
 assert full_ack_age(availability,proof,native,observations,7)['conditional_all_source_frames_under3s']
 partial=copy.deepcopy(native);partial['development_ACKs'].pop(0);partial['HDMI_physical_frames']=64
 partial_age=full_ack_age(availability,dict(proof,new_frames=64,proof_records_total=64),partial,observations,7)
 assert partial_age['conditional_all_presented_frames_under3s']
 assert not partial_age['conditional_all_source_frames_under3s'] and partial_age['missing_source_frames']==[0]
 for fault in ('missing','omitted','identity','tail'):
  n=copy.deepcopy(native);p=copy.deepcopy(proof)
  if fault=='missing':n['development_ACKs'].pop(0)
  if fault=='omitted':n['development_ACK_omitted']=1
  if fault=='identity':n['development_ACKs'][0][0]^=1<<32
  if fault=='tail':p['records'][0]['drm_tv_usec']+=1
  try:full_ack_age(availability,p,n,observations,7)
  except ValueError:pass
  else:raise AssertionError(fault)
 return dict(whole_census_clean_passed=True,early_late_frame_detected_beyond_tail=True,
             missing_source_separate_from_observation_loss=True,invalid_censuses_rejected=4)

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);positive=[]
 for scale in [Fraction(1),Fraction(25001,25000)]:
  board0=10**12;offset=10**13;observations=[]
  for t in [0,10000000,20000000,60000000000,60010000000,60020000000]:
   board=board0+t;host=int(scale*board+offset)
   observations.append(dict(board_monotonic_ns=board,host_before_ns=host-1000000,host_after_ns=host+1000000,DRM_timestamp_monotonic=1))
  capture_delta=int(scale*1000000000)//1000
  availability=dict(capture_epoch_host_monotonic_us=int(scale*board0+offset)//1000,capture_times_us=[capture_delta])
  physical=dict(records=[dict(source_session_id=7,source_frame_id=0,source_pts_ns=capture_delta*1000,drm_tv_sec=1001,drm_tv_usec=500000)])
  result=tail_age(availability,physical,observations,7);row=result['records'][0];known=int(scale*500000000)
  assert row['age_lower_ns']<=known<=row['age_upper_ns'] and result['conditional_all_retained_tail_under3s']
  assert not result['all_frames_or_production_clock_mapping_or_standalone_age_acceptance_passed']
  positive.append(dict(true_scale=str(scale),known_age_ns=known,interval=result))
 faults=[]
 for name in ['source_identity','session','unverified_DRM','extrapolation','inconsistent_clocks']:
  aa=copy.deepcopy(availability);pp=copy.deepcopy(physical);oo=copy.deepcopy(observations)
  if name=='source_identity':pp['records'][0]['source_pts_ns']+=1
  if name=='session':pp['records'][0]['source_session_id']=8
  if name=='unverified_DRM':oo[0]['DRM_timestamp_monotonic']=0
  if name=='extrapolation':pp['records'][0]['drm_tv_sec']=999
  if name=='inconsistent_clocks':oo[-1]['host_before_ns']=oo[-1]['host_after_ns']=1
  try:tail_age(aa,pp,oo,7)
  except ValueError as error:faults.append(dict(name=name,rejected=True,error=str(error)))
  else:raise AssertionError('bad clock input accepted '+name)
 availability['capture_epoch_host_monotonic_us']+=10000000
 future=tail_age(availability,physical,observations,7)
 assert not future['conditional_all_retained_tail_under3s'] and future['maximum_retained_age_upper_ns']<0
 result=dict(completed=True,synthetic_controls_not_RF_or_age_evidence=True,RF_started=False,positive=positive,
  negative_controls=faults,future_source_negative_age_not_hidden=True,full_census_controls=full_census_controls(),
  all_frames_or_production_clock_mapping_passed=False)
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print('CLOCK_OBSERVATION_CONTROLS_PASS 2positive/5reject/1negative-age')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
