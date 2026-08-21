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
    """mapgroupproto.xml -> {building: {usg[], cont:[{cat[],tag[],n,eff}]}}

    `n` is the raw loot-point count. `eff` is how many of those points actually
    hold loot at once, and it is the number that matters.

    A point is a *place* an item can sit; `lootmax` caps how many are occupied.
    85% of containers declare a lootmax BELOW their point count -- a shed with 5
    points and lootmax=2 never holds more than 2 items -- so ranking on raw
    points overstates real loot, badly and unevenly. The group-level lootmax caps
    the whole building across its containers, applied here proportionally.

    Defaults come from <defaults> in the file itself (group 6, container 4)
    rather than being hardcoded, so an upstream change to them flows through.
    """
    root = ET.fromstring(data)
    dflt = {d.get("name"): int(d.get("lootmax"))
            for d in root.findall("./defaults/default") if d.get("lootmax")}
    g_default = dflt.get("group", 6)
    c_default = dflt.get("container", 4)

    out = {}
    for g in root.findall("group"):
        conts, caps = [], []
        for c in g.findall("container"):
            n = len(c.findall("point"))
            if not n:
                continue
            cap = min(n, int(c.get("lootmax") or c_default))
            caps.append(cap)
            conts.append({
                "cat": sorted({x.get("name") for x in c.findall("category")}),
                "tag": sorted({x.get("name") for x in c.findall("tag")}),
                "n": n,
            })
        if not conts:
            continue
        glm = int(g.get("lootmax") or g_default)
        total = sum(caps)
        scale = min(1.0, glm / total) if total else 1.0
        for c, cap in zip(conts, caps):
            c["eff"] = round(cap * scale, 3)
        out[g.get("name")] = {
            "usg": sorted({u.get("name") for u in g.findall("usage")}),
            "lootmax": glm,
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


# Landmark rules now live in config/landmarks.json -- see resolve_landmarks().


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


RE_CONST = re.compile(r"static\s+const\s+float\s+(\w+)\s*=\s*([\d.]+)")


def parse_player_constants(text: str) -> dict:
    """playerconstants.c -> the metabolism numbers a loot run actually costs.

    Energy and water both drain per second at a rate set by pace, out of 5000
    max. Published in Bohemia's own script source, so these are the real values
    the server uses rather than community estimates.
    """
    got = {k: float(v) for k, v in RE_CONST.findall(re.sub(r"//.*", "", text))}
    want = {
        "energyMax": "SL_ENERGY_MAX", "waterMax": "SL_WATER_MAX",
        "energyLow": "SL_ENERGY_LOW", "waterLow": "SL_WATER_LOW",
        "eBasal": "METABOLIC_SPEED_ENERGY_BASAL", "eWalk": "METABOLIC_SPEED_ENERGY_WALK",
        "eJog": "METABOLIC_SPEED_ENERGY_JOG", "eSprint": "METABOLIC_SPEED_ENERGY_SPRINT",
        "wBasal": "METABOLIC_SPEED_WATER_BASAL", "wWalk": "METABOLIC_SPEED_WATER_WALK",
        "wJog": "METABOLIC_SPEED_WATER_JOG", "wSprint": "METABOLIC_SPEED_WATER_SPRINT",
    }
    out = {k: got[v] for k, v in want.items() if v in got}
    missing = [v for k, v in want.items() if v not in got]
    if missing:
        raise ValueError(f"playerconstants.c missing {missing}")
    return out


def resolve_profiles(cfg: dict, items: dict) -> dict:
    """config/loot-profiles.json -> concrete item sets, validated against reality.

    The point of validating here: a selector that matches nothing is a build
    failure, not an empty set. When the upstream system renames or removes an
    item, the profile that referenced it breaks loudly at build time instead of
    silently becoming a no-op that quietly degrades every plan.

    That is the whole reason preferences live in reviewable data with a validator
    rather than in code -- code would just have the stale name too, without the
    check.
    """
    live = {n for n, it in items.items() if it["nom"] > 0}
    out, problems = [], []

    for prof in cfg["profiles"]:
        inc = prof.get("include", {})
        got: set[str] = set()

        for n in inc.get("names", []):
            if n in live:
                got.add(n)
            elif n in items:
                problems.append(f"{prof['id']}: '{n}' exists but has nominal 0")
            else:
                problems.append(f"{prof['id']}: no such item '{n}'")

        for pat in inc.get("patterns", []):
            rx = re.compile(pat, re.I)
            hit = {n for n in live if rx.search(n)}
            if not hit:
                problems.append(f"{prof['id']}: pattern '{pat}' matched nothing")
            got |= hit

        for cat in inc.get("categories", []):
            hit = {n for n in live if items[n]["cat"] == cat}
            if not hit:
                problems.append(f"{prof['id']}: category '{cat}' matched nothing")
            got |= hit

        for usg in inc.get("usages", []):
            hit = {n for n in live if usg in items[n]["usg"]}
            if not hit:
                problems.append(f"{prof['id']}: usage '{usg}' matched nothing")
            got |= hit

        got -= set(prof.get("exclude", {}).get("names", []))
        if not got:
            problems.append(f"{prof['id']}: resolved to zero items")

        out.append({"id": prof["id"], "label": prof["label"],
                    "note": prof.get("note", ""), "items": sorted(got)})

    if problems:
        raise ValueError("loot-profiles.json is stale:\n  " + "\n  ".join(problems))

    return {"profiles": out, "avoidableUsages": cfg.get("avoidableUsages", [])}


def resolve_places(cfg: dict, rows: list[list[int]]) -> list[dict]:
    """config/places.json -> validated place labels.

    Names are the one thing in this project that cannot come from the data -- the
    CLE carries coordinates and class names, never a place name, and sign text
    lives in terrain PBOs Bohemia does not publish. So the names are human
    knowledge and the Cyrillic is a transliteration, both fallible.

    What we CAN check is the coordinates: a label with no buildings near it is a
    typo, and a typo here mislabels the map. Fail the build instead.
    """
    rule = cfg.get("minBuildingsWithin", {})
    radius, need = rule.get("radius", 400), rule.get("count", 4)
    r2 = radius * radius
    out, bad = [], []

    for pl in cfg["places"]:
        x, z = pl["x"], pl["z"]
        n = sum(1 for _ti, bx, bz, _t, _u in rows
                if (bx - x) ** 2 + (bz - z) ** 2 <= r2)
        if n < need:
            bad.append(f"{pl['name']} ({x},{z}): only {n} buildings within {radius}m")
            continue
        out.append({"name": pl["name"], "cyrillic": pl.get("cyrillic", ""),
                    "x": x, "z": z, "n": n,
                    "unverified": bool(pl.get("unverified"))})

    if bad:
        raise ValueError("places.json coordinates look wrong:\n  " + "\n  ".join(bad))
    out.sort(key=lambda p: p["name"])
    return out


# Species we surface, and whether they are a threat or a meal. Wolves and bears
# change how you route; hens do not.
SPECIES = {
    "wolf":       ("Wolves",     "\U0001f43a", "err",  True),
    "bear":       ("Bears",      "\U0001f43b", "err",  True),
    "zombie":     ("Infected",   "\U0001f9df", "warn", True),
    "wild_boar":  ("Wild boar",  "\U0001f417", "ok",   False),
    "red_deer":   ("Red deer",   "\U0001f98c", "ok",   False),
    "roe_deer":   ("Roe deer",   "\U0001f98c", "ok",   False),
    "cattle":     ("Cattle",     "\U0001f404", "ok",   False),
    "sheep_goat": ("Sheep/goat", "\U0001f411", "ok",   False),
    "pig":        ("Pigs",       "\U0001f416", "ok",   False),
    "hen":        ("Hens",       "\U0001f414", "ok",   False),
    "hare":       ("Hare",       "\U0001f407", "ok",   False),
    "fox":        ("Fox",        "\U0001f98a", "info", False),
}


def parse_territories(cache_dir: Path) -> list[dict]:
    """env/*_territories.xml -> animal zones.

    Each territory is a set of circular zones: Water, Rest and HuntingGround.
    HuntingGround is where the animal actually roams and is the one that matters
    for routing -- wolves and bears are a reason to go around, deer are a reason
    to go through.
    """
    out = []
    for key, (label, glyph, tone, hazard) in SPECIES.items():
        f = cache_dir / "env" / f"{key}_territories.xml"
        if not f.is_file():
            continue
        zones = []
        for terr in ET.fromstring(f.read_bytes()).findall("territory"):
            for z in terr.findall("zone"):
                try:
                    zones.append([round(float(z.get("x"))), round(float(z.get("z"))),
                                  round(float(z.get("r"))), z.get("name", "")])
                except (TypeError, ValueError):
                    continue
        if not zones:
            continue
        zones.sort()
        out.append({"id": key, "label": label, "glyph": glyph, "tone": tone,
                    "hazard": hazard, "zones": zones,
                    "roam": sum(1 for z in zones if z[3] == "HuntingGround")})
    out.sort(key=lambda a: (not a["hazard"], a["label"]))
    return out


def resolve_landmarks(cfg: dict, names: list[str], rows: list[list[int]]) -> tuple[dict, list]:
    """config/landmarks.json -> markers, plus a per-rule provenance record.

    Returns (layer data, trace). The trace names every class each rule matched and
    how many instances it produced, so the question "where did this marker come
    from" has a written answer rather than requiring someone to re-read a regex.

    A rule matching nothing fails the build unless explicitly `optional`. That is
    not hypothetical: a `Hunting|Chalet|Cabin` rule shipped here matching zero
    buildings, and a silent empty layer looks exactly like a deliberate one.
    """
    pats = [(r, re.compile(r["pattern"], re.I)) for r in cfg["rules"]]
    kinds, pts, trace = [], [], []
    idx, matched = {}, {r["id"]: set() for r in cfg["rules"]}

    for r, _ in pats:
        idx[r["id"]] = len(kinds)
        kinds.append({"id": r["id"], "label": r["label"], "glyph": r["glyph"],
                      "tone": r.get("tone", "info"), "n": 0})

    for ti, x, z, _tier, _use in rows:
        b = names[ti]
        for r, rx in pats:                       # first match wins; order is meaningful
            if rx.search(b):
                pts.append([idx[r["id"]], x, z])
                kinds[idx[r["id"]]]["n"] += 1
                matched[r["id"]].add(b)
                break

    dead = [r["id"] for r, _ in pats
            if not matched[r["id"]] and not r.get("optional")]
    if dead:
        raise ValueError("landmarks.json rules matched nothing: " + ", ".join(dead))

    for r, _ in pats:
        trace.append({"rule": r["id"], "pattern": r["pattern"],
                      "classes": sorted(matched[r["id"]]),
                      "instances": kinds[idx[r["id"]]]["n"]})
    pts.sort()
    return {"kinds": kinds, "points": pts}, trace


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
    profiles = resolve_profiles(
        json.loads((ROOT / "config" / "loot-profiles.json").read_text()), items)
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
    write_json(OUT / "profiles.json", profiles)
    tier_px = render_tier_png(OUT / "tiers.png", area)
    print(f"  docs/data/tiers.png  {(OUT/'tiers.png').stat().st_size:,}b  ({tier_px}x{tier_px})")
    uq = render_unique_png(OUT / "unique.png", area)
    print(f"  docs/data/unique.png  {(OUT/'unique.png').stat().st_size:,}b  ({uq} cells)")
    zones = render_usage_pngs(OUT, area, limits["usage"])
    print(f"  docs/data/usage_*.png  {len(zones)} zone overlays")
    landmarks, lm_trace = resolve_landmarks(
        json.loads((ROOT / "config" / "landmarks.json").read_text()), names, rows)
    write_json(OUT / "landmarks.json", landmarks)
    animals = parse_territories(ce)
    write_json(OUT / "animals.json", animals)
    places = resolve_places(
        json.loads((ROOT / "config" / "places.json").read_text()), rows)
    write_json(OUT / "places.json", places)
    toxic = parse_effect_areas((ce / "cfgEffectArea.json").read_bytes())
    write_json(OUT / "toxic.json", toxic)
    events = parse_events((ce / "cfgeventspawns.xml").read_bytes(),
                          (ce / "db" / "events.xml").read_bytes())
    write_json(OUT / "events.json", events)
    pspawn = parse_player_spawns((ce / "cfgplayerspawnpoints.xml").read_bytes())
    write_json(OUT / "spawns.json", pspawn)
    pconst = parse_player_constants(
        (CACHE / "script-diff" / "scripts/3_game/playerconstants.c").read_text(errors="ignore"))
    write_json(OUT / "player.json", pconst)
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

    # --- provenance -----------------------------------------------------------
    # "Where did this list come from" must have a written answer. For every derived
    # artifact: which source files (by sha256 at a pinned commit), which config
    # authored the rules, and what each rule actually matched.
    prov = {
        "generator": "tools/build_index.py",
        "dayzBuild": lock.get("dayz_build"),
        "sources": {
            name: {
                "repo": src["repo"],
                "commit": src["commit"],
                "files": {p: {"sha256": m.get("sha256"), "bytes": m.get("bytes")}
                          for p, m in sorted(src["files"].items())},
            } for name, src in sorted(lock["sources"].items())
        },
        "artifacts": [
            {"file": "items.json", "from": ["db/types.xml"], "config": None,
             "count": counts["items"]},
            {"file": "groups.json", "from": ["mapgroupproto.xml"], "config": None,
             "count": counts["groups"],
             "note": "eff = min(points, container lootmax), scaled by group lootmax"},
            {"file": "instances.json",
             "from": ["mapgrouppos.xml", "areaflags.map", "cfglimitsdefinition.xml"],
             "config": None, "count": counts["instances"],
             "note": "tier/usage flags resolved per building from areaflags.map"},
            {"file": "landmarks.json", "from": ["mapgrouppos.xml"],
             "config": "config/landmarks.json", "rules": lm_trace,
             "count": len(landmarks["points"])},
            {"file": "places.json", "from": ["mapgrouppos.xml"],
             "config": "config/places.json", "count": len(places),
             "note": "coordinates validated against building density; names are human input"},
            {"file": "profiles.json", "from": ["db/types.xml"],
             "config": "config/loot-profiles.json", "count": len(profiles["profiles"]),
             "rules": [{"rule": p["id"], "items": len(p["items"])}
                       for p in profiles["profiles"]]},
            {"file": "animals.json",
             "from": [f"env/{k}_territories.xml" for k in sorted(SPECIES)],
             "config": None, "count": sum(len(a["zones"]) for a in animals)},
            {"file": "events.json", "from": ["cfgeventspawns.xml", "db/events.xml"],
             "config": None, "count": sum(len(e["points"]) for e in events)},
            {"file": "spawns.json", "from": ["cfgplayerspawnpoints.xml"],
             "config": None, "count": sum(len(s["points"]) for s in pspawn)},
            {"file": "toxic.json", "from": ["cfgEffectArea.json"], "config": None,
             "count": len(toxic)},
            {"file": "recipes.json",
             "from": ["scripts/4_world/classes/recipes/recipes/*.c"], "config": None,
             "count": len(recipes)},
            {"file": "player.json", "from": ["scripts/3_game/playerconstants.c"],
             "config": None, "count": len(pconst)},
            {"file": "tiers.png / unique.png / usage_*.png", "from": ["areaflags.map"],
             "config": None, "count": 1 + 1 + len(zones),
             "note": "4096^2 planes downsampled 4x; highest tier bit per block"},
        ],
    }
    write_json(OUT / "provenance.json", prov)

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
