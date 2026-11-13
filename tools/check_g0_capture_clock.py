#!/usr/bin/env python3
"""Independent capture-clock enclosure and SOURCE-byte compatibility controls."""
import argparse
import hashlib
import io
import json
import struct
from pathlib import Path

from g0_capture_clock import CaptureClockBridge
from g0_camera_ready_probe import CameraBlocks
from g0_video_air_stream import HEADER
from g0_forward_timebase import load_clock_bracket
from g0_clock_observation import full_ack_age,wired_RAW_ack_age


def synthetic_controls():
    edges = CaptureClockBridge(raw_resolution_ns=2)
    edges.observe(9_000, 19_100, 9_100)
    edges.observe(9_999, 20_000, 10_000)
    edges.observe(10_999, 20_999, 11_000)
    # An observation touching either edge of the quantized microsecond
    # cannot alone establish the required strict ordering.
    assert edges.capture_interval(10) is None
    edges.observe(11_001, 21_001, 11_002)
    interval = edges.capture_interval(10)
    assert (interval.lower_ns,interval.upper_ns) == (19_098,21_003)
    snapshot = tuple(edges.observations)
    try:
        edges.observe(12_000,20_999,12_001)
    except ValueError:
        pass
    else:
        raise AssertionError('backwards RAW observation accepted')
    assert tuple(edges.observations) == snapshot
    bridge = CaptureClockBridge(raw_resolution_ns=1)
    captures = []
    checked = 0

    def mono(t):
        # Continuous piecewise clock adjustment, including an exaggerated
        # +2% interval. RAW truth is the independently supplied t+offset.
        adjustment = min(t, 2_000_000_000)//2000
        adjustment -= max(0, min(t-2_000_000_000, 2_000_000_000))//2000
        adjustment += max(0, t-4_000_000_000)//50
        return 10_000_000_000+t+adjustment

    for index in range(1000):
        t = index*7_000_000
        bridge.observe(mono(t), t+20_000_000_000, mono(t+50_000))
        if index%7 == 0:
            actual = t+3_000_501
            captures.append((mono(actual)//1000, actual+20_000_000_000))
        while captures and captures[0][0]*1000+999 < mono(t-100_000_000):
            capture_us, truth = captures.pop(0)
            interval = bridge.capture_interval(capture_us)
            assert interval is not None and interval[0] <= truth <= interval[1]
            assert interval[1]-interval[0] < 15_000_000
            assert interval.before[2] < capture_us*1000
            assert interval.after[0] > capture_us*1000+999
            assert interval.lower_ns == interval.before[1]-1
            assert interval.upper_ns == interval.after[1]+1
            checked += 1
        assert len(bridge.observations) <= bridge.capacity
    assert checked >= 100
    assert bridge.capture_interval(mono(0)//1000) is None
    assert bridge.capture_interval(mono(8_000_000_000)//1000) is None
    before = tuple(bridge.observations)
    for observation in ((0, 1, 2), (1, 1, 0), (True, 1, 2), (1, 1, 2)):
        try:
            bridge.observe(*observation)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid or backwards clock observation accepted')
        assert tuple(bridge.observations) == before
    return dict(enclosed_captures=checked, bounded_history=bridge.capacity,
                strict_microsecond_boundaries_and_RAW_order_checked=True,
                missing_history_and_future_return_unknown=True,
                invalid_observations_preserve_existing_history=True)


def source_compatibility(path):
    bridge = CaptureClockBridge(raw_resolution_ns=1)
    plain_stream, mapped_stream = io.BytesIO(), io.BytesIO()
    plain, mapped = CameraBlocks(plain_stream), CameraBlocks(mapped_stream, capture_clock=bridge)
    count = 0
    with path.open('rb') as stream:
        while count < 20:
            header = stream.read(32)
            magic, version, size, identity, capture_us, length, damaged = struct.unpack('<4sHHQQII', header)
            assert (magic, version, size, damaged) == (b'V3AU', 1, 32, 0)
            raw = stream.read(length)
            assert len(raw) == length
            capture = 10_000_000+capture_us
            # Missing history for frame zero and an unavailable mapper after
            # frame ten leave every original SOURCE byte unchanged.
            if 0 < count < 10:
                bridge.observe(capture*1000-2000, capture*1000+100_000, capture*1000-1000)
                bridge.observe(capture*1000+2000, capture*1000+104_000, capture*1000+3000)
            if count == 10:
                mapped.capture_clock = None
            plain.au(raw, capture, capture+150_000)
            mapped.au(raw, capture, capture+150_000)
            count += 1
    old, new = plain.finish(capture+150_000), mapped.finish(capture+150_000)
    assert plain_stream.getvalue() == mapped_stream.getvalue()
    assert all(new[key] == value for key,value in old.items())
    assert len(new['capture_RAW_intervals_ns']) == count
    assert new['capture_RAW_intervals_ns'][0] is None
    assert all(item is not None for item in new['capture_RAW_intervals_ns'][1:10])
    assert all(item is None for item in new['capture_RAW_intervals_ns'][10:])
    assert len(new['capture_clock_brackets_ns']) == count
    assert new['RAW_clock_resolution_ns'] == 1
    for index, brackets in enumerate(new['capture_clock_brackets_ns']):
        bounds = new['capture_RAW_intervals_ns'][index]
        if brackets is None:
            assert bounds is None
        else:
            before, after = brackets
            original = (new['capture_epoch_host_monotonic_us']+new['capture_times_us'][index])*1000
            assert before[2] < original and after[0] > original+999
            assert bounds == (before[1]-1, after[1]+1)
    return dict(real_encoded_AUs=count, SOURCE_bytes_identical=len(plain_stream.getvalue()),
                missing_or_failed_mapping_keeps_video_bytes=True)


def actual_capture(directory):
    manifest = json.loads((directory/'availability.json').read_text())
    times = manifest['capture_times_us']
    intervals = manifest['capture_RAW_intervals_ns']
    brackets = manifest['capture_clock_brackets_ns']
    assert len(times) == len(intervals) == len(brackets)
    raw_source = manifest['SOURCE_timestamp_clock'] == 'MONOTONIC_RAW'
    assert raw_source or manifest['SOURCE_timestamp_clock'] == 'original_V4L2_MONOTONIC'
    resolution = manifest['RAW_clock_resolution_ns']
    assert type(resolution) is int and resolution > 0 or resolution is None and all(x is None for x in intervals)
    widths = []
    for capture, interval, pair in zip(times, intervals, brackets):
        if pair is None:
            assert interval is None
            continue
        before, after = pair
        original = (manifest['capture_epoch_host_monotonic_us']+capture)*1000
        assert 0 < before[0] <= before[2] < original
        assert original+999 < after[0] <= after[2]
        assert before[1] <= after[1]
        assert interval == [before[1]-resolution, after[1]+resolution]
        widths.append(interval[1]-interval[0])
    source = (directory/'source-symbols.bin').read_bytes()
    assert hashlib.sha256(source).hexdigest() == manifest['source_sha256']
    assert len(source)%512 == 0
    identities = set()
    for offset in range(0,len(source),512):
        symbol = source[offset:offset+512]
        if not any(symbol):
            continue
        frame, timestamp, *fields = HEADER.unpack_from(symbol)
        if raw_source:
            wire_time = manifest['wire_capture_times_us'][frame]
            width = manifest['wire_capture_uncertainty_us'][frame]
            assert timestamp == wire_time and fields[-1] == width and fields[-3]&8
            bounds = intervals[frame]
            if width != 0xffffffff:
                low=(manifest['capture_epoch_host_RAW_us']+wire_time)*1000
                assert bounds is not None and low<=bounds[0]<=bounds[1]<=low+width*1000
        else:
            assert timestamp == times[frame] and fields[-1] == 0
        identities.add(frame)
    assert identities == set(range(len(times)))
    return dict(frames=len(times), mapped=len(widths), unknown=len(times)-len(widths),
                minimum_interval_ns=min(widths,default=None),
                maximum_interval_ns=max(widths,default=None),
                mean_interval_ns=sum(widths)/len(widths) if widths else None,
                original_SOURCE_timestamp_and_reserved_field_verified=True,
                SOURCE_bytes=len(source), SOURCE_sha256=manifest['source_sha256'])


def raw_SOURCE_controls(path):
    bridge=CaptureClockBridge(raw_resolution_ns=1)
    bridge.observe(9_999_000_000,10_199_000_000,9_999_000_001)
    plain_io,raw_io=io.BytesIO(),io.BytesIO()
    blocks=[]
    def delivered(data,ready,epoch,ordinal,**kw):
        meta=kw['capture_metadata']
        assert 0<=meta['original_min_capture_us']<=meta['original_max_capture_us']<=ready
        blocks.append(meta)
    old=CameraBlocks(plain_io);new=CameraBlocks(raw_io,delivered,bridge,True)
    unavailable_io=io.BytesIO();unavailable=CameraBlocks(unavailable_io,raw_source_clock=True)
    with path.open('rb') as stream:
        for frame in range(20):
            magic,version,size,identity,capture_us,length,damaged=struct.unpack('<4sHHQQII',stream.read(32))
            assert (magic,version,size,damaged)==(b'V3AU',1,32,0)
            payload=stream.read(length);capture=10_000_000+capture_us
            if 0<frame<10:
                spread=60_000_000 if frame==9 else 2_000
                if frame!=9:bridge.observe(capture*1000-spread,capture*1000+200_000_000-spread,capture*1000-spread+1)
                bridge.observe(capture*1000+spread,capture*1000+200_000_000+spread,capture*1000+spread+1)
            if frame==10:new.capture_clock=None
            old.au(payload,capture,capture+150_000);new.au(payload,capture,capture+150_000)
            unavailable.au(payload,capture,capture+150_000)
    a,b=old.finish(capture+150_000),new.finish(capture+150_000)
    missing=unavailable.finish(capture+150_000)
    assert all(w==0xffffffff for w in missing['wire_capture_uncertainty_us'])
    assert all(t==0 for t in missing['wire_capture_times_us'])
    assert missing['capture_epoch_host_RAW_us'] is None
    assert missing['capture_RAW_intervals_ns']==[None]*20
    assert a['AnnexB_sha256']==b['AnnexB_sha256'] and a['capture_times_us']==b['capture_times_us']
    assert len(plain_io.getvalue())==len(raw_io.getvalue())
    for offset in range(0,len(raw_io.getvalue()),512):
        legacy=plain_io.getvalue()[offset:offset+512];raw=raw_io.getvalue()[offset:offset+512]
        assert legacy[40:]==raw[40:]
        if not any(legacy):assert raw==legacy;continue
        x,y=HEADER.unpack_from(legacy),HEADER.unpack_from(raw)
        assert x[0]==y[0] and x[2:9]==y[2:9] and x[9]==y[9]&7 and y[9]&8
        assert y[-1]==b['wire_capture_uncertainty_us'][y[0]]
    assert all(w!=0xffffffff for w in b['wire_capture_uncertainty_us'][1:9])
    assert b['wire_capture_uncertainty_us'][0]==0xffffffff
    assert all(w==0xffffffff for w in b['wire_capture_uncertainty_us'][9:])
    assert blocks and all(m['source_capture_epoch_us']==10_199_000 for m in blocks)
    values=[20_000_000_000,20_000_000_001,0,1_000_000]
    typed=dict(clock_domain='MONOTONIC_RAW',values=values)
    assert load_clock_bracket(typed,True).clock_domain=='MONOTONIC_RAW'
    rejected=0
    for value,raw in [(values,True),(typed,False),({**typed,'clock_domain':'MONOTONIC'},True),({**typed,'extra':0},True)]:
        try:load_clock_bracket(value,raw)
        except ValueError:rejected+=1
        else:raise AssertionError('mixed clock domain accepted')
    availability=dict(capture_times_us=[0],capture_epoch_host_monotonic_us=1,
        SOURCE_timestamp_clock='MONOTONIC_RAW',wire_capture_times_us=[100000],wire_capture_uncertainty_us=[2000],
        capture_RAW_intervals_ns=[[20_100_000_000,20_102_000_000]],capture_epoch_host_RAW_us=20_000_000)
    record=dict(source_session_id=1,source_frame_id=0,source_pts_ns=100000000,capture_uncertainty_us=2000,
                drm_tv_sec=10,drm_tv_usec=500000)
    physical=dict(records=[record],new_frames=1,proof_records_total=1)
    native=dict(development_ACKs=[[(1<<32),100000000,10_500_000_000,2000]],development_ACK_monotonic=True,
                development_ACK_omitted=0,HDMI_physical_frames=1)
    observations=[dict(board_monotonic_ns=x,host_before_ns=x+10_000_000_000,host_after_ns=x+10_000_000_000,
        DRM_timestamp_monotonic=1,host_clock_domain='MONOTONIC_RAW') for x in [10_000_000_000,11_000_000_000]]
    age=full_ack_age(availability,physical,native,observations,1)
    assert age['minimum_age_lower_ns']==398_000_000 and age['maximum_age_upper_ns']==400_000_000
    wired=[[20_000_000_000,10_000_000_000,10_000_000_000,20_000_000_000],
           [21_000_000_000,11_000_000_000,11_000_000_000,21_000_000_000]]
    independent=wired_RAW_ack_age(availability,physical,native,wired,1,'MONOTONIC_RAW')
    assert independent['minimum_age_lower_ns']==397_800_000 and independent['maximum_age_upper_ns']==400_200_000
    for domain,rows in [('MONOTONIC',wired),('MONOTONIC_RAW',[wired[0],[21_001_000_000,*wired[1][1:3],21_001_000_000]])]:
        try:wired_RAW_ack_age(availability,physical,native,rows,1,domain)
        except ValueError:pass
        else:raise AssertionError('invalid wired clock proof accepted')
    return dict(encoded_AUs=20,original_times_and_video_payload_unchanged=True,
        unknown_on_missing_history_excess_width_and_observer_failure=True,
        mixed_domain_controls_rejected=rejected,independent_ACK_interval_ns=[398000000,400000000],
        bounded_rate_wired_ACK_interval_ns=[397800000,400200000],wired_domain_and_excess_rate_rejected=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--au-fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--actual-capture', type=Path)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    result = dict(synthetic=synthetic_controls(), source=source_compatibility(args.au_fixture),
                  RF_started=False, hardware_accessed=False, completed=True)
    result['RAW_SOURCE_and_ACK']=raw_SOURCE_controls(args.au_fixture)
    if args.actual_capture:
        result['actual_capture'] = actual_capture(args.actual_capture)
    (args.output/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
