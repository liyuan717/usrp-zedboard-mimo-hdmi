"""Finite first-AU technical gate. No hardware access and no TX payload input."""
import json

def check(native,video,proof,frame_ids):
    problems=[]
    if native.get('captured_blocks')!=2048:problems.append('incomplete fresh ADC')
    if native.get('crc_rejected_frames')!=0:problems.append('PL CRC rejection')
    count=native.get('detected_frames',0)
    if count not in (41,42):problems.append('unexpected decoded record count')
    ipc=native.get('delivery',{})
    if ipc.get('ipc_enqueued_records')!=count or ipc.get('ipc_dropped_records')!=0:problems.append('IPC count/drop')
    if len(frame_ids)!=count or sorted(set(frame_ids))!=list(range(1,42)):problems.append('missing unique RF business frame')
    if any(frame_ids.count(k)!=(2 if k==1 and count==42 else 1) for k in range(1,42)):problems.append('unexpected duplicate/replay')
    if not video.get('valid') or len(video.get('accepted',[]))!=1:problems.append('video did not accept one AU')
    t=video.get('terminal',{})
    if not (t.get('stop_ack') is True and t.get('helper_returncode')==0 and t.get('presenter_returncode')==0 and t.get('pending')==0 and t.get('workers_alive')==[] and t.get('session_state')=='closed'):problems.append('unclean pipeline termination')
    profile=[]
    for line in video.get('diagnostics',{}).get('helper_stderr','').splitlines():
        if line.startswith('CODEC_PROFILE_V1 '):
            try:profile.append(json.loads(line.split(' ',1)[1]))
            except ValueError:pass
    if not profile or profile[-1].get('frames',0)<1:problems.append('no decoder output frame')
    if not (proof.get('physical') is True and proof.get('acceptance_valid') is True and proof.get('capture_status')=='complete' and proof.get('presented_at_hdmi_boundary')==1 and proof.get('proof_failures')==0 and proof.get('backend_failures')==0 and not proof.get('failed_closed') and not proof.get('capture_invalidated')):problems.append('no valid physical HDMI boundary')
    if proof.get('scanout_repeats',0)<100:problems.append('idle scanout refresh missing')
    records=proof.get('records',[])
    if len(records)!=1:problems.append('proof record count')
    else:
        p=records[0]
        if not all(type(p.get(k)) is int and p[k]>0 for k in ['token','drm_sequence','observer_session']):problems.append('invalid physical ACK identity')
        if p.get('source_session_id')!=714 or p.get('source_frame_id')!=0:problems.append('wrong video source identity')
        accepted=video.get('accepted',[])
        try:
            if not accepted or int(accepted[0]['accepted_payload_crc32'],16)!=p.get('payload_crc32'):problems.append('RGB receipt mismatch')
        except (KeyError,ValueError,TypeError):problems.append('invalid RGB receipt')
    return problems
