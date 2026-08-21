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


def points_for(building, it, strict_tags=True, field="eff"):
    """Mirror of pointsFor() in docs/index.html. If these drift, the site lies.

    field="eff" counts slots that actually hold loot (lootmax applied);
    field="n" counts raw loot points. The gap between them is large -- 85% of
    containers cap below their point count -- so ranking on raw points overstates
    real loot roughly 4x.
    """
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
        n += c[field]
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
raw = sum(points_for(inst["types"][r[0]], sh, field="n") for r in inst["rows"])
eff = sum(points_for(inst["types"][r[0]], sh) for r in inst["rows"])
gated = sum(points_for(inst["types"][r[0]], sh) for r in inst["rows"] if r[3] & mask)
check("raw loot points", raw, 32624)
check("effective slots", round(eff, 1), 11490.7)
check("effective, tier-gated", round(gated, 1), 3918.6)
# lootmax matters: if this ratio ever approaches 1, the cap stopped being applied
check("lootmax cuts loot by >half", eff < raw * 0.5, True)

# The two 300m cluster cells that make the case for tier gating.
# Chernogorsk is structurally excellent for a sledgehammer and almost entirely
# the wrong tier; Zelenogorsk is the real SW answer and survives untouched.
# Without the gate they look like a 352 vs 356 tie -- a coin flip that would send
# you to the wrong town.
def cell_points(cx, cz, gate):
    return round(sum(points_for(inst["types"][r[0]], sh) for r in inst["rows"]
                     if r[1] // 300 == cx and r[2] // 300 == cz
                     and (not gate or r[3] & mask)), 1)

check("Chernogorsk ungated", cell_points(21, 8, False), 102.4)
check("Chernogorsk tier-gated", cell_points(21, 8, True), 7.0)
check("Zelenogorsk ungated", cell_points(8, 17, False), 108.2)
check("Zelenogorsk tier-gated", cell_points(8, 17, True), 108.2)

# --- reverse loot lookup ---------------------------------------------------
# Mirrors buildingLoot() in docs/index.html. The tier gate uses the flags of the
# INSTANCE, so the same shed holds different loot in Tier 1 and Tier 3 -- that
# difference is the whole reason the lookup is per-building.
def building_loot(type_name, tier_flags):
    g = groups.get(type_name)
    if not g:
        return []
    out = []
    for c in g["cont"]:
        got = []
        for name, it in items.items():
            if not it["nom"] or not it["cat"]:
                continue
            if it["cat"] not in c["cat"]:
                continue
            if it["tag"] and not any(t in c["tag"] for t in it["tag"]):
                continue
            if it["usg"] and not any(u in g["usg"] for u in it["usg"]):
                continue
            mask = sum(TIER[t] for t in it["tier"] if t in TIER)
            if mask and not (tier_flags & mask):
                continue
            got.append(name)
        if got:
            out.append(got)
    return out

t1 = [n for c in building_loot("Land_Shed_M1", TIER["Tier1"]) for n in c]
t3 = [n for c in building_loot("Land_Shed_M1", TIER["Tier3"]) for n in c]
check("Shed_M1 Tier1 item count", len(t1), 56)
check("Shed_M1 Tier1 excludes SledgeHammer", "SledgeHammer" in t1, False)
check("Shed_M1 Tier3 includes SledgeHammer", "SledgeHammer" in t3, True)
# A higher tier is NOT a superset: some items are deliberately low-tier only, so
# walking north genuinely costs you access to them. Sickle is Tier1/Tier2.
check("Sickle is low-tier only", items["Sickle"]["tier"], ["Tier1", "Tier2"])
check("Sickle in Tier1 shed", "Sickle" in t1, True)
check("Sickle absent from Tier3 shed", "Sickle" in t3, False)

# Unique gating. Plastic_Explosive is category `explosives`, RemoteDetonator is
# `tools`, so they need containers of different kinds -- pick accordingly.
def gated_by_unique(item, need_cat):
    b = next((b for b, g in groups.items()
              if "Industrial" in g["usg"]
              and any(need_cat in c["cat"] for c in g["cont"])), None)
    if not b:
        return None
    without = [n for c in building_loot(b, TIER["Tier3"]) for n in c]
    withuq = [n for c in building_loot(b, TIER["Tier3"] | TIER["Unique"]) for n in c]
    return (item in without, item in withuq)

check("Plastic_Explosive gated by Unique",
      gated_by_unique("Plastic_Explosive", "explosives"), (False, True))
check("RemoteDetonator gated by Unique",
      gated_by_unique("RemoteDetonator", "tools"), (False, True))

# --- loot profiles: the preference config must stay resolvable ----------------
# resolve_profiles() already fails the BUILD on a stale selector; these assert the
# resolved output is sane, and that the derived-value case is actually captured.
profiles = load("profiles")
by_id = {p["id"]: p for p in profiles["profiles"]}
check("profile ids", sorted(by_id),
      ["basebuild", "foodwater", "guncare", "hunting", "medical", "ragsource"])
check("no empty profile", [p["id"] for p in profiles["profiles"] if not p["items"]], [])
check("basebuild has SledgeHammer", "SledgeHammer" in by_id["basebuild"]["items"], True)
check("ragsource is all clothes",
      all(items[n]["cat"] == "clothes" for n in by_id["ragsource"]["items"]), True)
check("hunting profile is Hunting-usage",
      all("Hunting" in items[n]["usg"] for n in by_id["hunting"]["items"]), True)
# every referenced item must still spawn, or the profile is quietly dead weight
dead = [n for p in profiles["profiles"] for n in p["items"] if not items[n]["nom"]]
check("no non-spawning items in profiles", dead[:3], [])

# --- cargo: loot is not flat ---------------------------------------------------
# 298 spawning items arrive carrying something. Counting a spawned backpack as one
# item understates a run -- most for weapons, which bring ammo and attachments.
cargo = load("cargo")
spawning_with_cargo = [n for n in cargo if n in items and items[n]["nom"] > 0]
check("items with cargo that spawn", len(spawning_with_cargo), 298)
check("M4A1 carries attachments", "M4A1" in cargo, True)
check("DryBag carries rope sometimes", "Rope" in cargo.get("DryBag_Black", {}), True)
# Cumulative-selection semantics: a group's expected items cannot exceed 1 per
# group. M4A1 has 4 groups, so its total must stay at or under 4.
check("M4A1 cargo within group bound",
      round(sum(cargo["M4A1"].values()), 2) <= 4.0, True)
# No item should claim to contain itself.
selfref = [n for n, bag in cargo.items() if n in bag]
check("no self-containing items", selfref[:3], [])

# --- saturating objective ----------------------------------------------------
# P(at least one) must flatten, or the planner would keep spending time on a
# target it has already almost certainly found.
import math
p_at = lambda lam: 1 - math.exp(-lam)
check("lambda=1 -> 63%", round(p_at(1), 2), 0.63)
check("lambda=2 -> 86%", round(p_at(2), 2), 0.86)
check("lambda=3 -> 95%", round(p_at(3), 2), 0.95)
check("3rd unit adds less than 1st", (p_at(3) - p_at(2)) < (p_at(1) - p_at(0)), True)

# Deer stands: the value-per-second case. A narrow usage pool concentrates odds.
ds = groups["Land_Misc_DeerStand1"]
check("deer stand is Hunting-only", ds["usg"], ["Hunting"])
check("deer stand is small", sum(c["n"] for c in ds["cont"]) <= 4, True)

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
