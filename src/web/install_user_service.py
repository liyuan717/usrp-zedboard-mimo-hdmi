#!/usr/bin/python3
"""Install the ordinary user's local backend and desktop launcher."""
from pathlib import Path
import os
import subprocess

APP = Path(__file__).resolve().parent


def main():
    if os.getuid() != 1000:
        raise RuntimeError('请以 leeeo 普通用户安装网页后台')
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    text = f'''[Unit]
Description=Local MIMO fixed-photo web console
[Service]
Type=simple
ExecStart=/usr/bin/python3 "{APP / 'server.py'}"
WorkingDirectory={APP}
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec=110
UMask=0077
[Install]
WantedBy=default.target
'''
    target = units / 'mimo-photo-console.service'
    if target.is_symlink():
        raise RuntimeError('拒绝覆盖符号链接')
    target.write_text(text)
    applications = Path.home() / '.local/share/applications'
    applications.mkdir(parents=True, exist_ok=True)
    (applications / 'mimo-photo-console.desktop').write_text(f'''[Desktop Entry]
Type=Application
Name=MIMO 照片控制台
Comment=固定 seu.png 新空口接收与 HDMI
Exec=/usr/bin/python3 "{APP / 'open_console.py'}"
Icon=network-wireless
Terminal=false
Categories=Development;
''')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'mimo-photo-console.service'], check=True)
    print('普通用户后台已启用：http://127.0.0.1:8098/')


if __name__ == '__main__':
    main()
