#!/usr/bin/env python3
"""Finite fixed8 dual-TX video -> PL SOURCE -> native decode/HDMI G0 probe.

Offline qualification precedes hardware access. Prerecorded video is not
camera, source-clock, reliable-capacity or full G0 signoff.
"""
import argparse,hashlib,http.server,json,re,secrets,socket,subprocess,threading,time,uuid,zlib
from pathlib import Path
from board_command import command
from build_g0_stream_decode import RUNTIME
from hardware_lease import HardwareLease
from g0_video_air_stream import blackout_window,preflight,INITIAL_SOURCE_HOLD_US
from run_g0_source_air import RX_SETUP,rx_setup_for_bandwidth,SERIAL,HOST,BOARD
from run_g0_source_long_air import qualification,verify_tx_mapping
from workspace_storage_guard import check,GIB
from g0_camera_air_support import plan_camera,reference_source,review_camera
from g0_clock_observation import full_ack_age,full_ack_records,observe,tail_age,wired_RAW_ack_age
from g0_rf_setup import read_rf_setup
from g0_forward_timebase import TxClockBracket,load_clock_bracket
from replay_g0_camera_iq import load_replay,load_coded_lane_proof
from g0_ethernet_clock_probe import (PATH_CALIBRATION_MODEL,path_delay_bounds,port_from_log,read_samples,
    server_script as wired_server_script,stop_script as wired_stop_script)

def require_decoder_measurement_options(binary,a):
    # A stale but otherwise valid native STOP build may lack newer CLI options.
    for option,requested in ((b'capture-acks',getattr(a,'capture_all_acks',False)),
                             (b'age-bound-ns=',getattr(a,'age_probe_path_bound_ns',0)),
                             (b'enforce-age',getattr(a,'source_expiry_proof',None)),
                             (b'enforce-age-lower',getattr(a,'source_expiry_lower_only',False))):
        if requested and option+b'\0' not in binary:
            raise ValueError('native decoder does not support '+option.decode())

def host_management_route(interface):
    route=subprocess.run(['ip','-j','-4','route','get',BOARD],capture_output=True,text=True,timeout=3)
    if route.returncode:
        return False
    rows=json.loads(route.stdout)
    return len(rows)==1 and rows[0].get('dev')==interface and rows[0].get('prefsrc')==HOST

def restore_host_management(interface,profile):
    if host_management_route(interface):
        return False
    carrier=Path('/sys/class/net')/interface/'carrier'
    deadline=time.monotonic()+8
    while carrier.read_text().strip()!='1':
        if time.monotonic()>=deadline:
            raise RuntimeError('Host management carrier did not return after receiver STOP')
        time.sleep(.25)
    subprocess.run(['nmcli','--wait','12','connection','up','id',profile,'ifname',interface],
                   check=True,timeout=15)
    if not host_management_route(interface):
        raise RuntimeError('Host management route did not return on the specified interface')
    return True

def video_qualification(a):
    boot,crc=qualification(a)
    binary=a.decoder/'g0-stream-decode'
    stop=json.loads((a.decoder_native/'receipt.json').read_text())
    assert stop['native_external_stop_READY_tail_and_reentry_passed'] and stop['boot_id']==boot
    binary_data=binary.read_bytes()
    assert stop['decoder_binary_sha256']==hashlib.sha256(binary_data).hexdigest()
    require_decoder_measurement_options(binary_data,a)
    if getattr(a,'auto_session',False):
        build=json.loads((a.decoder/'receipt.json').read_text())
        assert 'ps/include/g0_source_session.hpp' in build['source_sha256'],'auto-session capable native build required'
    if getattr(a,'live_camera_frames',0):
        plan=plan_camera(a)
        return boot,crc,plan,{i:None for i in range(a.live_camera_frames)}
    if archive := getattr(a,'archived_camera_run',None):
        plan,_,pixels,_=load_replay(archive)
        return boot,crc,plan,pixels
    if a.symbols is None or a.pixel_reference is None:raise ValueError('prerecorded source and native pixel reference required')
    pixels=json.loads(a.pixel_reference.read_text())['pixels']
    plan=preflight(a.symbols)
    frames={p['capture_id']&0xffffffff:p['rgb565_crc32'] for p in pixels}
    assert len(frames)==len(pixels)==plan['video_frames'] and set(frames)==set(range(len(frames)))
    source=json.loads((a.symbols.parent/'receipt.json').read_text())
    assert source.get('clean_roundtrip_transmitted_AnnexB_byteexact',source['clean_roundtrip_original_AnnexB_byteexact'])
    assert 1<=source.get('source_repeat_count',1)<=6
    assert plan['video_frames']==200*source.get('source_repeat_count',1)
    assert source['symbol_fixture_sha256']==hashlib.sha256(a.symbols.read_bytes()).hexdigest()
    assert source['source_bytes_including_video_headers_and_all_padding']==plan['source_bytes']
    return boot,crc,plan,frames

def finite_exchange(tx,ready,arm,after_tx,on_go,on_eob,timeout):
    """No GO after absent TX READY or unsuccessful RX arm; cleanup is caller-owned."""
    if not ready.wait(30) or tx.poll() is not None:raise RuntimeError('TX READY absent; no GO')
    arm()
    # Mark attempted emission BEFORE writing: partial pipe failure is uncertain.
    on_go();tx.stdin.write('GO\n');tx.stdin.flush()
    if tx.wait(timeout=timeout)!=0:raise RuntimeError('TX failed; inspect EOB, no video pass')
    on_eob();after_tx()

def review_physical_quality(r,p,expected,session,known):
    records=full_ack_records(p,r,len(expected),session)
    frames=[x['source_frame_id'] for x in records]
    identities=[(session<<32)|frame for frame in frames]
    if any(identity not in known for identity in identities):
        raise ValueError('physical ACK has no matching decoded RGB565 identity')
    times=[x['drm_tv_sec']*1000000000+x['drm_tv_usec']*1000 for x in records]
    gaps=[b-a for a,b in zip(times,times[1:])]
    freezes=[dict(after_frame=a,before_frame=b,gap_ns=gap)
             for a,b,gap in zip(frames,frames[1:],gaps) if gap>1000000000]
    longest=max(gaps,default=0)
    if (times[0]!=r['HDMI_first_ack_ns'] or times[-1]!=r['HDMI_last_ack_ns']
            or longest!=r['HDMI_max_new_frame_gap_ns']
            or len(freezes)!=r['HDMI_new_frame_gaps_over_1s']):
        raise ValueError('physical ACK census differs from the native gap summary')
    return dict(scope='all physical new-frame ACKs',valid=True,
                missing_frames=sorted(set(expected)-set(frames)),
                RGB565_CRC_changed_frames=[f for f,i in zip(frames,identities) if known[i]!=expected[f]],
                new_ACK_span_fps=(len(times)-1)*1000000000/(times[-1]-times[0]) if gaps else 0.0,
                max_new_ACK_gap_ns=longest,freezes_over_1s=freezes)


def review_video(r,p,expected,session,require_full_acks=False):
    rows=r['pixels'];actual={x['capture_id']&0xffffffff:x['rgb565_crc32'] for x in rows}
    duplicates=len(rows)-len({x['capture_id'] for x in rows})
    unknown=[x['capture_id'] for x in rows if x['capture_id']>>32!=session or (x['capture_id']&0xffffffff) not in expected]
    missing=sorted(set(expected)-set(actual));changed=sorted(f for f in actual if f in expected and actual[f]!=expected[f])
    zero=['SOURCE_DMA_stopped_running','SOURCE_DMA_stopped_queued','SOURCE_DMA_stopped_user',
          'SOURCE_DMA_stopped_ready','SOURCE_DMA_stopped_poisoned','SOURCE_DMA_stopped_pinned',
          'owners_after_codec_close','bytes_after_codec_close','source_fragment_current_bytes']
    safe=not r['reader_error'] and not r['SOURCE_DMA_lease_error'] and all(r[k]==0 for k in zero)
    safe=safe and r['pool_peak_bytes']<=2097280 and r['pool_peak_owners']<=10 and r['peak_ready']<=8
    safe=safe and r['HDMI_drain_passed'] and r['HDMI_stop_passed'] and r['HDMI_backend_failures']==0
    known={x['capture_id']:x['rgb565_crc32'] for x in rows}
    loss_notices=p.get('signal_loss_notices',0)
    physical=(p['physical'] and p['acceptance_valid'] and p['capture_status']=='complete'
              and p['timestamp_order_valid'] and not p['failed_closed'] and not p['capture_invalidated']
              and p['backend_failures']==p['proof_failures']==0
              and p['presented_at_hdmi_boundary']==p['new_frames']==r['HDMI_physical_frames']
              and r['HDMI_queued_frames']==r['HDMI_physical_frames']+r.get('HDMI_queued_expired_frames',0)
              and loss_notices==r.get('HDMI_signal_loss_notices',0)
              and len(p['records'])==min(64,p['proof_records_total'])
              and len({x['token'] for x in p['records']})==len(p['records']))
    physical=physical and all(known.get((x['source_session_id']<<32)|x['source_frame_id'])==x['payload_crc32'] for x in p['records'])
    quality=dict(scope='retained DRM tail only',valid=False)
    if require_full_acks or 'development_ACKs' in r:
        quality=dict(scope='all physical new-frame ACKs',valid=False)
        if physical:
            try:
                if duplicates:raise ValueError('ambiguous decoded RGB565 identity')
                quality=review_physical_quality(r,p,expected,session,known)
            except (ValueError,TypeError,KeyError) as error:quality['error']=str(error)
        physical=physical and quality['valid']
    clean=not missing and not changed and not unknown and not duplicates and len(rows)==len(expected)
    ingress=r['SOURCE_DMA_started'] and r['SOURCE_records']>0 and not r['SOURCE_DMA_timeout']
    ingress=ingress and all(r[k]==0 for k in ['SOURCE_DMA_bad_metadata','SOURCE_DMA_bad_records','SOURCE_DMA_missing_batches','SOURCE_DMA_duplicate_batches'])
    return dict(missing_frames=missing,RGB565_CRC_changed_frames=changed,unknown_identities=unknown,
                duplicate_output_frames=duplicates,decoded_frames=len(rows),bounded_release_and_display_stop_passed=bool(safe),
                physical_ACK_identity_CRC_join_passed=bool(physical),physical_new_frames=r['HDMI_physical_frames'],
                physical_ACK_quality=quality,
                full_physical_picture_clean=bool(quality['valid'] and not quality['missing_frames']
                    and not quality['RGB565_CRC_changed_frames'] and not quality['freezes_over_1s']),
                max_new_ACK_gap_ns=r['HDMI_max_new_frame_gap_ns'],new_ACK_gaps_over_1s=r['HDMI_new_frame_gaps_over_1s'],
                signal_loss_notices=loss_notices,first_signal_loss_ACK_ns=p.get('first_signal_loss_ack_ns',0),
                last_signal_loss_ACK_ns=p.get('last_signal_loss_ack_ns',0),
                SOURCE_records=r['SOURCE_records'],clean_DMA_ingress_observed=bool(ingress),
                finite_prerecorded_RF_DMA_decode_HDMI_clean_join_passed=bool(safe and ingress and physical and clean and r['HDMI_physical_frames']==len(expected)),
                source_clock_age_passed=False,real_new_camera_20fps_passed=False,reliable_capacity_30percent_passed=False,
                full_G0_or_video_passed=False)

def stop_script(remote):
    # Validate exact owned executable before signalling; never kill a shared consumer.
    return f'''python3 - <<'V3STOP'
import os,pathlib,signal,time
p=pathlib.Path('{remote}')
if (p/'pid').exists() and not (p/'rc').exists():
 pid=int((p/'pid').read_text())
 try:
  assert str(p/'probe').encode() in pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
  os.kill(pid,signal.SIGTERM)
 except (ProcessLookupError,FileNotFoundError):pass
 deadline=time.monotonic()+10
 while not (p/'rc').exists() and time.monotonic()<deadline:time.sleep(.05)
assert (p/'rc').exists() and (p/'rc').read_text().strip()=='0'
V3STOP
test "$(devmem 0x7c430008)" = 0x00000000'''

def rx_memory_script(remote, observe_link=False):
    """Finite, optional /proc sampler for the owned ARM receiver."""
    return f'''cat > {remote}/memory.py <<'V3MEM'
import json,mmap,os,pathlib,struct,time
p=pathlib.Path(__file__).parent
pid=int((p/'pid').read_text())
proc=pathlib.Path('/proc')/str(pid)
assert str(p/'probe').encode() in (proc/'cmdline').read_bytes().split(b'\\0')
start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
maxima={{}}
minima={{}}
first=last=0
count=0
link=[]
registers=None
if {observe_link!r}:
 fd=os.open('/dev/mem',os.O_RDONLY|os.O_SYNC)
 try:registers=mmap.mmap(fd,4096,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ,offset=0x7c430000)
 finally:os.close(fd)
phy=pathlib.Path('/sys/bus/iio/devices/iio:device0')
def fields(path,names):
 return {{key:int(value.split()[0]) for line in path.read_text().splitlines()
         if ':' in line for key,value in [line.split(':',1)] if key in names}}
for _ in range(440):
 try:
  if (p/'rc').exists() or (proc/'stat').read_text().rsplit(')',1)[1].split()[19]!=start:break
  process=fields(proc/'status',{{'VmRSS','VmHWM','Threads'}})
  system=fields(pathlib.Path('/proc/meminfo'),{{'MemAvailable','CmaFree','CmaTotal','SwapFree'}})
 except (FileNotFoundError,ProcessLookupError):break
 if not {{'VmRSS','VmHWM','Threads'}}<=process.keys():break
 assert {{'MemAvailable','CmaFree','CmaTotal','SwapFree'}}<=system.keys()
 for key,value in process.items():maxima[key]=max(maxima.get(key,value),value)
 for key,value in system.items():minima[key]=min(minima.get(key,value),value)
 last=time.monotonic_ns()
 if not count:first=last
 if registers is not None:
  offsets={{'sources_committed':0x40,'packets_accepted':0x50,'packets_erased':0x54,'ingress_faults':0x58,'reacquisitions':0x5c}}
  row={{key:struct.unpack_from('<I',registers,offset)[0] for key,offset in offsets.items()}}
  row['begin_ns']=last
  row['rssi_readback']=[(phy/f'in_voltage{{i}}_rssi').read_text().strip() for i in range(2)]
  row['end_ns']=time.monotonic_ns()
  link.append(row)
 count+=1
 time.sleep(.25)
report={{'samples':count,'first_board_monotonic_ns':first,'last_board_monotonic_ns':last,
        'maximum_process_values':maxima,'minimum_system_values_KiB':minima}}
if registers is not None:
 registers.close()
 (p/'link.json').write_text(json.dumps(link)+'\\n')
part=p/'memory.tmp'
part.write_text(json.dumps(report)+'\\n')
part.replace(p/'memory.json')
V3MEM
python3 -u {remote}/memory.py > {remote}/memory.log 2>&1 &'''


def read_memory_counters(raw):
    values={key:int(value) for key,value in re.findall(r'(?m)^(MemAvailable|CmaFree|CmaTotal|SwapFree):[ \t]+(\d+) kB\r?$',raw)}
    if len(values)!=4:raise ValueError('missing board memory counters')
    return values

def expiry_provenance(path,boot,bit_crc,path_bound,gain_db,rf_setup=None,decoder_sha=None,rf_configuration=None):
    raw=path.read_bytes()
    proof=json.loads(raw)
    if proof.get('RF_setup')!=rf_setup or (rf_setup and proof.get('decoder_binary_sha256')!=decoder_sha):
        raise ValueError('source expiry calibration differs from the recorded RF setup or decoder')
    if rf_configuration is not None and (proof.get('sampled_age_RF_configuration')!=rf_configuration
            or proof.get('decoder_binary_sha256')!=decoder_sha
            or proof.get('full_physical_ACK_census_requested') is not True):
        raise ValueError('sampled age calibration requires matching TX, RX and decoder configuration and all ACKs')
    age=proof.get('conditional_source_age',{})
    sampled=proof.get('sampled_RF_path_calibration',{})
    anchors=sampled.get('anchors',[])
    if not (path_bound and proof.get('completed') and proof.get('RF_started') and
            proof.get('EOB_confirmed') and proof.get('cleanup_LOs_off') and
            proof.get('boot_id')==boot and proof.get('bit_crc32')==bit_crc and
            proof.get('TX_gain_db')==gain_db and proof.get('forward_timebase_requested') and
            proof.get('review',{}).get('physical_ACK_identity_CRC_join_passed') and
            age.get('physical_ACK_census_complete') and
            sampled.get('model')==PATH_CALIBRATION_MODEL and
            sampled.get('all_sampled_paths_consistent_with_budget') and
            sampled.get('candidate_path_budget_ns')==path_bound and
            isinstance(anchors,list) and 2<=len(anchors)<=9):
        raise ValueError('source expiry requires a same-boot physical clock calibration receipt')
    last=-1
    for anchor in anchors:
        if not isinstance(anchor,dict):raise ValueError('invalid calibrated path observation')
        frame,lower,upper=(anchor.get(key) for key in ('superframe','lower_ns','upper_ns'))
        if not (all(type(value) is int for value in (frame,lower,upper)) and
                last<frame<3200 and lower<=upper and 0<=upper<=path_bound):
            raise ValueError('invalid calibrated path observation')
        last=frame
    return dict(path=str(path.resolve()),sha256=hashlib.sha256(raw).hexdigest())

def main(a):
    boot,bit_crc,plan,expected=video_qualification(a)  # before lease/UART/USRP
    live=bool(getattr(a,'live_camera_frames',0))
    archive=getattr(a,'archived_camera_run',None)
    lane_proof=getattr(a,'archived_lane_control_proof',None)
    live_lane_control=getattr(a,'live_coded_lane_control',False)
    if live_lane_control:
        if not (live and getattr(a,'auto_session',False) and getattr(a,'capture_all_acks',False)):
            raise ValueError('Live coded-slot control requires fresh camera and complete automatic SOURCE/HDMI ACKs')
        evidence=Path(__file__).resolve().parents[1]/'docs/evidence/g0-R22-G6-coded-lane-controls-20261106.json'
        prior=json.loads(evidence.read_text())
        swapped=next(row for row in prior['SOURCE_capture_trials'] if row['mode']=='swapped')
        if ((swapped['boot_id'],swapped['bit_crc32'],prior['TX_gain_dB_per_port'])!=(boot,bit_crc,a.gain_db)
                or swapped['SOURCE_delivered_byteexact']!=swapped['SOURCE_offered']):
            raise ValueError('Live coded-slot control requires matching same-boot archive SOURCE evidence')
    if lane_proof and not archive:
        raise ValueError('Coded-slot HDMI control requires the qualified archived camera run')
    capture_acks=getattr(a,'capture_all_acks',False)
    if capture_acks and not getattr(a,'auto_session',False):
        raise ValueError('all ACK capture requires automatic SOURCE')
    blackout_periods=getattr(a,'blackout_periods',None)
    if archive and blackout_periods is not None:
        raise ValueError('A byte-exact archive cannot contain an added silence interval')
    if live and blackout_periods is not None and (not capture_acks or live_lane_control):
        raise ValueError('Live silence requires physical ACK capture and canonical coded slots')
    blackout_plan={'periods':a.live_camera_frames*50//20} if live else plan
    blackout=blackout_window(blackout_plan,blackout_periods)
    silence_args=['--blackout-periods',*map(str,blackout_periods)] if blackout else []
    observe_rx_link=getattr(a,'observe_rx_link',False)
    observe_rx_memory=getattr(a,'observe_rx_memory',False) or observe_rx_link
    forward_timebase=getattr(a,'forward_timebase',False)
    raw_source_clock=getattr(a,'raw_source_clock',False)
    age_path_bound=getattr(a,'age_probe_path_bound_ns',0)
    expiry_lower_only=getattr(a,'source_expiry_lower_only',False)
    hold_initial=getattr(a,'hold_initial_source',False)
    expiry_path=getattr(a,'source_expiry_proof',None)
    observation_path=getattr(a,'observe_sampled_age_proof',None)
    detach_receiver_ethernet=getattr(a,'receiver_ethernet_down_during_rf',False)
    host_management=getattr(a,'receiver_host_profile',None)
    if forward_timebase and not (live and getattr(a,'auto_session',False)):
        raise ValueError('forward timebase requires live camera and automatic receiver session')
    if raw_source_clock and not (forward_timebase and capture_acks):
        raise ValueError('RAW source clock requires live forward timebase and all physical ACKs')
    if age_path_bound and (not forward_timebase or not 0<age_path_bound<=10_000_000):
        raise ValueError('age probe requires forward timebase and an explicit path bound up to 10ms')
    if archive and (live or getattr(a,'symbols',None) or getattr(a,'pixel_reference',None)
                    or not (capture_acks and getattr(a,'auto_session',False))):
        raise ValueError('Archived camera replay requires automatic SOURCE and all physical ACKs without another source')
    if expiry_lower_only and (not (forward_timebase or archive) or age_path_bound or expiry_path):
        raise ValueError('lower-only expiry requires forward timebase without a path bound or calibration proof')
    if observation_path and (not detach_receiver_ethernet or not age_path_bound or not capture_acks
                             or expiry_path or getattr(a,'observe_clock',False)):
        raise ValueError('sampled age observation requires isolated Ethernet, a path budget and all ACKs without wired clock or expiry')
    calibrate_path=bool(age_path_bound and not observation_path)
    if detach_receiver_ethernet and calibrate_path:
        raise ValueError('receiver Ethernet must stay up for wired path calibration')
    if host_management:
        interface,profile=host_management
        if not detach_receiver_ethernet or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',interface) or not profile:
            raise ValueError('Host management profile requires the isolated receiver Ethernet mode')
        if not host_management_route(interface):
            raise ValueError('Expected host management route is absent before RF')
    bandwidth=getattr(a,'rx_bandwidth_hz',4_000_000)
    rx_setup=rx_setup_for_bandwidth(bandwidth)
    if hold_initial and not (getattr(a,'live_camera_frames',0)==200 and forward_timebase and (expiry_lower_only or expiry_path)
                             and capture_acks and not (blackout or live_lane_control)):
        raise ValueError('initial SOURCE hold requires the 200-frame forward-timebase expiry and ACK probe')
    if bandwidth!=4_000_000 and expiry_lower_only and not hold_initial:
        raise ValueError('Receive bandwidth diagnostics must not enable source expiry')
    if age_path_bound and (blackout or live_lane_control):
        raise ValueError('sampled path calibration and observation require an unmodified RF waveform')
    setup_raw,rf_setup=read_rf_setup(getattr(a,'rf_setup',None),a.tx_binary,rx_setup,[0,1])
    decoder_sha=hashlib.sha256((a.decoder/'g0-stream-decode').read_bytes()).hexdigest()
    age_configuration=None
    observation=None
    if age_path_bound:
        age_configuration=dict(TX_binary_sha256=hashlib.sha256(a.tx_binary.read_bytes()).hexdigest(),
                               RX_commands_sha256=hashlib.sha256(rx_setup.encode()).hexdigest(),
                               RX_bandwidth_hz=bandwidth,logical_IQ_by_RF_port=[0,1])
        if raw_source_clock:age_configuration['SOURCE_clock_domain']='MONOTONIC_RAW'
    expiry=expiry_provenance(expiry_path,boot,bit_crc,age_path_bound,a.gain_db,
                             rf_setup,decoder_sha,age_configuration) if expiry_path else None
    if observation_path:
        observation=expiry_provenance(observation_path,boot,bit_crc,age_path_bound,a.gain_db,
                                      rf_setup,decoder_sha,age_configuration)
    sid=a.session if a.session is not None else uuid.uuid4().int&0xffffffff
    if archive:
        _,original_sid,_,archived_iq=load_replay(archive)
        if a.session is not None and a.session!=original_sid:
            raise ValueError('Archived replay must preserve the original RF session')
        sid=original_sid
        if lane_proof:archived_iq=load_coded_lane_proof(lane_proof,sid,archived_iq)
    assert 0<=sid<=0xffffffff
    v=Path(__file__).resolve().parents[1];out=a.output.resolve()
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,64}',out.name)
    out.mkdir(parents=True,exist_ok=False);remote='/tmp/'+out.name
    result=dict(scope=__doc__,completed=False,RF_started=False,GO_attempted=False,EOB_confirmed=False,
                FMCOMMS3_TX_enabled=False,feedback_enabled=False,TDD_enabled=False,SD_written=False,
                fixed_FEC_banks=8,session=sid,boot_id=boot,bit_crc32=bit_crc,plan=plan,
                frequency_hz=5500000000,sample_rate_hz=4000000,TX_gain_db=a.gain_db,TX_LO_offset_hz=1000000,
                RX_gain_db=60,RX_bandwidth_hz=bandwidth,
                ports='X310 RF0 vertical/RF1 horizontal; FMCOMMS3 RX1A vertical/RX2A horizontal; board TX off',
                cleanup_LOs_off=False,full_G0_or_video_passed=False)
    result['live_camera_requested']=live
    result['receiver_periodic_observations_requested']={'link':observe_rx_link,'memory':observe_rx_memory}
    if archive:
        result['archived_camera_IQ_expected']={key:archived_iq[key] for key in ('bytes','IQ_sha256','IQ_crc32')}
    if live_lane_control:
        result['coded_lanes_swapped']=True
        result['live_coded_lane_control_evidence_sha256']=hashlib.sha256(evidence.read_bytes()).hexdigest()
    if lane_proof:
        result['coded_lanes_swapped']=True
        result['coded_lane_control_proof_sha256']=hashlib.sha256(lane_proof.read_bytes()).hexdigest()
    result['decoder_binary_sha256']=decoder_sha
    if rf_setup:
        (out/'RF-setup.json').write_bytes(setup_raw)
        result['RF_setup']=rf_setup
    result['full_physical_ACK_census_requested']=capture_acks
    if blackout:result['controlled_dual_IQ_silence']=blackout
    if hold_initial:result['controlled_initial_SOURCE_hold_requested']=True
    result['forward_timebase_requested']=forward_timebase
    result['SOURCE_clock_domain']='MONOTONIC_RAW' if raw_source_clock else 'MONOTONIC'
    result['conditional_age_path_bound_ns']=age_path_bound
    result['source_expiry_calibration']=expiry
    if age_configuration:result['sampled_age_RF_configuration']=age_configuration
    if observation:result['sampled_age_observation_calibration']=observation
    if expiry_lower_only:result['source_expiry_lower_only_requested']=True
    if detach_receiver_ethernet:
        result.update(receiver_ethernet_down_requested=True,receiver_ethernet_down_during_RF=False,
                      receiver_ethernet_restored=False)
    result['receiver_session_mode']='auto_PL_SOURCE' if getattr(a,'auto_session',False) else 'pinned_G0'
    result['receiver_out_of_band_session_required']=not getattr(a,'auto_session',False)
    result['measurement_helper_sha256_at_start']={name:hashlib.sha256((v/'tools'/name).read_bytes()).hexdigest()
        for name in ['run_g0_video_air.py','g0_video_air_stream.py','g0_fec_session_waveform.py',
                     'g0_camera_air_support.py','g0_clock_observation.py','g0_forward_timebase.py',
                     'g0_ethernet_clock_probe.py','g0_rf_setup.py','hardware_lease.py','replay_g0_camera_iq.py']}
    before=check('before-finite-video-RF');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    producer=tx=server=producer_log=None;threads=[];uart_ready=True;arm_attempted=False;installed=False;preflight_passed=False
    wired_thread=None;wired_started=False;wired_stop=threading.Event();wired={}
    wired_remote=remote+'/wired-clock'
    ethernet_restore_needed=False
    ethernet_up="ip -o link show dev eth0 | grep -Eq '<([^>]*,)?UP[,>]'"
    ethernet_down="v3_link=$(ip -o link show dev eth0)\nif echo \"$v3_link\" | grep -Eq '<([^>]*,)?UP[,>]'; then exit 1; fi"
    ethernet_address=f"ip -o -4 addr show dev eth0 | grep -F ' {BOARD}/16 '"
    try:
        assert before['disk']['available_bytes']>=5*GIB
        files={'probe':a.decoder/'g0-stream-decode',**{n:a.decoder/'runtime'/n for n in RUNTIME}}
        crc={n:f'{zlib.crc32(p.read_bytes()):08x}' for n,p in files.items()}
        runtime='/tmp/v3-g0-rt-'+f'{zlib.crc32(json.dumps({n:crc[n] for n in RUNTIME},sort_keys=True).encode()):08x}'
        with HardwareLease() as lease:
            def run(name,text,timeout=30):
                nonlocal uart_ready
                subprocess.run(['sh','-n'],input=text,text=True,check=True)
                assert max((len(s.encode()) for s in text.splitlines()),default=0)<=470
                (out/(name+'.sh')).write_text(text+'\n')
                try:return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
                except TimeoutError:
                    uart_ready=False;result['UART_timeout_requires_inspection']=True;raise
            def finish_wired_clock():
                nonlocal wired_started
                wired_stop.set()
                if wired_thread is not None:wired_thread.join(2)
                if wired_started and uart_ready:
                    wired_started=False
                    try:run('wired-clock-stop',wired_stop_script(wired_remote),4)
                    except (RuntimeError,TimeoutError) as error:result['optional_wired_clock_error']=repr(error)
            def restore_receiver_ethernet():
                nonlocal ethernet_restore_needed
                run('RX-eth0-restore','ip link set dev eth0 up\n'+ethernet_up+'\n'+ethernet_address,10)
                ethernet_restore_needed=False
                result['receiver_ethernet_restored']=True
                if host_management:
                    result['host_management_profile_reactivated']=restore_host_management(*host_management)
            try:
                storage='needed='+str(files['probe'].stat().st_size+8*1024*1024)+'\n'
                storage+='sizes='+repr({n:files[n].stat().st_size for n in RUNTIME})+'\n'
                storage+=f"for n,size in sizes.items():\n if not os.path.exists('{runtime}/'+n):needed+=size\n"
                run('preflight',f'''test "$(cat /proc/sys/kernel/random/boot_id)" = {boot}
grep ' / ext4 ro,' /proc/mounts
test -c /dev/v3-g0-source-ring
test -c /dev/dri/card0
test "$(devmem 0x7c430008)" = 0x00000000
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test ! -e {remote}
python3 - <<'V3SPACE'
import os
{storage}s=os.statvfs('/tmp')
assert s.f_bavail*s.f_frsize>=needed
V3SPACE''')
                preflight_passed=True
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        path=files.get(self.path.lstrip('/'))
                        if self.client_address[0]!=BOARD or path is None:self.send_error(403);return
                        data=path.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                    def log_message(self,*args):pass
                server=http.server.ThreadingHTTPServer((HOST,0),Handler)
                threading.Thread(target=server.serve_forever,daemon=True).start()
                setup=f'mkdir {remote}\nmkdir -p {runtime}\n'
                for n in files:
                    target=(runtime if n in RUNTIME else remote)+'/'+n
                    setup+=f'if test ! -f {target}; then wget -q -O {target} http://{HOST}:{server.server_address[1]}/{n}; fi\nbusybox crc32 {target}\n'
                setup+=f'chmod 700 {remote}/probe {runtime}/ld-linux-armhf.so.3\n'
                log=run('install',setup,55)
                for n in files:
                    target=(runtime if n in RUNTIME else remote)+'/'+n
                    assert re.search(r'(?m)^'+crc[n]+' '+re.escape(target)+r'\r?$',log),n
                installed=True
                if detach_receiver_ethernet:
                    run('RX-eth0-preflight',ethernet_up+'\n'+ethernet_address,10)
                memory_counters="grep -E '^(MemAvailable|CmaFree|CmaTotal|SwapFree):' /proc/meminfo"
                if observe_rx_memory:
                    try:result['RX_memory_before_KiB']=read_memory_counters(run('RX-memory-before',memory_counters))
                    except (RuntimeError,ValueError) as error:result['optional_RX_memory_before_error']=repr(error)
                run('RX-setup',rx_setup)
                producer_log=(out/'producer.log').open('w')
                if live:
                    fd=lease.owner.fileno()
                    clock_path=out/'tx-clock.json'
                    clock_args=['--tx-clock',str(clock_path)] if forward_timebase else []
                    if raw_source_clock:clock_args.append('--raw-source-clock')
                    coded_args=['--swap-coded-lanes'] if live_lane_control else []
                    hold_args=['--hold-initial-source'] if hold_initial else []
                    producer=subprocess.Popen(['/usr/bin/python3',str(v/'tools/g0_camera_live_stream.py'),
                        '--frames',str(a.live_camera_frames),'--session',str(sid),'--output',str(out/'wave'),
                        '--wait-go','--lease-fd',str(fd),*clock_args,*coded_args,*silence_args,*hold_args],stdin=subprocess.PIPE,text=True,pass_fds=(fd,),stdout=producer_log,stderr=subprocess.STDOUT)
                elif archive:
                    control_flags=['--swap-coded-lanes'] if lane_proof else []
                    producer=subprocess.Popen(['/usr/bin/python3',str(v/'tools/replay_g0_camera_iq.py'),
                        '--archive',str(archive.resolve()),'--output',str(out/'wave'),'--paced','--wait-go',*control_flags],
                        stdin=subprocess.PIPE,text=True,stdout=producer_log,stderr=subprocess.STDOUT)
                else:
                    producer=subprocess.Popen(['/usr/bin/python3',str(v/'tools/g0_video_air_stream.py'),
                        '--symbols',str(a.symbols.resolve()),'--session',str(sid),'--output',str(out/'wave'),*silence_args],stdout=producer_log,stderr=subprocess.STDOUT)
                end=time.monotonic()+20
                while not (out/'wave/iq.fifo').exists():
                    if producer.poll() is not None:raise RuntimeError('video producer stopped before FIFO')
                    if time.monotonic()>end:raise TimeoutError('FIFO initialization')
                    time.sleep(.05)
                args=[str(a.tx_binary.resolve()),'--iq',str(out/'wave/iq.fifo'),'--freq-hz','5500000000',
                      '--gain-db',str(a.gain_db),'--tx-lo-offset-hz','1000000','--max-samples',str(plan['TX_samples']),'--enable-tx']
                if forward_timebase:args.append('--observe-clock-end')
                if raw_source_clock:args.append('--raw-clock')
                (out/'TX-command.json').write_text(json.dumps(args,indent=2)+'\n')
                tx=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
                ready=threading.Event()
                def read_tx():
                    with (out/'TX.log').open('w') as f:
                        for line in tx.stdout:
                            f.write(line);f.flush()
                            if line.strip()=='G0_READY WAIT_GO':ready.set()
                            clock_tag='G0_TX_CLOCK_RAW ' if raw_source_clock else 'G0_TX_CLOCK '
                            if forward_timebase and line.startswith(clock_tag) and not clock_path.exists():
                                try:
                                    fields=[int(value) for value in line.split()[1:]]
                                    value={'clock_domain':'MONOTONIC_RAW','values':fields} if raw_source_clock else fields
                                    load_clock_bracket(value,raw_source_clock)
                                    temporary=clock_path.with_suffix('.tmp')
                                    temporary.write_text(json.dumps(value)+'\n')
                                    temporary.replace(clock_path)
                                except (TypeError,ValueError,OSError) as error:
                                    result['optional_TX_clock_error']=repr(error)
                reader=threading.Thread(target=read_tx,daemon=True);reader.start();threads.append(reader)
                def arm():
                    nonlocal arm_attempted,wired_thread,wired_started,ethernet_restore_needed
                    if rf_setup or age_path_bound:
                        verify_tx_mapping((out/'TX.log').read_text(),[0,1],True)
                    if live:
                        end=time.monotonic()+10
                        while not (out/'wave/waiting-for-GO.json').exists():
                            if producer.poll() is not None:raise RuntimeError('camera exited before GO')
                            if time.monotonic()>end:raise TimeoutError('camera GO readiness')
                            time.sleep(.02)
                        marker=json.loads((out/'wave/waiting-for-GO.json').read_text())
                        assert not marker['camera_started'] and marker['IQ_bytes']==0
                        result['camera_ready_before_RX_arm']=True
                    if live and getattr(a,'observe_clock',False):
                        result['clock_observations']=[]
                        for i in range(3):
                            try:result['clock_observations'].append(observe(run,'clock-before-'+str(i),raw=True) if raw_source_clock else observe(run,'clock-before-'+str(i)))
                            except (RuntimeError,ValueError) as error:
                                result['optional_clock_error']=repr(error);break
                    if calibrate_path:
                        try:
                            token=secrets.token_bytes(16)
                            window=min(110,int(plan['TX_duration_s'])+25)
                            n=window*4
                            wired_started=True
                            port=port_from_log(run('wired-clock-start',wired_server_script(wired_remote,token,n,window),7))
                            def sample_clock():
                                try:
                                    wired['rows']=read_samples(port,token,n,250,wired_stop,raw=True) if raw_source_clock else read_samples(port,token,n,250,wired_stop)
                                    wired['host_clock_domain']='MONOTONIC_RAW' if raw_source_clock else 'MONOTONIC'
                                except (OSError,ValueError) as error:wired['error']=repr(error)
                            wired_thread=threading.Thread(target=sample_clock,daemon=True)
                            wired_thread.start()
                        except (OSError,ValueError,RuntimeError) as error:
                            result['optional_wired_clock_error']=repr(error)
                    arm_attempted=True
                    receiver_session='auto' if getattr(a,'auto_session',False) else str(sid)
                    age_arg=f' age-bound-ns={age_path_bound}' if age_path_bound else ''
                    if expiry:age_arg+=' enforce-age'
                    if expiry_lower_only:age_arg+=' enforce-age-lower'
                    if capture_acks:age_arg+=' capture-acks'
                    run('RX-arm',f'''(
{runtime}/ld-linux-armhf.so.3 --library-path {runtime} {remote}/probe /dev/v3-g0-source-ring {remote}/report.json paced source-dma {receiver_session} 65536 110000 hdmi{age_arg} > {remote}/stdout 2> {remote}/stderr &
v3_rx=$!
echo "$v3_rx" > {remote}/pid
if wait "$v3_rx"; then echo 0 > {remote}/rc; else echo $? > {remote}/rc; fi
) &
v3_ready=0
for i in $(seq 1 200); do
 test ! -e {remote}/rc || break
 if test "$(devmem 0x7c430008)" = 0x00000001 && test "$(( $(busybox devmem 0x7c430010) ))" -gt 0; then v3_ready=1; break; fi
 sleep 0.02
done
test "$v3_ready" = 1
kill -0 "$(cat {remote}/pid)"
echo VIDEO_RX_READY''',15)
                    result['RX_ready_before_GO']=True
                    if observe_rx_memory:
                        try:run('RX-memory-start',rx_memory_script(remote,observe_rx_link))
                        except (RuntimeError,ValueError) as error:result['optional_RX_memory_start_error']=repr(error)
                    if detach_receiver_ethernet:
                        ethernet_restore_needed=True
                        run('RX-eth0-down','ip link set dev eth0 down\n'+ethernet_down,10)
                def go():
                    if live:
                        producer.stdin.write('GO\n');producer.stdin.flush()
                        result['camera_GO_sent_after_RX_ready']=True
                    if archive:
                        producer.stdin.write('GO\n');producer.stdin.flush()
                        result['archived_replay_GO_sent_after_RX_ready']=True
                    result.update(RF_started=True,GO_attempted=True)
                def eob():
                    reader.join(5);assert not reader.is_alive()
                    text=(out/'TX.log').read_text()
                    result['TX_reported_error_lines']=[line for line in text.splitlines() if '[ERROR]' in line or 'UHD asynchronous TX failure' in line][-64:]
                    result['TX_control_error_free_observed']=not result['TX_reported_error_lines']
                    assert 'TX_BURST_ACK channel_mask=3' in text
                    if forward_timebase:
                        try:
                            tag='G0_TX_CLOCK_END_RAW' if raw_source_clock else 'G0_TX_CLOCK_END'
                            (end_clock,)=re.findall(r'(?m)^'+tag+r' (\d+ \d+ \d+)$',text)
                            begin=load_clock_bracket(json.loads(clock_path.read_text()),raw_source_clock)
                            end_bracket=list(map(int,end_clock.split()))
                            result['TX_clock_rate_observation']=begin.rate_after_burst(*end_bracket)
                            result['TX_clock_end_bracket']=end_bracket
                        except (TypeError,ValueError,OSError) as error:
                            result['optional_TX_clock_rate_unavailable']=repr(error)
                    if live:
                        p=json.loads((out/'wave/receipt.json').read_text())
                        assert p['completed'] and p['IQ_samples']<=plan['TX_samples']
                        assert f"TX completed: {p['IQ_samples']} dual-stream samples" in text
                        result['actual_TX_samples']=p['IQ_samples']
                    else:
                        assert f"TX completed: {plan['TX_samples']} dual-stream samples" in text
                        result['actual_TX_samples']=plan['TX_samples']
                    result.update(EOB_confirmed=True,EOB_acknowledged_channels=[0,1])
                def after_tx():
                    assert producer.wait(timeout=10)==0
                    receipt=json.loads((out/'wave/receipt.json').read_text())
                    assert receipt['completed']
                    if live:
                        assert receipt['delegated_hardware_lease'] and receipt['explicit_GO_received']
                        assert receipt['IQ_bytes']==result['actual_TX_samples']*16
                        assert not receipt['camera_worker_alive_after_cleanup']
                        assert receipt['original_dynamic_framerate']==receipt['restored_dynamic_framerate']
                        assert all(receipt[k]==0 for k in ['queued_SOURCE_owners_after_cleanup','scheduler_SOURCE_owners_after_cleanup','coded_owners_after_cleanup','audit_equation_owners_after_cleanup'])
                        if live_lane_control:
                            assert receipt['coded_lanes_swapped']
                            assert receipt['dual_coded_packet_counts']==[16*receipt['transmitted_SOURCE_blocks']]*2
                            assert receipt['source_blocks_after12_CRC_erasures_byteexact']==receipt['transmitted_SOURCE_blocks']
                        result['camera_producer']=receipt
                    else:
                        assert receipt['bytes_written']==plan['TX_samples']*16
                        if archive:
                            assert receipt['session']==sid and receipt['paced']
                            assert all(receipt[key]==archived_iq[key] for key in ('IQ_sha256','IQ_crc32'))
                            result['archived_coded_lane_control_IQ_byteexact' if lane_proof else
                                   'archived_camera_IQ_byteexact']=True
                    if hold_initial:
                        observed_hold=receipt['controlled_initial_SOURCE_hold']
                        assert observed_hold['original_capture_times_preserved'] is True
                        assert observed_hold['first_admission_since_capture_epoch_us']>=observed_hold['requested_us']==INITIAL_SOURCE_HOLD_US
                        result['controlled_initial_SOURCE_hold']=observed_hold
                    if blackout:
                        observed=receipt['controlled_dual_IQ_silence']
                        assert all(observed[k]==value for k,value in blackout.items())
                        assert observed['zero_IQ_bytes']==blackout['zero_IQ_samples']*16 and observed['masked_coded_packets']>0
                        result['controlled_dual_IQ_silence']=observed
                    result['producer_complete']=True
                    run('RX-at-EOB',f'kill -0 "$(cat {remote}/pid)"\ntest ! -e {remote}/rc\nsleep 0.5\n')
                    result['RX_alive_at_EOB']=True
                finite_exchange(tx,ready,arm,after_tx,go,eob,plan['TX_duration_s']+35)
                if calibrate_path:finish_wired_clock()
                run('RX-stop',stop_script(remote),15)
                if detach_receiver_ethernet:
                    run('RX-eth0-at-stop',ethernet_down,10)
                    result['receiver_ethernet_down_during_RF']=True
                    restore_receiver_ethernet()
                if observe_rx_memory and 'optional_RX_memory_start_error' not in result:
                    try:
                        raw=run('RX-memory-result',f'''for i in $(seq 1 15); do
 test ! -e {remote}/memory.json || break
 sleep 0.10
done
cat {remote}/memory.json
{memory_counters}''',5)
                        (memory_json,)=re.findall(r'(?m)^\{"samples":.*\}\r?$',raw)
                        summary=json.loads(memory_json)
                        if not 1<=summary['samples']<=440:raise ValueError('invalid finite memory sample count')
                        result['RX_memory_after_KiB']=read_memory_counters(raw)
                        result['RX_memory_during_decode']=summary
                    except (KeyError,RuntimeError,ValueError) as error:result['optional_RX_memory_result_error']=repr(error)
                # Existing PL counters only, sampled after STOP. No new RTL,
                # data-plane tracing or packet cache; diagnostics cannot gate video.
                try:
                    text=run('PL-stopped-counters',"""python3 - <<'V3COUNTERS'
import json,subprocess
names=['sources_committed','sources_dropped','sources_bad','sources_aborted',
 'packets_accepted','packets_erased','ingress_faults','reacquisitions',
 'cal_faults','cal_range','batch_bad','batch_timeout','expired','event_drops']
def read(a):
 return int(subprocess.check_output(['busybox','devmem',hex(a)]).strip(),16)
assert read(0x7c430008)==0
print(json.dumps({'PL_stopped_counters':{n:read(0x7c430040+4*i) for i,n in enumerate(names)}}))
V3COUNTERS""")
                    rows=[line for line in text.splitlines() if line.startswith('{"PL_stopped_counters":')]
                    if len(rows)!=1:raise ValueError('one stopped PL counter snapshot required')
                    result['PL_stopped_counters']=json.loads(rows[0])['PL_stopped_counters']
                except (RuntimeError,ValueError,KeyError) as error:
                    result['optional_PL_counters_unverified']=repr(error)
                if live and getattr(a,'observe_clock',False) and 'optional_clock_error' not in result:
                    for i in range(3):
                        try:result['clock_observations'].append(observe(run,'clock-after-'+str(i),raw=True) if raw_source_clock else observe(run,'clock-after-'+str(i)))
                        except (RuntimeError,ValueError) as error:
                            result['optional_clock_error']=repr(error);break
                reply={}
                with socket.socket() as listener:
                    listener.bind((HOST,0));listener.listen(1);listener.settimeout(15)
                    def receive():
                        try:
                            conn,peer=listener.accept();assert peer[0]==BOARD
                            with conn:
                                conn.settimeout(15);chunks=[];count=0
                                while True:
                                    data=conn.recv(min(65536,1048577-count))
                                    if not data:break
                                    chunks.append(data);count+=len(data);assert count<=1048576
                            reply['data']=b''.join(chunks)
                        except BaseException as error:reply['error']=repr(error)
                    thread=threading.Thread(target=receive,daemon=True);thread.start()
                    run('report-transfer',f'''python3 - <<'V3RETURN'
import pathlib,json,socket
p=pathlib.Path('{remote}')
assert all((p/n).stat().st_size<=400000 for n in ['report.json','report.json.physical.json'])
out=dict(report=(p/'report.json').read_text(),proof=(p/'report.json.physical.json').read_text())
link=p/'link.json'
if link.is_file() and link.stat().st_size<=120000:out['link']=link.read_text()
b=json.dumps(out).encode()
assert len(b)<=1048576
s=socket.create_connection(('{HOST}',{listener.getsockname()[1]}),5)
s.settimeout(15)
s.sendall(b)
s.shutdown(socket.SHUT_WR)
s.close()
V3RETURN''')
                    thread.join(20);assert not thread.is_alive() and 'error' not in reply,reply
                envelope=json.loads(reply['data']);r=json.loads(envelope['report']);p=json.loads(envelope['proof'])
                (out/'native.json').write_text(envelope['report']);(out/'physical.json').write_text(envelope['proof'])
                if observation and (r.get('conditional_age_path_bound_ns')!=age_path_bound
                                    or r.get('source_expiry_enabled') is not False):
                    raise ValueError('native sampled age observation used an unexpected budget or expiry mode')
                if expiry_lower_only and (r.get('conditional_age_lower_only') is not True
                        or r.get('conditional_age_within_3s')!=0 or 'conditional_age_max_ns' not in r
                        or r['conditional_age_max_ns'] is not None):
                    raise ValueError('native lower-only report contains an unsupported upper age claim')
                if observe_rx_link:
                    try:
                        rows=json.loads(envelope['link'])
                        if not 2<=len(rows)<=440:raise ValueError('invalid finite link sample count')
                        (out/'RX-link-observations.json').write_text(envelope['link'])
                        result['RX_link_observation_samples']=len(rows)
                    except (KeyError,ValueError) as error:result['optional_RX_link_error']=repr(error)
                assert r['machine']=='armv7l' and r['physical'] and r['SOURCE_DMA_started']
                if getattr(a,'auto_session',False):
                    assert r['SOURCE_auto_session']
                    result['receiver_session_adoptions']=r['SOURCE_sessions_adopted']
                    result['receiver_autonomous_session_startup_observed']=r['SOURCE_records']>0 and r['SOURCE_sessions_adopted']>0
                if live:
                    result['RF_measurement_complete']=True
                    # RF has ended and DMA/display owners are released. Generate
                    # the clean oracle from the ACTUAL camera bytes, then decode
                    # once on the same Cortex-A9; no new RF/camera dependency.
                    clean=reference_source(out/'wave',out,sid,a.live_camera_frames)
                    files['clean-camera.aus']=clean
                    clean_crc=f'{zlib.crc32(clean.read_bytes()):08x}'
                    run('camera-reference',f'''wget -q -O {remote}/clean-camera.aus http://{HOST}:{server.server_address[1]}/clean-camera.aus
test "$(busybox crc32 {remote}/clean-camera.aus | cut -d ' ' -f1)" = {clean_crc}
{runtime}/ld-linux-armhf.so.3 --library-path {runtime} {remote}/probe {remote}/clean-camera.aus {remote}/clean.json paced > {remote}/clean.stdout 2> {remote}/clean.stderr
test "$(devmem 0x7c430008)" = 0x00000000
cat {remote}/clean.json''',a.live_camera_frames/20+20)
                    text=(out/'camera-reference.log').read_text(errors='replace')
                    rows=[line.strip() for line in text.splitlines() if line.strip().startswith('{"machine":')]
                    assert len(rows)==1,'one complete actual native reference report required'
                    reference=json.loads(rows[0]);(out/'clean-native.json').write_text(json.dumps(reference,indent=2)+'\n')
                    assert reference['machine']=='armv7l' and not reference['reader_error'] and not reference['dropped']
                    assert reference['decoded_frames']==reference['input_aus']==a.live_camera_frames
                    assert reference['owners_after_codec_close']==reference['bytes_after_codec_close']==0
                    expected={x['capture_id']&0xffffffff:x['rgb565_crc32'] for x in reference['pixels']}
                    assert set(expected)==set(range(a.live_camera_frames))
                result.update(completed=True,review=review_video(r,p,expected,sid,require_full_acks=capture_acks))
                if live:
                    result['review']=review_camera(result['review'],result['camera_producer']['camera'],p)
                    if age_path_bound or expiry_lower_only:
                        age={key:value for key,value in r.items() if key.startswith('conditional_age_')}
                        counts=[age.get('conditional_age_'+key,0) for key in ('within_3s','over_3s','ambiguous','unknown')]
                        total=age.get('conditional_age_acks',0)
                        census=bool(age.get('conditional_age_DRM_monotonic') and total>0 and
                                    total==sum(counts)==p['presented_at_hdmi_boundary'])
                        result['conditional_source_age']={**age,'physical_ACK_census_complete':census,
                                                         'all_observed_ACKs_conditionally_within_3s':census and total==counts[0]}
                        if expiry or expiry_lower_only:
                            result['source_expiry']={
                                'runtime_enabled':bool(r.get('source_expiry_enabled')),
                                'presentations_dropped':r.get('source_expired_presentations',0),
                                'age_bound_used':'lower' if expiry_lower_only else 'upper',
                            }
                            if expiry_lower_only:
                                result['source_expiry']['lower_bound_only']=True
                                result['source_expiry']['definitely_expired_presentations_dropped']=r.get('source_expired_presentations',0)
                        if 'error' in wired:result['optional_wired_clock_error']=wired['error']
                        if 'rows' in wired:
                            result['wired_clock_observations']=wired['rows']
                            result['wired_clock_host_domain']=wired.get('host_clock_domain','MONOTONIC')
                            try:
                                begin=load_clock_bracket(json.loads(clock_path.read_text()),raw_source_clock)
                                result['sampled_RF_path_calibration']=path_delay_bounds(
                                    wired['rows'],r['path_calibration_anchors'],begin,
                                    result['TX_clock_end_bracket'],sid,age_path_bound)
                                if raw_source_clock:
                                    availability=json.loads((out/'wave/camera/availability.json').read_text())
                                    independent=wired_RAW_ack_age(availability,p,r,wired['rows'],sid,wired['host_clock_domain'])
                                    (out/'wired-RAW-all-source-age.json').write_text(json.dumps(independent,indent=2)+'\n')
                                    result['independent_wired_RAW_all_ACK_age']={k:v for k,v in independent.items() if k!='records'}
                            except (KeyError,ValueError,OSError,TypeError) as error:
                                result['optional_RF_path_calibration_unavailable']=repr(error)
                    if getattr(a,'observe_clock',False) and 'optional_clock_error' not in result:
                        try:
                            clock_control=json.loads(a.camera_clock_control.read_text())
                            assert clock_control['completed'] and clock_control['received']==3
                            assert all(x['timestamp_type']==8192 for x in clock_control['buffers'])
                            availability=json.loads((out/'wave/camera/availability.json').read_text())
                            age=tail_age(availability,p,result['clock_observations'],sid)
                            age['source_timestamp_domain_native_proof']=str(a.camera_clock_control)
                            (out/'clock-tail-age.json').write_text(json.dumps(age,indent=2)+'\n')
                            result['development_wired_clock_tail_age']=age
                            if capture_acks:
                                if not result['review']['physical_ACK_identity_CRC_join_passed']:
                                    raise ValueError('physical source ACK review did not pass')
                                if not all(x['timestamp_source']==65536 for x in clock_control['buffers']):
                                    raise ValueError('V4L2 start-of-exposure timestamp proof required')
                                all_age=full_ack_age(availability,p,r,result['clock_observations'],sid)
                                (out/'clock-all-source-age.json').write_text(json.dumps(all_age,indent=2)+'\n')
                                result['development_wired_clock_all_source_age']={
                                    k:v for k,v in all_age.items() if k!='records'}
                        except (ValueError,AssertionError,TypeError,AttributeError,OSError,KeyError,IndexError) as error:
                            result['optional_clock_age_unverified']=repr(error)
            finally:
                cleanup_errors=[]
                if calibrate_path:finish_wired_clock()
                if tx is not None and tx.poll() is None:
                    try:
                        try:tx.stdin.close()
                        except BrokenPipeError:pass
                        try:tx.wait(timeout=plan['TX_duration_s']+35 if result['GO_attempted'] else 5)
                        except subprocess.TimeoutExpired:
                            result['forced_TX_exit_EOB_unconfirmed']=True
                            tx.terminate();tx.wait(timeout=5)
                    except BaseException as error:cleanup_errors.append(['TX',repr(error)])
                if producer is not None and producer.poll() is None:
                    try:producer.terminate();producer.wait(timeout=10 if live else 5)
                    except BaseException as error:cleanup_errors.append(['producer',repr(error)])
                for thread in threads:thread.join(5)
                if producer_log is not None:producer_log.close()
                if preflight_passed and uart_ready:
                    if arm_attempted:
                        try:run('RX-cleanup-stop',stop_script(remote),15)
                        except BaseException as error:cleanup_errors.append(['RX-stop',repr(error)])
                if ethernet_restore_needed:
                    if uart_ready and (tx is None or tx.poll() is not None):
                        try:restore_receiver_ethernet()
                        except BaseException as error:cleanup_errors.append(['RX-eth0-restore',repr(error)])
                    if ethernet_restore_needed:result['receiver_ethernet_restore_unconfirmed']=True
                if preflight_passed and uart_ready:
                    try:
                        run('RX-idle','''echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(devmem 0x7c430008)" = 0x00000000''')
                        result['cleanup_LOs_off']=True
                        if installed and result['completed']:
                            copies={f'{remote}/probe':crc['probe']}
                            if live:copies[f'{remote}/clean-camera.aus']=clean_crc
                            checks='\n'.join(f'''test "$(busybox crc32 {path} | cut -d ' ' -f1)" = {digest}'''
                                             for path,digest in copies.items())
                            run('release-generated-probe',f'''{checks}
python3 - <<'V3RELEASE'
import os
paths={list(copies)!r}
for pid in os.listdir('/proc'):
 if not pid.isdigit():continue
 try:
  maps=open('/proc/'+pid+'/maps').read()
  assert all(p not in maps for p in paths)
  for fd in os.listdir('/proc/'+pid+'/fd'):
   try:assert os.readlink('/proc/'+pid+'/fd/'+fd) not in paths
   except FileNotFoundError:pass
 except (FileNotFoundError,ProcessLookupError):pass
for p in paths:os.unlink(p)
V3RELEASE''')
                            result['generated_probe_released']=True
                            if live:result['generated_clean_reference_released']=True
                    except BaseException as error:
                        cleanup_errors.append(['RX-idle-or-copy-release',repr(error)])
                        if not result['cleanup_LOs_off']:result['cleanup_unconfirmed']=True
                elif preflight_passed:result['cleanup_unconfirmed']=True
                else:result['cleanup_not_attempted_before_ownership_preflight']=True
                if cleanup_errors:
                    result['cleanup_errors']=cleanup_errors
                    if 'review' in result:
                        key='finite_live_camera_RF_DMA_decode_HDMI_clean_join_passed' if live else 'finite_prerecorded_RF_DMA_decode_HDMI_clean_join_passed'
                        result['review'][key]=False
                    raise RuntimeError('Finite probe cleanup incomplete; inspect receipt')
    except BaseException as error:
        result['error']=repr(error);raise
    finally:
        if server is not None:server.shutdown();server.server_close()
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        (out/'disk-after.json').write_text(json.dumps(check('after-finite-video-RF'),indent=2)+'\n')
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['platform','ram-assets','boot-log','native-control','tx-binary','decoder','decoder-native','output']:
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--receiver-sd-bundle',type=Path,help='qualified SD bundle when the boot reuses the FSBL PL')
    parser.add_argument('--symbols',type=Path);parser.add_argument('--pixel-reference',type=Path)
    parser.add_argument('--archived-camera-run',type=Path,
                        help='byte-exact R22 camera IQ control with original native pixel reference')
    parser.add_argument('--archived-lane-control-proof',type=Path,help='offline coded-slot control proof')
    parser.add_argument('--live-coded-lane-control',action='store_true',help='diagnostic live permutation of coded slots only')
    parser.add_argument('--live-camera-frames',type=int,choices=(200,1200),default=0)
    parser.add_argument('--camera-control',type=Path,help='actual live1200-frame paced FIFO proof directory')
    parser.add_argument('--observe-rx-link',action='store_true',help='finite optional dual RSSI and PL counter observations')
    parser.add_argument('--observe-rx-memory',action='store_true',help='finite optional board /proc RSS, CMA and free memory sampling')
    parser.add_argument('--observe-clock',action='store_true',help='optional development UART brackets, not production source-clock mapping')
    parser.add_argument('--rf-setup',type=Path,help='measured antenna geometry JSON; use the same record for calibration and measurement')
    parser.add_argument('--capture-all-acks',action='store_true',help='finite physical frame and gap census for automatic SOURCE, at most 1200 ACKs')
    parser.add_argument('--forward-timebase',action='store_true',help='publish live UHD-to-camera observations on a capable RAM candidate')
    parser.add_argument('--raw-source-clock',action='store_true',help='use bounded RAW capture intervals and matching RAW UHD/PC clock observations')
    parser.add_argument('--age-probe-path-bound-ns',type=int,default=0,
                        help='conditional one-way path budget for the finite physical ACK census')
    parser.add_argument('--observe-sampled-age-proof',type=Path,
                        help='observe age with same-boot sampled RF calibration while receiver Ethernet is down')
    parser.add_argument('--source-expiry-proof',type=Path,help='same-boot physical calibration required for upper-bound age expiry')
    parser.add_argument('--source-expiry-lower-only',action='store_true',
                        help='probe definite age drops with forward timebase and an unknown RF path maximum')
    parser.add_argument('--receiver-ethernet-down-during-rf',action='store_true',
                        help='verify receiver eth0 down before GO and after STOP; restore management for evidence transfer')
    parser.add_argument('--receiver-host-profile',nargs=2,metavar=('INTERFACE','PROFILE'),
                        help='restore an existing host management profile after the RF window if its route was lost')
    parser.add_argument('--camera-clock-control',type=Path,help='actual V4L2 timestamp flags proof JSON')
    parser.add_argument('--auto-session',action='store_true',help='PS learns session from PL-validated SOURCE, never receives the TX SID in argv')
    parser.add_argument('--hold-initial-source',action='store_true',help='finite 200-frame receiver expiry diagnostic with real source hold')
    parser.add_argument('--blackout-periods',type=int,nargs=2,metavar=('START','COUNT'),help='bounded dual-channel IQ silence in a live or prerecorded recovery probe')
    parser.add_argument('--gain-db',type=int,choices=(12,18),default=12)
    parser.add_argument('--rx-bandwidth-hz',type=int,choices=(2000000,4000000),default=4000000)
    parser.add_argument('--session',type=lambda s:int(s,0))
    main(parser.parse_args())
