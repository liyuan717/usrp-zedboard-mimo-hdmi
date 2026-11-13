#!/usr/bin/env python3
"""PTY-only proof: exclusive board command cleanup stays raw115200 on all exits.
The9600 initial state is simulated on /dev/pts, never the Cypress hardware.
"""
import argparse,hashlib,json,os,pty,re,select,subprocess,termios,threading,time
from pathlib import Path
from board_command import command

class PTYLease:
 def assert_held(self):pass

def trial(fn,path,status):
 master,slave=pty.openpty();serial=os.ttyname(slave);assert serial.startswith('/dev/pts/')
 a=termios.tcgetattr(slave);a[4]=a[5]=termios.B9600;a[3]|=termios.ECHO;termios.tcsetattr(slave,termios.TCSANOW,a)
 failures=[]
 def peer():
  try:
   wire=bytearray();end=time.monotonic()+3
   while time.monotonic()<end:
    if select.select([master],[],[],.1)[0]:wire.extend(os.read(master,65536))
    match=re.search(rb"'([0-9a-f]{16})' '([0-9a-f]{16})'",wire)
    if match:
     if status is not None:os.write(master,b'\nPTY_BOARD_RESPONSE\nV3_DONE_'+match[1]+match[2]+b'_'+str(status).encode()+b'\n')
     return
   raise TimeoutError('PTY did not receive actual command framing')
  except BaseException as e:failures.append(repr(e))
 thread=threading.Thread(target=peer);thread.start();outcome='success'
 try:
  try:fn(serial,'echo simulated-board',path,.5,lease=PTYLease())
  except RuntimeError:outcome='board_error'
  except TimeoutError:outcome='timeout'
  thread.join(3);assert not thread.is_alive() and not failures,failures
  expected='timeout' if status is None else 'success' if status==0 else 'board_error';assert outcome==expected,(outcome,expected)
  after=termios.tcgetattr(slave)
  return {'outcome':outcome,'input115200':after[4]==termios.B115200,'output115200':after[5]==termios.B115200,'raw_no_echo':after[0]==after[1]==after[3]==0,'CLOCAL':bool(after[2]&termios.CLOCAL)}
 finally:os.close(master);os.close(slave)

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
 for label,status in [('success',0),('board_error',7),('timeout',None)]:
  r=trial(command,out/(label+'.log'),status);assert all(r[k] for k in ['input115200','output115200','raw_no_echo','CLOCAL']),r;rows.append(r)
 old=subprocess.check_output(['git','show',a.historical_ref+':video_radio_v3/tools/board_command.py'],cwd=v.parent,text=True)
 namespace={'__name__':'historical_policy_control'};exec(compile(old,'historical_board_command.py','exec'),namespace)
 negative=trial(namespace['command'],out/'historical-control.log',0);assert not negative['input115200'] and not negative['raw_no_echo'],negative
 result={'scope':__doc__,'current_cleanup_exits':rows,'historical_control':negative,'historical_ref':a.historical_ref,'board_command_sha256':hashlib.sha256((v/'tools/board_command.py').read_bytes()).hexdigest(),'actual_Cypress_UART_or_RF_used':False,'passed':True}
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--historical-ref',default='24a7c9f');main(p.parse_args())
