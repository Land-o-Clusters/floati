from __future__ import annotations

import sys
import time
from pathlib import Path


MARKER = Path(sys.argv[1])

for _raw in sys.stdin.buffer:
    sys.stdout.buffer.write(b"{not-json}\n")
    sys.stdout.buffer.flush()
    MARKER.write_text("malformed frame queued", encoding="utf-8")
    time.sleep(2)
