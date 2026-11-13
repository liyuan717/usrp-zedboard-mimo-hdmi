#!/usr/bin/env python3
"""Build bounded G0 stream decoder using the already qualified pinned runtime."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB

RUNTIME = ('ld-linux-armhf.so.3', 'libavcodec.so.60', 'libavutil.so.58',
           'libstdc++.so.6', 'libm.so.6', 'libgcc_s.so.1', 'libc.so.6')

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
    before=check('before-stream-decoder-build');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    assert before['disk']['available_bytes']>=5*GIB
    try:
        reference=a.reference.resolve();receipt=json.loads((reference/'build-receipt.json').read_text())
        assert receipt['ffmpeg_commit']=='b1a4534186ca51b0457579fc05a5739eb2cc45cd'
        sources=['ps/g0_stream_decode.cpp','ps/include/g0_au_decode_queue.hpp','ps/g0_video_fragment.c','ps/g0_video_au.c','ps/include/g0_video_fragment.h','ps/include/g0_video_au.h','ps/include/g0_source_dma_ingress.hpp','ps/include/g0_source_ring.h','ps/include/g0_dma_batch.h','ps/g0_dma_batch.c']
        sources+=['ps/include/g0_source_session.hpp','ps/include/g0_source_video_ingress.hpp','ps/include/g0_source_age.hpp']
        hdmi_stems=['protocol','rgb_expand','presenter','null_backend','drm_backend']
        sources+=['vendor/hdmi/src/'+n+'.cpp' for n in hdmi_stems]
        sources+=sorted(str(p.relative_to(v)) for p in (v/'vendor/hdmi/include').rglob('*.h'))
        snapshot=out/'sources'
        for name in sources:
            target=snapshot/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(v/name,target)
        main_source=str(snapshot/'ps/g0_stream_decode.cpp')
        commands=json.loads((reference/'commands.json').read_text())
        compile=next(c for c in commands if str(v/'ps/g0_decode_display.cpp') in c)
        compile=compile.copy();compile[compile.index(str(v/'ps/g0_decode_display.cpp'))]=main_source
        compile[compile.index('-o')+1]=str(out/'stream.o')
        compile[compile.index('-I'+str(v/'vendor/hdmi/include'))]='-I'+str(snapshot/'vendor/hdmi/include')
        compile+=['-fstack-usage']
        lib=Path(receipt['codec_lib'])
        component_commands=[]
        for stem in ['g0_video_fragment','g0_video_au','g0_dma_batch']:
            c=compile.copy();c[0]=compile[0][:-3]+'gcc';c[c.index('-std=c++17')]='-std=c11'
            c[c.index(main_source)]=str(snapshot/('ps/'+stem+'.c'));c[c.index('-o')+1]=str(out/(stem+'.o'))
            component_commands.append(c)
        hdmi_commands=[]
        for stem in hdmi_stems:
            c=compile.copy();c[c.index(main_source)]=str(snapshot/('vendor/hdmi/src/'+stem+'.cpp'));c[c.index('-o')+1]=str(out/(stem+'.o'));hdmi_commands.append(c)
        link=[compile[0],'-pthread',str(out/'stream.o'),str(out/'g0_video_fragment.o'),str(out/'g0_video_au.o'),str(out/'g0_dma_batch.o'),str(reference/'pixels.o'),*[str(out/(n+'.o')) for n in hdmi_stems],
              str(lib/'libavcodec.so.60'),str(lib/'libavutil.so.58'),'-Wl,-rpath-link,'+str(lib),'-o',str(out/'g0-stream-decode')]
        (out/'commands.json').write_text(json.dumps([compile,*component_commands,*hdmi_commands,link],indent=2)+'\n')
        for name,cmd in [('compile',compile),*[(str(i),c) for i,c in enumerate(component_commands)],*zip(hdmi_stems,hdmi_commands),('link',link)]:
            with (out/(name+'.log')).open('w') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
        runtime=out/'runtime';runtime.mkdir()
        for name in RUNTIME:
            shutil.copyfile(reference/'runtime'/name,runtime/name)
        result={'ARM_compiled':True,'native_passed':False,'full_video_passed':False,
                'source_sha256':{p:hashlib.sha256((snapshot/p).read_bytes()).hexdigest() for p in sources},'source_snapshot':str(snapshot),
                'reused_reference':str(reference),'ffmpeg_commit':receipt['ffmpeg_commit']}
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(check('after-stream-decoder-build'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);main(p.parse_args())
