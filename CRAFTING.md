# Crafting Guide

A separate feature from the [loot map](README.md), sharing its data layer.

**"How do I make X?"** — answered as a **hierarchical tree** that recurses until every leaf
is either something you already have or something you must find as loot. At which point it
hands off to the loot map: *here is where that leaf spawns near you.*

That handoff is the reason these two features belong in one repo.

---

## Data source

Vanilla recipes are **not** in the CLE XML. They are Enforce Script classes in Bohemia's
published script source:

| Source | What we use |
|---|---|
| [BohemiaInteractive/DayZ-Script-Diff](https://github.com/BohemiaInteractive/DayZ-Script-Diff) | Official script source, updated per patch. |
| └ `scripts/4_world/classes/recipes/recipes/*.c` | **224 files.** One class per recipe. |
| └ `scripts/4_world/classes/recipes/recipebase.c` | Base class — the semantics of every field below. |

Since we target **PS5 Official**, this vanilla set is exactly right: no mods to reconcile.

A recipe is machine-parseable. `craftbow.c`, trimmed:

```c
class CraftBow extends RecipeBase
{
    override void Init()
    {
        m_Name = "#STR_CraftBow0";
        m_AnimationLength = 1.5;

        InsertIngredient(0,"Rope");
        m_IngredientDestroy[0] = true;

        InsertIngredient(1,"LongWoodenStick");
        m_IngredientDestroy[1] = true;

        AddResult("QuickieBow");
    }
}
```

- `InsertIngredient(slot, "ClassName")` — called repeatedly on the **same slot** to list
  *alternatives* ("any of these knives"), not additional requirements. **Slots are AND,
  entries within a slot are OR.** Getting this backwards produces nonsense.
- `AddResult("ClassName")` — outputs, possibly several.
- `m_IngredientDestroy[slot]` — see [Trap 1](#trap-1-tools-vs-materials).
- `m_Name` is a `#STR_` localisation key, so we also need the string table for display names.

---

## Verified against real data

A parse of all 224 files already runs:

- **177** define results via `AddResult`; **47** use other mechanisms ([Trap 4](#trap-4-recipebase-is-not-all-of-crafting))
- **133** distinct craftable outputs
- Recursion terminates at loot leaves — with the caveats below

Example output for `QuickieBow`, after handling the traps:

```
QuickieBow  ⟵ craft [CraftBow]
├─ Rope             ← LOOT
├─ LongWoodenStick  ⟵ craft [CraftLongWoodenStick]
│  └─ WoodenStick   ← LOOT / gather
└─ tool: any knife  ← LOOT  (not consumed)
```

---

## The four traps

Found by actually running the parser. Each one silently produces a *plausible but wrong*
tree, which is worse than an error.

### Trap 1: tools vs materials

The knife in a de-craft recipe is a **tool** — you must *have* it, it is not consumed. Rope
is a **material** — it is destroyed. Naively they look identical.

The data distinguishes them cleanly:

```c
m_IngredientDestroy[0] = true;   // FurCourierBag — consumed
m_IngredientDestroy[1] = false;  // the knife     — tool, kept
```

**Rule:** `m_IngredientDestroy[slot] == false` ⟹ render as a tool requirement, do not
recurse into it as a material, and de-duplicate tools across the whole tree ("you need one
knife", not five).

### Trap 2: de-craft recipes invert the graph

`DeCraftLeatherCourierBag` takes a `FurCourierBag` + knife and yields `WildboarPelt` + `Rope`.
Treated as a normal recipe, "how do I get Rope?" answers **"destroy a courier bag"** — and
the real run did exactly that:

```
QuickieBow ⟵ CraftBow
└ Rope ⟵ DeCraftLeatherCourierBag        ← absurd
   └ FurCourierBag ⟵ CraftLeatherCourierBag
      └ WildboarPelt ⟵ DeCraftLeatherCourierBag
         └ FurCourierBag  [cycle]
```

**Rule:** classify each recipe as `craft` / `decraft` / `transform` and default the tree to
`craft` edges only. Surface de-craft separately as *"you can also salvage this from…"* —
useful, but never the primary path.

### Trap 3: cycles

Craft + de-craft of the same item form a 2-cycle, visible above. Any real item graph will
have more.

**Rule:** track the visited set down each branch; cut on revisit. Then rank remaining paths
by cost rather than taking the first one found — the first is arbitrary, and the run above
picked a genuinely terrible path.

### Trap 4: `RecipeBase` is not all of crafting

`ImprovisedShelter` returned `← LOOT` in the real run. It is obviously craftable — but via
the **construction/action** system, not `RecipeBase`. The 47 files without `AddResult`
(`opencan.c`, `cleanrags.c`, `sawoffmagnum.c`, `patchitem.c`, `fuelchainsaw.c`, …) are a
related gap: in-place transforms rather than item-producing recipes.

**Rule:** never render "← LOOT" as a *fact*. If an item has no known craft path, say
**"no recipe found in `RecipeBase`"** — an honest gap, not a false claim. Closing it means
also covering the base-building / action systems in `4_world/classes/useractionscomponent/`.

---

## What it should do

### v1

- [ ] Parse all 224 recipes into a normalised JSON graph at build time
- [ ] Classify `craft` / `decraft` / `transform`; tag tool vs material slots
- [ ] Resolve `#STR_` keys to display names
- [ ] Search any item → hierarchical tree, expand/collapse
- [ ] Cycle-safe traversal, cheapest path preferred
- [ ] Mark leaves clearly: **craftable** / **loot-only** / **no known recipe**
- [ ] "I already have…" — check off inventory, tree collapses to what's left

### v2 — the payoff

- [ ] **Every loot leaf links straight into the loot map.** "You need Rope + a knife →
      here's the nearest place each spawns."
- [ ] Flatten a whole tree into a single shopping list, deduped, tools separated
- [ ] Route it: one trip hitting the buildings that cover the most of the list
- [ ] Show salvage alternatives from de-craft edges
- [ ] Reverse lookup: "I'm holding a Sickle — what can I make?"

### Out of scope

- Mod recipes (PS5 Official is vanilla)
- Recipe conditions on damage/quantity (`m_MinDamageIngredient`, etc.) beyond warning that
  a ruined ingredient may not work

---

## Open questions

1. **Parser coverage.** What do the 47 non-`AddResult` files actually do, and do any belong
   in the tree?
2. **Base building.** Is the construction system in scope for v1, or is "no recipe found"
   an acceptable v1 answer? (Leaning: acceptable, if labelled honestly.)
3. **Path cost.** What makes one craft path better — fewer steps, fewer loot leaves, or
   leaves that are *easier to find* per the loot map? The last is the most useful and needs
   the map's ranking model.
4. **Localisation.** Where does the `#STR_` string table live in `DayZ-Script-Diff`?
5. **Version drift.** `DayZ-Script-Diff` tracks patches; PS5 Official may lag PC. Pin to
   the tag matching the current console build.

---

## Status

**Specification.** Parser is prototyped and its output verified — every number and every
trap above came from running it against the real 224 files, not from estimates.
