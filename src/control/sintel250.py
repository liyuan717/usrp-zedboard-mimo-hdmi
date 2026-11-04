"""Fixed higher-quality Sintel profile; original verified adapter stays unchanged."""
from pathlib import Path
import hashlib,inspect,io,json,re,tarfile,textwrap
from measured_video import R,wf

BASE_ADAPTER=R/'tools/video25_sintel.py'
BASE_SHA='414aade00a2423423f177ae537ed39269ec058f539a365839b9f76d45ed52977'
MEDIA=R/'candidates/sintel250-media-r1'

def make_video25(out):
    out=Path(out)
    assert hashlib.sha256(BASE_ADAPTER.read_bytes()).hexdigest()==BASE_SHA
    text=BASE_ADAPTER.read_text()
    replacements={
        'candidates/video25-sintel-r1':'candidates/sintel250-media-r1',
        'candidates/capture3840-r2/validation/live-rx-arm':'candidates/capture5120-r1/validation/live-rx-arm',
        '020f36c6816b66a941d898ec8b1bd5c69d46f8566473055969597e4ee9921735':'e495484f03be6beb6ca6ec35df5cc5840f365bdd3961951cbf436873cb9788ed',
        '544057373ad9f58c0304958b7d528cc490cfba8bdd9280d2c3b72bccf9dd6f76':'b230fcf23991371163489805242e503266191c51e14c4705be0ff2354559895f',
        'aaec3de2754be29e1c694385265887f884b1a73c4be7e7d970608315b6be4cca':'09fd7222ec31434d5643c9235171add705c747e5e9b853da2182f99228d83eec',
        'RF347':'RF859','fresh3840ADC':'fresh5120ADC','tested3840':'tested5120','finite347':'finite859',
        'capture_limit_only_changed_2048_to_3840':'capture_limit_only_changed_2048_to_5120',
        '>= 128 * 1024':'>= 192 * 1024'}
    for old,new in replacements.items():
        assert old in text,old;text=text.replace(old,new)
    numbers={'3840':'5120','1332480':'3298560','344':'856','347':'859','348':'860','343':'855','345':'857'}
    text=re.sub(r'\b(3840|1332480|344|347|348|343|345)\b',lambda m:numbers[m[0]],text)
    namespace=dict(__file__=str(BASE_ADAPTER),__name__='fixed_sintel250_private_adapter')
    exec(compile(text,'fixed-sintel250-profile','exec'),namespace)
    cls,inner_build=namespace['make_video25'](out)
    cap_methods={}
    for name,count in [('request',2),('fetch',1)]:
        source=textwrap.dedent(inspect.getsource(getattr(wf.Manager,name)))
        assert source.count('32 << 20')==count
        source=source.replace('32 << 20','48 << 20')
        ctx=dict(wf.__dict__);exec(compile(source,'fixed-sintel250-'+name,'exec'),ctx);cap_methods[name]=ctx[name]
    class Sintel250Manager(cls):
        request=cap_methods['request']
        fetch=cap_methods['fetch']
    def build(path,run_id,boot_id,secret):
        inner_build(out/'before-evidence-cap-app.tar.gz',run_id,boot_id,secret)
        with tarfile.open(out/'before-evidence-cap-app.tar.gz') as archive:
            members=archive.getmembers();files={m.name:archive.extractfile(m).read() for m in members};modes={m.name:m.mode for m in members}
        agent=files['board_agent.py'].decode();assert agent.count('32 << 20')==1
        files['board_agent.py']=agent.replace('32 << 20','48 << 20').encode()
        auth=json.loads(files['AUTHORIZATION.json']);auth.update(maximum_evidence_file_bytes=48<<20,video_target_bitrate_bps=250000)
        files['AUTHORIZATION.json']=json.dumps(auth).encode()
        rows=[dict(name=n,bytes=len(b),sha256=hashlib.sha256(b).hexdigest()) for n,b in files.items() if n!='FILES.json']
        files['FILES.json']=json.dumps(rows).encode()
        with tarfile.open(path,'x:gz') as archive:
            for name,raw in files.items():
                info=tarfile.TarInfo(name);info.size=len(raw);info.mode=modes[name];archive.addfile(info,io.BytesIO(raw))
        review=json.loads((out/'BUNDLE-REVIEW.json').read_text());review.update(files=rows,video_target_bitrate_bps=250000,
            maximum_evidence_file_bytes=48<<20,expected_ADC_file_bytes=5120*8224,
            shared_photo_web_source_unchanged=True,original_successful_Sintel25_profile_preserved=True,
            display_binary_driver_bitstream_kernel_DTB_unchanged_from_successful_Sintel25=True,
            physical_quality_verified=False,visual_pass=False,promoted=False)
        wf.write_json(out/'BUNDLE-REVIEW.json',review)
        return rows
    wf.write_json(out/'PROFILE-DERIVATION.json',dict(original_source=str(BASE_ADAPTER),original_SHA256=BASE_SHA,
        exact_string_substitutions=replacements,exact_bounded_number_substitutions=numbers,
        compiled_private_profile_SHA256=hashlib.sha256(text.encode()).hexdigest(),shared_sources_modified=False))
    return Sintel250Manager,build
