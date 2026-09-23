# DayZ Loot Run Planner

### ▶ Live: **https://brucedombrowski.github.io/DayZ/**

A browser tool that plans a loot run: **"I'm here, my base is there, I need X, I have 45
minutes — where do I go, in what order, and what do I fill up with on the way home?"**

This is not a hand-curated loot table. It is derived from the game's actual **Central Loot
Economy (CLE)** configuration, so the answers reflect what the server is really doing.

## What it does

| | |
|---|---|
| **Run planner** | The point of the whole thing — see [Designing a good run](#designing-a-good-run). |
| **Event sweep** | "I want a heli crash or a convoy" — routes through the sites most likely to be live right now. See [Sweeping event sites](#sweeping-event-sites). |
| **Loot map** | "I need item X, I'm at Y — where do I go?" |
| **Catalog** | Browse 1,389 items by category, tier and usage, because nobody remembers `WaterproofBag_Orange`. |
| **Building lookup** | Click any building — everything that can spawn in it, gated by the tier it stands in. |
| **[Crafting guide](CRAFTING.md)** | "How do I make X?" — recurses to loot leaves, which hand off to the map. |

They share the parsers, the item index, and the data-sync pipeline.

### Loot points are not loot

A loot *point* is a place an item can sit; `lootmax` caps how many are occupied at once.
**85% of containers declare a lootmax below their point count** — a shed with 5 points and
`lootmax=2` never holds more than 2 items. Map-wide, only **24% of loot points hold loot**.

Ranking on raw points overstates real loot ~4x, and unevenly, so it reorders results. We
compute an effective slot count per container at build time (`eff`), capped again by the
group's own `lootmax`, and rank on that. For `SledgeHammer`: 32,624 raw points →
**11,491 effective**, or 3,919 after tier gating.

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

**And here is why step 4 matters.** The sledgehammer is `Tier3`/`Tier4`; southwest coastal
Chernarus is largely Tier 1–2. With tier gating now applied:

| Cluster | Ungated | Tier-gated |
|---|---:|---:|
| Zelenogorsk industrial | 356 | **356** |
| Chernogorsk industrial | 352 | **13** |

Structurally the two look like a coin flip. In reality Chernogorsk spawns almost no
sledgehammers. **Go to Zelenogorsk.** A tool without tier gating would have sent you to the
wrong town half the time.

---

## How it works

```
  types.xml            mapgroupproto.xml         mapgrouppos.xml         areaflags.map
  ─────────            ─────────────────         ───────────────         ─────────────
  item  ──►  usage/category/tag/tier
                  │
                  └──► building types  ──►  world instances  ──►  tier filter  ──►  ranked map
                       (134)                 (4,821)               (1,686)
```

Then rotate each prototype `point` by the instance's yaw (`a` / `rpy`) and add it to the
instance origin to get true world coordinates for individual loot spots.

---

## Map display

| Source | Status |
|---|---|
| [iZurvive](https://www.izurvive.com/) | **Link out only — see open question 2.** Supports deep links via `#location=x;y;zoom`. Their [FAQ](https://www.izurvive.com/tutorial/) notes their display coordinates are not the in-game coordinate system. |
| [iv-mexx/izurvive-sdk](https://github.com/iv-mexx/izurvive-sdk) | Community SDK — worth reading for their coordinate transform, unofficial. |
| [Leaflet](https://leafletjs.com/) | Planned map engine, using `L.CRS.Simple` over a flat 15360×15360 game-coordinate space. |

## Input sources — the deterministic inputs

Every derived artifact traces to a pinned file. Nothing is fetched at page load; the
browser only reads `docs/data/*`, which is generated from the table below and committed.

`sources.lock.json` pins each file to a commit SHA **and** a sha256 of its contents.
`docs/data/provenance.json` records, for every artifact, which sources produced it,
which config authored its rules, and what each rule matched.

### From `BohemiaInteractive/DayZ-Central-Economy` — `dayzOffline.chernarusplus/`

| File | Produces | What we take |
|---|---|---|
| `db/types.xml` | `items.json` | Per-item `nominal`, `min`, `category`, `tag`, `usage`, `value`, `count_in_*` flags |
| `mapgroupproto.xml` | `groups.json` | Building prototypes: containers, category/tag filters, loot points, **`lootmax`** |
| `mapgrouppos.xml` | `instances.json`, `landmarks.json`, `places.json` | World position of every loot-bearing building |
| `areaflags.map` | `instances.json`, `tiers.png`, `unique.png`, `usage_*.png` | Per-cell tier and usage zones (4096² × u32 usage, then 4096² × u8 value) |
| `cfglimitsdefinition.xml` | `limits.json` | The flag lists — **declaration order is bit order in `areaflags.map`** |
| `cfgEffectArea.json` | `toxic.json` | Permanent contaminated zones, true centre and radius |
| `cfgeventspawns.xml` + `db/events.xml` | `events.json` | Heli crashes, convoys, dynamic toxic, vehicles: candidate sites + how many are live |
| `cfgplayerspawnpoints.xml` | `spawns.json` | Fresh / hop / travel spawn points |
| `env/*_territories.xml` (12) | `animals.json` | Wolf, bear, infected, deer, boar and other territory zones |
| `cfgrandompresets.xml` + `cfgspawnabletypes.xml` | `cargo.json` | What spawns **inside** things — 298 spawning items carry contents |

### From `BohemiaInteractive/DayZ-Script-Diff`

| File | Produces | What we take |
|---|---|---|
| `scripts/4_world/classes/recipes/recipes/*.c` (224) | `recipes.json` | Crafting graph — **parsed as text, never executed** ([SECURITY.md](SECURITY.md)) |
| `scripts/3_game/playerconstants.c` | `player.json` | Energy/water drain per pace, and the 5000 maxima |

### From `brucedombrowski/Human-Factors-Engineering`

| File | Produces | What we take |
|---|---|---|
| `design-tokens/colors.css` | `docs/tokens/colors.css` | Colour tokens, dark + light, WCAG AA |
| `design-tokens/typography.css` | `docs/tokens/typography.css` | Type scale, weights, font stacks |

### Loot is not flat

298 items that spawn in buildings arrive **carrying something**: a `DryBag` has a 5.3%
chance of `Rope` inside, an `M4A1` brings roughly three attachments. Counting a spawned
container as one item understates the run, and unevenly:

| Profile | Yield understated by |
|---|---:|
| Gun & gear repair | **14.7%** |
| Food & water | 5.7% |
| Medical | 3.7% |
| Base building | 2.7% |
| Hunting, rags | <1% |

**Semantics, stated because it is an assumption.** Within one `<cargo>` or
`<attachments>` group the item chances are **cumulative and one item is drawn**, not
rolled independently. The tell is the `M4A1` magazine group — `0.15, 0.50, 0.70, 1.00`.
A trailing `1.00` is a guaranteed fallback for a walk-the-list draw; under independent
rolls it would mean "always spawns every magazine size at once". So a group contributes
its own `chance` in expected items, split across its entries by cumulative share.

### Deliberately not used

`mapgroupcluster*.xml` (23 MB of fruit trees, berry bushes and stone piles — a real
foraging layer), `cfgweather.xml`, `db/globals.xml`, `cfgeventgroups.xml`,
`cfgIgnoreList.xml`, `mapclusterproto.xml`. Listed here so "unused" stays a decision
rather than an oversight.

## Gaps in the source data, and what we do instead

Where the DayZ files simply do not carry something, we say so and hand-author a config for
it. **Every such config is validated against the live data at build time**, and a selector
that matches nothing fails the build — which is what keeps bespoke input from rotting.

| Gap in DayZ data | Consequence | Our bespoke input | Validation |
|---|---|---|---|
| **No place names.** `mapgrouppos.xml` is coordinates and class names. Sign text lives in terrain PBOs. | Map cannot label anything | [`config/places.json`](config/places.json) — names + Cyrillic | Coordinates must have ≥4 buildings within 400 m. Caught Novaya Petrovka 1.2 km off. Names flagged `unverified` render dimmed. |
| **No landmark taxonomy.** A class name says `Land_Misc_DeerStand1`, not "navigation landmark". | No way to group markers | [`config/landmarks.json`](config/landmarks.json) — ordered match rules | Every rule must match ≥1 class. Caught a dead `Hunting\|Chalet\|Cabin` rule and `watchtower` being shadowed by `military`. |
| **No preference model.** CLE has categories, not intentions. Clothing's value is that you shred it for rags — a fact two crafting steps away. | Cannot express "come back full" | [`config/loot-profiles.json`](config/loot-profiles.json) | Every selector must resolve. Caught `Toolbox`, an item that does not exist. |
| **No movement speeds.** `human.c` declares `GetCurrentMovementSpeed()` as `proto native`; the values are in the engine. | Travel time unknowable | Calibratable min/km slider | Labelled as estimate, with reference distances |
| **No item sizes.** `itemSize` is in `config.cpp`, unpublished. | Cannot model real inventory | Free-slots slider | Labelled as estimate |
| **No display names or icons.** No `stringtable.csv` published. | Class names only | — | Open, [#11](https://github.com/brucedombrowski/DayZ/issues/11) |
| **No non-loot structures.** `mapgrouppos.xml` omits anything the CLE puts no loot in. | Tall radio masts, water towers absent | — | Documented scope limit in `config/landmarks.json` |

Run `python3 tools/derive_config.py audit` to check every config against current data.
It also **proposes** entries — `places` clusters buildings into candidate towns,
`landmarks` lists unclassified building families — but never writes. Code derives
structure; a human supplies the knowledge.

## Data pipeline — deterministic by design

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

### Designing a good run

A run has **two objectives that behave differently**, and conflating them produces bad routes.

**The target saturates.** You go out for one sledgehammer; a second is worth nothing. With
small independent per-slot odds, `P(none) ≈ exp(−λ)`, so what matters is
**`P(at least one) = 1 − exp(−λ)`** — 63% at λ=1, 86% at 2, 95% at 3. Maximising *expected
count* instead would cheerfully send you to five sledgehammer towns for the same practical
result. The planner stops chasing a target once the odds flatten.

**The fill is linear until the pack is full**, then worthless. So once the needs are
satisfied, remaining budget goes to coming home loaded.

```
value(route) = Π P(found each need)  +  0.45 × min(fill, slots)/slots
```

Selection is on **value per second**, not raw value. That is what surfaces deer stands: 2–3
slots drawn from a narrow `Hunting` pool means ~5% odds per slot against ~1% in a generic
wreck, and a ~35 s stop. They earn a place between towns despite holding far less loot in
total — which matches how players actually navigate.

- **Expected yield** per stop = `eff` slots × the item's share of the nominal competing for
  that slot. An estimate, and an upper bound: it assumes nothing has been looted.
- **Pace** sets metabolic drain, which is published, and a default travel pace, which is
  **not**. DayZ's movement speeds live in the engine's animation graph inside the PBOs —
  `human.c` only declares `GetCurrentMovementSpeed()` as `proto native`. So travel pace is a
  slider you calibrate, expressed in **minutes per km of straight-line map distance**, which
  folds in hills and detours with no separate fudge factor. Reference distances:
  Cherno→Elektro 3.7 km, Kamenka→Cherno 4.8 km, Zelenogorsk→NWAF 5.5 km.
- **Energy and water** come from `playerconstants.c` — Bohemia's own numbers, not community
  estimates. Jogging costs 0.3 energy and 0.3 water per second out of 5000.
- **Search time** scales with the building, not per building: a fixed cost to approach,
  enter and leave, plus a per-loot-spot cost for walking to and checking each one. A 3-point
  shed is a glance; a 20-point apartment block is minutes of stairs. Also a slider — not
  published either.
- **Candidates** are shortlisted by both raw yield *and* yield-per-metre from where you
  stand. Yield alone starves the solver of nearby options and leaves short budgets unused.

Three of the planner's inputs — travel pace, search speed, and free slots — are **estimates
we cannot look up**. They are sliders, labelled as such, with calibration hints. Everything
downstream of them is computed from Bohemia's published config.

### Sweeping event sites

The Run tab can route through **dynamic event sites** instead of item clusters — heli
crashes and military convoys — for the player who wants what only those spawn: NVGs,
plate carriers, the NBC suit, the SVD. The currency is different from an item run, and it is
worth stating why.

**A site is not an event.** `db/events.xml` keeps `nominal` events live at once, placed at
random among the fixed positions in `cfgeventspawns.xml`; when one is cleaned up, its
replacement is re-rolled. So the odds that any one site holds a live event **right now** are
`nominal / sites`:

| Event | Live | Sites | Odds per site |
|---|--:|--:|--:|
| Military convoy | 6 | 23 | **26%** |
| Heli crash | 3 | 95 | **3%** |

That gap is the reason this mode exists. A 90-minute sweep of five convoy sites has a 78%
chance of finding one; the same 90 minutes across heli crash sites is a long shot, and the
planner says so rather than drawing a hopeful line.

**The objective is expected live events found**, `Σ p` over the visited sites, with
`P(at least one) = 1 − Π(1 − p)` reported alongside. Selection is the same greedy-by-value-
per-second and 2-opt as an item run; only the value function changes.

Three mechanics from `events.xml` shape the advice, and two of them the estimate cannot see:

- **`saferadius`** (500 m convoy, 1 km heli): a site with a player inside that radius is
  ineligible to receive a spawn. You never watch one appear; it appears while you are away.
  Sites inside the start's safe radius are therefore dropped from the route.
- **`lifetime`** (30 min convoy, 35 min heli) and **`cleanupradius`** (1 km): once the
  lifetime is up, the event is removed the first time nobody is within 1 km, and the slot
  re-rolls immediately (`restock` 0). A convoy someone looted ten minutes ago still holds its
  slot until then — so the per-site odds are an **upper bound**.
- **Idle mode** (`db/globals.xml`, `IdleModeStartup=1`): with nobody online the economy
  freezes. A server with a handful of players elsewhere cycles fastest; an empty one does not
  cycle at all.

Dwell reuses the search sliders: one wreck with 10–15 loot points for a heli
(`events.xml` children), 5.3 wrecks averaging 12–18 items for a convoy
(`cfgeventgroups.xml`, which the build otherwise does not read — the two per-kind figures
are constants in the page, noted as such).

### Preferences are config, not code

What a run optimises for is a *product decision*, so it lives in
[`config/loot-profiles.json`](config/loot-profiles.json) as reviewable data. Changing what
"come back full" means is a diff to that file.

Every selector is resolved against the live item index at build time, and **a selector that
matches nothing fails the build**. That is not ceremony — the first run of the validator
rejected `Toolbox`, an item I had assumed existed and which DayZ does not have. Without the
check it would have shipped as a silently empty preference.

It also captures value the source system cannot express. `clothes` is a CLE category, but
its worth on a run is that you shred it into rags for bandages and rope — a fact that lives
two steps away in a crafting recipe. No amount of reading `types.xml` reveals that; it has
to be declared.

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
- [ ] Dynamic event loot (heli crashes, convoys) — what is *in* the wreck. Routing to the
      sites is done ([Sweeping event sites](#sweeping-event-sites)); the popup still only
      says where.

### Later / maybe

- [ ] Route planning across multiple stops
- [ ] Community server support by uploading a custom `types.xml`
- [ ] Cargo/attachment spawns via `cfgspawnabletypes.xml`

### Explicitly out of scope

- Anything that reads game memory or talks to a live server
- Real-time player/loot positions
- Re-hosting iZurvive tiles or Bohemia data

---

## Known unknowns

Things we deliberately do not know, recorded so they are not silently forgotten.

**Untagged containers** (issue #2, closed as "not worth the playtime"). 30 `tools`
containers declare no `tag` at all, including `Land_Tisy_Garages` at 41 points. Whether a
`floor`-tagged item may spawn there is unconfirmed. We take the conservative reading
(`STRICT_TAGS` in `docs/index.html`), so a site that *might* spawn nothing is never
recommended. Flipping it would add ~2% of instances.

**Display names and icons** (issue #11). Vanilla names live behind `#STR_` keys and icons
live in the game PBOs; neither is published anywhere public. Class names are all we have,
which is why `DryBag` / `WaterproofBag` / `DrysackBag` are hard to tell apart.

**Hoarding pressure** (issue #12). `count_in_player` / `count_in_hoarder` decide whether
player-held copies suppress respawns. We parse `nominal` but not these flags yet, so a
hoard-sensitive item can look more available than it is on a busy server.

## Open questions

These need answering before or during v1. They are the real risk in this project.

### 1. ~~Tier gating (`areaflags.map`)~~ — SOLVED

Decoded and shipped. Format, for anyone who needs it:

```
offset 0    u32 gridW=4096, gridH=4096, worldW=15360, worldH=15360, bitsPerCell=32, reserved=0
offset 24   gridW*gridH  u32   usage bitmask
then        gridW*gridH  u8    value bitmask (tier)
```

Two blocks, not "5 bytes per cell" as first guessed: a 4096² `uint32` plane followed by a
4096² `uint8` plane. `24 + 4096²·4 + 4096²·1 = 83,886,104` — the exact file size. Cell
`(row, col) = (z / 3.75, x / 3.75)`; row increases with +z, no flip.

**Bit order is the declaration order in `cfglimitsdefinition.xml`** — `usageflags` for the
u32 plane (Military=bit0 … Historical=bit16), `valueflags` for the u8 plane (Tier1..4 =
bits 0–3, `Unique` = bit 4). That last one explains the odd `0x11`/`0x12`/`0x14` cells:
tier + Unique.

Confirmed two independent ways: the declaration-order hypothesis, and an empirical check
reading the usage bitmask at the position of every building whose prototype declares exactly
one usage. Military→bit0, Police→bit1, Medic→bit2, Firefighter→bit3, Industrial→bit4,
Farm→bit5, Coast→bit6, Hunting→bit9 all matched.

Tier is resolved **at build time, per building**, so the browser gets five integers per row
instead of a 16 MB grid.

**What it changed:** for `SledgeHammer`, gating removes 66% of candidate loot points
(32,624 → 11,182). Chernogorsk drops from 352 points to 13; Zelenogorsk keeps all 356.
Ungated they looked like a 352-vs-356 coin flip — and the coin would have sent you to the
wrong town.

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

## Running it

```bash
python3 tools/build_index.py sync     # fetch pinned upstream files, verify sha256
python3 tools/build_index.py build    # parse cache/ -> docs/data/*.json
python3 tools/test_golden.py          # lock the numbers this README cites
python3 -m http.server -d docs 8000   # http://localhost:8000
```

`sync --update` repins to upstream HEAD and rewrites `sources.lock.json`. Rebuilding without
changing the lockfile must produce byte-identical output; that is the determinism contract.

`cache/` is gitignored — upstream data is fetched, never vendored.

## Status

**v1 live.** The loot map and crafting tree both work against real data. The numbers in this
README come from queries against the real files, and the browser reproduces them exactly.

**Tier gating is live**, so results reflect where items can actually spawn.

Open questions are tracked as
[issues](https://github.com/brucedombrowski/DayZ/issues); those labelled
[`needs-decision`](https://github.com/brucedombrowski/DayZ/issues?q=is%3Aissue+is%3Aopen+label%3Aneeds-decision)
are blocked on a human answer.

See also [SECURITY.md](SECURITY.md) — how upstream `.c` and XML files are ingested safely.
