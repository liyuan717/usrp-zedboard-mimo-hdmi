#!/usr/bin/env python3
"""Offline coordinator controls: no serial, DMA, UHD, HDMI or RF access."""
import argparse,contextlib,copy,hashlib,io,json,re,socket,subprocess,zlib
from pathlib import Path
from types import SimpleNamespace
import run_g0_video_air as air
from g0_forward_timebase import TX_PERIOD_SAMPLES, TX_TRAINING_SAMPLE
from g0_rf_setup import match_rf_evidence

def ack_gap_summary(report):
    times=[row[2] for row in report['development_ACKs']]
    gaps=[b-a for a,b in zip(times,times[1:])]
    report.update(HDMI_first_ack_ns=times[0],HDMI_last_ack_ns=times[-1],
                  HDMI_max_new_frame_gap_ns=max(gaps),HDMI_new_frame_gaps_over_1s=sum(x>10**9 for x in gaps))


def synthesize_full_acks(base,proof,sid):
    report=copy.deepcopy(base)
    tail=proof['records']
    first=tail[0]['source_frame_id']
    before=tail[0]['drm_tv_sec']*10**9+tail[0]['drm_tv_usec']*1000
    samples=[[(sid<<32)|i,i*50_000_000,before-(first-i)*50_000_000] for i in range(first)]
    samples.extend([[(x['source_session_id']<<32)|x['source_frame_id'],x['source_pts_ns'],
                     x['drm_tv_sec']*10**9+x['drm_tv_usec']*1000] for x in tail])
    assert len(samples)==base['HDMI_physical_frames']
    report.update(development_ACK_monotonic=True,development_ACK_omitted=0,development_ACKs=samples)
    ack_gap_summary(report)
    return report


def full_ack_quality_controls(base,proof,expected,sid):
    report=synthesize_full_acks(base,proof,sid)
    legacy=air.review_video(base,proof,expected,sid)
    assert not legacy['full_physical_picture_clean'] and legacy['physical_ACK_identity_CRC_join_passed']
    assert not air.review_video(base,proof,expected,sid,True)['physical_ACK_identity_CRC_join_passed']
    assert air.review_video(report,proof,expected,sid,True)['full_physical_picture_clean']
    checked=['full_ACK_quality','required_census_missing','legacy_tail_stays_scoped']
    for name in ('duplicate_early_ACK','wrong_early_session','omitted_ACK','ambiguous_decode','gap_summary_mismatch'):
        broken=copy.deepcopy(report)
        if name=='duplicate_early_ACK':broken['development_ACKs'][1]=broken['development_ACKs'][0]
        if name=='wrong_early_session':broken['development_ACKs'][1][0]^=1<<32
        if name=='omitted_ACK':broken['development_ACK_omitted']=1
        if name=='ambiguous_decode':broken['pixels'].append(broken['pixels'][1])
        if name=='gap_summary_mismatch':broken['HDMI_max_new_frame_gap_ns']+=1000
        reviewed=air.review_video(broken,proof,expected,sid,True)
        assert not reviewed['physical_ACK_identity_CRC_join_passed'] and not reviewed['full_physical_picture_clean']
        checked.append(name)
    damaged=copy.deepcopy(report)
    less_proof=copy.deepcopy(proof)
    damaged['development_ACKs'].pop(1)
    damaged['pixels'][2]['rgb565_crc32']^=1
    damaged.update(HDMI_queued_expired_frames=1,HDMI_physical_frames=report['HDMI_physical_frames']-1)
    for key in ('presented_at_hdmi_boundary','new_frames','proof_records_total'):less_proof[key]-=1
    for ack in damaged['development_ACKs'][:2]:ack[2]-=2*10**9
    ack_gap_summary(damaged)
    quality=air.review_video(damaged,less_proof,expected,sid,True)['physical_ACK_quality']
    assert quality['valid'] and quality['missing_frames']==[1] and quality['RGB565_CRC_changed_frames']==[2]
    assert len(quality['freezes_over_1s'])==1
    checked.append('known_expired_display_with_separate_physical_damage_and_freeze')
    return [dict(name=name,passed=True) for name in checked]


def transport_controls(a,out,base,proof,expected,sid,live=False,auto=False):
    """Exercise generated scripts/cleanup with local doubles, never board tools."""
    saved={n:getattr(air,n) for n in ['HardwareLease','command','video_qualification','reference_source',
        'observe','read_samples','HOST','BOARD','host_management_route','restore_host_management']}
    popen=air.subprocess.Popen
    plan=air.preflight(a.symbols)
    class Lease:
        owner=SimpleNamespace(fileno=lambda:77)  # synthetic FD; never passed to a real process
        def __enter__(self):return self
        def __exit__(self,*args):pass
    results=[]
    try:
        air.HardwareLease=Lease;air.HOST=air.BOARD='127.0.0.1'
        air.video_qualification=lambda args:('synthetic-boot','synthetic-bit',plan,expected)
        def reference(wave,case,session,frames):
            path=case/'synthetic.aus';path.write_bytes(b'SYNTHETIC REFERENCE ONLY');return path
        if live:air.reference_source=reference
        failures=[None,'preflight','RX-arm','RX-stop']+(['camera-not-ready','camera-GO-pipe','camera-reference','clock-unavailable','clock-proof-missing'] if live else [])
        if auto:failures=[None,'RX-arm','auto-receiver-report-pinned','PL-stopped-counters','forward-timebase',
                          'wired-clock','sampled-age-2MHz','all-acks-no-clock','setup-wrong-map','lower-only','lower-only-upper-claim',
                          'ethernet-down-failure','ethernet-lost-uart']
        for failure in failures:
            events=[];txes=[];case=out/(('auto-' if auto else '')+('live-' if live else '')+'transport-'+(failure or 'normal'))
            lower_only=failure in ('lower-only','lower-only-upper-claim')
            sampled=failure=='sampled-age-2MHz'
            detached=sampled or lower_only or failure in ('ethernet-down-failure','ethernet-lost-uart')
            ethernet_up=True
            host_management_up=True
            forward=sampled or failure in ('forward-timebase','wired-clock') or lower_only
            air.observe=saved['observe']
            air.read_samples=saved['read_samples']
            def host_route(interface):
                assert interface=='synthetic-mgmt'
                return host_management_up
            def restore_host(interface,profile):
                nonlocal host_management_up
                assert interface=='synthetic-mgmt' and profile=='synthetic-profile'
                assert ethernet_up and not host_management_up
                assert 'RX-stop' in events and 'RX-eth0-restore' in events
                events.append('host-management-profile-restored')
                host_management_up=True
                return True
            air.host_management_route=host_route
            air.restore_host_management=restore_host
            if failure=='wired-clock':
                air.read_samples=lambda *a:[(t-40000,t,t+3000,t+48000) for t in range(1_000_000_000,11_000_000_001,1_000_000_000)]
            if failure in ['clock-unavailable','clock-proof-missing']:
                def clock(run,label):
                    events.append('synthetic-clock-observation')
                    if failure=='clock-unavailable':raise RuntimeError('synthetic optional clock unavailable')
                    return dict(board_monotonic_ns=1,host_before_ns=1,host_after_ns=2,DRM_timestamp_monotonic=1)
                air.observe=clock
            if failure=='lower-only':
                full_report=synthesize_full_acks(base,proof,sid)
                ack_times=[row[2] for row in full_report['development_ACKs']]
                capture_times=[row[1]//1000 for row in full_report['development_ACKs']]
                host_offset=10**10
                capture_epoch=(ack_times[0]+host_offset-500_000_000)//1000-capture_times[0]
                def detached_clock(run,label):
                    assert ethernet_up
                    events.append(label)
                    before=label.startswith('clock-before-')
                    board_ns=(ack_times[0]-10**9 if before else ack_times[-1]+10**9)+int(label[-1])*10**7
                    return dict(board_monotonic_ns=board_ns,host_before_ns=board_ns+host_offset-1000,
                                host_after_ns=board_ns+host_offset+1000,DRM_timestamp_monotonic=1)
                air.observe=detached_clock
            class Process:
                def __init__(self,args,**kwargs):
                    self.stdin=io.StringIO();self.done=False
                    if '--symbols' in args or '--frames' in args:
                        wave=Path(args[args.index('--output')+1]);wave.mkdir();(wave/'iq.fifo').touch()
                        p=dict(completed=True,bytes_written=plan['TX_samples']*16)
                        if live:
                            events.append('camera-spawn-with-inherited-lease')
                            assert kwargs['pass_fds']==(77,) and '--wait-go' in args and '--lease-fd' in args
                            if forward:assert '--tx-clock' in args
                            p.update(IQ_samples=plan['TX_samples'],IQ_bytes=plan['TX_samples']*16,delegated_hardware_lease=True,
                                explicit_GO_received=True,camera_worker_alive_after_cleanup=False,original_dynamic_framerate=1,restored_dynamic_framerate=1,
                                camera=dict(frames=200,unique_capture_timestamps=200,measured_new_capture_fps=20.01))
                            for key in ['queued_SOURCE_owners_after_cleanup','scheduler_SOURCE_owners_after_cleanup','coded_owners_after_cleanup','audit_equation_owners_after_cleanup']:p[key]=0
                            if failure=='lower-only':
                                (wave/'camera').mkdir()
                                (wave/'camera/availability.json').write_text(json.dumps(dict(
                                    capture_epoch_host_monotonic_us=capture_epoch,capture_times_us=capture_times)))
                            if failure!='camera-not-ready':(wave/'waiting-for-GO.json').write_text(json.dumps(dict(camera_started=False,IQ_bytes=0)))
                            class CameraGo(io.StringIO):
                                def write(self,value):
                                    events.append('camera-GO')
                                    assert 'RX-arm' in events
                                    if failure=='camera-GO-pipe':raise BrokenPipeError('synthetic camera GO pipe')
                                    return super().write(value)
                            self.stdin=CameraGo()
                        (wave/'receipt.json').write_text(json.dumps(p))
                        self.done=True
                    else:
                        txes.append(self)
                        clock=clock_end=''
                        if forward:
                            assert '--observe-clock-end' in args
                            clock='G0_TX_CLOCK 1000000000 1000100000 2000000 3000000\n'
                            clock_end='G0_TX_CLOCK_END 11000000000 11000100000 42000000\n'
                        if sampled or failure=='wired-clock':clock='G0_TX_IQ_MAPPING rf0=0 rf1=1\n'+clock
                        if failure=='setup-wrong-map':clock='G0_TX_IQ_MAPPING rf0=1 rf1=0\n'
                        self.stdout=io.StringIO('G0_READY WAIT_GO\n'+clock+'TX_BURST_ACK channel_mask=3\n'+clock_end+'TX completed: '+str(plan['TX_samples'])+' dual-stream samples at 4 MSPS.\n')
                def poll(self):return 0 if self.done else None
                def wait(self,timeout):self.done=True;return 0
                def terminate(self):self.done=True
            def board(serial,text,output,timeout,lease):
                nonlocal ethernet_up,host_management_up
                name=output.stem;events.append(name);output.write_text('SYNTHETIC TRANSPORT ONLY\n')
                if name=='RX-eth0-preflight':assert ethernet_up and '/16' in text
                if name=='RX-eth0-down':
                    assert ethernet_up and 'ip link set dev eth0 down' in text
                    ethernet_up=False
                    if failure=='lower-only':host_management_up=False
                    if failure=='ethernet-down-failure':raise RuntimeError('synthetic down readback failure')
                if name=='RX-eth0-at-stop':
                    assert not ethernet_up
                    if failure=='ethernet-lost-uart':raise TimeoutError('synthetic unavailable UART')
                if name=='RX-eth0-restore':
                    assert not ethernet_up and 'ip link set dev eth0 up' in text
                    ethernet_up=True
                if detached and name in ('RX-at-EOB','RX-stop'):assert not ethernet_up
                if failure==name:
                    if name=='RX-arm':raise TimeoutError('synthetic UART timeout')
                    raise RuntimeError('synthetic '+name+' failure')
                if name=='install':
                    crcs={n:f'{zlib.crc32((a.decoder/("runtime/"+n if n in air.RUNTIME else "g0-stream-decode")).read_bytes()):08x}' for n in [*air.RUNTIME,'probe']}
                    return ('\n'.join(crcs[Path(p).name]+' '+p for p in re.findall(r'(?m)^busybox crc32 (.+)$',text))+'\n').encode()
                if name=='report-transfer':
                    assert ethernet_up and host_management_up
                    port=int(re.search(r"create_connection\(\('127.0.0.1',(\d+)\)",text)[1])
                    with socket.create_connection(('127.0.0.1',port),5) as conn:
                        report=copy.deepcopy(base)
                        if sampled or failure in ('all-acks-no-clock','lower-only'):report=synthesize_full_acks(base,proof,sid)
                        if auto:report.update(SOURCE_auto_session=failure!='auto-receiver-report-pinned',SOURCE_sessions_adopted=1)
                        if lower_only:
                            report.update(conditional_age_lower_only=True,conditional_age_DRM_monotonic=True,
                                conditional_age_max_ns=None if failure=='lower-only' else 1,
                                conditional_age_acks=proof['presented_at_hdmi_boundary'],conditional_age_within_3s=0,
                                conditional_age_ambiguous=proof['presented_at_hdmi_boundary'],
                                source_expiry_enabled=True,source_expired_presentations=0)
                        if sampled:
                            report.update(conditional_age_DRM_monotonic=True,conditional_age_path_bound_ns=3000000,
                                          conditional_age_acks=proof['presented_at_hdmi_boundary'],
                                          conditional_age_within_3s=proof['presented_at_hdmi_boundary'],
                                          source_expiry_enabled=False)
                        if failure=='wired-clock':
                            report.update(conditional_age_DRM_monotonic=True,
                                          conditional_age_acks=proof['presented_at_hdmi_boundary'],
                                          conditional_age_within_3s=proof['presented_at_hdmi_boundary'],
                                          source_expiry_enabled=True,
                                          source_expired_presentations=0,
                                          path_calibration_anchors=[
                                              dict(sid=sid,superframe=sf,
                                                   board_rx_lower_ns=1_250_821_000+sf*20_000_000-10000,
                                                   board_rx_upper_ns=1_250_821_000+sf*20_000_000+10000)
                                              for sf in (100,250)])
                        envelope=dict(report=json.dumps(report),proof=json.dumps(proof))
                        if auto and failure is None:envelope['link']=json.dumps([{'sources_committed':0},{'sources_committed':7}])
                        conn.sendall(json.dumps(envelope).encode())
                if name in ('RX-memory-before','RX-memory-result'):
                    memory='MemAvailable: 400000 kB\nCmaFree: 60000 kB\nCmaTotal: 65536 kB\nSwapFree: 0 kB\n'
                    if name=='RX-memory-result':
                        memory=json.dumps(dict(samples=40,maximum_process_values={'VmHWM':16000},
                                                minimum_system_values_KiB={'CmaFree':51000}))+'\n'+memory
                    return memory.encode()
                if name=='wired-clock-start':return b'CLOCK_PORT=31234\n'
                if name=='camera-reference':output.write_text(json.dumps(base)+'\n')
                if name=='release-generated-probe':
                    assert ('clean-camera.aus' in text) == live
                if name=='PL-stopped-counters':
                    return (json.dumps({'PL_stopped_counters':{'sources_committed':base['SOURCE_records']}})+'\n').encode()
                if name=='RX-arm' and auto:assert ' source-dma auto ' in text and (' source-dma '+str(sid)+' ') not in text
                if name=='RX-arm' and failure=='wired-clock':assert ' age-bound-ns=3000000 enforce-age ' in text
                if name=='RX-arm' and lower_only:
                    assert ' enforce-age-lower ' in text and 'age-bound-ns=' not in text
                if name=='RX-arm' and failure in ('all-acks-no-clock','lower-only'):assert ' capture-acks ' in text
                return b''
            air.command=board;air.subprocess.Popen=Process
            # sh -n must use the real Popen; main's run() delegates through subprocess.run.
            real_run=air.subprocess.run
            def shell(*args,**kwargs):
                air.subprocess.Popen=popen
                try:return real_run(*args,**kwargs)
                finally:air.subprocess.Popen=Process
            air.subprocess.run=shell
            args=SimpleNamespace(decoder=a.decoder,symbols=a.symbols,output=case,session=sid,gain_db=12,tx_binary=Path('/synthetic/TX'))
            args.auto_session=auto
            args.capture_all_acks=failure in ('all-acks-no-clock','lower-only')
            args.observe_rx_memory=auto and failure is None
            args.observe_rx_link=args.observe_rx_memory
            if forward:args.forward_timebase=True
            args.source_expiry_lower_only=lower_only
            args.receiver_ethernet_down_during_rf=detached
            if failure=='wired-clock':
                args.age_probe_path_bound_ns=3000000
                prior=out/'synthetic-expiry-calibration.json'
                candidate=dict(completed=True,RF_started=True,EOB_confirmed=True,cleanup_LOs_off=True,
                    boot_id='synthetic-boot',bit_crc32='synthetic-bit',TX_gain_db=12,forward_timebase_requested=True,
                    review=dict(physical_ACK_identity_CRC_join_passed=True),
                    conditional_source_age=dict(physical_ACK_census_complete=True),
                    sampled_RF_path_calibration=dict(candidate_path_budget_ns=3000000,model=air.PATH_CALIBRATION_MODEL,
                        all_sampled_paths_consistent_with_budget=True,anchors=[
                            dict(superframe=sf,lower_ns=-900000,upper_ns=1200000) for sf in (100,250)]))
                prior.write_text(json.dumps(candidate))
                for boot,bit,gain in [('different-boot','synthetic-bit',12),('synthetic-boot','old-bit',12),('synthetic-boot','synthetic-bit',18)]:
                    try:air.expiry_provenance(prior,boot,bit,3000000,gain)
                    except ValueError:pass
                    else:raise AssertionError('foreign boot, bit or gain accepted as expiry evidence')
                for update in [dict(model='affine-only'),dict(anchors=[{},{}]),
                               dict(anchors=[dict(superframe=sf,lower_ns=-900000,upper_ns=3000001) for sf in (100,250)])]:
                    bad=copy.deepcopy(candidate);bad['sampled_RF_path_calibration'].update(update)
                    prior.write_text(json.dumps(bad))
                    try:air.expiry_provenance(prior,'synthetic-boot','synthetic-bit',3000000,12)
                    except ValueError:pass
                    else:raise AssertionError('invalid sampled calibration accepted as expiry evidence')
                prior.write_text(json.dumps(candidate))
                assert air.expiry_provenance(prior,'synthetic-boot','synthetic-bit',3000000,12)
                args.tx_binary=out/'synthetic-TX-build'
                args.tx_binary.write_bytes(b'SYNTHETIC TX IDENTITY')
                args.rf_setup=out/'synthetic-rf-setup.json'
                description=dict(schema='G0-rf-setup-v1',setup_id='synthetic-ten-metre',
                                 antenna_separation_m=10,distance_measurement='synthetic',geometry='synthetic')
                args.rf_setup.write_text(json.dumps(description))
                _,setup=air.read_rf_setup(args.rf_setup,args.tx_binary,air.RX_SETUP,[0,1])
                decoder_sha=hashlib.sha256((a.decoder/'g0-stream-decode').read_bytes()).hexdigest()
                try:air.expiry_provenance(prior,'synthetic-boot','synthetic-bit',3000000,12,setup,decoder_sha)
                except ValueError:pass
                else:raise AssertionError('unbound historical calibration accepted for recorded setup')
                config4=dict(TX_binary_sha256=setup['TX_binary_sha256'],RX_commands_sha256=setup['RX_commands_sha256'],
                             RX_bandwidth_hz=4000000,logical_IQ_by_RF_port=[0,1])
                candidate.update(RF_setup=setup,decoder_binary_sha256=decoder_sha,
                                 sampled_age_RF_configuration=config4,full_physical_ACK_census_requested=True)
                prior.write_text(json.dumps(candidate))
                changed_configs=[
                    dict(setup,logical_IQ_by_RF_port=[1,0]),
                    dict(setup,RX_commands_sha256='different RX commands'),
                    dict(setup,TX_binary_sha256='different TX binary'),
                    dict(setup,description_sha256='different geometry'),
                ]
                for wrong,decoder in [(s,decoder_sha) for s in changed_configs]+[(setup,'different decoder'),(None,decoder_sha)]:
                    try:air.expiry_provenance(prior,'synthetic-boot','synthetic-bit',3000000,12,wrong,decoder)
                    except ValueError:pass
                    else:raise AssertionError('unmatched setup or decoder accepted for calibration')
                args.source_expiry_proof=prior
            if live:args.live_camera_frames=200
            if sampled:
                args.capture_all_acks=True
                args.rx_bandwidth_hz=2000000
                args.age_probe_path_bound_ns=3000000
                args.tx_binary=out/'synthetic-TX-build'
                prior=out/'synthetic-expiry-calibration.json'
                candidate=json.loads(prior.read_text())
                candidate.pop('RF_setup')
                setup_2mhz=air.rx_setup_for_bandwidth(2000000)
                config=dict(TX_binary_sha256=hashlib.sha256(args.tx_binary.read_bytes()).hexdigest(),
                            RX_commands_sha256=hashlib.sha256(setup_2mhz.encode()).hexdigest(),
                            RX_bandwidth_hz=2000000,logical_IQ_by_RF_port=[0,1])
                candidate.update(sampled_age_RF_configuration=config,full_physical_ACK_census_requested=True)
                args.observe_sampled_age_proof=out/'synthetic-sampled-age-calibration.json'
                for bad in (dict(candidate,sampled_age_RF_configuration=dict(config,RX_bandwidth_hz=4000000)),
                            dict(candidate,full_physical_ACK_census_requested=False)):
                    args.observe_sampled_age_proof.write_text(json.dumps(bad))
                    try:air.main(args)
                    except ValueError as error:assert 'sampled age calibration requires matching' in str(error)
                    else:raise AssertionError('unmatched or incomplete sampled age calibration accepted')
                    assert not events and not case.exists()
                args.observe_sampled_age_proof.write_text(json.dumps(candidate))
            if failure=='setup-wrong-map':
                args.tx_binary=out/'synthetic-TX-build'
                args.rf_setup=out/'synthetic-rf-setup.json'
            if failure in ['clock-unavailable','clock-proof-missing']:
                args.observe_clock=True;args.camera_clock_control=case/'does-not-exist.json'
            if failure=='lower-only':
                args.observe_clock=True
                args.receiver_host_profile=('synthetic-mgmt','synthetic-profile')
                args.camera_clock_control=out/'synthetic-v4l2-clock.json'
                args.camera_clock_control.write_text(json.dumps(dict(completed=True,received=3,
                    buffers=[dict(timestamp_type=8192,timestamp_source=65536) for _ in range(3)])))
            success=sampled or failure in [None,'clock-unavailable','clock-proof-missing','PL-stopped-counters','forward-timebase','wired-clock','all-acks-no-clock','lower-only']
            try:
                with contextlib.redirect_stdout(io.StringIO()):air.main(args)
                assert success
            except (RuntimeError,TimeoutError,BrokenPipeError):assert not success
            except ValueError as error:
                if failure=='lower-only-upper-claim':assert 'unsupported upper age claim' in str(error)
                else:assert failure=='setup-wrong-map' and 'logical IQ mapping' in str(error)
            except AssertionError:assert failure=='auto-receiver-report-pinned'
            finally:air.subprocess.run=real_run
            receipt=json.loads((case/'receipt.json').read_text())
            receipt['transport_simulated_no_hardware_access']=True
            (case/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
            if failure in ['preflight','RX-arm']:assert not receipt['GO_attempted']
            if failure=='setup-wrong-map':
                assert not receipt['GO_attempted'] and not receipt['RF_started'] and receipt['cleanup_LOs_off']
                assert 'RX-arm' not in events
            if failure=='preflight':assert 'RX-idle' not in events and not txes
            if failure=='RX-arm':assert 'RX-idle' not in events and receipt['cleanup_unconfirmed']
            if failure=='RX-stop':assert 'RX-idle' in events and receipt['cleanup_LOs_off']
            if success:
                assert receipt['completed'] and receipt['EOB_confirmed'] and receipt['cleanup_LOs_off']
                key='finite_live_camera_RF_DMA_decode_HDMI_clean_join_passed' if live else 'finite_prerecorded_RF_DMA_decode_HDMI_clean_join_passed'
                assert receipt['review'][key]
                if live:
                    assert events.index('RX-arm')<events.index('camera-GO') and receipt['camera_GO_sent_after_RX_ready']
                    assert receipt['generated_clean_reference_released']
            if failure in ['camera-not-ready','camera-GO-pipe']:
                assert not receipt['GO_attempted'] and not receipt['RF_started']
            if args.observe_rx_memory:
                assert events.index('RX-arm')<events.index('RX-memory-start')<events.index('camera-GO')
                assert events.index('RX-stop')<events.index('RX-memory-result')<events.index('report-transfer')
                assert receipt['RX_memory_before_KiB']['CmaFree']==60000
                assert receipt['RX_memory_during_decode']['samples']==40
                assert receipt['RX_memory_during_decode']['maximum_process_values']['VmHWM']==16000
                assert receipt['RX_memory_after_KiB']['MemAvailable']==400000
                assert receipt['RX_link_observation_samples']==2
                assert json.loads((case/'RX-link-observations.json').read_text())[-1]['sources_committed']==7
            if failure=='camera-reference':assert receipt['RF_measurement_complete'] and not receipt['completed'] and receipt['cleanup_LOs_off']
            if failure=='clock-unavailable':assert receipt['optional_clock_error'] and receipt['completed']
            if failure=='clock-proof-missing':assert receipt['optional_clock_age_unverified'] and receipt['completed']
            if failure=='PL-stopped-counters':assert receipt['optional_PL_counters_unverified'] and receipt['completed']
            if failure=='all-acks-no-clock':
                assert receipt['review']['full_physical_picture_clean']
                assert not any('clock' in event for event in events)
            if forward:
                assert receipt['forward_timebase_requested']
                assert json.loads((case/'tx-clock.json').read_text())==[1000000000,1000100000,2000000,3000000]
                assert receipt['TX_clock_rate_observation']['observed_rate_within_projection']
            if failure=='wired-clock':
                assert events.index('wired-clock-start')<events.index('RX-arm')
                assert events.index('RX-at-EOB')<events.index('wired-clock-stop')<events.index('RX-stop')
                assert receipt['conditional_source_age']['all_observed_ACKs_conditionally_within_3s']
                assert receipt['sampled_RF_path_calibration']['all_sampled_paths_consistent_with_budget']
                assert receipt['source_expiry']['runtime_enabled'] and receipt['source_expiry']['age_bound_used']=='upper'
                assert receipt['RF_setup']==setup and receipt['decoder_binary_sha256']==decoder_sha
                assert (case/'RF-setup.json').read_bytes()==args.rf_setup.read_bytes()
            if sampled:
                assert not any('clock' in event for event in events)
                assert receipt['sampled_age_observation_calibration']
                assert receipt['sampled_age_RF_configuration']==config
                assert receipt['conditional_source_age']['all_observed_ACKs_conditionally_within_3s']
                assert 'source_expiry' not in receipt and 'sampled_RF_path_calibration' not in receipt
                arm=(case/'RX-arm.sh').read_text()
                assert 'age-bound-ns=3000000' in arm and 'enforce-age' not in arm
            if lower_only:
                assert not any(event.startswith('wired-clock') for event in events)
                if success:
                    assert receipt['source_expiry']['runtime_enabled'] and receipt['source_expiry']['lower_bound_only']
                    age=receipt['conditional_source_age']
                    assert age['physical_ACK_census_complete'] and not age['all_observed_ACKs_conditionally_within_3s']
                    assert age['conditional_age_max_ns'] is None
                    assert receipt['host_management_profile_reactivated']
                    assert events.index('RX-eth0-restore')<events.index('host-management-profile-restored')<events.index('report-transfer')
                    uart_age=receipt['development_wired_clock_all_source_age']
                    assert uart_age['conditional_all_source_frames_under3s']
                    assert not uart_age['production_or_standalone_age_acceptance_passed']
                    for i in range(3):
                        assert events.index(f'clock-before-{i}')<events.index('RX-eth0-down')
                        assert events.index('RX-eth0-restore')<events.index(f'clock-after-{i}')<events.index('report-transfer')
                else:assert not receipt['completed'] and receipt['cleanup_LOs_off']
            if detached:
                assert events.index('RX-eth0-preflight')<events.index('RX-eth0-down')
                if failure=='ethernet-down-failure':
                    assert not receipt['GO_attempted'] and receipt['receiver_ethernet_restored'] and ethernet_up
                elif failure=='ethernet-lost-uart':
                    assert receipt['EOB_confirmed'] and receipt['receiver_ethernet_restore_unconfirmed']
                    assert not ethernet_up and 'RX-eth0-restore' not in events
                else:
                    assert events.index('RX-eth0-down')<events.index('camera-GO')<events.index('RX-stop')
                    assert events.index('RX-eth0-at-stop')<events.index('RX-eth0-restore')<events.index('report-transfer')
                    assert receipt['receiver_ethernet_down_during_RF'] and receipt['receiver_ethernet_restored'] and ethernet_up
            if auto and success:
                assert not receipt['receiver_out_of_band_session_required'] and receipt['receiver_autonomous_session_startup_observed']
                assert receipt['receiver_session_adoptions']==1
            if failure=='auto-receiver-report-pinned':assert not receipt['completed'] and receipt['cleanup_LOs_off']
            results.append(dict(name=('auto_' if auto else '')+('synthetic_live_transport_' if live else 'synthetic_transport_')+str(failure),events=events,passed=True))
    finally:
        for n,value in saved.items():setattr(air,n,value)
        air.subprocess.Popen=popen
    return results

def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False)
    results=[]
    setup_file=out/'invalid-rf-setup.json'
    for invalid in (0, -10, '10', True, float('inf')):
        setup_file.write_text(json.dumps(dict(schema='G0-rf-setup-v1',setup_id='synthetic',
            antenna_separation_m=invalid,distance_measurement='synthetic',geometry='synthetic')))
        try:air.read_rf_setup(setup_file,a.decoder/'g0-stream-decode',air.RX_SETUP,[0,1])
        except ValueError:pass
        else:raise AssertionError('invalid geometry accepted')
    results.append(dict(name='invalid_RF_distance_rejected_before_hardware',passed=True))
    requests=SimpleNamespace(capture_all_acks=True,age_probe_path_bound_ns=10000000,source_expiry_proof=True,
                             source_expiry_lower_only=True)
    options=[b'capture-acks',b'age-bound-ns=',b'enforce-age',b'enforce-age-lower']
    air.require_decoder_measurement_options(b'\0'.join(options)+b'\0',requests)
    for missing in options:
        binary=b'\0'.join(option for option in options if option!=missing)+b'\0'
        try:air.require_decoder_measurement_options(binary,requests)
        except ValueError as error:assert missing.decode() in str(error)
        else:raise AssertionError('decoder measurement option absent but accepted')
    results.append(dict(name='decoder_measurement_options_preflight',passed=True))
    begin=air.TxClockBracket(1_000_000_000,1_000_010_000,2_000_000,3_000_000)
    end=(61_000_000_000,61_000_010_000,242_000_000)
    wired=[(t-40000,t,t+3000,t+48000) for t in range(1_000_000_000,61_000_000_001,1_000_000_000)]
    # The endpoint rate is nominal, but a 150ppm speed-up then slow-down
    # within the allowed rate can put interior transmit times ahead of it.
    anchors=[]
    for frame in (1495,1500):
        tx=begin.start_ticks+frame*TX_PERIOD_SAMPLES+TX_TRAINING_SAMPLE
        elapsed=(tx-begin.observed_ticks)*250
        actual_tx=begin.before_ns+5000+elapsed-min(elapsed,60_000_000_000-elapsed)*150//1_000_000
        rx=actual_tx+5_500_000
        anchors.append(dict(sid=1,superframe=frame,board_rx_lower_ns=rx-10000,board_rx_upper_ns=rx+10000))
    calibration=air.path_delay_bounds(wired,anchors,begin,end,1,3_000_000)
    assert not calibration['all_sampled_paths_consistent_with_budget']
    assert all(row['affine_upper_ns']<3_000_000 and row['lower_ns']<=5_500_000<=row['upper_ns']
               for row in calibration['anchors'])
    results.append(dict(name='bounded_rate_reversal_inside_nominal_endpoints',passed=True,calibration=calibration))
    class Tx:
        def __init__(self,rc=0,write_fail=False):
            self.stdin=io.StringIO();self.rc=rc
            if write_fail:
                class Broken(io.StringIO):
                    def write(self,value):raise BrokenPipeError('injected partial/uncertain GO write')
                self.stdin=Broken()
        def poll(self):return None
        def wait(self,timeout):assert timeout==10;return self.rc
    class Ready:
        def __init__(self,value):self.value=value
        def wait(self,timeout):assert timeout==30;return self.value
    for name,ready,arm_error,txrc,broken in [
        ('normal',True,None,0,False),('not_ready',False,None,0,False),
        ('RX_arm_failed',True,RuntimeError,0,False),('UART_arm_timeout',True,TimeoutError,0,False),
        ('TX_failed',True,None,1,False),('GO_pipe_uncertain',True,None,0,True)]:
        events=[];tx=Tx(txrc,broken)
        def arm():
            events.append('arm')
            if arm_error:raise arm_error('injected arm failure')
        try:
            air.finite_exchange(tx,Ready(ready),arm,lambda:events.append('after'),
                                lambda:events.append('GO_attempt'),lambda:events.append('EOB'),10)
            assert name=='normal'
        except (RuntimeError,TimeoutError,BrokenPipeError):assert name!='normal'
        if name=='normal':assert events==['arm','GO_attempt','EOB','after'] and tx.stdin.getvalue()=='GO\n'
        if name in ['not_ready','RX_arm_failed','UART_arm_timeout']:
            assert 'GO_attempt' not in events and tx.stdin.getvalue()==''
        if name in ['TX_failed','GO_pipe_uncertain']:assert 'GO_attempt' in events and 'EOB' not in events
        results.append(dict(name=name,events=events,passed=True))
    # Real saved native evidence provides schema and pixels; all mutations below
    # are synthetic review controls, never physical RF evidence.
    base=json.loads((a.hdmi_reference/'ordered_clean_reordered.json').read_text())
    proof=json.loads((a.hdmi_reference/'ordered_clean_reordered.physical.json').read_text())
    dma=json.loads((a.stop_native/'DMA_external_stop.json').read_text())
    for k,v in dma.items():
        if k.startswith('SOURCE_DMA_'):base[k]=v
    base['SOURCE_records']=2912  # synthetic SOURCE count; file/empty DMA never proves this join
    expected={x['capture_id']&0xffffffff:x['rgb565_crc32'] for x in base['pixels']}
    sid=base['pixels'][0]['capture_id']>>32
    good=air.review_video(base,proof,expected,sid)
    assert good['finite_prerecorded_RF_DMA_decode_HDMI_clean_join_passed']
    results+=full_ack_quality_controls(base,proof,expected,sid)
    results+=transport_controls(a,out,base,proof,expected,sid)
    results+=transport_controls(a,out,base,proof,expected,sid,live=True)
    results+=transport_controls(a,out,base,proof,expected,sid,live=True,auto=True)
    video=json.loads((out/'auto-live-transport-wired-clock/receipt.json').read_text())
    assert match_rf_evidence(video,copy.deepcopy(video))['RF_setup']==video['RF_setup']
    for wrong in (dict(video,boot_id='new boot'),dict(video,TX_gain_db=18),
                  dict(video,RF_setup=None),dict(video,RF_setup=dict(video['RF_setup'],description_sha256='changed'))):
        try:match_rf_evidence(video,wrong)
        except ValueError:pass
        else:raise AssertionError('unmatched video and capacity receipts accepted')
    results.append(dict(name='capacity_video_pair_requires_same_RF_context',passed=True))
    notice=air.review_video(dict(base,HDMI_signal_loss_notices=1),
                            dict(proof,signal_loss_notices=1,last_signal_loss_ack_ns=1),expected,sid)
    assert notice['physical_ACK_identity_CRC_join_passed']
    assert notice['physical_new_frames']==good['physical_new_frames'] and notice['signal_loss_notices']==1
    results.append(dict(name='signal_loss_ack_separate',review=notice,passed=True))
    for name in ['missing','CRC_changed','unknown_session','duplicate','owner_retained','proof_CRC_changed','zero_SOURCE','zero_records_only','notice_count_mismatch']:
        r=copy.deepcopy(base);p=copy.deepcopy(proof)
        if name=='missing':r['pixels'].pop(2)
        if name=='CRC_changed':r['pixels'][2]['rgb565_crc32']^=1
        if name=='unknown_session':r['pixels'][2]['capture_id']^=1<<32
        if name=='duplicate':r['pixels'].append(r['pixels'][2])
        if name=='owner_retained':r['SOURCE_DMA_stopped_ready']=1
        if name=='proof_CRC_changed':p['records'][0]['payload_crc32']^=1
        if name=='zero_SOURCE':r['pixels']=[];r['SOURCE_records']=0
        if name=='zero_records_only':r['SOURCE_records']=0
        if name=='notice_count_mismatch':p['signal_loss_notices']=1
        review=air.review_video(r,p,expected,sid)
        assert not review['finite_prerecorded_RF_DMA_decode_HDMI_clean_join_passed']
        assert not review['full_G0_or_video_passed']
        results.append(dict(name=name,review=review,passed=True))
    # Actual unfinished R9 qualification must fail before HardwareLease, with
    # no output creation or device/network access.
    def forbidden():raise AssertionError('hardware access before qualification')
    old=air.HardwareLease;air.HardwareLease=forbidden
    args=SimpleNamespace(platform=a.platform,ram_assets=a.platform,boot_log=a.platform,output=out/'must-not-exist')
    try:
        try:air.main(args);raise AssertionError('unfinished R9 unexpectedly qualified')
        except FileNotFoundError as error:
            assert str(error.filename).endswith('/route-review.json')
            results.append(dict(name='actual_R9_missing_route_blocks_before_hardware',passed=True,error=str(error)))
    finally:air.HardwareLease=old
    assert not args.output.exists()
    for remote in ['/tmp/g0-video-run','/tmp/'+'x'*64]:
        script=air.stop_script(remote)
        subprocess.run(['sh','-n'],input=script,text=True,check=True)
        assert max(map(len,script.splitlines()))<=470
        code=script.split("<<'V3STOP'\n",1)[1].split('\nV3STOP',1)[0]
        compile(code,'owned-stop','exec')
    result=dict(scope=__doc__,completed=True,controls=results,synthetic_controls_not_RF_evidence=True,
                RF_started=False,hardware_accessed=False,full_G0_or_video_passed=False)
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print('VIDEO_AIR_OFFLINE_CONTROLS_PASS',len(results))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','hdmi-reference','stop-native','platform','decoder','symbols']:p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
