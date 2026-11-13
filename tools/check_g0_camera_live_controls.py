#!/usr/bin/env python3
"""Deterministic bounded mailbox/expiry/ownership/idle recovery controls.
Actual SOURCE fixture bytes, host wire/RS oracle only; no hardware opened.
"""
import argparse,hashlib,json,struct,time
from fractions import Fraction
from pathlib import Path
from g0_camera_live_stream import Mailbox,LiveSchedule,WireAudit
from g0_forward_timebase import TxClockBracket,unpack as unpack_timebase
from g0_video_air_stream import VideoSchedule,swap_coded_lanes,INITIAL_SOURCE_HOLD_US

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False)
 raw=a.symbols.read_bytes();assert len(raw)>=10*10240;now=time.monotonic_ns()//1000
 mailbox=Mailbox()
 for i in range(8):mailbox.put(raw[i*10240:(i+1)*10240],200000,now-400000,i)
 assert mailbox.overflow==[0,1] and len(mailbox.items)==6
 schedule=LiveSchedule(mailbox,0x56334601,100);audit=WireAudit(schedule)
 for sf in range(8):
  wires,_=schedule.period(sf);assert all(w[5]==5 for w in wires);audit.period(wires,sf)
 pinned=[m['source_ordinal'] for _,m in schedule.pending];assert pinned==[2,3]
 for i in range(8,10):mailbox.put(raw[i*10240:(i+1)*10240],300000,now-400000,i)
 assert [m['source_ordinal'] for _,m in schedule.pending]==pinned
 mailbox.finish()
 for sf in range(8,100):
  wires,_=schedule.period(sf);audit.period(wires,sf)
  if schedule.finished():break
 assert schedule.finished() and audit.blocks==8 and mailbox.overflow==[0,1]
 assert [m['source_ordinal'] for m in schedule.admitted]==list(range(2,10))
 stale=Mailbox();stale.put(raw[:10240],0,time.monotonic_ns()//1000-1000000,0);stale.finish()
 assert stale.take(time.monotonic_ns()//1000) is None and stale.expired==[0] and stale.drained()
 held=Mailbox(hold_initial=True)
 for i in range(held.capacity):
  held.put(raw[:10240],200000,now,i)
 try:held.put(raw[:10240],200000,now,held.capacity)
 except ValueError as error:assert 'fixed capacity' in str(error)
 else:raise AssertionError('diagnostic hold exceeded its fixed capacity')
 assert held.take(now+INITIAL_SOURCE_HOLD_US-1) is None and len(held.items)==held.capacity
 for i in range(held.capacity):
  block,meta=held.take(now+INITIAL_SOURCE_HOLD_US)
  assert block==raw[:10240] and meta['source_ordinal']==i and meta['capture_epoch_us']==now
 assert not held.overflow and not held.expired
 gap=Mailbox();live=LiveSchedule(gap,0x56334602,100);ga=WireAudit(live)
 for sf in range(50):
  wires,_=live.period(sf);assert all(w[5]==5 for w in wires);ga.period(wires,sf)
 gap.put(raw[:10240],100000,time.monotonic_ns()//1000-200000,0);gap.finish()
 for sf in range(50,100):
  wires,_=live.period(sf);ga.period(wires,sf)
  if live.finished():break
 assert live.finished() and ga.blocks==1 and live.session==0x56334602
 # Compare the actual scheduler with and without a timed SOB observation.
 # The fixture clock is synthetic, and this comparison opens no UHD device.
 clock_path=out/'tx-clock.json'
 before=time.monotonic_ns();values=[before,before+180011,7_000_000,8_000_000]
 clock=TxClockBracket(*values)
 clock_path.write_text(json.dumps(values)+'\n')
 malformed=out/'invalid-clock.json';malformed.write_text('[0, 1, 2, 3]\n')
 epoch=before//1000-250000
 schedules=[]
 for path in (None,clock_path,malformed,clock_path):
  queue=Mailbox()
  for i in range(6):queue.put(raw[i*10240:(i+1)*10240],100000,epoch,i)
  queue.finish()
  candidate=LiveSchedule(queue,0x56334603,40,path)
  schedules.append((candidate,WireAudit(candidate,len(schedules)==3)))
 anchors=profiles=0
 for sf in range(40):
  rows=[c.period(sf) for c,_ in schedules]
  baseline,forward,fallback,swapped=[wires for wires,_ in rows]
  swap_coded_lanes(swapped,rows[3][1])
  assert forward[:2]==swapped[:2]
  assert baseline==fallback and baseline[2:]==forward[2:]
  for (_,checker),wires in zip(schedules,(baseline,forward,fallback,swapped)):checker.period(wires,sf)
  if forward[0][5]==3 and forward[0][38]&2:
   assert sf in (8,16,24,32) and forward[1]==baseline[1]
   profiles+=1
  if forward[1][32:36]==b'VRT3':
   anchor=unpack_timebase(forward[1][32:64]);anchors+=1
   assert sf not in (8,16,24,32) and anchor.tx_sample_index==sf*80000+1684
   ticks=1_000_000+anchor.tx_sample_index
   minimum=(Fraction(before)+Fraction(ticks*250*1_000_000,1_000_200)-250)/1000-epoch
   maximum=(Fraction(before+180011)+Fraction(ticks*250*1_000_000,999800)+250)/1000-epoch
   assert anchor.source_lower_us<=minimum<=maximum<=anchor.source_upper_us
 assert anchors==28 and profiles==4
 assert all(c.finished() and checker.blocks==6 for c,checker in schedules)
 assert len({checker.source.hexdigest() for _,checker in schedules})==1
 assert schedules[3][1].dual_count==schedules[1][1].dual_count==[96,96]
 assert [x.hexdigest() for x in schedules[3][1].dual]==[x.hexdigest() for x in reversed(schedules[1][1].dual)]
 assert schedules[2][0].clock_rejected and not schedules[2][0].timebases
 r=dict(completed=True,RF_started=False,overflow_drops_oldest_unadvertised_blocks=mailbox.overflow,
  pinned_blocks_unchanged=pinned,all_remaining8_blocks_byteexact_after12_CRC_erasures=True,
  expired_old_unadvertised_blocks=stale.expired,
  diagnostic_hold_boundary_capacity_and_byte_order_passed=True,idle50periods_then_data_recovers_without_session_reset=True,
  fixed_FEC_banks=8,mailbox_cap_blocks=6,scheduler_cap_blocks=2,
  synthetic_timed_SOB_source_and_data_slots_unchanged=True,VRT3=anchors,VRS3_publications=profiles,
  coded_lane_swap_retains_controls_and_recovers_same_SOURCE=True,
  invalid_observation_keeps_legacy_wire_byteexact=True,integer_interval_contains_extreme_rate_projection=True)
 (out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
