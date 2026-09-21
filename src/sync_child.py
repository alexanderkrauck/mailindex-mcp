"""Tiny process-group guardian: parent death must also kill OCR descendants.

Runs no application imports. The sole writer of the pipe belongs to the root
supervisor. EOF is therefore an OS-backed parent-death signal, independent of
Python cancellation, heartbeat threads, PID reuse, and the account event loop.
"""

import os
import selectors
import signal
import subprocess
import sys


def main():
    fd = int(sys.argv[1])
    # Group TERM reaches the application too. Let it clean up until root's KILL.
    signal.signal(signal.SIGTERM, lambda *_: None)
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL)  # noqa: S603
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            if selector.select(timeout=0.02) and not os.read(fd, 1):
                os.killpg(os.getpgrp(), signal.SIGKILL)
            code = child.poll()
            if code is not None:
                # Root will kill leftover group members before reusing the slot.
                return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
