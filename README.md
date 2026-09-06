# eclipse-pack-lint

Static validator for the Cobblemon Eclipse client resource pack.

It loads a Cobblemon jar and one or more resource packs, **emulates Cobblemon's
client-side asset loader**, and reports everything that would break — before a
pack reaches players.

The 1.8 cutover on 2026-09-05 shipped a pack that crashed clients four different
ways. Every one of them was statically detectable from the pack zip plus the
jar. This tool detects all four, and found two more nobody had noticed.

```
python -m packlint validate \
    --jar D:\JARS\1.8-release\Cobblemon-fabric-1.8.0+1.21.1.jar \
    --pack D:\JARS\resource_pack-V38d-unsquashed.zip
```

Python 3.9+, standard library only. No install, no dependencies.

---

## Why a pack breaks clients

Cobblemon's loader has three properties that a resource pack author does not
expect, and all four of tonight's crashes came out of them:

1. **Models, posers and animation groups are keyed by FILENAME, not by path.**
   `bedrock/pokemon/models/0027_sandshrew/sandshrew.geo.json` keys as
   `cobblemon:sandshrew.geo`. Any file anywhere in a scanned directory with the
   same basename replaces it. Animation groups do not even keep the namespace —
   `assets/cobblemon_reanimodel/.../gliscor.animation.json` silently replaces
   Cobblemon's own `gliscor` group. (`VaryingModelRepository.kt:570` / `:694`,
   `BedrockAnimationRepository.kt:57`.)

2. **Posers and models resolve INDEPENDENTLY.** A resolver variation that sets
   only `poser` pairs that poser with whatever model a *different* variation
   supplies. A texture-only event resolver can therefore point a Kotlin poser at
   a model it has never seen. (`VaryingRenderableResolver.kt:64-66`.)

3. **Pose bodies are deserialized LAZILY, at render time.** A poser that names a
   bone the model does not have loads cleanly, boots cleanly, and then kills the
   client the moment somebody sees that Pokémon.
   (`VaryingModelRepository.kt:155-161`, `JsonPose.kt:64-67`.)

Every rule in the tool cites the Cobblemon line it was derived from. Run
`python -m packlint validate --explain PL-C001` for any of them.

---

## Finding classes

| Class | Meaning | Exit code |
|---|---|---|
| `FATAL` | the pack fails to load for everyone; no Pokémon renders | non-zero |
| `CRASH` | the client crashes when the affected model renders | non-zero |
| `WARN` | renders, but degraded (Substitute fallback, T-pose, missing texture) | 0 |
| `RESIDUE` | pack asset shadowing something the jar now ships itself | 0 |

`python -m packlint rules` lists all 22 rules. Highlights:

- **PL-F001** a resolver `model` with no effective model. One of these aborts
  `registerVariations` for the *whole game*, leaving an unpredictable share of
  species with an uninitialised repository — which is why a single bad model
  reads as "the entire pack is broken".
- **PL-F002** geo that Cobblemon cannot deserialize, so it silently never
  registers. Usually per-face UV: **Cobblemon geo must be Box UV.**
- **PL-C001** a JSON poser `transformedParts[].part` naming a bone the paired
  model does not have. Note a JSON poser exposes the root bone only as `__root`,
  never under its own name.
- **PL-C002 / PL-C003** a built-in Kotlin poser's root bone / required bone
  missing from the paired model.
- **PL-C004** a built-in poser resolving an animation through `getAnimation()`,
  which *throws*. This is the V36 Gliscor bug: our reanimodel group shadowed the
  official one and dropped `cry`, so every Gliscor trade crashed both clients.
- **PL-C005** the legacy `bedrock(group, anim)` poser form for a missing
  animation. Unlike the modern `q.bedrock('g','a')` (swallowed → T-pose), this
  one throws.
- **PL-R002 / PL-R003** the residue class: a pack file shadowing an official
  asset by loader key, and a shadowing animation group that drops keys the
  official group defined.
- **PL-W009** pack files that are *inert* — another file wins their key, so
  shipping them does nothing and, worse, editing them to fix a bug does nothing
  either.

Findings are calibrated against a **jar-only baseline**: if the jar on its own
already produces the same pairing with the same missing bone, the fault is
vanilla's (or our extraction's) and is suppressed rather than reported. The
suppressed list is kept in `--json` so the calibration itself stays auditable.

---

## Where it sits in the deploy pipeline

`AI/ResourcePack/DEPLOY.md` describes the flow:

```
polymer/ include_zips  ->  polymer generate-pack  ->  PackSquash  ->  R2  ->  Velocity
        ^                          ^
        |                          |
   LINT HERE (cheap)          LINT HERE (authoritative)
```

**Before generating** — lint the include dir. `--include-dir` merges every
`.zip` in it exactly as polymer would (later filename wins), so a bad include is
caught without a server round trip:

```
python -m packlint validate --jar <cobblemon.jar> --include-dir <local polymer/>
```

**After generating** — lint the real merge, which is what players get. This is
the gate; do not upload a pack that exits non-zero:

```
python -m packlint validate --jar <cobblemon.jar> --pack resource_pack.zip \
    --json report.json --fix-manifest fix.json
```

Then apply the suggested deletions into a **new** zip (never in place) and
re-lint:

```
python -m packlint fix --pack resource_pack.zip --manifest fix.json --out cleaned.zip
python -m packlint validate --jar <cobblemon.jar> --pack cleaned.zip
```

> **A residue manifest is a suggestion list, not a safe bulk apply.** Deleting
> our copy of an asset restores the jar's copy, and the jar's copy may not
> satisfy a poser or resolver we still ship. Applying all 599 residue deletions
> to V38e at once took it from 5 crashes to 18. Delete in slices, re-lint after
> each slice, and keep only the slices that lower the crash count. The
> `PL-W005` (junk), `PL-W006` (`data/`) and `PL-W009` (inert - the file never
> loads at all) deletions are the ones that are safe wholesale.

Lint the *unsquashed* pack. PackSquash rewrites JSON, and a squashed pack is
harder to map back to a source include.

---

## Eclipse-owned assets

The residue class asks "what in our pack is now Cobblemon's job?". The answer
changes every Cobblemon release, and the exempt set changes every time we ship a
cosmetic line — so it lives in `packlint/data/eclipse-owned.json`, not in code:

```json
{
  "namespaces": ["eclipse-cosmetics", "eclipse-plushies"],
  "aspects": ["radiant", "gilded", "plushie", "za_mega"],
  "path_globs": ["assets/minecraft/**"],
  "species": [],
  "keys": []
}
```

Pass a different one with `--config`. An exemption removes an asset from
`RESIDUE` only — it **never** exempts it from `FATAL` or `CRASH`. Owning an
asset does not make it safe.

---

## Updating for a new Cobblemon version

Two things move between Cobblemon versions, and both have a defined refresh
step.

**The built-in poser table.** Cobblemon registers ~390 posers in Kotlin
(`registerInBuiltPosers`), and those classes hard-require bones and animations
by name. Re-extract them from the new source tree and commit the result:

```
python -m packlint extract-builtin --source <path to cobblemon>/common/src/main/kotlin/com/cobblemon/mod/common
```

That writes `packlint/data/builtin_bones_<version>.json`; `validate` picks the
table matching the jar's `fabric.mod.json` version and falls back to the newest
shipped table (saying so in the header) if there is no exact match.

**The loader rules themselves.** Diff these four files against the new version —
they are the only ones the tool depends on, and every rule cites its line:
`repository/VaryingModelRepository.kt` (the directory lists at :76-106 and the
keying at :570/:694), `client/render/VaryingRenderableResolver.kt` (the
independent poser/model resolution), `blockbench/JsonPose.kt` (what throws
versus what is swallowed), and `bedrock/animation/BedrockAnimationRepository.kt`
(the file-stem group key). Anything that changed lands in `packlint/loader.py`,
whose top section holds every version-sensitive constant in one place, or in the
matching rule's citation in `packlint/findings.py`. Then run the suite, and run
the tool against the previous pack: the finding counts should not move except
where the Cobblemon change explains them.

---

## Tests

```
python -m unittest discover -s tests -t .
```

Synthetic jar/pack fixtures cover every rule class and run in under a second;
CI runs these. The 2026-09-05 regression expectations run against the real
corpus and skip unless you point at it:

```
PACKLINT_JAR=D:/JARS/1.8-release/Cobblemon-fabric-1.8.0+1.21.1.jar \
PACKLINT_PACK=D:/JARS/resource_pack-V38d-unsquashed.zip \
python -m unittest discover -s tests -t .
```

## Commands

```
packlint validate         lint a pack (or include dir) against a jar
packlint fix              apply a {delete, write} manifest into a NEW zip
packlint extract-builtin  regenerate the built-in poser table from Kotlin source
packlint rules            list every rule
packlint validate --explain PL-C001    print a rule's Cobblemon source citation
```
