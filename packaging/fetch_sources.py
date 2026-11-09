"""Downloads the third-party sources that packaging/third_party_sources.json lists into third-party-sources/,
and checks every one.

    .venv\\Scripts\\python.exe packaging\\fetch_sources.py           # download what is missing, check all
    .venv\\Scripts\\python.exe packaging\\fetch_sources.py --check   # check only, download nothing

The checks: each file's size and SHA-256 against the list; a GitHub commit archive (a name ending in
-<commit>.tar.gz) must carry that commit; inside an MSYS2 source package (.src.tar.zst), every file must match
the checksum the package's own .SRCINFO gives. A file the list gives no SHA-256 or size for yet is downloaded and
its measured values are printed, to be written into the list once the file is checked by hand;
packaging/package_release.py packs pinned files only.

It downloads, so it runs on its own: the build and the packaging never call it.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "third_party_sources.json"
SOURCES = ROOT / "third-party-sources"
USER_AGENT = "Fotodit-release-tools"
COMMIT_ARCHIVE = re.compile(r"-([0-9a-f]{40})\.tar\.gz$")


def load(manifest: Path = MANIFEST) -> dict:
    return json.loads(manifest.read_text(encoding="utf-8"))


def measure(path: Path) -> tuple[int, str]:
    """Size and SHA-256 of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return path.stat().st_size, digest.hexdigest()


def download(url: str, target: Path, attempts: int = 4) -> None:
    """Fetches url into target through target.part, resuming a part an earlier try left behind."""
    part = target.with_name(target.name + ".part")
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        if have:
            request.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                resumed = bool(have) and response.status == 206  # a server without ranges sends it all again
                with open(part, "ab" if resumed else "wb") as fh:
                    while block := response.read(1 << 16):
                        fh.write(block)
            part.replace(target)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:  # nothing left to send: the part is whole, the checks will tell
                part.replace(target)
                return
            if exc.code < 500 and exc.code not in (408, 429):
                raise
            if attempt == attempts:
                raise
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            if attempt == attempts:
                raise
        time.sleep(2 * attempt)


def commit_archive_problems(path: Path, commit: str) -> list[str]:
    """A GitHub commit archive carries its commit id in the tar header."""
    with tarfile.open(path, "r:gz") as tf:
        carried = tf.pax_headers.get("comment", "")
    return [] if carried == commit else [f"the archive carries commit {carried or 'none'}, not {commit}"]


def msys2_problems(path: Path) -> list[str]:
    """Inside an MSYS2 source package, every source must match the checksum its .SRCINFO gives."""
    with tarfile.open(path, "r:zst") as tf:
        files = {m.name.split("/", 1)[-1]: tf.extractfile(m).read() for m in tf if m.isfile()}
    if ".SRCINFO" not in files:
        return ["no .SRCINFO inside"]
    sources, sums = [], []
    for line in files[".SRCINFO"].decode("utf-8").splitlines():
        key, _, value = line.strip().partition(" = ")
        if key == "source":
            sources.append(value)
        elif key == "sha256sums":
            sums.append(value)
    if not sources or len(sources) != len(sums):
        return ["its .SRCINFO does not give one checksum per source"]
    found = []
    for source, want in zip(sources, sums):
        name = source.split("::")[0] if "::" in source else source.rsplit("/", 1)[-1]
        data = files.get(name)
        if data is None:
            found.append(f"{name} is in its .SRCINFO but not inside")
        elif want != "SKIP" and hashlib.sha256(data).hexdigest() != want:
            found.append(f"{name} differs from the checksum in its .SRCINFO")
    return found


def problems(entry: dict, path: Path, size: int, digest: str) -> list[str]:
    """What is wrong with one file, by the list and by its own contents."""
    found = []
    if entry.get("size") is not None and size != entry["size"]:
        found.append(f"size {size}, the list says {entry['size']}")
    if entry.get("sha256") is not None and digest != entry["sha256"]:
        found.append(f"SHA-256 {digest}, the list says {entry['sha256']}")
    match = COMMIT_ARCHIVE.search(path.name)
    if match:
        found += commit_archive_problems(path, match.group(1))
    if path.name.endswith(".src.tar.zst"):
        found += msys2_problems(path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and check the third-party sources of a release.")
    parser.add_argument("--check", action="store_true", help="check the files already there; download nothing")
    args = parser.parse_args(argv)
    entries = load()["files"]
    SOURCES.mkdir(exist_ok=True)
    failed = unpinned = 0
    for entry in entries:
        path = SOURCES / entry["file"]
        if not path.is_file():
            if args.check:
                print(f"MISSING   {entry['file']}")
                failed += 1
                continue
            print(f"download  {entry['file']}", flush=True)
            try:
                download(entry["url"], path)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                print(f"FAILED    {entry['file']}: {exc}")
                failed += 1
                continue
        size, digest = measure(path)
        found = problems(entry, path, size, digest)
        if found:
            print(f"WRONG     {entry['file']}: " + "; ".join(found) + " (delete it and run again)")
            failed += 1
        elif entry.get("sha256") is None or entry.get("size") is None:
            print(f"UNPINNED  {entry['file']}: size {size}, sha256 {digest} (check it, then write both into the list)")
            unpinned += 1
        else:
            print(f"OK        {entry['file']}")
    print(f"{len(entries)} files: {failed} failed, {unpinned} not pinned yet")
    return 1 if failed or unpinned else 0


if __name__ == "__main__":
    sys.exit(main())
