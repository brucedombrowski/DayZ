#!/usr/bin/env python3
"""Build the browser data index from Bohemia's published DayZ source.

Two commands, matching the pipeline in README.md:

    sync    fetch pinned upstream files into cache/, verify sha256
    build   parse cache/ -> docs/data/*.json   (pure: no network, no clock)

`build` is a pure function of the cache. Same bytes in, byte-identical JSON out:
all dicts are emitted with sorted keys, all lists in a defined order. That is what
makes an upstream diff reviewable rather than mysterious.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import struct
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUT = ROOT / "docs" / "data"
LOCK = ROOT / "sources.lock.json"

RAW = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"

# Chernarus+ is 15360m square. Used for bounds sanity checks.
MAP_SIZE = 15360

# --- sanity bounds -----------------------------------------------------------
# Violating one of these is a RED-tier event per README: fail closed rather than
# ship a silently wrong index.
BOUNDS = {
    "items": (1000, 5000),
    "groups": (300, 2000),
    "instances": (5000, 40000),
}


def tls_context() -> ssl.SSLContext:
    """Verified TLS, always.

    python.org macOS builds ship no CA bundle, so the default context fails to
    verify. Fall back to certifi rather than to unverified TLS -- see
    SECURITY.md: transport verification is not optional here.
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(cafile=__import__("certifi").where())
    except Exception:
        pass  # system store already loaded; if it is empty we fail closed below
    return ctx


CTX = tls_context()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, context=CTX, timeout=60) as r:
        return r.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_lock() -> dict:
    return json.loads(LOCK.read_text())


# --- sync --------------------------------------------------------------------

def cmd_sync(update: bool) -> int:
    lock = load_lock()
    changed = False

    for src_name, src in sorted(lock["sources"].items()):
        repo, commit = src["repo"], src["commit"]

        if update:
            head = json.loads(fetch(
                f"https://api.github.com/repos/{repo}/commits/{src.get('branch', 'master')}"
            ))["sha"]
            if head != commit:
                print(f"  {src_name}: {commit[:8]} -> {head[:8]}")
                commit = src["commit"] = head
                changed = True

            # Expand watched directories into explicit per-file entries. Listing
            # every file individually is deliberate: a recipe appearing or
            # vanishing upstream then shows up as a lockfile diff rather than
            # silently changing the build.
            for d in src.get("dirs", []):
                listing = json.loads(fetch(
                    f"https://api.github.com/repos/{repo}/contents/{d}?ref={commit}"
                ))
                found = {e["path"] for e in listing if e["type"] == "file"}
                for gone in [p for p in src["files"] if p.startswith(d + "/") and p not in found]:
                    del src["files"][gone]
                    changed = True
                for p in sorted(found):
                    src["files"].setdefault(p, {})

        for path, meta in sorted(src["files"].items()):
            dest = CACHE / src_name / path
            dest.parent.mkdir(parents=True, exist_ok=True)

            if dest.exists() and not update:
                digest = sha256(dest.read_bytes())
                if digest == meta.get("sha256"):
                    continue

            data = fetch(RAW.format(repo=repo, commit=commit, path=path))
            digest = sha256(data)

            if update:
                if meta.get("sha256") != digest:
                    changed = True
                meta["sha256"], meta["bytes"] = digest, len(data)
            elif meta.get("sha256") and meta["sha256"] != digest:
                # Integrity failure: upstream bytes are not what we pinned.
                print(f"FAIL {path}\n  expected {meta['sha256']}\n  got      {digest}",
                      file=sys.stderr)
                return 1

            dest.write_bytes(data)
            print(f"  {src_name}/{Path(path).name}  {len(data):,}b")

    if update and changed:
        LOCK.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        print("sources.lock.json updated")
    return 0


# --- parsers (pure: bytes -> structure) --------------------------------------

def parse_types(data: bytes) -> dict:
    """types.xml -> {item: {cat, tag[], usg[], tier[], nominal, min}}"""
    out = {}
    for t in ET.fromstring(data).findall("type"):
        name = t.get("name")
        if not name:
            continue
        cat = t.find("category")
        entry = {
            "cat": cat.get("name") if cat is not None else None,
            "tag": sorted({x.get("name") for x in t.findall("tag")}),
            "usg": sorted({x.get("name") for x in t.findall("usage")}),
            "tier": sorted({x.get("name") for x in t.findall("value")}),
            "nom": int((t.findtext("nominal") or 0)),
            "min": int((t.findtext("min") or 0)),
        }
        out[name] = entry
    return out


def parse_proto(data: bytes) -> dict:
    """mapgroupproto.xml -> {building: {usg[], cont:[{cat[],tag[],n}]}}

    Only containers with at least one loot point matter to us.
    """
    out = {}
    for g in ET.fromstring(data).findall("group"):
        conts = []
        for c in g.findall("container"):
            n = len(c.findall("point"))
            if not n:
                continue
            conts.append({
                "cat": sorted({x.get("name") for x in c.findall("category")}),
                "tag": sorted({x.get("name") for x in c.findall("tag")}),
                "n": n,
            })
        if conts:
            out[g.get("name")] = {
                "usg": sorted({u.get("name") for u in g.findall("usage")}),
                "cont": conts,
            }
    return out


def parse_pos(data: bytes, known: set[str], area=None) -> tuple[list[str], list[list[int]]]:
    """mapgrouppos.xml -> (building type names, [typeIdx, x, z, tier, usage] rows)

    pos is "x y z" with y = altitude; we keep x (easting) and z (northing),
    rounded to whole metres. Sub-metre precision is noise at map scale and
    rounding keeps the payload small.

    Each building is annotated with the tier/usage flags of its cell in
    areaflags.map. Resolving tier here rather than shipping the 4096x4096 grid to
    the browser turns a 16 MB download into five extra integers per row: the map
    only ever needs the tier *at a building*, not the whole field.

    ~14.5% of cells carry no value flags at all. For those we widen to a small
    neighbourhood and OR, rather than declaring the building tierless and hiding
    it from every search.
    """
    names: list[str] = []
    idx: dict[str, int] = {}
    rows: list[list[int]] = []

    grid = usage_blk = value_blk = None
    if area:
        grid, usage_blk, value_blk = area
        cell = MAP_SIZE / grid

    def flags(x: float, z: float) -> tuple[int, int]:
        if not area:
            return 0, 0
        c, r = int(x / cell), int(z / cell)
        c = min(max(c, 0), grid - 1)
        r = min(max(r, 0), grid - 1)
        i = r * grid + c
        val = value_blk[i]
        use = int.from_bytes(usage_blk[i * 4:i * 4 + 4], "little")
        if val == 0:  # unassigned cell: widen to +/-4 cells (~15m) and OR
            for rr in range(max(0, r - 4), min(grid, r + 5)):
                base = rr * grid
                for cc in range(max(0, c - 4), min(grid, c + 5)):
                    val |= value_blk[base + cc]
        return val, use

    for g in ET.fromstring(data).findall("group"):
        name = g.get("name")
        if name not in known:
            continue
        x, _y, z = (float(v) for v in g.get("pos").split())
        if not (0 <= x <= MAP_SIZE and 0 <= z <= MAP_SIZE):
            continue
        if name not in idx:
            idx[name] = len(names)
            names.append(name)
        tier, use = flags(x, z)
        rows.append([idx[name], round(x), round(z), tier, use])

    rows.sort()
    return names, rows


def parse_limits(data: bytes) -> dict:
    """cfglimitsdefinition.xml -> {'usage': [...], 'value': [...]} in declared order.

    Declaration order IS bit order in areaflags.map -- verified empirically by
    checking the usage bitmask at the position of every building whose prototype
    declares exactly one usage. Military->bit0, Police->bit1 ... Hunting->bit9 all
    matched independently.
    """
    root = ET.fromstring(data)
    return {
        "usage": [u.get("name") for u in root.findall("./usageflags/usage")],
        "value": [v.get("name") for v in root.findall("./valueflags/value")],
        "category": [c.get("name") for c in root.findall("./categories/category")],
        "tag": [t.get("name") for t in root.findall("./tags/tag")],
    }


def parse_areaflags(data: bytes) -> tuple[int, memoryview, memoryview]:
    """areaflags.map -> (grid size, usage u32 block, value u8 block).

    Layout, reverse-engineered and confirmed against cfglimitsdefinition.xml:

        offset 0   u32 gridW, gridH, worldW, worldH, bitsPerCell(32), reserved
        offset 24  gridW*gridH * u32   usage bitmask
        then       gridW*gridH * u8    value/tier bitmask

    Cell (row, col) = (z / cellSize, x / cellSize); row maps to +z with no flip.
    """
    gw, gh, world_w, world_h, bits, _ = struct.unpack_from("<6I", data, 0)
    if not (gw == gh and world_w == world_h == MAP_SIZE and bits == 32):
        raise ValueError(f"unexpected areaflags header: {gw},{gh},{world_w},{world_h},{bits}")
    cells = gw * gh
    want = 24 + cells * 4 + cells
    if len(data) != want:
        raise ValueError(f"areaflags size {len(data)} != expected {want}")
    mv = memoryview(data)
    return gw, mv[24:24 + cells * 4], mv[24 + cells * 4:]


RE_CLASS = re.compile(r"class\s+(\w+)\s+extends\s+RecipeBase")
RE_ING = re.compile(r'InsertIngredient\s*\(\s*(\d+)\s*,\s*"([^"]+)"')
RE_RES = re.compile(r'AddResult\s*\(\s*"([^"]+)"')
RE_DESTROY = re.compile(r"m_IngredientDestroy\[(\d+)\]\s*=\s*(true|false)")
RE_NAME = re.compile(r'm_Name\s*=\s*"([^"]+)"')


def parse_recipe(text: str) -> dict | None:
    """One RecipeBase .c file -> recipe dict, or None if it produces nothing.

    Line comments are stripped first: several files keep disabled ingredients
    commented out, and counting those would invent requirements.
    """
    src = re.sub(r"//.*", "", text)
    cls = RE_CLASS.search(src)
    results = RE_RES.findall(src)
    if not cls or not results:
        return None

    destroy = {int(s): v == "true" for s, v in RE_DESTROY.findall(src)}
    slots: dict[int, list[str]] = defaultdict(list)
    for slot, item in RE_ING.findall(src):
        s = int(slot)
        if item not in slots[s]:
            slots[s].append(item)

    name = cls.group(1)
    label = RE_NAME.search(src)
    return {
        "id": name,
        "label": label.group(1) if label else name,
        # decraft/salvage recipes invert the graph; flag them so the tree can
        # default to real craft edges only (CRAFTING.md, trap 2).
        "kind": "decraft" if name.lower().startswith("decraft") else "craft",
        # destroy=false means a tool you must hold, not a consumed material.
        "ing": [
            {"any": slots[s], "tool": not destroy.get(s, True)}
            for s in sorted(slots)
        ],
        "out": results,
    }


# --- build -------------------------------------------------------------------

def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"  {path.relative_to(ROOT)}  {path.stat().st_size:,}b")


def cmd_build() -> int:
    ce = CACHE / "central-economy" / "dayzOffline.chernarusplus"
    items = parse_types((ce / "db" / "types.xml").read_bytes())
    groups = parse_proto((ce / "mapgroupproto.xml").read_bytes())
    limits = parse_limits((ce / "cfglimitsdefinition.xml").read_bytes())
    area = parse_areaflags((ce / "areaflags.map").read_bytes())
    names, rows = parse_pos((ce / "mapgrouppos.xml").read_bytes(), set(groups), area)

    recipe_dir = CACHE / "script-diff" / "scripts/4_world/classes/recipes/recipes"
    recipes = []
    if recipe_dir.is_dir():
        for f in sorted(recipe_dir.glob("*.c")):
            r = parse_recipe(f.read_text(errors="ignore"))
            if r:
                recipes.append(r)

    counts = {"items": len(items), "groups": len(groups), "instances": len(rows)}
    for key, (lo, hi) in BOUNDS.items():
        if not lo <= counts[key] <= hi:
            print(f"FAIL sanity: {key}={counts[key]} outside [{lo},{hi}]", file=sys.stderr)
            return 1

    lock = load_lock()
    write_json(OUT / "items.json", items)
    write_json(OUT / "groups.json", groups)
    write_json(OUT / "instances.json", {"types": names, "rows": rows})
    write_json(OUT / "limits.json", limits)
    write_json(OUT / "recipes.json", recipes)
    write_json(OUT / "meta.json", {
        "map": "chernarusplus",
        "mapSize": MAP_SIZE,
        "dayzBuild": lock.get("dayz_build"),
        "commits": {k: v["commit"] for k, v in sorted(lock["sources"].items())},
        "counts": {**counts, "recipes": len(recipes)},
        "areaGrid": area[0],
        "tierResolved": True,
    })

    print(f"\n  {counts['items']} items, {counts['groups']} building types, "
          f"{counts['instances']} instances, {len(recipes)} recipes")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="fetch pinned upstream files")
    s.add_argument("--update", action="store_true",
                   help="repin to upstream HEAD and rewrite the lockfile")
    sub.add_parser("build", help="parse cache -> docs/data")

    a = ap.parse_args()
    return cmd_sync(a.update) if a.cmd == "sync" else cmd_build()


if __name__ == "__main__":
    sys.exit(main())
