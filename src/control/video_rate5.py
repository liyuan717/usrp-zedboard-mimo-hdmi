"""Fixed finite new-OTA 5fps adapter; importing/preparing never uses hardware."""
from pathlib import Path
import hashlib, io, json, tarfile, types
from measured_video import make_adapter, R, D, BASE, wf


def cadence(proof):
    rows = proof['records']
    if len(rows) != 10 or proof['presented_at_hdmi_boundary'] != 10:
        raise ValueError('need all ten physical presentations')
    if [r['source_frame_id'] for r in rows] != list(range(10)):
        raise ValueError('physical frame sequence')
    pts = [r['source_pts_ns'] for r in rows]
    if [p-pts[0] for p in pts] != [n*200000000 for n in range(10)]:
        raise ValueError('source PTS must remain original 5fps')
    times = [r['drm_tv_sec']+r['drm_tv_usec']/1e6 for r in rows]
    gaps = [(b-a)*1000 for a,b in zip(times,times[1:])]
    if not all(g>0 for g in gaps):
        raise ValueError('nonmonotonic physical timestamps')
    return dict(nominal_source_fps=5, source_schedule_scale=1,
                physical_intervals_ms=gaps, physical_span_s=times[-1]-times[0],
                physical_span_fps=9/(times[-1]-times[0]),
                min_physical_interval_ms=min(gaps), max_physical_interval_ms=max(gaps),
                continuous_realtime_verified=False, visual_confirmation_pending=True,
                receipt_is_not_visual_acceptance=True)


def make_rate5(out):
    out = Path(out)
    cls, original_build = make_adapter('priority', out)
    # A private grader preserves every original ADC/packet/AU/pixel gate.
    # Only expected playback scale/duration and its asset root are changed.
    grader = out/'grader'; grader.mkdir()
    text = (BASE/'grade_evidence.py').read_text()
    changes = [("D=HERE.parent", "D=Path("+repr(str(D))+")"),
               ("pace.get('scale')!=5", "pace.get('scale')!=1"),
               ("<8.9:raise ValueError('short video played as a burst')",
                "<1.79:raise ValueError('short video played as a burst')")]
    for before, after in changes:
        if text.count(before) != 1: raise ValueError('grader anchor drift: '+before)
        text = text.replace(before, after)
    compile(text, 'rate5-independent-grader', 'exec')
    (grader/'grade_evidence.py').write_text(text)
    for name in ('acceptance_gate.py', 'receive_gate.py'):
        (grader/name).write_bytes((BASE/name).read_bytes())
    # make_adapter creates its own private VideoManager class per invocation.
    # Rebind only that class's grade function globals, not the shared workflow.
    parent = cls.__mro__[1]
    original_grade = parent.grade
    context = dict(original_grade.__globals__, BASE=grader)
    parent.grade = types.FunctionType(original_grade.__code__, context, original_grade.__name__,
                                     original_grade.__defaults__, original_grade.__closure__)

    def build(path, run_id, boot_id, secret):
        original_build(out/'slow-app.tar.gz', run_id, boot_id, secret)
        with tarfile.open(out/'slow-app.tar.gz') as archive:
            members = archive.getmembers()
            files = {m.name:archive.extractfile(m).read() for m in members}
            modes = {m.name:m.mode for m in members}
        source = files['board_supervisor.py'].decode()
        changes = [("auth['display_time_scale'] == 5", "auth['display_time_scale'] == 1"),
                   ("'--diagnostic-time-scale', '5'", "'--diagnostic-time-scale', '1', '--diagnostic-pace-source-pts'"),
                   ("pace.get('scale') == 5", "pace.get('scale') == 1"),
                   (">= 8.9, 'video compressed into a burst'", ">= 1.79, 'video compressed into a burst'")]
        for before,after in changes:
            if source.count(before) != 1:raise ValueError('supervisor anchor drift: '+before)
            source = source.replace(before,after)
        needle="result['video_report'].get('diagnostic_source_pacing', {})"
        if source.count(needle)!=1:raise ValueError('pacing-report guard anchor drift')
        source=source.replace(needle,"(result['video_report'].get('diagnostic_source_pacing') or {})")
        compile(source,'rate5-board-supervisor','exec')
        files['board_supervisor.py'] = source.encode()
        auth = json.loads(files['AUTHORIZATION.json']);auth['display_time_scale']=1
        files['AUTHORIZATION.json']=json.dumps(auth).encode()
        rows=[dict(name=n,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
              for n,raw in files.items() if n!='FILES.json']
        files['FILES.json']=json.dumps(rows).encode()
        with tarfile.open(path,'x:gz') as archive:
            for n,raw in files.items():
                info=tarfile.TarInfo(n);info.size=len(raw);info.mode=modes[n]
                archive.addfile(info,io.BytesIO(raw))
        review=json.loads((out/'BUNDLE-REVIEW.json').read_text())
        review.update(display_nominal_fps=5,display_time_scale=1,files=rows,
                      changes_from_approved_priority_round='source-PTS display scale5->1 only; same accelerated ARM native01af, original waveform131/ADC2048/10AU, existing priority module',
                      driver_is_existing_experimental_candidate=True,visual_pass=False,promoted=False)
        wf.write_json(out/'BUNDLE-REVIEW.json',review)
        return rows

    class Rate5Manager(cls):
        def preflight(self):
            super().preflight()
            p=self.run/'video-profile.json';v=json.loads(p.read_text())
            v.update(diagnostic_display_fps=5,display_time_scale=1,
                     actual_TX_stream_sha256='80b0be13f751d4d093db1a237afa5dea7f5da44ea46915143923498a858c3741')
            wf.write_json(p,v)
        def grade(self,evidence):
            result=super().grade(evidence)
            result['presentation_cadence']=cadence(json.loads((evidence/'hdmi-proof.json').read_text()))
            return result
    return Rate5Manager,build
