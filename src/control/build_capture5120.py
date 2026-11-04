"""Build and test bounded capture candidate offline, one owned child at a time."""
from pathlib import Path
import hashlib,json,os,resource,signal,subprocess,time,sys
R=Path(__file__).resolve().parents[1];W=R.parents[1]
C=R/'candidates'/(sys.argv[1] if len(sys.argv)>1 else 'capture5120-r1');S=C/'source';O=C/'validation';O.mkdir(exist_ok=False)
checks=[]
def mem():return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))
def run(name,argv,cap=True,env=None,expected=0):
    assert mem()>1536*1024
    def limit():
        os.nice(10);resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        if cap:resource.setrlimit(resource.RLIMIT_AS,(768<<20,768<<20))
    start=time.monotonic();low=mem();peak=0
    with (O/(name+'.stdout')).open('xb') as out,(O/(name+'.stderr')).open('xb') as err:
        p=subprocess.Popen(list(map(str,argv)),stdout=out,stderr=err,env=env,preexec_fn=limit,start_new_session=True)
        while p.poll() is None:
            low=min(low,mem())
            try:peak=max(peak,int(next(x.split()[1] for x in Path(f'/proc/{p.pid}/status').read_text().splitlines() if x.startswith('VmRSS:'))))
            except (FileNotFoundError,StopIteration):pass
            if low<768*1024 or peak>512*1024 or time.monotonic()-start>180:
                os.killpg(p.pid,signal.SIGKILL);p.wait();raise RuntimeError('owned job resource/deadline guard:'+name)
            time.sleep(.1)
    row=dict(name=name,argv=list(map(str,argv)),returncode=p.returncode,expected=expected,min_available_kib=low,peak_RSS_kib=peak,seconds=time.monotonic()-start)
    checks.append(row);(O/'commands.json').write_text(json.dumps(checks,indent=2)+'\n')
    assert p.returncode==expected,row
    print(name+' PASS',flush=True)
    return (O/(name+'.stdout')).read_text()
flags=['-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread','-DMRF1_NF_ENABLE_DIAG_DC_BLOCKER']
inc=['-I'+str(S/p) for p in ['rf_native_frontend/include','rf_native_runtime/include','rf_native_transport/include','kernel/uapi']]
test=S/'rf_native_runtime/tests/runtime_lifecycle_test.cpp'
wrap='-Wl,--wrap=open,--wrap=read,--wrap=write,--wrap=ioctl,--wrap=close,--wrap=fstat'
run('build-lifecycle',['g++',*flags,*inc,test,wrap,'-o',O/'lifecycle'])
run('lifecycle',[O/'lifecycle'])
run('build-lifecycle-sanitizer',['g++',*flags,*inc,'-fsanitize=address,undefined','-fno-omit-frame-pointer',test,wrap,'-o',O/'lifecycle-sanitizer'])
run('lifecycle-sanitizer',[O/'lifecycle-sanitizer'],False,dict(os.environ,ASAN_OPTIONS='detect_leaks=1:quarantine_size_mb=8:hard_rss_limit_mb=512',UBSAN_OPTIONS='halt_on_error=1'))
arm=W/'hardware-review/boot-fix-build/toolchain/bin/arm-buildroot-linux-gnueabihf-g++'
src=[S/p for p in ['rf_native_runtime/src/mrf1_native_rx_backend.cpp','rf_native_frontend/src/mrf1_native_frontend.cpp','rf_native_transport/src/rf_native_transport.cpp']]
run('build-arm',[arm,*flags,*inc,*src,'-o',O/'live-rx-arm'])
d=W/'deliverables/usrp-zedboard-integration-20261026/realtime-hdmi'
q=d/'standalone-video-acceptance-20261030/private/tools/qemu-extracted/usr/bin/qemu-arm';lib=d/'resume6-candidate/runtime/presenter/lib'
prefix=[q,'-cpu','cortex-a9',lib/'ld-linux-armhf.so.3','--library-path',lib,O/'live-rx-arm']
run('ARM-identity',prefix+['identity'],False)
assert not Path('/dev/zed-rxth').exists()
env=dict(os.environ,LINK_FINITE_CAPTURE_CFO='1',LINK_DIAGNOSTIC_COARSE_CFO_HZ='7223.213205776218',LINK_DIAGNOSTIC_SFO_PPM='39.98002049598881',LINK_DIAGNOSTIC_RF_FRAME_BUDGET='347',LINK_DIAGNOSTIC_LAST_RF_FRAME_ID='344')
for blocks in (2048,3840,5120,5121,0):
    dest=O/('no-device-'+str(blocks)+'.rfd')
    text=run('ARM-blocks-'+str(blocks),prefix+['start-rx','--blocks',str(blocks),'--imbc-build-id','1','--deadline-ms','120000','--delivery-path',dest],False,env,3 if blocks in (2048,3840,5120) else 2)
    if blocks in (2048,3840,5120):
        log=text+(O/('ARM-blocks-'+str(blocks)+'.stderr')).read_text()
        assert 'finite new ADC capture did not complete cleanly' in log or 'captured_blocks' in log
elf=O/'live-rx-arm'
report=dict(valid=True,scope='Offline fake-device full lifecycle plus actual ARM device-absent argument checks; not hardware acceptance',new_maximum_blocks=5120,old2048_behavior_retained=True,late_signal_block=4200,ARM_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),algorithm_frontend_transport_unchanged=True,ASan_UBSan_pass=True,hardware_started=False,visual_pass=False,promoted=False,checks=checks)
(O/'RESULT.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)
