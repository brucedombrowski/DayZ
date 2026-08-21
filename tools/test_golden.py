#!/usr/bin/env python3
"""Golden tests: lock the numbers the docs cite and the invariants we rely on.

Run after `build`. A failure here is not necessarily a bug -- Bohemia may simply
have shipped a patch. It means *look at the diff before publishing*, which is the
whole point of the tiered auto-ingest in README.md.

    python3 tools/build_index.py sync && python3 tools/build_index.py build
    python3 tools/test_golden.py
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "data"
load = lambda n: json.loads((OUT / f"{n}.json").read_text())

items, groups, inst = load("items"), load("groups"), load("instances")
limits, recipes, meta = load("limits"), load("recipes"), load("meta")

TIER = {n: 1 << i for i, n in enumerate(limits["value"])}
fails, checks = [], 0


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")


def points_for(building, it, strict_tags=True):
    """Mirror of pointsFor() in docs/index.html. If these drift, the site lies."""
    g = groups.get(building)
    if not g:
        return 0
    if it["usg"] and not any(u in g["usg"] for u in it["usg"]):
        return 0
    n = 0
    for c in g["cont"]:
        if it["cat"] and it["cat"] not in c["cat"]:
            continue
        if it["tag"] and (strict_tags or c["tag"]) and not any(t in c["tag"] for t in it["tag"]):
            continue
        n += c["n"]
    return n


# --- flag bit order: this is what makes areaflags.map readable at all ---------
check("Tier1 bit", TIER["Tier1"], 1)
check("Tier4 bit", TIER["Tier4"], 8)
check("usage[0]", limits["usage"][0], "Military")
check("usage[4]", limits["usage"][4], "Industrial")
check("usage[9]", limits["usage"][9], "Hunting")

# --- corpus size -------------------------------------------------------------
check("items", meta["counts"]["items"], 1970)
check("building types", meta["counts"]["groups"], 431)
check("instances", meta["counts"]["instances"], 11360)
check("recipes", meta["counts"]["recipes"], 163)
check("area grid", meta["areaGrid"], 4096)

# --- every building resolved a tier -----------------------------------------
check("buildings with no tier", sum(1 for r in inst["rows"] if r[3] == 0), 0)

# --- the SledgeHammer chain, end to end (README's worked example) ------------
sh = items["SledgeHammer"]
check("SledgeHammer category", sh["cat"], "tools")
check("SledgeHammer tags", sh["tag"], ["floor"])
check("SledgeHammer usage", sh["usg"], ["Industrial"])
check("SledgeHammer tiers", sh["tier"], ["Tier3", "Tier4"])

check("proto types matching", sum(1 for b in groups if points_for(b, sh) > 0), 134)

mask = sum(TIER[t] for t in sh["tier"])
ungated = sum(points_for(inst["types"][r[0]], sh) for r in inst["rows"])
gated = sum(points_for(inst["types"][r[0]], sh) for r in inst["rows"] if r[3] & mask)
check("points ungated", ungated, 32624)
check("points tier-gated", gated, 11182)

# The two 300m cluster cells that make the case for tier gating.
# Chernogorsk is structurally excellent for a sledgehammer and almost entirely
# the wrong tier; Zelenogorsk is the real SW answer and survives untouched.
# Without the gate they look like a 352 vs 356 tie -- a coin flip that would send
# you to the wrong town.
def cell_points(cx, cz, gate):
    return sum(points_for(inst["types"][r[0]], sh) for r in inst["rows"]
               if r[1] // 300 == cx and r[2] // 300 == cz
               and (not gate or r[3] & mask))

check("Chernogorsk ungated", cell_points(21, 8, False), 352)
check("Chernogorsk tier-gated", cell_points(21, 8, True), 13)
check("Zelenogorsk ungated", cell_points(8, 17, False), 356)
check("Zelenogorsk tier-gated", cell_points(8, 17, True), 356)

# --- crafting graph ----------------------------------------------------------
made_by = {}
for r in recipes:
    for o in r["out"]:
        made_by.setdefault(o, []).append(r)
check("craftable outputs", len(made_by), 133)

bow = [r for r in made_by.get("QuickieBow", []) if r["kind"] == "craft"]
check("QuickieBow craft recipes", len(bow), 1)
check("QuickieBow materials",
      sorted(g["any"][0] for g in bow[0]["ing"] if not g["tool"]),
      ["LongWoodenStick", "Rope"])

# A tool ingredient must exist somewhere, or trap 1 has regressed.
check("some recipe has a tool slot",
      any(g["tool"] for r in recipes for g in r["ing"]), True)

# --- SECURITY.md: names must stay inert before we interpolate them into HTML --
bad = [n for n in list(items) + list(groups) if not n.replace("_", "").isalnum()]
check("item/building names are alphanumeric", bad[:3], [])

print(f"{checks - len(fails)}/{checks} checks passed")
for f in fails:
    print("  FAIL", f, file=sys.stderr)
sys.exit(1 if fails else 0)
