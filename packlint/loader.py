"""Emulation of Cobblemon's client asset load.

Everything here mirrors `VaryingModelRepository` (blockbench/repository/
VaryingModelRepository.kt), `VaryingRenderableResolver` (client/render/) and
`BedrockAnimationRepository`. The directory lists and the keying rules are the
part that moves between Cobblemon versions, so they live together at the top of
this file and nowhere else.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from typing import Any

from packlint.sources import EffectiveView, Entry

# --------------------------------------------------------------------------
# VaryingModelRepository.kt:76-106 - keep in sync per Cobblemon version.
# --------------------------------------------------------------------------

TYPES = ["pokemon", "fossils", "npcs", "poke_balls", "generic", "block_entities"]

#: VaryingModelRepository.kt:85-93. Order matters: a later directory's key wins.
POSER_DIRECTORIES = [
    "bedrock/posers",
    "bedrock/pokemon/posers",
    "bedrock/fossils/posers",
    "bedrock/block_entities/posers",
    "bedrock/npcs/posers",
    "bedrock/poke_balls/posers",
    "bedrock/generic/posers",
]

#: VaryingModelRepository.kt:95-98. These MERGE (sorted by `order`), not replace.
VARIATION_DIRECTORIES = ["bedrock/species", "bedrock/pokemon/resolvers"] + [
    f"bedrock/{t}/variations" for t in TYPES
]

#: VaryingModelRepository.kt:100-102. Later directory wins on key collision.
MODEL_DIRECTORIES = ["bedrock/models"] + [f"bedrock/{t}/models" for t in TYPES]

#: VaryingModelRepository.kt:104-106.
ANIMATION_DIRECTORIES = ["bedrock/animations"] + [f"bedrock/{t}/animations" for t in TYPES]

#: VaryingModelRepository.kt:108
FALLBACK_SPECIES = "cobblemon:substitute"

#: entity/PoseType.kt:24-38
POSE_TYPES = {
    "STAND",
    "WALK",
    "SLEEP",
    "HOVER",
    "FLY",
    "FLOAT",
    "SWIM",
    "GLIDE",
    "SHOULDER_LEFT",
    "SHOULDER_RIGHT",
    "PROFILE",
    "PORTRAIT",
    "OPEN",
    "NONE",
}


def parse_resource_location(raw: str, default_namespace: str = "minecraft") -> str:
    """`ResourceLocation.parse` - no colon means the `minecraft` namespace.

    Resolvers deserialize identifiers through IdentifierAdapter, which is a bare
    `ResourceLocation.parse` (util/adapters/IdentifierAdapter.kt:27). A
    namespace-less `"model": "sandshrew.geo"` therefore resolves to
    `minecraft:sandshrew.geo` and will NOT find `cobblemon:sandshrew.geo`.
    """
    raw = raw.strip()
    if ":" in raw:
        ns, _, path = raw.partition(":")
        return f"{ns}:{path}"
    return f"{default_namespace}:{raw}"


def _stem(path: str, drop: str = ".json") -> str:
    """`File(identifier.path).nameWithoutExtension` - strips ONE extension.

    `sandshrew.geo.json` -> `sandshrew.geo`; `sandshrew.json` -> `sandshrew`.
    """
    name = posixpath.basename(path)
    if drop and name.endswith(drop):
        return name[: -len(drop)]
    return name


# --------------------------------------------------------------------------


@dataclass
class KeyedAsset:
    """One entry in a filename-keyed registry, plus what it displaced."""

    key: str
    entry: Entry
    directory: str
    #: Entries with the same key that this one overwrote, in load order.
    shadowed: list[tuple[Entry, str]] = field(default_factory=list)


@dataclass
class Variation:
    """`ModelAssetVariation` - client/render/VaryingRenderableResolver.kt:169."""

    aspects: list[str]
    poser: str | None
    model: str | None
    texture: Any
    layers: list[dict[str, Any]]
    sprites: dict[str, str]
    source_path: str
    source_name: str
    condition: str | None = None


@dataclass
class Resolver:
    """`VaryingRenderableResolver` - one species' merged variation list."""

    species: str
    variations: list[Variation] = field(default_factory=list)
    #: The files that contributed, in `order` then discovery order.
    sets: list[tuple[str, str, int]] = field(default_factory=list)


@dataclass
class AnimationGroup:
    #: Group key = file stem with `.animation.json` removed. NO namespace.
    key: str
    entry: Entry
    animations: set[str]
    #: The scanned directory this file was found under.
    directory: str = ""
    parse_error: str | None = None
    shadowed: list[tuple[Entry, str]] = field(default_factory=list)


@dataclass
class Repository:
    """The effective client-side registry state after a load."""

    models: dict[str, KeyedAsset] = field(default_factory=dict)
    posers: dict[str, KeyedAsset] = field(default_factory=dict)
    resolvers: dict[str, Resolver] = field(default_factory=dict)
    animations: dict[str, AnimationGroup] = field(default_factory=dict)
    #: (path, error) for resolver/species files that would not deserialize.
    bad_resolvers: list[tuple[Entry, str]] = field(default_factory=list)
    #: (path, error) for poser files that would not deserialize.
    bad_posers: list[tuple[Entry, str]] = field(default_factory=list)
    #: Poser file stem -> parsed JSON body, for the posers that did load.
    poser_json: dict[str, dict[str, Any]] = field(default_factory=dict)


def _scan(view: EffectiveView, directory: str, suffix: str) -> list[tuple[str, str, Entry]]:
    """`ResourceManager.listResources(directory) { it.endsWith(suffix) }`."""
    out = []
    for ns, rel, entry in view.under(directory):
        if rel.endswith(suffix):
            out.append((ns, rel, entry))
    # Resource-manager order within a directory is not specified; sorting keeps
    # our own output deterministic and makes collisions reproducible.
    return sorted(out, key=lambda t: (t[0], t[1]))


def load_models(view: EffectiveView) -> dict[str, KeyedAsset]:
    """registerModels - VaryingModelRepository.kt:607-622.

    Key is `ResourceLocation(namespace, nameWithoutExtension)`, so
    `.../models/sandshrew.geo.json` keys as `cobblemon:sandshrew.geo`.
    Directories are walked in order and later ones overwrite.
    """
    models: dict[str, KeyedAsset] = {}
    for directory in MODEL_DIRECTORIES:
        for ns, rel, entry in _scan(view, directory, ".geo.json"):
            key = f"{ns}:{_stem(rel)}"
            previous = models.get(key)
            asset = KeyedAsset(key, entry, directory)
            if previous is not None:
                asset.shadowed = previous.shadowed + [(previous.entry, previous.directory)]
            models[key] = asset
    return models


def load_posers(view: EffectiveView, repo: Repository) -> dict[str, KeyedAsset]:
    """registerJsonPosers - VaryingModelRepository.kt:563-575.

    Key is the filename minus `.json`. Note the JSON is parsed EAGERLY here
    (loadJsonPoser's `gson.fromJson(json, JsonObject::class.java)`), which is
    why a syntax error is FATAL, while the pose bodies are only deserialized
    lazily at render time - which is why a bad bone is a CRASH.
    """
    posers: dict[str, KeyedAsset] = {}
    for directory in POSER_DIRECTORIES:
        for ns, rel, entry in _scan(view, directory, ".json"):
            key = f"{ns}:{_stem(rel)}"
            try:
                body = json.loads(entry.read_text())
                if not isinstance(body, dict):
                    raise ValueError("top level is not a JSON object")
            except Exception as exc:
                repo.bad_posers.append((entry, str(exc)))
                continue
            previous = posers.get(key)
            asset = KeyedAsset(key, entry, directory)
            if previous is not None:
                asset.shadowed = previous.shadowed + [(previous.entry, previous.directory)]
            posers[key] = asset
            repo.poser_json[key] = body
    return posers


def load_animations(view: EffectiveView) -> dict[str, AnimationGroup]:
    """loadAnimations - BedrockAnimationRepository.kt:36-69.

    The group key is `identifier.path.substringAfterLast("/")` with
    `.animation.json` removed - the NAMESPACE IS DISCARDED. That is why an
    `assets/cobblemon_reanimodel/.../gliscor.animation.json` replaces the
    official `assets/cobblemon/.../gliscor.animation.json` outright (V36).
    """
    groups: dict[str, AnimationGroup] = {}
    for directory in ANIMATION_DIRECTORIES:
        for _ns, rel, entry in _scan(view, directory, ".animation.json"):
            key = _stem(rel, ".animation.json")
            error = None
            names: set[str] = set()
            try:
                body = json.loads(entry.read_text())
                anims = body.get("animations") if isinstance(body, dict) else None
                if not isinstance(anims, dict):
                    raise ValueError("no `animations` object")
                names = set(anims.keys())
            except Exception as exc:
                error = str(exc)
            previous = groups.get(key)
            group = AnimationGroup(key, entry, names, directory, error)
            if previous is not None:
                group.shadowed = previous.shadowed + [(previous.entry, previous.directory)]
            groups[key] = group
    return groups


def _variation_from_json(raw: dict[str, Any], entry: Entry) -> Variation:
    model = raw.get("model")
    poser = raw.get("poser")
    return Variation(
        aspects=[str(a) for a in raw.get("aspects") or []],
        poser=parse_resource_location(poser) if isinstance(poser, str) else None,
        model=parse_resource_location(model) if isinstance(model, str) else None,
        texture=raw.get("texture"),
        layers=[layer for layer in (raw.get("layers") or []) if isinstance(layer, dict)],
        sprites={k: v for k, v in (raw.get("sprites") or {}).items() if isinstance(v, str)},
        source_path=entry.path,
        source_name=entry.source.name,
        condition=raw.get("condition") if isinstance(raw.get("condition"), str) else None,
    )


def load_resolvers(view: EffectiveView, repo: Repository) -> dict[str, Resolver]:
    """registerVariations - VaryingModelRepository.kt:581-605.

    Variation SETS merge per species name and are sorted by `order`; within the
    merged list, `getVariationValue` takes the LAST fitting variation, so a
    later set overrides an earlier one field by field.
    """
    by_species: dict[str, list[tuple[int, int, dict[str, Any], Entry]]] = {}
    seq = 0
    for directory in VARIATION_DIRECTORIES:
        for _ns, rel, entry in _scan(view, directory, ".json"):
            try:
                body = json.loads(entry.read_text())
                if not isinstance(body, dict):
                    raise ValueError("top level is not a JSON object")
            except Exception as exc:
                repo.bad_resolvers.append((entry, str(exc)))
                continue
            # ModelVariationSet: @SerializedName("name", alternate=["species","pokeball"])
            name = body.get("name") or body.get("species") or body.get("pokeball")
            if not isinstance(name, str):
                repo.bad_resolvers.append(
                    (entry, "no `name`/`species`/`pokeball` field - set would key as cobblemon:thing")
                )
                continue
            order = body.get("order")
            order = order if isinstance(order, int) else 0
            species = parse_resource_location(name, default_namespace="minecraft")
            by_species.setdefault(species, []).append((order, seq, body, entry))
            seq += 1

    resolvers: dict[str, Resolver] = {}
    for species, sets in by_species.items():
        resolver = Resolver(species)
        for order, _seq, body, entry in sorted(sets, key=lambda t: (t[0], t[1])):
            resolver.sets.append((entry.path, entry.source.name, order))
            for raw in body.get("variations") or []:
                if isinstance(raw, dict):
                    resolver.variations.append(_variation_from_json(raw, entry))
        resolvers[species] = resolver
    return resolvers


def load(view: EffectiveView) -> Repository:
    """`VaryingModelRepository.reload` - the whole client-side asset load."""
    repo = Repository()
    repo.models = load_models(view)
    repo.posers = load_posers(view, repo)
    repo.animations = load_animations(view)
    repo.resolvers = load_resolvers(view, repo)
    return repo
