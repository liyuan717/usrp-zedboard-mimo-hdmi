#!/usr/bin/env python3
"""TX-only CLI: share the validated PHY/codec, reject loopback and skip local RX preflight."""
import subprocess
import sys
from transmitter import main

if __name__ == '__main__':
    try:
        raise SystemExit(main(tx_only=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('ERROR: '+str(error), file=sys.stderr)
        raise SystemExit(2)
