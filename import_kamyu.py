"""Import free LeetCode solutions from kamyu104/LeetCode-Solutions into ./solutions.

Usage:
  python import_kamyu.py                      # all free algorithm problems
  python import_kamyu.py --difficulty easy,medium --limit 300
  python import_kamyu.py --dry-run

Downloads the repo tarball once (no per-file requests) and the public LeetCode
problem list (no login needed) to drop premium-only problems.
"""

from __future__ import annotations

import argparse
import io
import random
import sys
import tarfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SOLUTIONS_DIR = ROOT / "solutions"
LICENSE_FILE = ROOT / "THIRD_PARTY_LICENSE"

TARBALL_URL = "https://codeload.github.com/kamyu104/LeetCode-Solutions/tar.gz/refs/heads/master"
PROBLEMS_URL = "https://leetcode.com/api/problems/algorithms/"
SOURCE_DIR = "Python"
SOURCE_EXT = ".py"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
DIFFICULTY = {1: "easy", 2: "medium", 3: "hard"}


def free_problems(difficulties: set[str]) -> set[str]:
    resp = requests.get(PROBLEMS_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    slugs = set()
    for pair in resp.json()["stat_status_pairs"]:
        if pair["paid_only"]:
            continue
        if DIFFICULTY.get(pair["difficulty"]["level"]) not in difficulties:
            continue
        slugs.add(pair["stat"]["question__title_slug"])
    return slugs


def kamyu_sources() -> tuple[dict[str, bytes], bytes | None]:
    resp = requests.get(TARBALL_URL, timeout=300)
    resp.raise_for_status()
    files: dict[str, bytes] = {}
    license_text = None
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # Paths look like "LeetCode-Solutions-master/Python/two-sum.py".
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue
            rel = parts[1]
            if rel == "LICENSE.md":
                license_text = tar.extractfile(member).read()
            elif rel.startswith(SOURCE_DIR + "/") and rel.endswith(SOURCE_EXT) and rel.count("/") == 1:
                slug = rel[len(SOURCE_DIR) + 1 : -len(SOURCE_EXT)]
                files[slug] = tar.extractfile(member).read()
    return files, license_text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--difficulty", default="easy,medium,hard", help="comma list of easy/medium/hard")
    parser.add_argument("--limit", type=int, default=0, help="import at most N problems (random sample)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for --limit sampling")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    difficulties = {d.strip().lower() for d in args.difficulty.split(",") if d.strip()}
    if not difficulties <= set(DIFFICULTY.values()):
        parser.error("--difficulty must be a comma list of easy, medium, hard")

    print("Fetching LeetCode problem list...")
    free = free_problems(difficulties)
    print(f"  {len(free)} free problems matching {sorted(difficulties)}")

    print("Downloading kamyu104/LeetCode-Solutions tarball...")
    sources, license_text = kamyu_sources()
    print(f"  {len(sources)} {SOURCE_DIR}/*{SOURCE_EXT} files")

    SOLUTIONS_DIR.mkdir(exist_ok=True)
    existing = {p.stem for p in SOLUTIONS_DIR.iterdir() if p.is_file()}
    candidates = sorted(s for s in sources if s in free and s not in existing)
    if args.limit and len(candidates) > args.limit:
        candidates = sorted(random.Random(args.seed).sample(candidates, args.limit))

    print(f"  {len(candidates)} new solutions to import ({len(existing)} already present)")
    if args.dry_run:
        for slug in candidates[:20]:
            print(f"    {slug}")
        if len(candidates) > 20:
            print(f"    ... and {len(candidates) - 20} more")
        return 0

    for slug in candidates:
        (SOLUTIONS_DIR / f"{slug}{SOURCE_EXT}").write_bytes(sources[slug])

    if license_text and not LICENSE_FILE.exists():
        LICENSE_FILE.write_bytes(
            b"Files in solutions/ imported by import_kamyu.py come from\n"
            b"https://github.com/kamyu104/LeetCode-Solutions under the following license:\n\n"
            + license_text
        )

    print(f"Imported {len(candidates)} files into {SOLUTIONS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
