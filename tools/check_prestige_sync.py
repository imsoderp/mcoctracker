#!/usr/bin/env python3
"""Check the baked-in PRESTIGE_7 table in index.html against live mcochub data.

Usage:
    python3 tools/check_prestige_sync.py

Fetches https://mcochub.insaneskull.com/data/prestige.json?tier=7&rank={1..6},
takes each champion's max-sig (sig 200) value — the same convention PRESTIGE_7
uses — and reports:

  * VALUE MISMATCHES  — a number in index.html differs from mcochub
  * MISSING LOCALLY   — champions on mcochub with no PRESTIGE_7 entry (new 7★s),
                        printed as paste-ready lines
  * EXTRA LOCALLY     — PRESTIGE_7 keys that no longer match anything on mcochub
  * UNMATCHED NAMES   — mcochub names the matcher couldn't map to a local key;
                        add these to MANUAL_ALIASES below and rerun

Exit codes: 0 = in sync, 1 = differences found, 2 = fetch/parse error.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://mcochub.insaneskull.com/data/prestige.json?tier=7&rank={rank}"
INDEX_HTML = Path(__file__).resolve().parent.parent / "index.html"
RANKS = range(1, 7)

# mcochub display name -> local PRESTIGE_7 key (or list of keys, when one
# mcochub row covers several local entries). Extend this when the script
# reports a name it can't map.
MANUAL_ALIASES = {
    "Goldpool": "deadpool_gold",
    "Platinum Pool": "deadpool_platinum",
    "Kang the Conqueror": "kang",
    "Magneto (House of X)": "magneto",
    "Mister Fantastic": "mrfantastic",
    "Void": "void_current",              # local: The Void
    "Ultron (Classic)": "ultron_prime",  # plain "Ultron" auto-matches local 'ultron'
}


def norm(s):
    """Lowercase, alphanumerics only — 'Spider-Man' -> 'spiderman'."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def wordset(s):
    """Order-insensitive word key — 'Hulk (Immortal)' == 'Immortal Hulk'."""
    return frozenset(re.findall(r"[a-z0-9]+", s.lower()))


def parse_index():
    html = INDEX_HTML.read_text(encoding="utf-8")

    m = re.search(r"const PRESTIGE_7 = \{([\s\S]*?)\n\};", html)
    if not m:
        sys.exit("Could not find PRESTIGE_7 in index.html")
    local = {}
    for key, vals in re.findall(r"^\s*([A-Za-z0-9_]+):\[([^\]]+)\]", m.group(1), re.M):
        local[key] = [None if v.strip() == "null" else int(v) for v in vals.split(",")]

    m = re.search(r"const CHAMPION_LIST = \[([\s\S]*?)\n\];", html)
    if not m:
        sys.exit("Could not find CHAMPION_LIST in index.html")
    champs = {}  # key -> display name
    for key, n1, n2 in re.findall(
        r"\{key:'([^']+)'\s*,\s*name:(?:'([^']*)'|\"([^\"]*)\")", m.group(1)
    ):
        champs[key] = n1 or n2
    return local, champs


def fetch_remote():
    """Return {slug: {'name': str, 'byrank': [r1..r6 or None]}}."""
    remote = {}
    for rank in RANKS:
        req = urllib.request.Request(
            BASE_URL.format(rank=rank),
            headers={"User-Agent": "mcoc-tracker-sync-check"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        for row in data["rows"]:
            sigs = row.get("sigs") or {}
            val = sigs.get("200")
            if val is None and sigs:  # fall back to the highest listed sig
                val = sigs[max(sigs, key=int)]
            entry = remote.setdefault(
                row["slug"], {"name": row["name"], "byrank": [None] * 6}
            )
            entry["byrank"][rank - 1] = val
    return remote


def match_remote_to_local(remote, champs, local):
    """Map each mcochub slug to local key(s). Returns (mapping, unmatched_slugs).

    mapping is {slug: [key, ...]} — normally one key, but a manual alias may
    fan one mcochub row out to several local entries.
    """
    by_norm_name = {}
    by_wordset = {}
    for key, name in champs.items():
        by_norm_name.setdefault(norm(name), key)
        by_wordset.setdefault(wordset(name), key)
    local_keys_norm = {norm(k): k for k in local}
    alias_norm = {
        norm(n): (k if isinstance(k, list) else [k]) for n, k in MANUAL_ALIASES.items()
    }

    mapping, unmatched = {}, []
    for slug, info in remote.items():
        n = norm(info["name"])
        keys = alias_norm.get(n)
        if not keys:
            key = (
                by_norm_name.get(n)
                or by_wordset.get(wordset(info["name"]))
                or local_keys_norm.get(norm(slug))
            )
            keys = [key] if key else None
        if keys:
            mapping[slug] = keys
        else:
            unmatched.append(slug)

    # Guard against two different remote rows landing on the same local key
    seen = {}
    for slug, keys in list(mapping.items()):
        for key in keys:
            if key in seen and seen[key] != slug:
                print(f"⚠ ambiguous match: '{remote[slug]['name']}' and "
                      f"'{remote[seen[key]]['name']}' both map to '{key}' — fix MANUAL_ALIASES")
                mapping[slug] = [k for k in mapping[slug] if k != key]
                if not mapping[slug]:
                    del mapping[slug]
                    unmatched.append(slug)
            else:
                seen[key] = slug
    return mapping, unmatched


def fmt_row(key, vals):
    return f"  {key}:[{','.join('null' if v is None else str(v) for v in vals)}],"


def main():
    local, champs = parse_index()
    print(f"Local:  {len(local)} champions in PRESTIGE_7")
    try:
        remote = fetch_remote()
    except Exception as e:
        sys.exit(f"Failed to fetch mcochub data: {e}")
    print(f"Remote: {len(remote)} champions on mcochub (tier 7)\n")

    mapping, unmatched = match_remote_to_local(remote, champs, local)
    problems = 0

    # 1. Value mismatches on champions present in both
    diffs = []
    slug_of = {}
    for slug, keys in mapping.items():
        for key in keys:
            slug_of[key] = slug
            if key not in local:
                continue
            for i, (lv, rv) in enumerate(zip(local[key], remote[slug]["byrank"])):
                if lv != rv:
                    diffs.append((key, i + 1, lv, rv))
    if diffs:
        problems += len(diffs)
        print(f"VALUE MISMATCHES ({len(diffs)}):")
        for key, rank, lv, rv in diffs:
            print(f"  {key} R{rank}: local={lv}  mcochub={rv}")
        print("\n  Corrected paste-ready lines:")
        for key in sorted({d[0] for d in diffs}):
            print(fmt_row(key, remote[slug_of[key]]["byrank"]))
        print()

    # 2. On mcochub but missing from PRESTIGE_7 (new 7★ releases)
    missing = [
        (slug, key)
        for slug, keys in mapping.items()
        for key in keys
        if key not in local
    ]
    if missing or unmatched:
        print(f"MISSING LOCALLY ({len(missing) + len(unmatched)}):")
        for slug, key in sorted(missing, key=lambda x: x[1]):
            print(f"  {remote[slug]['name']} — in CHAMPION_LIST as '{key}', add to PRESTIGE_7:")
            print(fmt_row(key, remote[slug]["byrank"]))
        for slug in sorted(unmatched):
            info = remote[slug]
            # Same key format the in-app generator (slugifyChampionName) produces
            gen_key = re.sub(r"[^a-z0-9]+", "_", info["name"].lower()).strip("_")
            print(f"  {info['name']} (slug '{slug}') — no CHAMPION_LIST match found.")
            print(f"    If new to the game: add via the champion generator (triple-click the logo),")
            print(f"    entering these prestige values: {fmt_row(gen_key, info['byrank']).strip()}")
            print(f"    If it exists under another name: add to MANUAL_ALIASES and rerun.")
        problems += len(missing) + len(unmatched)
        print()

    # 3. Local keys that matched nothing on mcochub
    matched_keys = {k for keys in mapping.values() for k in keys}
    extra = [k for k in local if k not in matched_keys]
    if extra:
        problems += len(extra)
        print(f"EXTRA LOCALLY ({len(extra)}) — in PRESTIGE_7 but not on mcochub:")
        for k in sorted(extra):
            print(f"  {k} ({champs.get(k, '?')})")
        print()

    if problems:
        print(f"✗ {problems} difference(s) found.")
        sys.exit(1)
    print("✓ PRESTIGE_7 is in sync with mcochub.")


if __name__ == "__main__":
    main()
