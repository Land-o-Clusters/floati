"""Explicit Linux clock evidence; never imported by default test discovery."""
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time


def main():
    # Linux UAPI CLOCK_REALTIME_COARSE = 5. Capture both sides of creation;
    # no sleeps, tolerance estimate or assumed scheduler frequency.
    samples = []
    with tempfile.TemporaryDirectory(prefix="rider8-clock-") as temporary:
        for ordinal in range(16):
            path = Path(temporary) / str(ordinal)
            coarse_begin = time.clock_gettime_ns(5)
            wall_begin = time.time_ns()
            path.write_bytes(b"native clock evidence")
            info = path.stat()
            wall_end = time.time_ns()
            coarse_end = time.clock_gettime_ns(5)
            samples.append(dict(coarse_begin_ns=coarse_begin, wall_begin_ns=wall_begin,
                mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns,
                wall_end_ns=wall_end, coarse_end_ns=coarse_end,
                device=info.st_dev))
        filesystem = subprocess.run(["/usr/bin/stat", "-f", "-c", "%T", temporary],
            capture_output=True, text=True, check=True).stdout.strip()
    print(json.dumps(dict(platform=platform.platform(), filesystem=filesystem,
        git=subprocess.run(["/usr/bin/git", "--version"], capture_output=True,
            text=True, check=True).stdout.strip(),
        coarse_resolution_seconds=time.clock_getres(5), samples=samples), indent=2))


if __name__ == "__main__":
    main()
