"""Development-only wired clock observations and conditional DRM frame age.

Assume one positive affine relation between board MONOTONIC and the explicitly selected host clock during
the finite probe. All UART request/reply uncertainty is retained. This is
not forward RF timebase implementation or standalone source-age signoff.
"""
import itertools,json,math,re,time
from fractions import Fraction

def observe(run,label,raw=False):
 clock=(lambda:time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)) if raw else time.monotonic_ns
 before=clock()
 text=run(label,'''python3 - <<'V3CLOCK'
import os,time,fcntl,struct,json
fd=os.open('/dev/dri/card0',os.O_RDWR)
try:
 b=bytearray(struct.pack('=QQ',6,0))
 fcntl.ioctl(fd,0xc010640c,b,True)
 cap=struct.unpack('=QQ',b)[1]
finally:os.close(fd)
print('V3_CLOCK='+json.dumps(dict(board_monotonic_ns=time.monotonic_ns(),DRM_timestamp_monotonic=cap)))
V3CLOCK''',15)
 after=clock()
 rows=re.findall(r'(?m)^V3_CLOCK=(\{[^\r\n]+\})\r?$',text)
 if len(rows)!=1:raise ValueError('one actual clock observation required')
 result=dict(json.loads(rows[0]),host_before_ns=before,host_after_ns=after,label=label)
 if raw:result['host_clock_domain']='MONOTONIC_RAW'
 return result

def vertices(observations):
 if len(observations)<2:raise ValueError('clock anchors required')
 if len({o.get('host_clock_domain','MONOTONIC') for o in observations})!=1:raise ValueError('mixed host clock observations')
 origin=observations[0]['board_monotonic_ns'];host=observations[0]['host_before_ns']
 rows=[(o['board_monotonic_ns']-origin,o['host_before_ns']-host,o['host_after_ns']-host) for o in observations]
 if any(low>high for x,low,high in rows) or len({x for x,_,_ in rows})<2:raise ValueError('invalid clock brackets')
 lines=[(x,bound) for x,low,high in rows for bound in (low,high)];result=set()
 for (x1,y1),(x2,y2) in itertools.combinations(lines,2):
  if x1==x2:continue
  scale=Fraction(y2-y1,x2-x1);offset=y1-scale*x1
  if scale>0 and all(low<=scale*x+offset<=high for x,low,high in rows):result.add((scale,offset))
 if not result:raise ValueError('no positive affine clock map fits all observations')
 return origin,host,result

def tail_age(availability,physical,observations,session=None):
 if any(o.get('DRM_timestamp_monotonic')!=1 for o in observations):raise ValueError('DRM monotonic timebase not verified')
 origin,host,models=vertices(observations);times=availability['capture_times_us'];rows=[]
 raw=availability.get('SOURCE_timestamp_clock')=='MONOTONIC_RAW'
 domain='MONOTONIC_RAW' if raw else 'MONOTONIC'
 if any(o.get('host_clock_domain','MONOTONIC')!=domain for o in observations):raise ValueError('capture/observation clock domains differ')
 wire_times=availability.get('wire_capture_times_us',times)
 low_board=min(o['board_monotonic_ns'] for o in observations);high_board=max(o['board_monotonic_ns'] for o in observations)
 for row in physical['records']:
  if session is not None and row['source_session_id']!=session:raise ValueError('clock tail session mismatch')
  frame=row['source_frame_id'];board=row['drm_tv_sec']*1000000000+row['drm_tv_usec']*1000
  if not 0<=frame<len(times) or row['source_pts_ns']!=wire_times[frame]*1000:raise ValueError('physical ACK/capture identity mismatch')
  if not low_board<=board<=high_board:raise ValueError('no clock extrapolation allowed')
  estimates=[host+scale*(board-origin)+offset for scale,offset in models]
  capture=(availability['capture_epoch_host_monotonic_us']+times[frame])*1000
  low=high=capture
  if raw:
   width=availability['wire_capture_uncertainty_us'][frame]
   if row.get('capture_uncertainty_us')!=width or width==0xffffffff:raise ValueError('unknown or unmatched capture interval')
   low,high=availability['capture_RAW_intervals_ns'][frame]
   wire_low=(availability['capture_epoch_host_RAW_us']+wire_times[frame])*1000
   if not wire_low<=low<=high<=wire_low+width*1000:raise ValueError('wire interval does not enclose observed capture')
  rows.append(dict(frame=frame,age_lower_ns=math.floor(min(estimates))-high,age_upper_ns=math.ceil(max(estimates))-low))
 if not rows:raise ValueError('actual retained physical ACKs required')
 return dict(scope=__doc__,retained_tail_frames=len(rows),records=rows,
  fitted_clock_scale_range=[float(min(a for a,b in models)),float(max(a for a,b in models))],
  maximum_retained_age_upper_ns=max(r['age_upper_ns'] for r in rows),
  minimum_retained_age_lower_ns=min(r['age_lower_ns'] for r in rows),
  conditional_all_retained_tail_under3s=all(0<=r['age_lower_ns']<=r['age_upper_ns']<=3000000000 for r in rows),
  all_frames_or_production_clock_mapping_or_standalone_age_acceptance_passed=False)


def full_ack_records(physical,native,expected,session):
    """Read the bounded physical ACK census and cross-check its retained DRM tail."""
    samples=native['development_ACKs']
    if not (native['development_ACK_monotonic'] and native['development_ACK_omitted']==0
            and 1<=len(samples)<=expected<=1200
            and len(samples)==native['HDMI_physical_frames']==physical['new_frames']==physical['proof_records_total']):
        raise ValueError('complete bounded source ACK census required')
    records=[]
    previous_time=-1
    previous_frame=-1
    for row in samples:
        if len(row) not in (3,4) or not all(type(value)==int for value in row):
            raise ValueError('invalid source ACK row')
        identity,pts,board=row[:3]
        if len(row)==4 and not (1<=row[3]<=50000 or row[3]==0xffffffff):raise ValueError('invalid ACK capture uncertainty')
        frame=identity&0xffffffff
        if not (identity>>32==session and previous_frame<frame<expected
                and 0<=pts<1<<64 and board>max(0,previous_time) and board%1000==0):
            raise ValueError('complete ordered source identity and DRM timestamp census required')
        previous_time=board
        previous_frame=frame
        sec,ns=divmod(board,1000000000)
        records.append(dict(source_session_id=session,source_frame_id=frame,
                            source_pts_ns=pts,drm_tv_sec=sec,drm_tv_usec=ns//1000))
        if len(row)==4:records[-1]['capture_uncertainty_us']=row[3]
    tail=physical['records']
    if len(tail)!=min(64,len(records)) or any(
            any(observed[k]!=value for k,value in captured.items())
            for captured,observed in zip(records[-len(tail):],tail)):
        raise ValueError('ACK census differs from retained physical proof')
    return records


def full_ack_age(availability,physical,native,observations,session):
    expected=len(availability['capture_times_us'])
    records=full_ack_records(physical,native,expected,session)
    age=tail_age(availability,dict(records=records),observations,session)
    missing=sorted(set(range(expected))-{row['source_frame_id'] for row in records})
    all_presented=age['conditional_all_retained_tail_under3s']
    return dict(scope='Development wired clock, fixed positive affine finite-window model; no forward RF clock signoff',
                physical_ACK_census_complete=True,source_frame_census_complete=not missing,
                source_frames_expected=expected,source_frames_presented=len(records),
                missing_source_frames=missing,records=age['records'],
                fitted_clock_scale_range=age['fitted_clock_scale_range'],
                maximum_age_upper_ns=age['maximum_retained_age_upper_ns'],
                minimum_age_lower_ns=age['minimum_retained_age_lower_ns'],
                conditional_all_presented_frames_under3s=all_presented,
                conditional_all_source_frames_under3s=not missing and all_presented,
                production_or_standalone_age_acceptance_passed=False)


def wired_RAW_ack_age(availability,physical,native,observations,session,host_clock_domain):
    """Independent causal wired age bounds; no RF path-delay assumption."""
    from g0_ethernet_clock_probe import host_interval,affine_rate_bounds,RATE_PPM,SCALE
    if host_clock_domain!='MONOTONIC_RAW' or availability.get('SOURCE_timestamp_clock')!='MONOTONIC_RAW':
        raise ValueError('matching RAW capture and wired observations required')
    low_rate,high_rate=affine_rate_bounds(observations)
    if not 1-Fraction(RATE_PPM,SCALE)<=low_rate<=high_rate<=1+Fraction(RATE_PPM,SCALE):
        raise ValueError('wired endpoints do not verify candidate relative clock allowance')
    expected=len(availability['capture_times_us'])
    acks=full_ack_records(physical,native,expected,session)
    first=min(r[1] for r in observations);last=max(r[2] for r in observations)
    results=[];unknown=[];outside=[]
    for row in acks:
        frame=row['source_frame_id'];width=availability['wire_capture_uncertainty_us'][frame]
        if (row.get('capture_uncertainty_us')!=width or
            row['source_pts_ns']!=availability['wire_capture_times_us'][frame]*1000):
            raise ValueError('wired age ACK capture interval mismatch')
        if width==0xffffffff:unknown.append(frame);continue
        board=row['drm_tv_sec']*1000000000+row['drm_tv_usec']*1000
        if not first<=board<=last:outside.append(frame);continue
        cl,ch=availability['capture_RAW_intervals_ns'][frame]
        wire_low=(availability['capture_epoch_host_RAW_us']+availability['wire_capture_times_us'][frame])*1000
        if not wire_low<=cl<=ch<=wire_low+width*1000:raise ValueError('invalid saved capture enclosure')
        low,high=host_interval(board,observations)
        results.append(dict(frame=frame,age_lower_ns=low-ch,age_upper_ns=high-cl))
    missing=sorted(set(range(expected))-{r['source_frame_id'] for r in acks})
    within=bool(results and not unknown and not outside and
                all(0<=r['age_lower_ns']<=r['age_upper_ns']<=3000000000 for r in results))
    return dict(scope='independent dedicated Ethernet causal bounds; candidate 400ppm host RAW/board MONOTONIC rate; no RF path-delay budget',
                physical_ACK_census_complete=True,source_frame_census_complete=not missing,
                expected_source_frames=expected,physical_frames=len(acks),known_frames=len(results),
                unknown_capture_frames=unknown,outside_clock_observation_frames=outside,missing_frames=missing,
                minimum_age_lower_ns=min((r['age_lower_ns'] for r in results),default=None),
                maximum_age_upper_ns=max((r['age_upper_ns'] for r in results),default=None),
                all_presented_frames_conditionally_under3s=within,
                all_source_frames_conditionally_under3s=within and not missing,records=results,
                production_or_standalone_age_acceptance_passed=False)
