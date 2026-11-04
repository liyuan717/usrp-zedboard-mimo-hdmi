"""Fixed Sintel25 candidate. Preparation/imports never access hardware."""
from pathlib import Path
import ast,hashlib,inspect,io,json,os,subprocess,tarfile,textwrap
from video_rate5 import make_rate5
from measured_video import R,W,D,BASE,N,wf
from finite_tx_trigger import feed_and_trigger

MEDIA=R/'candidates/video25-sintel-r1'
PRESENTER=R/'candidates/hdmi-source-pts-r1/build-arm-r2/hdmi-presenter'
CAPTURE_NATIVE=R/'candidates/capture3840-r2/validation/live-rx-arm'
SCOPE='finite Sintel25 RF347; fresh3840ADC and realPL; this-run RFD nativePTS HDMI only'
BLOCKS=3840
SAMPLES=1332480
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()


def change(text,before,after,count=1):
    if text.count(before)!=count:raise ValueError('fixed template drift: '+before)
    return text.replace(before,after)


def cadence25(proof):
    rows=proof['records']
    if len(rows)!=25 or proof['presented_at_hdmi_boundary']!=25 or proof.get('source_pts_pacing_enabled') is not True:
        raise ValueError('missing25 nativePTS physical presentations')
    if [x['source_frame_id'] for x in rows]!=list(range(25)) or any(x['source_pts_ns']!=n*200000000 for n,x in enumerate(rows)):
        raise ValueError('physical source sequence/PTS changed')
    stamps=[x['drm_tv_sec']+x['drm_tv_usec']/1e6 for x in rows]
    gaps=[(b-a)*1000 for a,b in zip(stamps,stamps[1:])]
    if not all(x>0 for x in gaps):raise ValueError('physical event time not monotonic')
    refresh=proof['mode_refresh_millihz']/1000
    if not 10<=refresh<=240:raise ValueError('physical refresh out of bound')
    ticks=round(.2*refresh);expected_ms=ticks*1000/refresh
    sequences=[(b['drm_sequence']-a['drm_sequence'])&0xffffffff for a,b in zip(rows,rows[1:])]
    return dict(nominal_source_fps=5,frames=25,physical_intervals_ms=gaps,physical_vsync_gaps=sequences,
                quantized_target_vsyncs=ticks,quantized_target_fps=refresh/ticks,
                physical_span_s=stamps[-1]-stamps[0],physical_span_fps=24/(stamps[-1]-stamps[0]),
                nominal_5fps_grid_followed=all(n==ticks for n in sequences) and all(abs(n-expected_ms)<1000/refresh for n in gaps),
                visual_confirmation_pending=True,continuous_realtime_verified=False)


def make_video25(out):
    out=Path(out)
    if sha(MEDIA/'FROZEN-MEDIA.json')!='020f36c6816b66a941d898ec8b1bd5c69d46f8566473055969597e4ee9921735':raise ValueError('media manifest pin')
    if sha(MEDIA/'PIXEL-REFERENCE.json')!='544057373ad9f58c0304958b7d528cc490cfba8bdd9280d2c3b72bccf9dd6f76':raise ValueError('pixel reference pin')
    manifest=json.loads((MEDIA/'FROZEN-MEDIA.json').read_text())
    if (manifest['AUs'],manifest['unique_RF_frames'],manifest['RF_bursts'],manifest['samples_per_channel'])!=(25,344,347,SAMPLES):
        raise ValueError('unexpected frozen Sintel profile')
    if sha(PRESENTER)!='9e822fb1a71d8c0e77202ad377daeb62ed5ec58c4637a359ab79641413328787':raise ValueError('presenter pin')
    if sha(CAPTURE_NATIVE)!='aaec3de2754be29e1c694385265887f884b1a73c4be7e7d970608315b6be4cca':raise ValueError('tested3840 receiver pin')
    for name,digest in json.loads((BASE/'CANDIDATE-MANIFEST.json').read_text())['critical_sha256'].items():
        if sha(name)!=digest:raise ValueError('original staged baseline changed: '+name)
    for name,item in manifest['files'].items():
        path=MEDIA/name if name.endswith(('.iqb','.rfd')) else MEDIA/'tx-offline'/name
        if sha(path)!=item['sha256']:raise ValueError('media pin: '+name)
    cls,old_build=make_rate5(out)
    measured_grade=cls.__mro__[1].grade
    grader=out/'grader'
    receive=(grader/'receive_gate.py').read_text()
    for a,b in [('range(128, 132)','range(344, 348)'),('count - 127','count - 343'),('range(2, 129)','range(2, 345)'),("!= 2048","!= 3840")]:receive=change(receive,a,b)
    receive=receive.replace('range(10)','range(25)')
    (grader/'receive_gate.py').write_text(receive)
    # All occurrences of integer10 in this small gate are AU/token budgets.
    acceptance=(grader/'acceptance_gate.py').read_text()
    class Budget(ast.NodeTransformer):
        def visit_Constant(self,node):
            return ast.copy_location(ast.Constant(25),node) if type(node.value) is int and node.value==10 else node
    acceptance=ast.unparse(Budget().visit(ast.parse(acceptance)))+'\n'
    (grader/'acceptance_gate.py').write_text(acceptance)
    text=(grader/'grade_evidence.py').read_text().replace('range(10)','range(25)')
    text=change(text,"len(source)!=10 or len(aus)!=10","len(source)!=25 or len(aus)!=25")
    text=change(text,'expected_blocks=2048','expected_blocks=3840')
    text=change(text,"LEDGER=D/'live-baseline/tx-live02/tx-packets.jsonl'",'LEDGER=Path('+repr(str(MEDIA/'tx-offline/tx-packets.jsonl'))+')')
    text=change(text,"ledger=D/'live-baseline/tx-live02/source-ledger.jsonl'",'ledger=Path('+repr(str(MEDIA/'tx-offline/source-ledger.jsonl'))+')')
    for old,name in [('8b4676fd82a3469a89ee79a7237d75d6ddbdd69c1a9bf84d892a3b8055b319bb','tx-packets.jsonl'),
                     ('1a9bf67de5738c390a76e50e187e5f4f8e4905c80c4336801b9153d456a1b1c6','source-ledger.jsonl'),
                     ('1a5ee998aa5d7fcd1ef57d65ddfd949719151ecf0abfad3747543432feb7780b','encoded.h264')]:
        text=change(text,old,manifest['files'][name]['sha256'])
    start=text.index("    pace=video.get('diagnostic_source_pacing',{})")
    end=text.index("    health=board.get('health')",start)
    text=text[:start]+"    if video.get('diagnostic_source_pacing') is not None or proof.get('source_pts_pacing_enabled') is not True:raise ValueError('nativePTS presenter must own playback pacing')\n"+text[end:]
    (grader/'grade_evidence.py').write_text(text)
    for p in grader.glob('*.py'):compile(p.read_text(),str(p),'exec')

    def build(path,run_id,boot_id,secret):
        old_build(out/'rate5-template-app.tar.gz',run_id,boot_id,secret)
        with tarfile.open(out/'rate5-template-app.tar.gz') as archive:
            members=archive.getmembers();files={m.name:archive.extractfile(m).read() for m in members};modes={m.name:m.mode for m in members}
        source=files['board_supervisor.py'].decode()
        for a,b in [('finite RF131 ten AUs; fresh ADC and real PL, then paced HDMI from this run only',SCOPE),
                    ("auth['maximum_frames'] == 131 and auth['ADC_blocks'] == 2048","auth['maximum_frames'] == 347 and auth['ADC_blocks'] == 3840"),
                    ("LINK_DIAGNOSTIC_RF_FRAME_BUDGET='131', LINK_DIAGNOSTIC_LAST_RF_FRAME_ID='128'","LINK_DIAGNOSTIC_RF_FRAME_BUDGET='347', LINK_DIAGNOSTIC_LAST_RF_FRAME_ID='344'"),
                    ("'--blocks', '2048'","'--blocks', '3840'"),
                    ("'--max-records', '131'","'--max-records', '347'"),
                    ("'--expected-access-units', '10'","'--expected-access-units', '25'"),
                    ("'--diagnostic-time-scale', '1', '--diagnostic-pace-source-pts'","'--diagnostic-time-scale', '1'"),
                    (">= 96 * 1024",">= 128 * 1024")]:source=change(source,a,b)
        start=source.index("        pace = (result['video_report'].get('diagnostic_source_pacing') or {})")
        end=source.index("        result['valid'] =",start)
        source=source[:start]+"        assert result['video_report'].get('diagnostic_source_pacing') is None, 'unexpected Python source pacing'\n        assert result['hdmi_proof'].get('source_pts_pacing_enabled') is True, 'nativePTS flag missing'\n"+source[end:]
        compile(source,'Sintel25-board-supervisor','exec');files['board_supervisor.py']=source.encode()
        files['receive_gate.py']=receive.encode();files['acceptance_gate.py']=acceptance.encode();files['hdmi-presenter']=PRESENTER.read_bytes()
        files['live-rx-arm']=CAPTURE_NATIVE.read_bytes()
        closure=files['make_runtime_closure.py'].decode();closure=change(closure,"['--repeat-displayed-on-idle','--rgb-expand'","['--repeat-displayed-on-idle','--source-pts-pacing','--rgb-expand'")
        files['make_runtime_closure.py']=closure.encode()
        auth=json.loads(files['AUTHORIZATION.json']);auth.update(scope=SCOPE,maximum_frames=347,unique_frames=344,ADC_blocks=BLOCKS,
            native_source_pts_pacing=True,media_version=manifest['media_version'],source_video_sha256=manifest['source_video_sha256'],presenter_sha256=sha(PRESENTER),native_sha256=sha(CAPTURE_NATIVE))
        files['AUTHORIZATION.json']=json.dumps(auth).encode()
        rows=[dict(name=n,bytes=len(b),sha256=hashlib.sha256(b).hexdigest()) for n,b in files.items() if n!='FILES.json']
        files['FILES.json']=json.dumps(rows).encode()
        if any(n.endswith(('.iqb','.fc32le','.rfd','.rxth','.mp4','.h264','.png')) for n in files):raise ValueError('receive bundle must not contain expected media or IQ')
        with tarfile.open(path,'x:gz') as archive:
            for name,data in files.items():
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=modes[name];archive.addfile(info,io.BytesIO(data))
        review=json.loads((out/'BUNDLE-REVIEW.json').read_text());review.update(source_media='Sintel first5s,25AU',maximum_RF_bursts=347,unique_RF_frames=344,new_ADC_blocks=BLOCKS,
            files=rows,native_source_pts_pacing=True,presenter_sha256=sha(PRESENTER),receiver_PL_CRC_boot_drivers_unchanged=False,
            receiver_PL_CRC_presenter_boot_assets_unchanged=False,MIMO_frontend_PL_CRC_boot_drivers_unchanged=True,
            native_sha256=sha(CAPTURE_NATIVE),capture_limit_only_changed_2048_to_3840=True,
            sender_trigger='validated configuration plus five-record prefill,0.65s timer concurrent with remaining stdin',
            no_media_or_IQ_deployed=True,visual_pass=False,promoted=False)
        wf.write_json(out/'BUNDLE-REVIEW.json',review);return rows

    original_class=next(n for n in ast.parse((R/'tools/video_core_round.py').read_text()).body if isinstance(n,ast.ClassDef) and n.name=='VideoManager')
    original_status=ast.unparse(next(n for n in original_class.body if isinstance(n,ast.FunctionDef) and n.name=='status'))
    original_status=change(original_status,'super().status()','wf.Manager.status(self)')
    original_status=change(original_status,'range(128, 132)','range(344, 348)')
    original_status=change(original_status,'range(10)','range(25)')
    original_status=change(original_status,'完整恢复十个 AU','完整恢复二十五个 AU')
    status_context=dict(wf=wf,json=json)
    exec(compile(original_status,'Sintel25-fixed-status','exec'),status_context)

    class Sintel25Manager(cls):
        status=status_context['status']
        def update(self,stage=None,detail=None,**fields):
            wording={
                '正在发送固定照片的 42 个双流 RF 突发':'正在发送 Sintel 的 347 个有限双流 RF 突发',
                'HDMI 正在显示本轮实收照片，保持约 60 秒；请观察画面':'HDMI 正在播放本轮实收 Sintel 视频，随后保持末帧约 60 秒',
                '照片显示已结束，正在观察收尾内核状态并保存证据':'视频显示已结束，正在观察收尾内核状态并保存证据',
                '读取本轮原始证据，逐字节核对照片包和 HDMI 回执':'读取本轮原始证据，逐字节核对 Sintel 视频包和 HDMI 回执'}
            return super().update(stage,wording.get(detail,detail),**fields)
        def preflight(self):
            super().preflight()
            profile=json.loads((self.run/'runtime-profile.json').read_text());profile['receiver_sha256']=sha(CAPTURE_NATIVE)
            wf.write_json(self.run/'runtime-profile.json',profile)
            wf.write_json(self.run/'video-profile.json',dict(media_version=manifest['media_version'],source_video_sha256=manifest['source_video_sha256'],
                RF_bursts=347,unique_RF_frames=344,ADC_blocks=BLOCKS,video_AUs=25,nominal_fps=5,native_sha256=sha(CAPTURE_NATIVE),presenter_sha256=sha(PRESENTER),
                IQB_sha256=manifest['IQB_sha256'],SD_written=False,continuous_realtime_verified=False))
        def grade(self,evidence):
            result=measured_grade(self,evidence)
            proof=json.loads((evidence/'hdmi-proof.json').read_text())
            pixel_reference=json.loads((MEDIA/'PIXEL-REFERENCE.json').read_text())['frames']
            if [(x['source_frame_id'],x['source_pts_ns'],x['payload_crc32']) for x in proof['records']]!=[(x['sequence'],x['source_pts_ms']*1000000,int(x['accepted_payload_crc32'],16)) for x in pixel_reference]:
                raise ValueError('pixel bytes/identity differ from frozen original ARM decoder')
            result.update(presentation_cadence=cadence25(proof),all25_frozen_pixel_CRCs_match=True,media_version=manifest['media_version'])
            return result
        def transmit(self):
            stream=(MEDIA/'video25-prefix3.iqb').read_bytes()
            if hashlib.sha256(stream).hexdigest()!=manifest['IQB_sha256']:raise ValueError('TX waveform pin')
            out=self.run/'tx';out.mkdir()
            argv=[str(BASE/'tx-gain18/continuous_tx'),'--stats',str(out/'transport.json'),'--actual',str(out/'actual-readback.json'),
                '--freq','5500000000','--bandwidth','40000000','--gain','18.0','--scale','1','--queue','8','--prefill','4',
                '--max-send-samples','65536','--clock','internal','--time','internal','--addr','192.168.10.2','--serial','3095BA5',
                '--enable-tx','YES','--tx-zero-preroll-samples','400000']
            wf.write_json(out/'command.json',argv)
            with (out/'stdout.log').open('xb') as stdout,(out/'stderr.log').open('xb') as stderr:
                self.tx=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,start_new_session=True)
                os.set_blocking(self.tx.stdin.fileno(),False)
                timing=feed_and_trigger(self.tx,stream,lambda:json.loads((out/'actual-readback.json').read_text()),
                    lambda:self.command('touch /tmp/link-live/photo-console.trigger','trigger-new-ADC',10),self.check_stop)
                wf.write_json(out/'timing.json',timing)
            stats=json.loads((out/'transport.json').read_text())
            if not (self.tx.returncode==0 and stats['transport_pass'] and stats['input_samples_per_channel']==stats['accepted_samples_per_channel']==SAMPLES
                and all(stats[k]==0 for k in ('underflows','late_errors','other_async_errors'))):raise RuntimeError('finite347 TX transport failed')
            wf.write_json(out/'result.json',dict(valid=True,new_RF=True,frames=347,unique_frames=344,statistics=stats,IQB_sha256=manifest['IQB_sha256']))
    return Sintel25Manager,build
