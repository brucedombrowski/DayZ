# DayZ Loot Finder

A browser map that answers one question: **"I need item X and I'm at position Y — where do I go?"**

You pick one or more items, drop your current position on the map, and it shows the real
spawn points for those items, ranked by how worthwhile the trip is.

This is not a hand-curated loot table. It is derived from the game's actual **Central Loot
Economy (CLE)** configuration, so the answers reflect what the server is really doing.

## Two features, one data layer

| | |
|---|---|
| **Loot map** (this doc) | "I need item X, I'm at Y — where do I go?" |
| **[Crafting guide](CRAFTING.md)** | "How do I make X?" — a hierarchical tree that recurses until every leaf is loot, then hands those leaves to the loot map. |

They are separate tools. They share the parsers, the item index, and the data-sync pipeline.

**Target environment: PS5 Official DayZ (vanilla Chernarus+, later Livonia).**
Official console servers run Bohemia's stock mission config unmodified, which means the
public [DayZ-Central-Economy](https://github.com/BohemiaInteractive/DayZ-Central-Economy)
repo is ground truth for our target — not an approximation.

---

## The driving use case

> "I need a sledgehammer and I'm in the SW part of the map. Where do I go?"

Here is the chain the app has to walk, using the real data:

**1. Look up the item in `db/types.xml`:**

```xml
<type name="SledgeHammer">
    <nominal>20</nominal>  <min>10</min>  <lifetime>14400</lifetime>
    <category name="tools"/>
    <tag name="floor"/>
    <usage name="Industrial"/>
    <value name="Tier3"/>
    <value name="Tier4"/>
</type>
```

That tells us a sledgehammer spawns on **floor** points, in **tools** containers, inside
**Industrial** buildings, but only in **Tier 3 / Tier 4** map areas.

**2. Find building types that can host it, in `mapgroupproto.xml`:**

```xml
<group name="Land_Shed_M1" lootmax="2">
    <usage name="Industrial" />
    <usage name="Farm" />
    <container name="lootFloor" lootmax="2">
        <category name="tools" />
        <category name="containers" />
        <tag name="ground" />  <tag name="floor" />
        <point pos="0.504883 -1.174019 0.807373" range="0.587867" height="1.469666" flags="32" />
        ...
    </container>
</group>
```

Match on `usage` ∩ `category` ∩ `tag`. **134 of Chernarus' building types qualify** for a
sledgehammer.

**3. Find every instance of those buildings in `mapgrouppos.xml`:**

```xml
<group name="Land_Shed_M1" pos="80.262581 113.793480 4422.178223" rpy="..." a="160.01" />
```

11,679 building instances on Chernarus; **4,821 of them can host a sledgehammer**, totalling
**32,624 candidate loot points**.

**4. Rank and filter** by tier, by distance from the player, and by point density.

### Prototype result (already verified)

Clustering those points at 300 m and filtering to the SW quadrant gives:

| Rank | Loot points | Buildings | Position (x, z) | Likely location |
|-----:|------------:|----------:|-----------------|-----------------|
| 1 | 356 | 48 | 2578, 5229 | Zelenogorsk industrial |
| 2 | 352 | 48 | 6416, 2550 | Chernogorsk industrial |
| 3 | 224 | 24 | 7082, 2826 | E of Chernogorsk |

**But note the catch, and it is the whole reason step 4 matters:** the sledgehammer is
`Tier3`/`Tier4`, and southwest coastal Chernarus is largely Tier 1–2. Those top-ranked
clusters may be *structurally* valid and still spawn almost nothing. **A tool that skips
tier gating will confidently send you to the wrong town.** See
[Open question 1](#1-tier-gating-areaflagsmap).

---

## How it works

```
  types.xml            mapgroupproto.xml         mapgrouppos.xml         areaflags.map
  ─────────            ─────────────────         ───────────────         ─────────────
  item  ──►  usage/category/tag/tier
                  │
                  └──► building types  ──►  world instances  ──►  tier filter  ──►  ranked map
                       (134)                 (4,821)               (Tier 3–4)
```

Then rotate each prototype `point` by the instance's yaw (`a` / `rpy`) and add it to the
instance origin to get true world coordinates for individual loot spots.

---

## Data sources

Everything below is public. **We do not vendor game data into this repo** — it is fetched
on demand and cached locally. That keeps us honest about licensing and means an update to
the official config flows through without a code change.

### Primary — game configuration

| Source | What we use it for |
|---|---|
| [BohemiaInteractive/DayZ-Central-Economy](https://github.com/BohemiaInteractive/DayZ-Central-Economy) | The whole pipeline. Official mission config for Chernarus+ (`dayzOffline.chernarusplus`) and Livonia (`dayzOffline.enoch`). |
| ├ `db/types.xml` (880 KB) | Per-item nominal/min/lifetime, `category`, `tag`, `usage`, `value` (tier). |
| ├ `mapgroupproto.xml` (1.2 MB) | Building prototypes: containers, their category/tag filters, and every loot `point` with local offset, range, height. |
| ├ `mapgrouppos.xml` (1.5 MB) | World placement of all 11,679 building instances: `pos`, `rpy`, `a`. |
| ├ `areaflags.map` (83 MB) | Per-cell tier / usage zones. 4096×4096 grid. See open question 1. |
| ├ `cfgspawnabletypes.xml` | Cargo & attachment spawns — items that appear *inside* other items. |
| ├ `cfgrandompresets.xml` | Named loot presets referenced by the above. |
| └ `cfgeventspawns.xml`, `db/events.xml` | Dynamic events (heli crashes, police cars, convoys) — a second, non-building spawn channel. |
| [BohemiaInteractive/DayZ-Script-Diff](https://github.com/BohemiaInteractive/DayZ-Script-Diff) | Official Enforce Script source, updated per patch. |
| └ `scripts/4_world/classes/recipes/recipes/*.c` | 224 crafting recipes — see [CRAFTING.md](CRAFTING.md). |

### Reference — documentation

| Source | Notes |
|---|---|
| [DayZ: Central Economy setup for custom terrains](https://community.bistudio.com/wiki/DayZ:Central_Economy_setup_for_custom_terrains) (Bohemia Wiki) | Authoritative description of `mapgroupproto` / `mapgrouppos` semantics. |
| [DayZ: Central Economy](https://community.bistudio.com/wiki/DayZ:Central_Economy) (Bohemia Wiki) | Tier model (Tier1 green → Tier4 red), usage tags, nominal/min restock behaviour. |
| [DayZ Modding Docs — types.xml](https://community.bistudio.com/wiki/DayZ:Central_Economy_Mission_Files) | Field-by-field reference for the economy XML files. |

### Map display

| Source | Status |
|---|---|
| [iZurvive](https://www.izurvive.com/) | **Link out only — see open question 2.** Supports deep links via `#location=x;y;zoom`. Their [FAQ](https://www.izurvive.com/tutorial/) notes their display coordinates are not the in-game coordinate system. |
| [iv-mexx/izurvive-sdk](https://github.com/iv-mexx/izurvive-sdk) | Community SDK — worth reading for their coordinate transform, unofficial. |
| [Leaflet](https://leafletjs.com/) | Planned map engine, using `L.CRS.Simple` over a flat 15360×15360 game-coordinate space. |

### Data pipeline — deterministic by design

**Requirement: the same lockfile must always produce byte-identical output.** Bohemia
updates these files every patch, and this project is worthless if an upstream change
silently shifts our answers without anyone noticing. So upstream data is *pinned*, not
*followed*.

```
sources.lock.json  ──►  fetch  ──►  verify  ──►  parse  ──►  build/*.json
  repo + commit SHA      pinned     sha256      pure fns     derived index
  + sha256 per file      commit     per file    no I/O       (committed)
```

**`sources.lock.json`** pins every upstream file to an exact commit SHA plus a `sha256` of
its contents:

```jsonc
{
  "dayz_build": "1.28",              // console build this was validated against
  "sources": {
    "central-economy": {
      "repo": "BohemiaInteractive/DayZ-Central-Economy",
      "commit": "<sha>",
      "files": {
        "dayzOffline.chernarusplus/db/types.xml":   { "sha256": "…", "bytes": 880570 },
        "dayzOffline.chernarusplus/mapgroupproto.xml": { "sha256": "…", "bytes": 1229499 }
      }
    },
    "script-diff": { "repo": "BohemiaInteractive/DayZ-Script-Diff", "commit": "<sha>", "files": { /* … */ } }
  }
}
```

Three commands:

| Command | Does |
|---|---|
| `sync` | Fetch exactly the pinned commits, verify every `sha256`, fail loudly on mismatch. Never silently accepts different bytes. |
| `build` | Parse cache → derived JSON index. Pure function of the cache: no network, no clock, no `Date.now()`, stable key ordering. Re-running must yield an identical file. |
| `check-upstream` | Compare pins against upstream, classify the change by blast radius, and either auto-update or escalate. Runs on a schedule. |

Rules that make this hold:

- **Parsers are pure.** Input bytes → output structure. No I/O, no ambient state. This is
  what makes them testable and the output reproducible.
- **Derived output is committed**, so a data change shows up as a reviewable diff.
- **Golden tests** lock the numbers this README cites (134 sledgehammer building types,
  4,821 instances, 32,624 points; 224 recipe files, 133 craftable outputs).

### Auto-ingest — tiered by blast radius

A Bohemia commit should not break us and mostly should not need us. Requiring a human for
every `nominal` tweak means the tool is perpetually stale, which is its own failure. So the
bot classifies each change and only escalates what actually warrants it:

| Tier | What changed | Action |
|---|---|---|
| 🟢 **Green** | Values move within the existing schema — `nominal`, `min`, `lifetime`, `restock`, `cost`. Items added/removed. Building instances added/moved. All golden invariants hold within tolerance. | **Auto-update the lock, rebuild, auto-merge, deploy.** No human. |
| 🟡 **Yellow** | New enum member (`usage`, `category`, `tag`, `value`/tier). Recipe file added/removed. Building *type* added/removed. A golden number moves beyond tolerance. | **Open a PR** with the semantic diff. Site keeps serving the last good build. |
| 🔴 **Red** | Schema shape changed, parse failure, file missing, `areaflags.map` header differs, sanity bound violated. | **Fail closed.** Alert, no deploy, last good build stays up. |

Green is the common case and is genuinely safe because the schema is unchanged — we are
ingesting different *numbers*, not different *structure*. Yellow exists because a new tier
or usage tag can silently change what our filters match, which is exactly the class of
change that would send you to the wrong town without anyone noticing.

Determinism survives this: the bot changes the **lockfile**, and the build stays a pure
function of it. Any past build is reproducible from its lock. "Automatic" and
"deterministic" are not in tension — what would break determinism is building against a
moving `master`, which we never do.

### Security

We ingest third-party data automatically, so the trust boundary needs to be explicit.

- **We never execute upstream content.** Recipe `.c` files are *parsed as text* — pattern
  extraction only. Enforce Script is never compiled, `eval`'d, or run. This is the single
  most important property, and it is a design constraint, not a precaution.
- **XML is parsed with a hardened reader**: DTDs and external entities disabled (XXE),
  entity expansion capped (billion-laughs). Non-negotiable given files arrive unattended.
- **Resource bounds.** `areaflags.map` is 83 MB and the cluster files 4.6 MB each. Declared
  size ceilings per file; exceeding one is 🔴, not an OOM.
- **Provenance is pinned.** Fetch by commit SHA from the canonical `BohemiaInteractive/*`
  repos over HTTPS, record the SHA and per-file `sha256` of exactly what we ingested. If
  upstream history is rewritten, checksums mismatch and we fail closed.
- **Residual risk: upstream account compromise.** A malicious commit to Bohemia's own repo
  would pass checksums — they are integrity, not authenticity. Mitigation is that the blast
  radius is small: no execution, and the schema/invariant checks in 🟡/🔴 catch structural
  tampering. Worst realistic case is wrong loot markers, not code execution.
- **CI permissions are least-privilege**: the sync job gets a scoped token, cannot push to
  `main` directly on 🟡/🔴, and its output is a data file — never code.

### Licensing posture

- `DayZ-Central-Economy` carries **no SPDX license file**. We treat it as
  reference-only: fetch at runtime, cache locally, never redistribute in this repo.
- **iZurvive tiles are proprietary.** We will not proxy, scrape, or re-host them.
  Integration is a deep link that opens iZurvive at a computed coordinate.
- Nothing here touches the game client, game memory, or a live server. It reads published
  config files. It is a reference tool, not a cheat — no different in kind from reading the
  wiki.

---

## Coordinate systems

One recurring source of bugs, so it is written down once here:

- **Game world coords** are `x y z` where **`x` = easting, `y` = altitude, `z` = northing**.
  Chernarus+ is 15360 × 15360 m. In `mapgrouppos.xml`, `pos="80.26 113.79 4422.17"` means
  x=80, altitude=114, z=4422.
- **Loot `point pos`** in `mapgroupproto.xml` is a **local offset** from the building
  origin, in the same `x y z` order. It must be rotated by the instance's yaw before use.
- **Map/screen coords** typically want `(x, 15360 − z)` so north is up.
- **iZurvive coords** are their own projection — convert explicitly, never assume.

---

## Scope

### v1 — the sledgehammer question, answered correctly

- [ ] Fetch + parse `types.xml`, `mapgroupproto.xml`, `mapgrouppos.xml`
- [ ] Resolve the tier zones so results are not misleading (open question 1)
- [ ] Item search with autocomplete over real `types.xml` names
- [ ] Leaflet map, game-coordinate CRS, offline base layer
- [ ] Set "my position" by clicking the map or entering coordinates
- [ ] Ranked result list: cluster, distance from player, expected loot points
- [ ] Deep link out to iZurvive for the chosen destination

### v2

- [ ] Multi-item search ("sledgehammer **and** a hacksaw") with combined routing
- [ ] Individual loot points, rotated into world space, at high zoom
- [ ] Spawn *probability* weighting, not just point counts — `nominal`, `min`, and
      competition from every other item sharing that container
- [ ] Livonia (`dayzOffline.enoch`)
- [ ] Dynamic event loot (heli crashes, convoys)

### Later / maybe

- [ ] Route planning across multiple stops
- [ ] Community server support by uploading a custom `types.xml`
- [ ] Cargo/attachment spawns via `cfgspawnabletypes.xml`

### Explicitly out of scope

- Anything that reads game memory or talks to a live server
- Real-time player/loot positions
- Re-hosting iZurvive tiles or Bohemia data

---

## Open questions

These need answering before or during v1. They are the real risk in this project.

### 1. Tier gating (`areaflags.map`)

**This is the highest-value unknown.** Without it, results are actively misleading — see
the sledgehammer example above.

Progress so far — the 128-byte header reads as:

```
00000000: 0010 0000  0010 0000  003c 0000  003c 0000   →  4096, 4096, 60, 60
00000010: 2000 0000  0000 0000                         →  32, 0
```

So: a **4096 × 4096** grid over 15360 m = **3.75 m per cell**. File is 83,886,104 bytes;
`83886104 − 24 = 83886080 = 4096 × 4096 × 5` exactly, so the body is **5 bytes per cell**
after a 24-byte header. What those 5 bytes encode (tier bits? usage bitmask? layered
planes?) is not yet confirmed.

Options, in order of preference:
1. Finish decoding the 5-byte cell and read tiers directly. Highest fidelity.
2. Derive tier zones empirically by cross-referencing known tier-exclusive items against
   where their host buildings actually are.
3. Approximate with community-drawn tier polygons. Fastest, least trustworthy.

### 2. Map base layer

iZurvive tiles are off the table. So what renders underneath the markers?

- Self-generated tiles from the game's own terrain/satellite export
- A community-licensed tile set with compatible terms
- A minimal vector base (coastline, roads, town labels) drawn from map data

Undecided. v1 could ship with a plain grid + town labels and still be useful.

### 3. Ranking model

"32,624 candidate points" is not an answer, it is noise. What actually makes a destination
good? Proposed score, to be tuned against real play:

```
score = tier_match × Σ(loot_points) × density_bonus / travel_cost(distance, terrain)
```

Open: how to weight `nominal`/`min` (how many exist map-wide) and container competition
(how many *other* items share that same slot) into a real probability rather than a count.

### 4. Version drift

Resolved in approach — pinned lockfile + tiered auto-ingest, see
[Data pipeline](#data-pipeline--deterministic-by-design). Still open: **PS5 Official may lag
the PC build** that `DayZ-Script-Diff` and `DayZ-Central-Economy` track. We need to
establish the current console build number and pin to the matching tag, not to `master`.

---

## Stack (proposed, not locked)

- **Frontend:** static site — no backend required for v1
- **Map:** Leaflet with `L.CRS.Simple`
- **Data prep:** an offline build step that parses the source XML into a compact JSON/binary
  index the browser can load fast. Shipping 3 MB of XML to the client and parsing it there
  is the wrong move.
- **Hosting:** GitHub Pages

---

## Status

**Specification.** No implementation yet — this README is the plan, and the numbers in it
come from real queries against the real data, not estimates.
