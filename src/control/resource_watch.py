"""Watch only this worker and its descendants; never kill other user apps."""
from pathlib import Path
import json,os,threading
class ResourceWatch:
 def __init__(self,manager,out):
  self.manager,self.out=manager,Path(out);self.done=threading.Event();self.report=dict(host_available_min_kib=self.mem(),owned_peak_rss_kib=0,aborted=False)
 def mem(self):return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))
 def tree(self):
  found={os.getpid()}
  while True:
   old=set(found)
   for pid in old:
    try:found.update(map(int,Path(f'/proc/{pid}/task/{pid}/children').read_text().split()))
    except FileNotFoundError:pass
   if old==found:return found
 def observe(self):
  while not self.done.wait(.25):
   available=self.mem();rss=0
   for pid in self.tree():
    try:rss+=int(next(x.split()[1] for x in Path(f'/proc/{pid}/status').read_text().splitlines() if x.startswith('VmRSS:')))
    except (FileNotFoundError,StopIteration):pass
   self.report['host_available_min_kib']=min(self.report['host_available_min_kib'],available);self.report['owned_peak_rss_kib']=max(self.report['owned_peak_rss_kib'],rss)
   if available<768*1024 or rss>512*1024:self.report['aborted']=True;self.manager.stop_event.set()
 def __enter__(self):
  self.thread=threading.Thread(target=self.observe,daemon=True);self.thread.start();return self
 def __exit__(self,*args):
  self.done.set();self.thread.join(2);(self.out/'RESOURCE-MONITOR.json').write_text(json.dumps(self.report,indent=2)+'\n')
