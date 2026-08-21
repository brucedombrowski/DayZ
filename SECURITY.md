# Security

This project **automatically ingests third-party data** from Bohemia Interactive's public
GitHub repositories and republishes a derived index to a public website. That is the trust
boundary, and this document describes how it is defended.

Scope: `tools/build_index.py` (the ingest pipeline) and `docs/` (the published static site).

---

## The main question: we ingest `.c` source files

The crafting guide reads **224 Enforce Script files** from
[`BohemiaInteractive/DayZ-Script-Diff`](https://github.com/BohemiaInteractive/DayZ-Script-Diff)
— `scripts/4_world/classes/recipes/recipes/*.c`. These are program source code fetched from
the internet, unattended, on a schedule. It is reasonable to be nervous about that.

**They are never executed. They are parsed as text.**

Enforce Script is not C, is not JavaScript, and has no runtime in this project. There is no
compiler, no interpreter, no `eval`, no `exec`, no dynamic import, no shell invocation. The
entire treatment of a recipe file is five regular expressions run over a string:

```python
RE_CLASS   = re.compile(r"class\s+(\w+)\s+extends\s+RecipeBase")
RE_ING     = re.compile(r'InsertIngredient\s*\(\s*(\d+)\s*,\s*"([^"]+)"')
RE_RES     = re.compile(r'AddResult\s*\(\s*"([^"]+)"')
RE_DESTROY = re.compile(r"m_IngredientDestroy\[(\d+)\]\s*=\s*(true|false)")
RE_NAME    = re.compile(r'm_Name\s*=\s*"([^"]+)"')
```

What comes out is a small record of strings, integers, and booleans. Anything in the file
that does not match one of those five patterns is discarded — it never reaches the output.

**This is a design constraint, not a precaution.** Any future change that compiles,
evaluates, or otherwise *runs* upstream content is a breaking change to this project's
security model and must not be made casually. If richer recipe semantics are ever needed,
the correct move is a real parser producing an inert AST — still never an evaluator.

### Consequences for the output

The extracted strings (item class names, `#STR_` keys) are rendered into HTML in the
crafting tree. A malicious upstream string is therefore an **XSS vector, not an RCE vector**
— a meaningfully smaller problem, but a real one. See
[Known gap: HTML escaping](#known-gap-html-escaping).

---

## Threat model

| Threat | Mitigation | Residual |
|---|---|---|
| Malicious code in upstream `.c` files | Never executed; regex extraction only | Extracted *strings* reach HTML — see XSS below |
| Malicious XML (XXE, entity expansion, quadratic blowup) | Hardened parser required — see below | — |
| Oversized payload → OOM | Per-file byte ceilings in `sources.lock.json`; 🔴 fail closed | — |
| Upstream repo tampering / history rewrite | Fetch by pinned commit SHA; `sha256` verified per file | — |
| Man-in-the-middle | Verified TLS enforced in `tls_context()`; never falls back to unverified | — |
| Silent semantic drift (new tier, new usage tag) | Schema/enum diffing + golden invariants; escalates to 🟡 PR | — |
| **Upstream account compromise** | Not preventable by checksums (integrity ≠ authenticity) | **Accepted** — see below |
| Malicious index served to users | Static site, no auth, no user data, no secrets | Wrong loot markers |

### Accepted risk: upstream compromise

If Bohemia's own GitHub account were compromised and a malicious commit pushed, our
checksums would happily verify it — they prove *we got the bytes we pinned*, not *the bytes
are trustworthy*. We accept this, because the blast radius is bounded by everything above:

- no execution path, so the ceiling is data corruption rather than code execution
- schema and enum changes escalate to human review (🟡) instead of auto-merging
- sanity bounds fail the build closed (🔴)
- the site holds no credentials, no user data, and no session state

Realistic worst case: **the map shows wrong loot locations.** That is a correctness bug, not
a compromise.

---

## Controls

### Transport

`tools/build_index.py` routes every network read through one `fetch()` helper using a
verified `ssl.SSLContext`. python.org's macOS builds ship no CA bundle, so the context falls
back to `certifi` — **never to unverified TLS**. There is no `verify=False` anywhere and no
option to add one.

### Integrity and provenance

`sources.lock.json` pins, for every ingested file:

- the **repository** (`BohemiaInteractive/*` only)
- the exact **commit SHA** — never a moving branch
- the **sha256** and **byte length** of the content

`sync` fails with a non-zero exit on any mismatch. Directory-sourced files (the 224 recipes)
are expanded into explicit per-file lock entries so that a file appearing or disappearing
upstream shows up as a reviewable lockfile diff rather than a silent build change.

### XML parsing — required hardening

The economy files are XML and arrive unattended. `xml.etree.ElementTree` in CPython does not
expand external entities and does not fetch external DTDs, which covers classic XXE and
billion-laughs for our current inputs.

> **Note:** this safety comes from the *implementation*, not from an explicit setting in our
> code. If the parser is ever swapped (e.g. to `lxml` for speed), XXE and entity-expansion
> protection **must be re-established explicitly** — `lxml` resolves entities by default.

### Blast-radius tiering

Auto-ingest is tiered (full table in [README](README.md#auto-ingest--tiered-by-blast-radius)):

- 🟢 values change within the known schema → auto-merge
- 🟡 new enum member, file added/removed, invariant moved → PR for review
- 🔴 schema shape change, parse failure, bound violation → **fail closed**, last good build stays up

The build is a pure function of the lockfile, so every published build is reproducible from
its pin.

### CI permissions

The sync job runs least-privilege: a scoped token, no direct push to `main` for 🟡/🔴, and
its only output is data. It never writes code.

---

## Known gap: HTML escaping

**Status: open.** The front-end builds DOM via template strings and `innerHTML`, interpolating
upstream-derived names (item class names, building class names, recipe ids). Today those are
all `[A-Za-z0-9_]` in practice, so nothing escapes — but that is an accident of the current
data, not an enforced property. A malicious or merely odd upstream name containing `<` would
inject markup.

Fix before this is treated as production-grade: escape all interpolated values, or validate
class names against `^[A-Za-z0-9_]+$` at build time and 🔴-fail otherwise. Build-time
validation is preferable — it stops bad data at ingest instead of at render.

Impact is limited: a static site with no credentials, no cookies, no user data, and no
cross-origin authority. It is a correctness and hygiene issue, not a path to account
compromise.

---

## What this project does not do

- Does not read game memory, inject into the game, or talk to a live game server
- Does not collect, store, or transmit any user data — there is no backend and no analytics
- Does not require or handle credentials, API keys, or secrets
- Does not re-host Bohemia's data or iZurvive's map tiles

---

## Reporting

This is a personal hobby project with no security guarantees and no SLA. If you find
something, open an issue on
[the repository](https://github.com/brucedombrowski/DayZ/issues). For anything you would
rather not post publicly, use GitHub's private vulnerability reporting on the same repo.
