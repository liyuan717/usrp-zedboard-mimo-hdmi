#!/usr/bin/python3
import subprocess

subprocess.run(['systemctl', '--user', 'start', 'mimo-photo-console.service'], check=True, timeout=10)
subprocess.Popen(['xdg-open', 'http://127.0.0.1:8098/'], start_new_session=True,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
