"""
Development launcher: runs zebra_print.py and restarts it whenever the file is saved.

Usage: python dev.py
Closing the app window stops the launcher. If the app crashes, the launcher waits for
the next save. A save restarts the app immediately, even in the middle of a print run.
"""

import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).with_name('zebra_print.py')


def modified_time() -> float | None:
    try:
        return APP.stat().st_mtime
    except OSError:
        return None     # editors can briefly replace the file while saving


def start() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(APP)])


def main():
    last = modified_time()
    proc = start()
    print("Watching zebra_print.py. Close the app window or press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(0.5)
            current = modified_time()
            if current is not None and current != last:
                last = current
                print("Change detected, restarting...")
                proc.terminate()
                proc.wait()
                proc = start()
            elif proc.poll() == 0:
                break
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == '__main__':
    main()
