#!/usr/bin/python3
"""One-time host-only setup; no device commands, RF, FPGA, SD or saveenv."""
from pathlib import Path
import os
import pwd
import subprocess
import sys

APP = Path(__file__).resolve().parent


def main():
    if os.getuid() != 0:
        raise SystemExit('请执行：sudo /usr/bin/python3 ' + str(APP / 'setup_once.py'))
    user = pwd.getpwnam('leeeo')
    assert user.pw_uid == 1000 and APP.parent == Path('/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3')
    units = {
        'mimo-photo-tftp.socket': '''[Unit]
Description=MIMO photo fixed RAM-boot TFTP socket
[Socket]
ListenDatagram=169.254.209.83:69
FreeBind=yes
Service=mimo-photo-tftp.service
[Install]
WantedBy=sockets.target
''',
        'mimo-photo-tftp.service': f'''[Unit]
Description=MIMO fixed-file RAM TFTP (unprivileged)
Requires=mimo-photo-tftp.socket
[Service]
Type=simple
User=leeeo
Group=leeeo
ExecStart=/usr/bin/python3 "{APP / 'tftp_service.py'}"
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_UNIX
UMask=0077
'''
    }
    for name, text in units.items():
        target = Path('/etc/systemd/system') / name
        if target.is_symlink():
            raise RuntimeError('拒绝覆盖符号链接：' + str(target))
        target.write_text(text)
        target.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'mimo-photo-tftp.socket'], check=True)
    print('首次配置完成：UDP69 由 systemd 提供，TFTP 以普通用户运行；只在网页任务有效期间提供三个固定启动文件。')
    print('未写 SD、未启动射频、未操作板卡。网页地址：http://127.0.0.1:8098')


if __name__ == '__main__':
    main()
