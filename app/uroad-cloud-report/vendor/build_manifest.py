#!/usr/bin/env python3
"""Build the deterministic offline zstandard vendor manifest."""
from __future__ import annotations

import hashlib
import json
import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
RUNTIMES = ROOT / "runtimes"
TAGS = ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--wheel-dir", required=True, type=Path, help="External directory containing reviewed source wheels")
arguments = parser.parse_args()
wheel_directory = arguments.wheel_dir.resolve(strict=True)

entries = []
for tag in TAGS:
    matches = list(wheel_directory.glob(f"zstandard-0.25.0-{tag}-{tag}-*.whl"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one official wheel for {tag}, found {len(matches)}")
    wheel = matches[0]
    pattern = rf"^zstandard-0\.25\.0-{tag}-{tag}-manylinux2014_x86_64\.manylinux_2_17_x86_64(?:\.manylinux_2_28_x86_64)?\.whl$"
    if not re.fullmatch(pattern, wheel.name):
        raise RuntimeError(f"Unexpected wheel identity: {wheel.name}")
    runtime = RUNTIMES / tag
    with tempfile.TemporaryDirectory(prefix=f"zstandard-{tag}-") as raw:
        extracted = Path(raw)
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                    raise RuntimeError(f"Unsafe wheel member: {member.filename}")
            archive.extractall(extracted)
        if runtime.exists():
            shutil.rmtree(runtime)
        shutil.copytree(extracted, runtime)
    files = []
    for path in sorted(item for item in runtime.rglob("*") if item.is_file()):
        files.append({
            "path": path.relative_to(runtime).as_posix(),
            "sha256": digest(path),
            "size": path.stat().st_size,
        })
    entries.append({
        "python_tag": tag,
        "abi_tag": tag,
        "platform": "linux_x86_64_glibc_manylinux_2_17",
        "runtime": f"runtimes/{tag}",
        "wheel": wheel.name,
        "wheel_sha256": digest(wheel),
        "files": files,
    })

runtime_licenses = [RUNTIMES / tag / "zstandard-0.25.0.dist-info" / "licenses" / "LICENSE" for tag in TAGS]
if any(not path.is_file() for path in runtime_licenses) or len({digest(path) for path in runtime_licenses}) != 1:
    raise RuntimeError("Wheel licenses are missing or inconsistent")
license_path = ROOT / "LICENSE.zstandard.txt"
shutil.copyfile(runtime_licenses[0], license_path)
manifest = {
    "schema_version": 1,
    "package": "zstandard",
    "version": "0.25.0",
    "source": "https://pypi.org/project/zstandard/0.25.0/",
    "license": {"path": license_path.name, "sha256": digest(license_path)},
    "supported": "CPython 3.9-3.14, Linux x86-64, glibc (manylinux_2_17)",
    "runtimes": entries,
}
(ROOT / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
