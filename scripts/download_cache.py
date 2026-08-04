import hashlib
import os
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

CACHE_URL = "https://github.com/Nozidoali/MiniFlash/releases/download/cache-v1/miniflash-cache-v1.tar.gz"
CACHE_SHA256 = "edbcd612091c4f54f0b7ccc7ba53b81f70f5f272fe7bda40558051effa971433"


def download_cache(cache_dir) -> bool:
    """Download the published cell-cache archive into cache_dir, once.

    No-op when the directory already holds cells or when download or
    checksum fails — a one-line warning goes to stderr. URL/sha overridable
    via ``MINIFLASH_CACHE_URL`` / ``MINIFLASH_CACHE_SHA256``.

    :param cache_dir: str | Path, the cell-cache directory to populate.
    :returns: bool, True when cells were installed.
    """
    cache_root = Path(cache_dir)
    if any(cache_root.glob("*.json")):
        return False

    url = os.environ.get("MINIFLASH_CACHE_URL", CACHE_URL)
    expected = os.environ.get("MINIFLASH_CACHE_SHA256", CACHE_SHA256)

    try:
        with urllib.request.urlopen(url, timeout=30) as response, tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
            archive.write(response.read())
            archive.flush()
            digest = hashlib.sha256(Path(archive.name).read_bytes()).hexdigest()
            if digest != expected:
                raise ValueError(f"checksum mismatch: got {digest[:12]}…, expected {expected[:12]}…")

            cache_root.mkdir(parents=True, exist_ok=True)
            installed = 0
            with tarfile.open(archive.name, "r:gz") as tar:
                for member in tar.getmembers():
                    name = Path(member.name).name
                    if not member.isfile() or not name.endswith(".json"):
                        continue
                    source = tar.extractfile(member)
                    (cache_root / name).write_bytes(source.read())
                    installed += 1

        print(f"miniflash: seeded {installed} cached cells into {cache_root} from {url}", file=sys.stderr)
        return True
    except Exception as error:
        print(f"miniflash: cache seed unavailable ({type(error).__name__}: {error}) — starting cold", file=sys.stderr)
        return False


def main():
    """CLI entry point: pre-warm a cache dir from the published seed archive.

    :returns: None.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Seed a miniflash cell cache from the published archive")
    parser.add_argument("cache_dir", nargs="?", default=".miniflash-cache", help="cache directory (default: .miniflash-cache)")
    arguments = parser.parse_args()

    download_cache(arguments.cache_dir)


if __name__ == "__main__":
    main()
