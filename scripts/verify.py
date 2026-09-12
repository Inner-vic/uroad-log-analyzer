"""Run every local, non-network test suite from one entry point."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(label: str, cwd: Path, args: list[str]) -> None:
    print(f"\n==> {label}", flush=True)
    completed = subprocess.run([sys.executable, *args], cwd=cwd, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    run(
        "uroad-cloud-report",
        ROOT / "app" / "uroad-cloud-report",
        ["-m", "pytest", "-q"],
    )
    run(
        "supervisor",
        ROOT / "app" / "supervisor",
        ["-m", "unittest", "discover", "-s", "tests", "-v"],
    )
    run(
        "ad-p99-analysis",
        ROOT / "app" / "ad-p99-analysis",
        ["-m", "unittest", "discover", "-s", "tests", "-v"],
    )
    print("\nAll local suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

