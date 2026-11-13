#!/usr/bin/env python3
"""Finite native-PC camera/encode measurement; no RF or camera-system signoff.

Integrated Camera MJPEG1280x720/30 -> select2of3 real frames ->1024x600/20.
The original selected capture timestamps precede encoder PTS normalization.
Dynamic framerate is temporarily disabled, then restored under the shared lease.
"""
import argparse,hashlib,json,re,subprocess,time
from fractions import Fraction
from pathlib import Path
from hardware_lease import HardwareLease
from workspace_storage_guard import check,GIB

CONTROL=r'''#include <time.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
int main(int argc,char **argv){
 if(argc<1||argc>2)return 3;
 int fd=open("/dev/video0",O_RDWR);if(fd<0)return 1;
 struct v4l2_control c={.id=V4L2_CID_EXPOSURE_AUTO_PRIORITY};
 if(ioctl(fd,VIDIOC_G_CTRL,&c))return 2;
 if(argc==2){
  if(strlen(argv[1])!=1||(argv[1][0]!='0'&&argv[1][0]!='1'))return 3;
  c.value=argv[1][0]-'0';
  if(ioctl(fd,VIDIOC_S_CTRL,&c)||ioctl(fd,VIDIOC_G_CTRL,&c))return 4;
 }
 printf("%d\n",c.value);close(fd);return 0;
}
'''

def review(out,frames):
 log=(out/'capture-encode.log').read_text()
 rows=[(int(n),int(pts),crc) for n,pts,crc in re.findall(
  r'Parsed_showinfo_1[^\n]*?n:\s*(\d+)\s+pts:\s*(\d+)[^\n]*?checksum:([0-9A-F]+)',log)]
 assert len(rows)==frames and [x[0] for x in rows]==list(range(frames))
 assert all(a[1]<b[1] for a,b in zip(rows,rows[1:]))
 decoded=re.findall(r'(\d+) frames decoded; (\d+) decode errors',log)
 assert len(decoded)==1 and int(decoded[0][0])>=frames and int(decoded[0][1])==0
 probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-count_frames',
  '-show_packets','-show_entries','stream=width,height,r_frame_rate,avg_frame_rate,nb_read_frames:packet=pts_time,duration_time',
  '-of','json',str(out/'camera.mp4')],text=True,timeout=15))
 s=probe['streams'][0]
 assert (s['width'],s['height'])==(1024,600) and Fraction(s['avg_frame_rate'])==20 and int(s['nb_read_frames'])==frames
 assert [Fraction(p['pts_time']) for p in probe['packets']]==[Fraction(i,20) for i in range(frames)]
 assert all(Fraction(p['duration_time'])==Fraction(1,20) for p in probe['packets'])
 rate=(frames-1)*1000000/(rows[-1][1]-rows[0][1])
 return dict(input_decoded_frames=int(decoded[0][0]),selected_real_frames=frames,
  selected_capture_pts_us=[r[1] for r in rows],unique_capture_timestamps=len({r[1] for r in rows}),
  unique_selected_input_checksums=len({r[2] for r in rows}),measured_selected_new_source_fps=rate,
  packet_PTS_and_duration_exact20fps=True,output_stream=s,
  finite_camera_capture_encode20_subcondition_passed=rate>=20,
  source_clock_mapping_or_end_to_end_age_passed=False)

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False)
 before=check('before-finite-camera20-probe');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 assert before['disk']['available_bytes']>=5*GIB
 (out/'control.c').write_text(CONTROL)
 subprocess.run(['gcc','-std=c11','-O2','-Wall','-Wextra','-Werror',str(out/'control.c'),'-o',str(out/'control')],check=True)
 vf="select='lt(mod(n,3),2)',showinfo,setpts=N/(20*TB),scale=1066:600:out_color_matrix=bt601:out_range=tv,crop=1024:600,setsar=1,format=yuv420p"
 x264='slices=2:sliced-threads=0:scenecut=0:open-gop=0:weightp=0:8x8dct=0:aud=1:repeat-headers=1:intra-refresh=0:fps=20/1'
 args=['ffmpeg','-nostdin','-hide_banner','-loglevel','verbose','-debug_ts','-f','v4l2','-input_format','mjpeg',
  '-video_size','1280x720','-framerate','30','-i','/dev/video0','-an','-vf',vf,
  '-frames:v',str(a.frames),'-enc_time_base','1:20','-fps_mode','passthrough',
  '-c:v','libx264','-profile:v','baseline','-level:v','3.1','-preset','veryfast','-tune','zerolatency','-threads','2',
  '-pix_fmt','yuv420p','-b:v','900000','-maxrate','900000','-bufsize','900000','-bf','0','-refs','1','-g','20','-keyint_min','20',
  '-color_primaries','bt470bg','-color_trc','bt709','-colorspace','smpte170m','-color_range','tv',
  '-x264-params',x264,'-movflags','+faststart',str(out/'camera.mp4')]
 (out/'command.json').write_text(json.dumps(args,indent=2)+'\n')
 result=dict(scope=__doc__,completed=False,RF_started=False,SD_written=False,frame_duplication_filter_present=False,
  full_camera_RF_or_native_ARM_HDMI_or_age_or_G0_passed=False)
 def control(*values):return subprocess.check_output([str(out/'control'),*values],text=True,timeout=5).strip()
 try:
  with HardwareLease():
   old=control();assert old in ('0','1');result['original_dynamic_framerate']=int(old)
   try:
    assert control('0')=='0';result['dynamic_framerate_during_capture']=0;start=time.monotonic()
    with (out/'capture-encode.log').open('w') as log:
     r=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,timeout=a.frames/20+15)
    result.update(returncode=r.returncode,wall_seconds=time.monotonic()-start)
    assert r.returncode==0
   finally:result['restored_dynamic_framerate']=int(control(old))
  result.update(review(out,a.frames),completed=True,
   encoded_file_sha256=hashlib.sha256((out/'camera.mp4').read_bytes()).hexdigest())
 finally:
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-finite-camera20-probe'),indent=2)+'\n')
 print(json.dumps({k:v for k,v in result.items() if k!='selected_capture_pts_us'}),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--frames',type=int,choices=(200,1200),default=200);main(p.parse_args())
