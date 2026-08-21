#!/usr/bin/env python3
"""Derive and audit config from the DayZ source data.

The configs in config/ are hand-authored because they hold knowledge the source
system does not contain -- place names, what "come back full" means, which
buildings are worth marking. But the STRUCTURE around that knowledge should not be
hand-typed, and hand-typed entries drift silently as upstream changes.

So this tool draws the line explicitly:

    code derives structure  |  human supplies knowledge

    coordinates             |  place names, Cyrillic
    class-name families     |  which families deserve a landmark, and its glyph
    what exists / spawns    |  which items matter for a given kind of run

Commands
    audit     compare every config against the live data and report drift
    places    propose place entries from building-density clustering
    landmarks propose landmark rules from unclassified building families

Nothing here writes config. It prints proposals; a human decides and commits.
That is deliberate -- a generator that edits its own inputs erases the
distinction above.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
CFG = ROOT / "config"

load = lambda n: json.loads((OUT / f"{n}.json").read_text())
cfg = lambda n: json.loads((CFG / f"{n}.json").read_text())


def clusters(rows, cell=300, min_n=8):
    """Grid-cluster building positions. Deterministic: fixed grid, sorted output."""
    cells = defaultdict(list)
    for ti, x, z, *_ in rows:
        cells[(x // cell, z // cell)].append((x, z))
    out = []
    for pts in cells.values():
        if len(pts) < min_n:
            continue
        out.append({"n": len(pts),
                    "x": round(sum(p[0] for p in pts) / len(pts)),
                    "z": round(sum(p[1] for p in pts) / len(pts))})
    out.sort(key=lambda c: (-c["n"], c["x"], c["z"]))
    return out


def cmd_places(args) -> int:
    """Propose place entries: coordinates from data, names left for a human."""
    inst = load("instances")
    known = cfg("places")["places"]
    found = clusters(inst["rows"], min_n=args.min)

    unnamed = []
    for c in found:
        near = min((abs(c["x"]-p["x"]) + abs(c["z"]-p["z"]), p["name"]) for p in known)
        if near[0] > args.radius:
            unnamed.append(c)

    print(f"{len(found)} building clusters >= {args.min} buildings; "
          f"{len(known)} named in config; {len(unnamed)} unnamed\n")
    print("Largest unnamed clusters -- these are places the map cannot label:")
    for c in unnamed[:args.limit]:
        print(f'    {{ "name": "?", "cyrillic": "?", "x": {c["x"]}, "z": {c["z"]} }},'
              f'   // {c["n"]} buildings')
    print("\nCoordinates are derived. Names are not derivable from any published "
          "Bohemia data -- fill them in by hand and the build will validate the "
          "coordinates.")
    return 0


def cmd_landmarks(args) -> int:
    """Propose landmark rules from building families no existing rule claims."""
    inst = load("instances")
    rules = cfg("landmarks")["rules"]
    pats = [(r["id"], re.compile(r["pattern"], re.I)) for r in rules]

    counts = Counter()
    for ti, *_ in inst["rows"]:
        counts[inst["types"][ti]] += 1

    unclaimed = Counter()
    examples = defaultdict(list)
    for name, n in counts.items():
        if any(rx.search(name) for _id, rx in pats):
            continue
        # family = the class name with trailing variant/number suffixes stripped
        fam = re.sub(r"(_[A-Za-z]+)?\d*$", "", name.replace("Land_", ""))
        unclaimed[fam] += n
        if len(examples[fam]) < 3:
            examples[fam].append(name)

    print(f"{len(counts)} building classes in mapgrouppos.xml, "
          f"{sum(1 for n in counts if not any(rx.search(n) for _i, rx in pats))} "
          f"unclaimed by any landmark rule\n")
    print("Largest unclassified families -- candidates for a new rule:")
    for fam, n in unclaimed.most_common(args.limit):
        print(f"    {n:5d}  {fam:28s}  e.g. {', '.join(examples[fam])}")
    print("\nWhether any of these deserves a map marker is a judgement call, "
          "which is why this prints instead of writing.")
    return 0


def cmd_audit(args) -> int:
    """Compare every config against live data. Exit non-zero on drift."""
    items, inst, groups = load("items"), load("instances"), load("groups")
    limits, places_out = load("limits"), load("places")
    problems, notes = [], []

    # --- places: coordinates still sit on buildings, names still present -----
    pcfg = cfg("places")
    rule = pcfg["minBuildingsWithin"]
    for p in pcfg["places"]:
        n = sum(1 for _t, x, z, *_ in inst["rows"]
                if (x-p["x"])**2 + (z-p["z"])**2 <= rule["radius"]**2)
        if n < rule["count"]:
            problems.append(f"places: {p['name']} has {n} buildings within "
                            f"{rule['radius']}m (need {rule['count']})")
    unver = [p["name"] for p in pcfg["places"] if p.get("unverified")]
    if unver:
        notes.append(f"places: {len(unver)} names unverified against the game: "
                     + ", ".join(sorted(unver)))

    # --- profiles: every selector still resolves ----------------------------
    live = {n for n, it in items.items() if it["nom"] > 0}
    for prof in cfg("loot-profiles")["profiles"]:
        inc = prof.get("include", {})
        for n in inc.get("names", []):
            if n not in live:
                problems.append(f"profiles/{prof['id']}: '{n}' no longer spawns")
        for pat in inc.get("patterns", []):
            if not any(re.search(pat, n, re.I) for n in live):
                problems.append(f"profiles/{prof['id']}: pattern '{pat}' matches nothing")
        for c in inc.get("categories", []):
            if not any(items[n]["cat"] == c for n in live):
                problems.append(f"profiles/{prof['id']}: category '{c}' matches nothing")
        for u in inc.get("usages", []):
            if u not in limits["usage"]:
                problems.append(f"profiles/{prof['id']}: usage '{u}' not in cfglimitsdefinition")

    # --- landmarks: every rule still claims something ------------------------
    classes = {inst["types"][ti] for ti, *_ in inst["rows"]}
    for r in cfg("landmarks")["rules"]:
        rx = re.compile(r["pattern"], re.I)
        if not any(rx.search(c) for c in classes) and not r.get("optional"):
            problems.append(f"landmarks/{r['id']}: pattern '{r['pattern']}' matches nothing")

    # --- coverage: how much of the world do we actually classify? ------------
    claimed = sum(1 for c in classes
                  if any(re.search(r["pattern"], c, re.I) for r in cfg("landmarks")["rules"]))
    notes.append(f"landmarks: {claimed}/{len(classes)} building classes classified "
                 f"({100*claimed/len(classes):.0f}%)")

    # --- flag aliases: vanilla does not use them; catch it if that changes ---
    bad = {v for it in items.values() for v in it["tier"] if v not in limits["value"]}
    bad |= {u for it in items.values() for u in it["usg"] if u not in limits["usage"]}
    if bad:
        problems.append("types.xml uses flag names absent from cfglimitsdefinition "
                        f"(user-defined aliases?): {sorted(bad)}")

    for n in notes:
        print(f"  note  {n}")
    for p in problems:
        print(f"  DRIFT {p}", file=sys.stderr)
    print(f"\n{len(problems)} drift issue(s), {len(notes)} note(s)")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="compare config against live data")
    p = sub.add_parser("places", help="propose place entries from clustering")
    p.add_argument("--min", type=int, default=25, help="min buildings per cluster")
    p.add_argument("--radius", type=int, default=600, help="match distance to a known place")
    p.add_argument("--limit", type=int, default=25)
    l = sub.add_parser("landmarks", help="propose landmark rules")
    l.add_argument("--limit", type=int, default=25)

    args = ap.parse_args()
    return {"audit": cmd_audit, "places": cmd_places,
            "landmarks": cmd_landmarks}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
