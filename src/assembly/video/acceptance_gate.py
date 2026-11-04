"""Finite 10-AU core gate; no TX payload or hardware input."""
import json

def check(native,video,proof,frame_ids):
    problems=[]
    from receive_gate import check_native
    try: check_native(native, frame_ids)
    except (ValueError, TypeError) as error: problems.append(str(error))
    accepted=video.get('accepted',[])
    if not video.get('valid') or len(accepted)!=10:problems.append('video did not accept ten AUs')
    if [p.get('sequence') for p in accepted]!=list(range(10)) or any(p.get('session_id')!=714 for p in accepted):problems.append('accepted AU identity/order')
    t=video.get('terminal',{})
    if not (t.get('stop_ack') is True and t.get('helper_returncode')==0 and t.get('presenter_returncode')==0 and t.get('pending')==0 and t.get('workers_alive')==[] and t.get('session_state')=='closed'):problems.append('unclean pipeline termination')
    profile=[]
    for line in video.get('diagnostics',{}).get('helper_stderr','').splitlines():
        if line.startswith('CODEC_PROFILE_V1 '):
            try:profile.append(json.loads(line.split(' ',1)[1]))
            except ValueError:pass
    if not profile or profile[-1].get('frames')!=10:problems.append('decoder did not output ten frames')
    if not (proof.get('physical') is True and proof.get('acceptance_valid') is True and proof.get('capture_status')=='complete' and proof.get('presented_at_hdmi_boundary')==10 and proof.get('proof_failures')==0 and proof.get('backend_failures')==0 and not proof.get('failed_closed') and not proof.get('capture_invalidated')):problems.append('no ten valid physical HDMI boundaries')
    if proof.get('scanout_repeats',0)<1000:problems.append('idle same-buffer refresh missing')
    records=proof.get('records',[])
    if len(records)!=10:problems.append('proof record count')
    if [p.get('source_frame_id') for p in records]!=list(range(10)) or any(p.get('source_session_id')!=714 for p in records):problems.append('physical AU identity/order')
    try:
        tokens=[p['token'] for p in records]
        if len(set(tokens))!=len(tokens):problems.append('duplicate physical source token')
        codec_tokens=[a['token'] for a in accepted]
        if len(set(codec_tokens))!=10 or any(type(t) is not int or t<=0 for t in codec_tokens):problems.append('codec token namespace invalid')
        # Presenter repeat submits consume scanout tokens; codec tokens are a different namespace.
        last_time=-1
        for index,p in enumerate(records):
            if not all(type(p.get(k)) is int and p[k]>0 for k in ['token','drm_sequence','observer_session']):problems.append('invalid physical ACK identity')
            if index>=len(accepted) or (p['source_session_id'],p['source_frame_id'],p['source_pts_ns'],p['payload_crc32'])!=(accepted[index]['session_id'],accepted[index]['sequence'],accepted[index]['source_pts_ms']*1000000,int(accepted[index]['accepted_payload_crc32'],16)):problems.append('source identity/PTS/RGB receipt mismatch')
            stamp=p['drm_tv_sec']*1000000+p['drm_tv_usec']
            if stamp<=last_time:problems.append('nonmonotonic physical presentation time')
            last_time=stamp
    except (KeyError,ValueError,TypeError):problems.append('malformed physical proof')
    return problems
