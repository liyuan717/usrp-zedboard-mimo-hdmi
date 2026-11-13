"""One exclusive owner for board UART/DMA and USRP during finite G0 probes."""
import fcntl
import json
import os
from pathlib import Path
import urllib.request


class HardwareLease:
    def __init__(self,inherited_fd=None):
        self.inherited_fd=inherited_fd

    def __enter__(self):
        lock=Path(f'/run/user/{os.getuid()}/mimo-photo-console/operation.lock')
        if not lock.parent.is_dir():raise RuntimeError('Existing ownership directory absent')
        self.active=False;self.owner=None
        try:
            if self.inherited_fd is None:self.owner=lock.open('a')
            else:
                # pass_fds preserves the parent's same open-file-description.
                # Closing this duplicate never explicitly unlocks its parent.
                self.owner=os.fdopen(os.dup(self.inherited_fd),'a')
                actual=os.fstat(self.owner.fileno());expected=lock.stat()
                if (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):
                    raise RuntimeError('Delegated lease FD is not the operation lock')
            fcntl.flock(self.owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for endpoint in ['state','video/state']:
                with opener.open('http://127.0.0.1:8098/api/'+endpoint,timeout=3) as response:
                    state=json.load(response)['state']
                if state.get('active'):raise RuntimeError('Existing console operation is active; wait')
            self.pid=os.getpid();self.active=True
            return self
        except BaseException:
            if self.owner is not None:self.owner.close()
            raise

    def assert_held(self):
        if not self.active or self.pid!=os.getpid() or self.owner.closed:
            raise RuntimeError('No live hardware lease in this process')

    def __exit__(self,*exception):
        self.active=False
        self.owner.close()
