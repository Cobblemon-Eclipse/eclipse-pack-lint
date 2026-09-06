"""The rule engine: run every check against an effective jar+pack view."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from packlint import builtin as builtin_mod
from packlint import findings as F
from packlint import loader, molang
from packlint.geo import GeoError, GeoModel, parse_geo
from packlint.ownership import Ownership
from packlint.sources import ROLE_JAR, EffectiveView, Entry

JUNK_PATTERNS = [
    "*.DS_Store",
    ".DS_Store",
    "__MACOSX/*",
    "*/Thumbs.db",
    "Thumbs.db",
    "*/desktop.ini",
    "*.bak",
    "*.bak.*",
    "*.bak-*",
    "*.orig",
    "*.rej",
    "*.swp",
    "*~",
    "*.tmp",
    "*/.gitkeep",
]

#: Asset directories whose contents Cobblemon keys by FILENAME, where a pack
#: file can therefore shadow an official one from a different path (PL-R002).
KEYED_DIRECTORIES = (
    [(d, ".geo.json", "model") for d in loader.MODEL_DIRECTORIES]
    + [(d, ".json", "poser") for d in loader.POSER_DIRECTORIES]
    + [(d, ".animation.json", "animation") for d in loader.ANIMATION_DIRECTORIES]
)


@dataclass
class Result:
    findings: list[F.Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    #: Detections we deliberately dropped, with why (kept for --json).
    suppressed: list[dict[str, str]] = field(default_factory=list)

    def add(self, finding: F.Finding) -> None:
        self.findings.append(finding)

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in F.SEVERITY_ORDER}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    @property
    def blocking(self) -> int:
        c = self.counts()
        return c.get(F.FATAL, 0) + c.get(F.CRASH, 0)


class Validator:
    def __init__(
        self,
        view: EffectiveView,
        ownership: Ownership,
        builtin_table: dict[str, Any],
        max_per_rule: int = 0,
    ) -> None:
        self.view = view
        self.ownership = ownership
        self.builtin = builtin_table.get("posers", {})
        self.builtin_meta = builtin_table
        self.result = Result()
        self.max_per_rule = max_per_rule
        self._rule_counts: dict[str, int] = {}
        self._geo_cache: dict[str, GeoModel | GeoError] = {}
        self._jar_geo_cache: dict[str, GeoModel | GeoError | None] = {}
        self._jar_repo: loader.Repository | None = None
        self._jar_pairs: set[tuple[str, str, str]] | None = None
        self._seen_builtin_anims: set[tuple[str, str, str]] = set()
        self.repo = loader.load(view)

    # -- emit ---------------------------------------------------------------

    def emit(self, rule: F.Rule, message: str, **kwargs: Any) -> None:
        seen = self._rule_counts.get(rule.id, 0)
        self._rule_counts[rule.id] = seen + 1
        if self.max_per_rule and seen >= self.max_per_rule:
            return
        self.result.add(F.make(rule, message, **kwargs))

    def suppress(self, rule: F.Rule, what: str, why: str) -> None:
        self.result.suppressed.append({"rule": rule.id, "what": what, "why": why})

    # -- geo ----------------------------------------------------------------

    def geo_of(self, entry: Entry) -> GeoModel | GeoError:
        cached = self._geo_cache.get(entry.path)
        if cached is None:
            try:
                cached = parse_geo(entry.read_text())
            except GeoError as exc:
                cached = exc
            self._geo_cache[entry.path] = cached
        return cached

    # -- vanilla baseline ---------------------------------------------------
    #
    # Calibration against the jar ALONE. A detection is only a real pack fault
    # if vanilla Cobblemon does not already have it: if the jar on its own
    # produces the same (species, poser, model) pairing and the same bone is
    # missing there too, then vanilla would crash as well, which means our
    # built-in extraction is wrong rather than the pack. Those are suppressed.
    #
    # This must be pairing-aware, not just "does the jar's geo have the bone".
    # The V38d rapidash crash is exactly a case where the jar's own
    # rapidash_galar.geo legitimately lacks a `rapidash` bone - because vanilla
    # never pairs that model with the `rapidash` poser. The pack's resolver is
    # what creates the pairing, so it is a true positive.

    @property
    def jar_repo(self) -> loader.Repository:
        if self._jar_repo is None:
            jar_view = EffectiveView(self.view.jar_sources())
            self._jar_repo = loader.load(jar_view)
        return self._jar_repo

    @property
    def jar_pairs(self) -> set[tuple[str, str, str]]:
        """(species, poser, model) triples the jar produces on its own."""
        if self._jar_pairs is None:
            pairs: set[tuple[str, str, str]] = set()
            for species, resolver in self.jar_repo.resolvers.items():
                for poser, model in reachable_pairs(resolver):
                    pairs.add((species, poser, model))
            self._jar_pairs = pairs
        return self._jar_pairs

    def jar_geo_for_key(self, key: str) -> GeoModel | None:
        """Parse the jar's own copy of a model key."""
        if key not in self._jar_geo_cache:
            asset = self.jar_repo.models.get(key)
            result: GeoModel | GeoError | None = None
            if asset is not None:
                try:
                    result = parse_geo(asset.entry.read_text())
                except GeoError as exc:
                    result = exc
            self._jar_geo_cache[key] = result
        cached = self._jar_geo_cache[key]
        return cached if isinstance(cached, GeoModel) else None

    def vanilla_pair(self, species: str, poser: str, model_key: str) -> bool:
        """True when the pack contributed nothing to this pairing.

        All three must hold: the jar alone produces the same
        (species, poser, model) triple, the winning poser file is the jar's, and
        the winning model file is the jar's. If the pack supplied any side, the
        pairing is ours to answer for even when the jar's own geo happens to
        lack the bone - that is the rapidash case, where the pack's resolver is
        what points RapidashModel at the jar's rapidash_galar.geo.

        Bone findings on a fully-vanilla pairing are not actionable by a pack
        operator (and are more likely to be our Kotlin extraction being wrong
        than a real Cobblemon bug), so they are suppressed.
        """
        if (species, poser, model_key) not in self.jar_pairs:
            return False
        poser_asset = self.repo.posers.get(poser)
        if poser_asset is not None and poser_asset.entry.from_pack:
            return False
        model_asset = self.repo.models.get(model_key)
        if model_asset is not None and model_asset.entry.from_pack:
            return False
        return True

    def vanilla_also_broken(
        self, species: str, poser: str, model_key: str, bone: str
    ) -> bool:
        """`vanilla_pair`, and the jar's own geo really is missing the bone."""
        if not self.vanilla_pair(species, poser, model_key):
            return False
        jar_geo = self.jar_geo_for_key(model_key)
        if jar_geo is None:
            return False
        return bone not in jar_geo.all_bones()

    # -- the run ------------------------------------------------------------

    def run(self) -> Result:
        self.check_models()
        self.check_parse_failures()
        self.check_hygiene()
        self.check_resolvers()
        self.check_posers_standalone()
        self.check_pairs()
        self.check_residue()
        self.result.stats = {
            "models": len(self.repo.models),
            "posers": len(self.repo.posers),
            "builtin_posers": len(self.builtin),
            "builtin_table": self.builtin_meta.get("source_file"),
            "builtin_table_version": self.builtin_meta.get("cobblemon_version"),
            "resolvers": len(self.repo.resolvers),
            "variations": sum(len(r.variations) for r in self.repo.resolvers.values()),
            "animation_groups": len(self.repo.animations),
            "sources": [f"{s.role}:{s.name}" for s in self.view.sources],
            "rule_totals": dict(self._rule_counts),
        }
        return self.result

    # -- PL-F002 / PL-F003 --------------------------------------------------

    def check_models(self) -> None:
        for key, asset in self.repo.models.items():
            geo = self.geo_of(asset.entry)
            if isinstance(geo, GeoError):
                rule = F.GEO_STRUCTURE_INVALID if geo.hard else F.GEO_UNLOADABLE
                self.emit(
                    rule,
                    f"model `{key}`: {geo.message}",
                    path=asset.entry.path,
                    source=asset.entry.source.name,
                    detail={"key": key, "aborts_all_model_loading": geo.hard},
                    suggest_delete=[asset.entry.path] if asset.entry.from_pack else [],
                )

    # -- PL-F004 / PL-F005 / PL-F007 ---------------------------------------

    def check_parse_failures(self) -> None:
        for entry, error in self.repo.bad_resolvers:
            self.emit(
                F.RESOLVER_JSON_INVALID,
                f"resolver `{entry.path}`: {error}",
                path=entry.path,
                source=entry.source.name,
                suggest_delete=[entry.path] if entry.from_pack else [],
            )
        for entry, error in self.repo.bad_posers:
            self.emit(
                F.POSER_JSON_INVALID,
                f"poser `{entry.path}`: {error}",
                path=entry.path,
                source=entry.source.name,
                suggest_delete=[entry.path] if entry.from_pack else [],
            )
        for key, group in self.repo.animations.items():
            if group.parse_error:
                self.emit(
                    F.ANIMATION_JSON_INVALID,
                    f"animation group `{key}`: {group.parse_error}",
                    path=group.entry.path,
                    source=group.entry.source.name,
                    suggest_delete=[group.entry.path] if group.entry.from_pack else [],
                )

    # -- PL-W004 / PL-W005 / PL-W006 ---------------------------------------

    def check_hygiene(self) -> None:
        collisions: list[tuple[str, str, Entry, str, Entry, str]] = []
        for key, asset in self.repo.models.items():
            for entry, directory in asset.shadowed:
                collisions.append((key, "model", entry, directory, asset.entry, asset.directory))
        for key, asset in self.repo.posers.items():
            for entry, directory in asset.shadowed:
                collisions.append((key, "poser", entry, directory, asset.entry, asset.directory))
        for key, group in self.repo.animations.items():
            for entry, directory in group.shadowed:
                collisions.append(
                    (key, "animation", entry, directory, group.entry, group.directory)
                )

        for key, label, loser, loser_dir, winner, winner_dir in collisions:
            if loser.path == winner.path:
                continue  # a pack overriding the jar at the same path is PL-R001
            if loser.from_pack:
                # The pack ships a file that never loads. Dead weight, and a
                # trap: editing it to fix a bug changes nothing.
                self.emit(
                    F.INERT_PACK_ASSET,
                    f"pack {label} `{loser.path}` never loads - `{winner.path}` "
                    f"({winner.source.name}) wins the key `{key}`",
                    path=loser.path,
                    source=loser.source.name,
                    detail={
                        "kind": label,
                        "key": key,
                        "winner": winner.path,
                        "winner_source": winner.source.name,
                        "winner_directory": winner_dir,
                        "loser_directory": loser_dir,
                        "content_differs": not self._same_bytes(loser, winner),
                    },
                    suggest_delete=[loser.path],
                )
            if (
                loser_dir
                and loser_dir == winner_dir
                and (loser.from_pack or winner.from_pack)
                and not self._same_bytes(loser, winner)
            ):
                # Same scanned directory: which one the resource manager hands
                # over first is not specified, so the winner is a coin flip.
                self.emit(
                    F.DUPLICATE_KEY,
                    f"{label} key `{key}` is claimed by two files with different content in the "
                    f"same directory `{loser_dir}`: `{winner.path}` and `{loser.path}` - which "
                    "one wins is resource-manager order, not something you control",
                    path=loser.path,
                    source=loser.source.name,
                    detail={"key": key, "paths": [winner.path, loser.path]},
                )

        for entry in self.view.entries():
            if not entry.from_pack:
                continue
            base = entry.path
            if any(fnmatch.fnmatch(base, pattern) for pattern in JUNK_PATTERNS):
                self.emit(
                    F.JUNK_FILE,
                    f"junk file `{base}`",
                    path=base,
                    source=entry.source.name,
                    suggest_delete=[base],
                )
            elif base.startswith("data/"):
                self.emit(
                    F.DATA_IN_RESOURCE_PACK,
                    f"datapack entry in a resource pack: `{base}`",
                    path=base,
                    source=entry.source.name,
                    suggest_delete=[base],
                )

    def _same_bytes(self, a: Entry, b: Entry) -> bool:
        try:
            return a.read() == b.read()
        except Exception:
            return False

    # -- PL-F001 / PL-C007 / PL-W001 / PL-W003 ------------------------------

    def check_resolvers(self) -> None:
        broken_species: list[str] = []
        for species in sorted(self.repo.resolvers):
            resolver = self.repo.resolvers[species]
            species_broken = False
            for variation in resolver.variations:
                if variation.model:
                    asset = self.repo.models.get(variation.model)
                    reason = None
                    if asset is None:
                        reason = "no model with that key exists in the jar or any pack"
                    elif isinstance(self.geo_of(asset.entry), GeoError):
                        reason = (
                            f"the model file `{asset.entry.path}` exists but Cobblemon cannot "
                            "load it, so it never enters `texturedModels`"
                        )
                    if reason:
                        species_broken = True
                        self.emit(
                            F.MODEL_MISSING,
                            f"{species}: variation model `{variation.model}` - {reason}",
                            path=variation.source_path,
                            source=variation.source_name,
                            detail={
                                "species": species,
                                "model": variation.model,
                                "aspects": variation.aspects,
                            },
                        )
                if variation.poser and not self._poser_exists(variation.poser):
                    self.emit(
                        F.POSER_MISSING,
                        f"{species}: variation poser `{variation.poser}` is neither a JSON poser "
                        "nor a built-in poser - renders as Substitute",
                        path=variation.source_path,
                        source=variation.source_name,
                        detail={
                            "species": species,
                            "poser": variation.poser,
                            "aspects": variation.aspects,
                        },
                    )
                self._check_textures(species, variation)
            if species_broken:
                broken_species.append(species)

        if broken_species:
            total = len(self.repo.resolvers)
            self.emit(
                F.RESOLVER_CASCADE,
                f"{len(broken_species)} species fail VaryingRenderableResolver.initialize(); the "
                f"first one aborts the init loop, so an unpredictable share of the other "
                f"{total - len(broken_species)} species render with an uninitialised repository "
                "and crash the client too",
                detail={"broken_species": broken_species[:200], "total_species": total},
            )

    def _poser_exists(self, poser: str) -> bool:
        if poser in self.repo.posers:
            return True
        namespace, _, name = poser.partition(":")
        # inbuilt() always registers under cobblemonResource(name)
        # (VaryingModelRepository.kt:578).
        return namespace == "cobblemon" and name in self.builtin

    def _check_textures(self, species: str, variation: loader.Variation) -> None:
        for label, raw in self._texture_refs(variation):
            path = self._asset_path(raw)
            if path and not self.view.has(path):
                self.emit(
                    F.TEXTURE_MISSING,
                    f"{species}: {label} texture `{raw}` does not exist",
                    path=variation.source_path,
                    source=variation.source_name,
                    detail={"species": species, "texture": raw, "expected_path": path},
                )

    def _texture_refs(self, variation: loader.Variation) -> Iterable[tuple[str, str]]:
        def unwrap(value: Any, label: str) -> Iterable[tuple[str, str]]:
            if isinstance(value, str):
                if value != "variable":
                    yield label, value
            elif isinstance(value, dict):
                for frame in value.get("frames") or []:
                    if isinstance(frame, str):
                        yield f"{label} frame", frame

        yield from unwrap(variation.texture, "variation")
        for layer in variation.layers:
            yield from unwrap(layer.get("texture"), f"layer `{layer.get('name', '?')}`")
        for sprite_type, value in variation.sprites.items():
            yield from unwrap(value, f"sprite `{sprite_type}`")

    @staticmethod
    def _asset_path(raw: str) -> str | None:
        if not raw or raw.startswith("#"):
            return None
        location = loader.parse_resource_location(raw)
        namespace, _, path = location.partition(":")
        if not path:
            return None
        return f"assets/{namespace}/{path}"

    # -- PL-F006 / PL-C005 / PL-C006 / PL-W002 (model-independent) ----------

    def check_posers_standalone(self) -> None:
        for key, body in self.repo.poser_json.items():
            entry = self.repo.posers[key].entry
            for pose_name, pose in self._poses(body):
                for raw in pose.get("poseTypes") or []:
                    if isinstance(raw, str) and raw.upper() not in loader.POSE_TYPES:
                        self.emit(
                            F.UNKNOWN_POSE_TYPE,
                            f"poser `{key}` pose `{pose_name}`: unknown poseType `{raw}`",
                            path=entry.path,
                            source=entry.source.name,
                            detail={"poser": key, "pose": pose_name, "poseType": raw},
                        )
                for text in self._pose_animation_strings(pose):
                    for ref in molang.animation_refs(text):
                        if self._animation_exists(ref):
                            continue
                        if self._vanilla_animation_broken(ref, referrer=entry):
                            self.suppress(
                                F.ANIMATION_MISSING,
                                f"poser `{key}` `{ref.key}`",
                                "the jar alone is missing this animation too",
                            )
                            continue
                        rule = (
                            F.POSER_LEGACY_ANIMATION_MISSING if ref.hard else F.ANIMATION_MISSING
                        )
                        self.emit(
                            rule,
                            f"poser `{key}` pose `{pose_name}`: animation `{ref.key}` is not in "
                            f"animation group `{ref.group}`"
                            + ("" if ref.group in self.repo.animations else " (group not found)"),
                            path=entry.path,
                            source=entry.source.name,
                            detail={
                                "poser": key,
                                "pose": pose_name,
                                "group": ref.group,
                                "animation": ref.key,
                                "expression": text,
                            },
                        )
                for text in self._quirk_strings(pose):
                    func = molang.top_level_function(text)
                    if func is not None and func not in molang.KNOWN_QUIRK_FUNCS:
                        self.emit(
                            F.POSER_QUIRK_UNRESOLVABLE,
                            f"poser `{key}` pose `{pose_name}`: quirk resolves "
                            f"`q.{func}(...)`, which does not produce a SimpleQuirk",
                            path=entry.path,
                            source=entry.source.name,
                            detail={"poser": key, "pose": pose_name, "expression": text},
                        )

    @staticmethod
    def _poses(body: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
        poses = body.get("poses")
        if isinstance(poses, dict):
            for name, pose in poses.items():
                if isinstance(pose, dict):
                    yield name, pose

    @staticmethod
    def _pose_animation_strings(pose: dict[str, Any]) -> Iterable[str]:
        for value in pose.get("animations") or []:
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict) and isinstance(value.get("animation"), str):
                yield value["animation"]
        for value in (pose.get("namedAnimations") or {}).values():
            if isinstance(value, str):
                yield value
        for value in (pose.get("transitions") or {}).values():
            if isinstance(value, str):
                yield value
        for quirk in pose.get("quirks") or []:
            if isinstance(quirk, str):
                yield quirk
            elif isinstance(quirk, dict):
                for value in quirk.get("animations") or []:
                    if isinstance(value, str):
                        yield value
                if isinstance(quirk.get("animation"), str):
                    yield quirk["animation"]

    @staticmethod
    def _quirk_strings(pose: dict[str, Any]) -> Iterable[str]:
        for quirk in pose.get("quirks") or []:
            if isinstance(quirk, str):
                yield quirk

    def _animation_exists(self, ref: molang.AnimRef) -> bool:
        group = self.repo.animations.get(ref.group)
        return group is not None and ref.key in group.animations

    def _vanilla_animation_broken(
        self, ref: molang.AnimRef, referrer: Entry | None = None
    ) -> bool:
        """True when the jar on its own is also missing this animation AND the
        reference to it is the jar's own.

        A pack-supplied poser asking for an animation nobody ships is the pack's
        problem even if the jar never had that animation either.
        """
        if not self.view.jar_sources():
            return False
        if referrer is not None and referrer.from_pack:
            return False
        group = self.repo.animations.get(ref.group)
        if group is not None and group.entry.from_pack:
            return False
        jar_group = self.jar_repo.animations.get(ref.group)
        return jar_group is None or ref.key not in jar_group.animations

    # -- PL-C001 / PL-C002 / PL-C003 / PL-C004 / PL-W007 / PL-W008 ---------

    def check_pairs(self) -> None:
        for species in sorted(self.repo.resolvers):
            resolver = self.repo.resolvers[species]
            for poser_key, model_key in reachable_pairs(resolver):
                model_asset = self.repo.models.get(model_key)
                if model_asset is None:
                    continue  # already PL-F001
                geo = self.geo_of(model_asset.entry)
                if isinstance(geo, GeoError):
                    continue  # already PL-F002/PL-F003
                if poser_key in self.repo.posers:
                    self._check_json_pair(species, poser_key, model_key, geo, model_asset)
                else:
                    namespace, _, name = poser_key.partition(":")
                    if namespace == "cobblemon" and name in self.builtin:
                        self._check_builtin_pair(species, name, model_key, geo, model_asset)

    def _check_builtin_pair(
        self,
        species: str,
        poser_name: str,
        model_key: str,
        geo: GeoModel,
        model_asset: loader.KeyedAsset,
    ) -> None:
        spec = self.builtin[poser_name]
        root = spec.get("root")
        where = f"{species} [poser cobblemon:{poser_name} + model {model_key}]"

        poser_key = f"cobblemon:{poser_name}"
        # Bone checks only make sense when the pack is implicated in the
        # pairing. The animation checks below run either way: an animation group
        # can be shadowed away even on a pairing the pack never touched, which
        # is exactly the V36 Gliscor and tonight's Decidueye.
        vanilla = self.vanilla_pair(species, poser_key, model_key)
        if vanilla:
            parts = geo.all_bones()
        elif root:
            parts = geo.builtin_parts(root)
            if parts is None:
                if self.vanilla_also_broken(species, poser_key, model_key, root):
                    self.suppress(
                        F.BUILTIN_ROOT_BONE_MISSING,
                        f"{where} root `{root}`",
                        "the jar alone produces this same pairing and lacks the bone too, so "
                        "vanilla would crash as well - the built-in extraction is wrong, not "
                        "the pack",
                    )
                    return
                self.emit(
                    F.BUILTIN_ROOT_BONE_MISSING,
                    f"{where}: `{spec['class']}` calls "
                    f"registerChildWithAllChildren(\"{root}\") but the model has no top-level "
                    f"bone `{root}` (it has: {', '.join(geo.non_locator_top_level()) or 'none'})",
                    path=model_asset.entry.path,
                    source=model_asset.entry.source.name,
                    detail={
                        "species": species,
                        "poser": f"cobblemon:{poser_name}",
                        "poser_class": spec["class"],
                        "model": model_key,
                        "missing_bone": root,
                        "top_level_bones": geo.non_locator_top_level(),
                    },
                    suggest_delete=(
                        [model_asset.entry.path] if model_asset.entry.from_pack else []
                    ),
                )
                return
        else:
            parts = geo.all_bones()

        for bone in sorted([] if vanilla else (spec.get("parts") or [])):
            if bone in parts:
                continue
            self.emit(
                F.BUILTIN_PART_MISSING,
                f"{where}: `{spec['class']}` calls getPart(\"{bone}\") but the model has no such "
                "bone",
                path=model_asset.entry.path,
                source=model_asset.entry.source.name,
                detail={
                    "species": species,
                    "poser": f"cobblemon:{poser_name}",
                    "poser_class": spec["class"],
                    "model": model_key,
                    "missing_bone": bone,
                },
                suggest_delete=[model_asset.entry.path] if model_asset.entry.from_pack else [],
            )

        for group, name in sorted(tuple(a) for a in spec.get("animations") or []):
            ref = molang.AnimRef(group, name, hard=True)
            if self._animation_exists(ref):
                continue
            if self._vanilla_animation_broken(ref):
                self.suppress(
                    F.BUILTIN_ANIMATION_MISSING,
                    f"{species} `{ref.key}`",
                    "the jar alone is missing this animation too, so vanilla behaves the same "
                    "way - the pack did not cause it",
                )
                continue
            if (species, ref.group, ref.key) in self._seen_builtin_anims:
                continue
            self._seen_builtin_anims.add((species, ref.group, ref.key))
            group_entry = self.repo.animations.get(group)
            self.emit(
                F.BUILTIN_ANIMATION_MISSING,
                f"{species}: `{spec['class']}` resolves `{ref.key}` through getAnimation(), which "
                f"throws - "
                + (
                    f"group `{group}` is provided by `{group_entry.entry.path}` and does not "
                    f"define it"
                    if group_entry
                    else f"there is no animation group `{group}` at all"
                ),
                path=group_entry.entry.path if group_entry else None,
                source=group_entry.entry.source.name if group_entry else None,
                detail={
                    "species": species,
                    "poser_class": spec["class"],
                    "group": group,
                    "animation": ref.key,
                },
            )

    def _check_json_pair(
        self,
        species: str,
        poser_key: str,
        model_key: str,
        geo: GeoModel,
        model_asset: loader.KeyedAsset,
    ) -> None:
        body = self.repo.poser_json.get(poser_key)
        if body is None:
            return
        poser_entry = self.repo.posers[poser_key].entry
        stem = poser_key.partition(":")[2]
        declared_root = body.get("rootBone") if isinstance(body.get("rootBone"), str) else None
        root = geo.json_poser_root(stem, declared_root)
        where = f"{species} [poser {poser_key} + model {model_key}]"
        # A pairing the pack did not touch is Cobblemon's own business; a pack
        # operator cannot act on it and it is more likely our extraction being
        # wrong than a real Cobblemon bug.
        if self.vanilla_pair(species, poser_key, model_key):
            return

        if root is None:
            self.emit(
                F.ROOT_BONE_UNRESOLVABLE,
                f"{where}: the model has no non-locator top-level bone, so loadJsonPoser's "
                "`.first()` throws",
                path=model_asset.entry.path,
                source=model_asset.entry.source.name,
                detail={"species": species, "poser": poser_key, "model": model_key},
            )
            return
        if declared_root and declared_root not in geo.top_level():
            self.emit(
                F.ROOT_BONE_UNRESOLVABLE,
                f"{where}: declared `rootBone: {declared_root}` is not a top-level bone; "
                f"loadJsonPoser silently falls back to `{root}`",
                path=poser_entry.path,
                source=poser_entry.source.name,
                detail={
                    "species": species,
                    "poser": poser_key,
                    "declared_root": declared_root,
                    "actual_root": root,
                },
            )

        parts = geo.json_poser_parts(root)
        for pose_name, pose in self._poses(body):
            for transformed in pose.get("transformedParts") or []:
                if not isinstance(transformed, dict):
                    continue
                bone = transformed.get("part")
                if not isinstance(bone, str) or bone in parts:
                    continue
                if self.vanilla_also_broken(species, poser_key, model_key, bone):
                    self.suppress(
                        F.POSER_PART_MISSING,
                        f"{where} pose `{pose_name}` part `{bone}`",
                        "the jar alone produces this same pairing and lacks the bone too",
                    )
                    continue
                self.emit(
                    F.POSER_PART_MISSING,
                    f"{where}: pose `{pose_name}` transforms part `{bone}`, which the model does "
                    f"not have (root bone `{root}`)",
                    path=poser_entry.path,
                    source=poser_entry.source.name,
                    detail={
                        "species": species,
                        "poser": poser_key,
                        "poser_path": poser_entry.path,
                        "model": model_key,
                        "model_path": model_asset.entry.path,
                        "pose": pose_name,
                        "missing_bone": bone,
                        "root_bone": root,
                    },
                    suggest_delete=[poser_entry.path] if poser_entry.from_pack else [],
                )
            for text in self._pose_animation_strings(pose):
                for bone in molang.bone_refs(text):
                    if bone in parts:
                        continue
                    if self.vanilla_also_broken(species, poser_key, model_key, bone):
                        continue
                    self.emit(
                        F.POSER_ANIM_BONE_MISSING,
                        f"{where}: pose `{pose_name}` animation `{text}` needs bone `{bone}`, "
                        "which the model does not have - the animation is silently dropped",
                        path=poser_entry.path,
                        source=poser_entry.source.name,
                        detail={
                            "species": species,
                            "poser": poser_key,
                            "model": model_key,
                            "pose": pose_name,
                            "missing_bone": bone,
                            "expression": text,
                        },
                    )

    # -- PL-R001 / PL-R002 / PL-R003 ---------------------------------------

    def check_residue(self) -> None:
        jar_sources = self.view.jar_sources()
        if not jar_sources:
            return
        jar_paths: set[str] = set()
        for source in jar_sources:
            jar_paths.update(source.paths())

        # PL-R001: the pack replaces a jar file at the same path.
        for entry in self.view.entries():
            if not entry.from_pack or entry.path not in jar_paths:
                continue
            owned = self.ownership.owns_path(entry.path)
            if owned:
                continue
            if not self._is_interesting(entry.path):
                continue
            self.emit(
                F.STALE_PATH_OVERRIDE,
                f"pack replaces the official `{entry.path}`",
                path=entry.path,
                source=entry.source.name,
                detail={"path": entry.path},
                suggest_delete=[entry.path],
            )

        # PL-R002 / PL-R003: the pack shadows a jar asset by loader KEY.
        jar_keys = self._jar_keys(jar_sources)
        for key, asset, kind in self._effective_keyed():
            if not asset.entry.from_pack:
                continue
            jar_path = jar_keys.get((kind, key))
            if jar_path is None or jar_path == asset.entry.path:
                continue
            if self.ownership.owns_path(asset.entry.path) or self.ownership.owns_key(key):
                continue
            self.emit(
                F.STALE_KEY_OVERRIDE,
                f"pack `{kind}` `{asset.entry.path}` shadows the official `{jar_path}` "
                f"(both key as `{key}`)",
                path=asset.entry.path,
                source=asset.entry.source.name,
                detail={"kind": kind, "key": key, "official_path": jar_path},
                suggest_delete=[asset.entry.path],
            )
            if kind == "animation":
                self._check_lost_animations(key, asset, jar_path, jar_sources)

    def _check_lost_animations(
        self, key: str, group: loader.AnimationGroup, jar_path: str, jar_sources: list[Any]
    ) -> None:
        official: set[str] = set()
        for source in jar_sources:
            if source.has(jar_path):
                try:
                    body = json.loads(source.read_text(jar_path))
                    anims = body.get("animations") if isinstance(body, dict) else None
                    if isinstance(anims, dict):
                        official = set(anims)
                except Exception:
                    return
                break
        lost = sorted(official - group.animations)
        if lost:
            self.emit(
                F.ANIMATION_GROUP_KEYS_LOST,
                f"animation group `{key}` from `{group.entry.path}` drops "
                f"{len(lost)} animation(s) the official `{jar_path}` defines: "
                + ", ".join(lost[:8])
                + (" ..." if len(lost) > 8 else ""),
                path=group.entry.path,
                source=group.entry.source.name,
                detail={"key": key, "official_path": jar_path, "lost": lost},
            )

    def _effective_keyed(self) -> Iterable[tuple[str, Any, str]]:
        for key, asset in self.repo.models.items():
            yield key, asset, "model"
        for key, asset in self.repo.posers.items():
            yield key, asset, "poser"
        for key, group in self.repo.animations.items():
            yield key, group, "animation"

    def _jar_keys(self, jar_sources: list[Any]) -> dict[tuple[str, str], str]:
        """Loader keys the jar alone would produce, and the path behind each."""
        out: dict[tuple[str, str], str] = {}
        for source in jar_sources:
            for path in source.paths():
                parts = path.split("/")
                if len(parts) < 3 or parts[0] != "assets":
                    continue
                namespace = parts[1]
                rel = "/".join(parts[2:])
                for directory, suffix, kind in KEYED_DIRECTORIES:
                    if not rel.startswith(directory + "/") or not rel.endswith(suffix):
                        continue
                    if kind == "animation":
                        key = loader._stem(rel, ".animation.json")
                    else:
                        key = f"{namespace}:{loader._stem(rel)}"
                    out[(kind, key)] = path
                    break
        return out

    @staticmethod
    def _is_interesting(path: str) -> bool:
        """Restrict PL-R001 to the asset classes the residue rule is about."""
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "assets":
            return False
        rel = "/".join(parts[2:])
        return rel.startswith("bedrock/") or rel.startswith("textures/pokemon/")


# --------------------------------------------------------------------------


def reachable_pairs(resolver: loader.Resolver) -> set[tuple[str, str]]:
    """Every (poser, model) pair `getPoser` can actually produce for a species.

    `getVariationValue` takes the LAST fitting variation that supplies the field
    (VaryingRenderableResolver.kt:64-66), and a variation fits when the state's
    aspects are a superset of its own. So a pair (poser from i, model from k) is
    reachable iff the state `aspects[i] | aspects[k]` is not also a superset of
    some LATER variation that supplies the same field.

    That is exact for aspect-only variations and conservative for
    `condition`-gated ones, which we treat as always satisfiable.
    """
    posers = [(i, v) for i, v in enumerate(resolver.variations) if v.poser]
    models = [(i, v) for i, v in enumerate(resolver.variations) if v.model]
    pairs: set[tuple[str, str]] = set()

    for pi, pv in posers:
        p_aspects = set(pv.aspects)
        for mi, mv in models:
            state = p_aspects | set(mv.aspects)
            if any(j > pi and set(v.aspects) <= state for j, v in posers):
                continue
            if any(j > mi and set(v.aspects) <= state for j, v in models):
                continue
            assert pv.poser and mv.model
            pairs.add((pv.poser, mv.model))
    return pairs


def validate(
    view: EffectiveView,
    ownership: Ownership,
    builtin_table: dict[str, Any] | None = None,
    jar_version: str | None = None,
    max_per_rule: int = 0,
) -> Result:
    table = builtin_table or builtin_mod.load_builtin(jar_version)
    return Validator(view, ownership, table, max_per_rule=max_per_rule).run()


_VERSION_IN_JAR = re.compile(r"^\s*\"?version\"?\s*[:=]\s*\"?([0-9][^\",\s]*)", re.M)


def detect_jar_version(view: EffectiveView) -> str | None:
    """Read the Cobblemon version out of the jar's fabric.mod.json."""
    for source in view.jar_sources():
        if not source.has("fabric.mod.json"):
            continue
        try:
            body = json.loads(source.read_text("fabric.mod.json"))
        except Exception:
            continue
        version = body.get("version")
        if isinstance(version, str):
            return version
    return None
