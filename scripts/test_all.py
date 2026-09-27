"""Every automated test in the project, one command (plan 8.5). Exit code 0 = all passed.

    .venv\\Scripts\\python scripts\\test_all.py            (Windows)  |  .venv/bin/python scripts/test_all.py
    ... scripts/test_all.py --race 50                     also repeat Trip's race tests 50 times (plan 8.5)

No Docker needed: each suite fakes what it doesn't own. For the real system, run scripts/e2e.py (8.4).
"""
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["libs/common", "services/gateway", "services/identity", "services/matching", "services/trip",
          "services/fare", "services/notification"]


def run(folder: str, *extra: str) -> tuple[bool, str, float]:
    start = time.perf_counter()
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", *extra], cwd=ROOT / folder,
                         capture_output=True, text=True)
    tail = (out.stdout.strip().splitlines() or ["(no output)"])[-1]
    return out.returncode == 0, re.sub(r"\s+in [\d.]+s.*$", "", tail), time.perf_counter() - start


def main() -> int:
    runs = [(f, ()) for f in SUITES]
    if "--race" in sys.argv:
        count = sys.argv[sys.argv.index("--race") + 1]
        runs.append(("services/trip", ("tests/test_concurrency.py", "--count", count)))
    ok_all, total = True, 0
    for folder, extra in runs:
        ok, summary, secs = run(folder, *extra)
        ok_all &= ok
        total += sum(int(n) for n in re.findall(r"(\d+) passed", summary))
        label = folder + (" (race x" + extra[-1] + ")" if extra else "")
        print(f"{'ok  ' if ok else 'FAIL'}  {label:42} {summary}  ({secs:.0f} s)")
    print(f"\n{total} tests passed" + ("" if ok_all else "  --  SOME SUITES FAILED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
