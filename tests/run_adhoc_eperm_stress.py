"""Serial roster battery under eight owned CPU burners, with per-run receipts."""

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"before", "after"}:
        raise SystemExit("usage: run_adhoc_eperm_stress.py before|after")
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        raise SystemExit("run from the script's own worktree")
    output = Path(tempfile.mkdtemp(prefix="adhoc-eperm-" + sys.argv[1] + "."))
    source = subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], text=True).strip()
    burners = []
    child = None
    rows = []
    try:
        for _ in range(8):
            burners.append(subprocess.Popen(["/usr/bin/yes"], stdout=subprocess.DEVNULL))
        print(json.dumps({"phase": sys.argv[1], "source": source,
            "output": str(output), "burner_pids": [p.pid for p in burners]}), flush=True)
        for iteration in range(1, 21):
            with tempfile.TemporaryDirectory(prefix="hw-home.") as home:
                environment = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
                log = output / ("run-%02d.log" % iteration)
                with log.open("wb") as stream:
                    child = subprocess.Popen(["/usr/bin/python3", "-B",
                        "tests/run_native_qualification.py", "-v",
                        "tests.test_roster_parity_battery"], env=environment,
                        stdout=stream, stderr=subprocess.STDOUT)
                    code = child.wait()
                    child = None
            text = log.read_text()
            count = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
            row = {"iteration": iteration, "exit": code,
                "tests": int(count[1]) if count else None,
                "seconds": float(count[2]) if count else None,
                "load": list(os.getloadavg()), "log": str(log)}
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        owned = ([child] if child is not None else []) + burners
        for process in owned:
            if process.poll() is None:
                process.terminate()
        cleanup = []
        for process in owned:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            cleanup.append({"pid": process.pid, "returncode": process.returncode})
        receipt = {"phase": sys.argv[1], "source": source, "runs": rows,
            "owned_cleanup": cleanup, "passed": sum(row["exit"] == 0 for row in rows)}
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({"receipt": str(output / "receipt.json"),
            "passed": receipt["passed"], "completed": len(rows), "cleanup": cleanup}), flush=True)
    return 0 if len(rows) == 20 and all(row["exit"] == 0 for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
