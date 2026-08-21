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
import zlib
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
        # count_in_* decide whether a copy held by a player still counts toward
        # `nominal`. When all three are 0, stashing one frees the economy to spawn
        # a replacement -- so hoarding cannot make that item scarcer.
        f = t.find("flags")
        if f is not None:
            entry["cnt"] = {k: int(f.get("count_in_" + k, 0))
                            for k in ("cargo", "hoarder", "map", "player")}
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


def write_png(path: Path, w: int, h: int, rgba: bytearray) -> None:
    """Minimal RGBA PNG encoder.

    Hand-rolled to keep the build dependency-free and deterministic: no Pillow, and
    zlib at a fixed level so the same input always produces the same bytes.
    """
    raw = bytearray()
    for y in range(h):                       # filter byte 0 (None) per scanline
        raw.append(0)
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b""))


# Tier1 green -> Tier4 red, matching Bohemia's own documentation.
TIER_RGB = [(90, 170, 70), (215, 190, 60), (225, 140, 45), (210, 65, 55)]


def render_unique_png(path: Path, area, down: int = 4) -> int:
    """The `Unique` valueflag (bit 4) as its own overlay.

    It is not a fifth tier -- it is an orthogonal flag layered on top of one, so
    it cannot share the tier ramp. Only five items carry it, all demolition gear:
    M79, Plastic_Explosive and RemoteDetonator spawn ONLY here; ClaymoreMine and
    Ammo_40mm_Explosive also accept Tier3/4.
    """
    grid, _usage, value = area
    n = grid // down
    px = bytearray(n * n * 4)
    hit = 0
    for oy in range(n):
        z0 = (n - 1 - oy) * down
        for ox in range(n):
            bits = 0
            for r in range(z0, z0 + down):
                base = r * grid + ox * down
                for c in range(down):
                    bits |= value[base + c]
            if bits & 0x10:
                i = (oy * n + ox) * 4
                px[i:i+4] = bytes((235, 90, 220, 175))
                hit += 1
    write_png(path, n, n, px)
    return hit


def render_tier_png(path: Path, area, down: int = 4) -> int:
    """Downsample the 4096^2 value plane into an RGBA overlay.

    Each output pixel takes the HIGHEST tier bit present in its block -- a Tier 4
    pocket inside a Tier 3 region is the interesting part and must not be averaged
    away. Rows are emitted north-first, since PNG row 0 is the top of the image
    while grid row 0 is z=0 (south).
    """
    grid, _usage, value = area
    n = grid // down
    px = bytearray(n * n * 4)
    for oy in range(n):
        z0 = (n - 1 - oy) * down          # flip: image top = high z = north
        for ox in range(n):
            bits = 0
            for r in range(z0, z0 + down):
                base = r * grid + ox * down
                for c in range(down):
                    bits |= value[base + c]
            i = (oy * n + ox) * 4
            top = bits & 0x0F
            if top:
                r, g, b = TIER_RGB[top.bit_length() - 1]
                px[i:i+4] = bytes((r, g, b, 150))
    write_png(path, n, n, px)
    return n


# Usage zones worth drawing, with a distinct colour each. Town (bit 7) is absent
# from the Chernarus area map entirely and Village covers 156 cells, so both come
# from the building prototype rather than the terrain -- not rendered.
USAGE_COLOUR = {
    "Military":         (200,  70,  60),
    "Industrial":       (150, 130, 200),
    "Coast":            ( 70, 150, 200),
    "Hunting":          (110, 175,  90),
    "Farm":             (200, 170,  70),
    "Medic":            (230, 120, 160),
    "Police":           ( 80, 110, 210),
    "Firefighter":      (225, 145,  60),
    "ContaminatedArea": (140, 200,  60),
    "Historical":       (170, 150, 120),
    "Lunapark":         (220, 100, 200),
}


def render_usage_pngs(outdir: Path, area, usage_names: list[str], down: int = 4) -> dict:
    """One RGBA overlay per usage zone, from the areaflags usage plane."""
    grid, usage, _value = area
    n = grid // down
    made = {}
    for name, (r, g, b) in sorted(USAGE_COLOUR.items()):
        if name not in usage_names:
            continue
        bit = 1 << usage_names.index(name)
        px = bytearray(n * n * 4)
        hit = 0
        for oy in range(n):
            z0 = (n - 1 - oy) * down           # image top = north
            for ox in range(n):
                found = False
                for rr in range(z0, z0 + down):
                    base = (rr * grid + ox * down) * 4
                    for cc in range(down):
                        o = base + cc * 4
                        if int.from_bytes(usage[o:o+4], "little") & bit:
                            found = True
                            break
                    if found:
                        break
                if found:
                    i = (oy * n + ox) * 4
                    px[i:i+4] = bytes((r, g, b, 165))
                    hit += 1
        if hit:
            write_png(outdir / f"usage_{name}.png", n, n, px)
            made[name] = {"rgb": [r, g, b], "cells": hit}
    return made


# Landmarks, derived from building class names -- the only place the data says
# what a building actually IS. Order matters: first match wins, so specific
# patterns (Mil_FireStation) must precede general ones (Mil_).
LANDMARKS = [
    ("church",      "Church",        "\u26ea", "#c9b26a", r"Church|Chapel"),
    ("firestation", "Fire station",  "\U0001f692", "#e08a3c", r"FireStation"),
    ("hospital",    "Hospital",      "\u2695",  "#e07a9a", r"City_Hospital"),
    ("police",      "Police",        "\U0001f6a8", "#6a86d6", r"PoliceStation"),
    ("school",      "School",        "\U0001f393", "#9a8ad6", r"City_School"),
    ("prison",      "Prison",        "\U0001f512", "#9aa088", r"Prison"),
    ("hangar",      "Hangar",        "\u2708",  "#8ab4d0", r"Hangar"),
    ("fuel",        "Fuel station",  "\u26fd", "#d6b44a", r"FuelStation"),
    ("factory",     "Factory",       "\U0001f3ed", "#a98ad6", r"Factory"),
    ("military",    "Military",      "\u2b50", "#d2694a", r"^Land_Mil_|Barracks|Airfield"),
    ("medtent",     "Medical tent",  "\u2695",  "#e07a9a", r"Medical_Tent"),
    ("deerstand",   "Deer stand",    "\U0001f98c", "#8fae6a", r"DeerStand"),
    ("barn",        "Barn",          "\U0001f33e", "#c2a15a", r"Barn"),
    ("boat",        "Boat / dock",   "\u26f5", "#6aaed6", r"Boat_Small|Boathouse"),
    ("watchtower",  "Watchtower",    "\U0001f5fc", "#a0a888", r"Tower_TC|GuardTower|Watchtower"),
]


def parse_effect_areas(data: bytes) -> list[dict]:
    """cfgEffectArea.json -> permanent contaminated zones with exact centre+radius.

    Preferred over the ContaminatedArea bit in areaflags: this is the authoritative
    trigger definition, so we can draw the true radius instead of a 3.75m raster
    approximation. Pos is [x, y, z] with y = altitude.
    """
    out = []
    for a in json.loads(data).get("Areas", []):
        d = a.get("Data", {})
        pos = d.get("Pos")
        if not pos or len(pos) != 3:
            continue
        out.append({
            "name": a.get("AreaName", "?"),
            "type": a.get("Type", ""),
            "x": round(pos[0]), "z": round(pos[2]),
            "r": round(d.get("Radius", 0)),
            "outer": round(d.get("Radius", 0)) + round(d.get("OuterOffset", 0)),
        })
    out.sort(key=lambda a: (a["x"], a["z"]))
    return out


# How each event group is presented. Anything not listed is skipped: the file
# carries loot-pile and decoration groups that are not worth a map layer.
EVENT_KINDS = {
    "StaticHeliCrash":        ("Heli crash",      "\U0001f681", "#d2694a", "event"),
    "StaticContaminatedArea": ("Toxic (dynamic)", "\u2623",     "#8fd633", "event"),
    "StaticMilitaryConvoy":   ("Military convoy", "\U0001f69b", "#c98a4a", "event"),
    "StaticPoliceSituation":  ("Police incident", "\U0001f693", "#6a86d6", "event"),
    "StaticAirplaneCrate":    ("Airdrop crate",   "\U0001f4e6", "#c9b26a", "event"),
    "StaticTrain":            ("Train",           "\U0001f686", "#9aa088", "event"),
    "StaticPoliceCar":        ("Police car",      "\U0001f6a8", "#5c6fa8", "event"),
    "StaticBonfire":          ("Bonfire",         "\U0001f525", "#c9773a", "event"),
    "VehicleTruck01":         ("Truck",           "\U0001f69a", "#8fae6a", "vehicle"),
    "VehicleOffroadHatchback":("Offroad hatch",   "\U0001f699", "#8fae6a", "vehicle"),
    "VehicleOffroad02":       ("Offroad 4x4",     "\U0001f699", "#8fae6a", "vehicle"),
    "VehicleSedan02":         ("Sedan",           "\U0001f697", "#8fae6a", "vehicle"),
    "VehicleHatchback02":     ("Hatchback",       "\U0001f697", "#8fae6a", "vehicle"),
    "VehicleCivilianSedan":   ("Civilian sedan",  "\U0001f697", "#8fae6a", "vehicle"),
    "VehicleBoat":            ("Boat",            "\U0001f6a4", "#6aaed6", "vehicle"),
}

SPAWN_KINDS = {
    "fresh":  ("Fresh spawn",  "\U0001f7e2", "#6ac46a"),
    "hop":    ("Server hop",   "\U0001f535", "#6a9cd6"),
    "travel": ("Travel spawn", "\U0001f7e1", "#d6c04a"),
}


def parse_events(spawns: bytes, events: bytes) -> list[dict]:
    """cfgeventspawns.xml + db/events.xml -> candidate positions per event.

    `nominal` is how many are live at once; the spawn file lists every position
    one COULD occupy. Both matter: 3 heli crashes rotate among 95 sites, so a
    marker means "sometimes here", never "here now". `active=0` groups are
    disabled on this mission and are kept, clearly flagged, rather than dropped.
    """
    meta = {}
    for e in ET.fromstring(events).findall("event"):
        meta[e.get("name")] = (int(e.findtext("nominal") or 0),
                               (e.findtext("active") or "0") == "1")
    out = []
    for e in ET.fromstring(spawns).findall("event"):
        name = e.get("name")
        if name not in EVENT_KINDS:
            continue
        label, glyph, colour, group = EVENT_KINDS[name]
        pts = []
        for p in e.findall("pos"):
            try:
                pts.append([round(float(p.get("x"))), round(float(p.get("z")))])
            except (TypeError, ValueError):
                continue
        if not pts:
            continue
        nominal, active = meta.get(name, (0, False))
        pts.sort()
        out.append({"id": name, "label": label, "glyph": glyph, "colour": colour,
                    "group": group, "nominal": nominal, "active": active,
                    "points": pts})
    out.sort(key=lambda e: (e["group"], e["label"]))
    return out


def parse_player_spawns(data: bytes) -> list[dict]:
    """cfgplayerspawnpoints.xml -> fresh / hop / travel spawn positions."""
    root = ET.fromstring(data)
    out = []
    for tag, (label, glyph, colour) in SPAWN_KINDS.items():
        grp = root.find(tag)
        if grp is None:
            continue
        pts = []
        for p in grp.iter("pos"):
            try:
                pts.append([round(float(p.get("x"))), round(float(p.get("z")))])
            except (TypeError, ValueError):
                continue
        if pts:
            pts.sort()
            out.append({"id": tag, "label": label, "glyph": glyph, "colour": colour,
                        "points": pts})
    return out


def build_landmarks(names: list[str], rows: list[list[int]]) -> dict:
    pats = [(k, lbl, glyph, col, re.compile(rx, re.I)) for k, lbl, glyph, col, rx in LANDMARKS]
    kinds, pts = [], []
    idx = {}
    for k, lbl, glyph, col, _ in pats:
        idx[k] = len(kinds)
        kinds.append({"id": k, "label": lbl, "glyph": glyph, "colour": col, "n": 0})
    for ti, x, z, _tier, _use in rows:
        b = names[ti]
        for k, _lbl, _g, _c, rx in pats:
            if rx.search(b):
                pts.append([idx[k], x, z])
                kinds[idx[k]]["n"] += 1
                break
    pts.sort()
    return {"kinds": kinds, "points": pts}


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
    tier_px = render_tier_png(OUT / "tiers.png", area)
    print(f"  docs/data/tiers.png  {(OUT/'tiers.png').stat().st_size:,}b  ({tier_px}x{tier_px})")
    uq = render_unique_png(OUT / "unique.png", area)
    print(f"  docs/data/unique.png  {(OUT/'unique.png').stat().st_size:,}b  ({uq} cells)")
    zones = render_usage_pngs(OUT, area, limits["usage"])
    print(f"  docs/data/usage_*.png  {len(zones)} zone overlays")
    landmarks = build_landmarks(names, rows)
    write_json(OUT / "landmarks.json", landmarks)
    toxic = parse_effect_areas((ce / "cfgEffectArea.json").read_bytes())
    write_json(OUT / "toxic.json", toxic)
    events = parse_events((ce / "cfgeventspawns.xml").read_bytes(),
                          (ce / "db" / "events.xml").read_bytes())
    write_json(OUT / "events.json", events)
    pspawn = parse_player_spawns((ce / "cfgplayerspawnpoints.xml").read_bytes())
    write_json(OUT / "spawns.json", pspawn)
    write_json(OUT / "recipes.json", recipes)
    write_json(OUT / "meta.json", {
        "map": "chernarusplus",
        "mapSize": MAP_SIZE,
        "dayzBuild": lock.get("dayz_build"),
        "commits": {k: v["commit"] for k, v in sorted(lock["sources"].items())},
        "counts": {**counts, "recipes": len(recipes)},
        "areaGrid": area[0],
        "zones": zones,
        "toxic": len(toxic),
        "uniqueCells": uq,
        "events": sum(len(e["points"]) for e in events),
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
