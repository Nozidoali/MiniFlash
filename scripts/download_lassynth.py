"""Download LaSsynth, the SAT cell synthesizer miniflash is built on.

Fetches the official artifact of "A SAT Scalpel for Lattice Surgery"
(Tan, Niu, Gidney — ISCA 2024, arXiv:2404.18369) from Zenodo and unpacks
its ``lassynth`` package into the repo root. Run from the repo root with
``PYTHONPATH=.`` (or none — stdlib only).
"""

import hashlib
import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

LASSYNTH_URL = "https://zenodo.org/api/records/12595677/files/isca24_SAT_scalpel.zip/content"
LASSYNTH_SHA256 = "8a9bf4734cf350ec4641566c7355b82a4e5d713148af6d0ed9e353f4ff2e2167"
_ARCHIVE_PREFIX = "isca24_SAT_scalpel/lassynth/"


def download_lassynth(root=None) -> bool:
    """Install the ``lassynth`` package from the Zenodo artifact, once.

    No-op when ``root/lassynth`` already exists. URL/sha overridable via
    ``MINIFLASH_LASSYNTH_URL`` / ``MINIFLASH_LASSYNTH_SHA256``.

    :param root: str | Path, directory to place ``lassynth/`` in
        (default: the repo root, i.e. this script's parent's parent).
    :returns: bool, True when the package was installed.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    target = root / "lassynth"
    if (target / "__init__.py").exists():
        return False

    url = os.environ.get("MINIFLASH_LASSYNTH_URL", LASSYNTH_URL)
    expected = os.environ.get("MINIFLASH_LASSYNTH_SHA256", LASSYNTH_SHA256)

    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        raise ValueError(f"download_lassynth: checksum mismatch: got {digest[:12]}…, expected {expected[:12]}…")

    installed = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.namelist():
            if not member.startswith(_ARCHIVE_PREFIX) or member.endswith("/"):
                continue
            relative = Path(member[len(_ARCHIVE_PREFIX):])
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
            installed += 1

    print(f"download_lassynth: installed {installed} files into {target}", file=sys.stderr)
    return True


def main():
    """CLI entry point: fetch LaSsynth into the repo root.

    :returns: None.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Download LaSsynth (Zenodo artifact) into the repo root")
    parser.add_argument("root", nargs="?", default=None, help="directory to place lassynth/ in (default: repo root)")
    arguments = parser.parse_args()

    if not download_lassynth(arguments.root):
        print("download_lassynth: lassynth/ already present — nothing to do", file=sys.stderr)


if __name__ == "__main__":
    main()
