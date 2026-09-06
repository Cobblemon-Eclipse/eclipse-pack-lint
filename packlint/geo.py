"""Bedrock .geo.json parsing, reproducing TexturedModel's success/failure modes.

The point of this module is not to bake a model - it is to answer two questions
the way Cobblemon answers them:

1. Would `TexturedModel.from(json)` + `.create()` succeed? If not, the model is
   absent from `texturedModels` (or the whole load aborts).
2. What bone names does the baked model expose, and under which root?
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: LocatorAccess.PREFIX - locators are baked in as empty bones with this prefix.
#: blockbench/LocatorAccess.kt:36
LOCATOR_PREFIX = "internal_locator__"


class GeoError(Exception):
    """A structural fault. `hard` = create() throws and registerModels aborts."""

    def __init__(self, message: str, hard: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.hard = hard


@dataclass
class GeoModel:
    """The bone tree a geo file bakes into."""

    #: bone name -> parent bone name (None for top level)
    parents: dict[str, str | None] = field(default_factory=dict)
    #: parent bone name (or None) -> child bone names, in declaration order
    children: dict[str | None, list[str]] = field(default_factory=dict)
    identifier: str | None = None

    def top_level(self) -> list[str]:
        """Direct children of the baked root part (`modelData.root`)."""
        return list(self.children.get(None, []))

    def non_locator_top_level(self) -> list[str]:
        return [b for b in self.top_level() if LOCATOR_PREFIX not in b]

    def descendants(self, bone: str) -> set[str]:
        out: set[str] = set()
        stack = list(self.children.get(bone, []))
        while stack:
            b = stack.pop()
            if b in out:
                continue
            out.add(b)
            stack.extend(self.children.get(b, []))
        return out

    def all_bones(self) -> set[str]:
        return set(self.parents)

    # -- the two views Cobblemon actually uses -----------------------------

    def builtin_parts(self, root_bone: str) -> set[str] | None:
        """`relevantPartsByName` for a built-in poser.

        `registerChildWithAllChildren(name)` registers `name` itself and every
        descendant (PosableModel.kt:381-386 + :404-411). Returns None when the
        root bone is not a TOP-LEVEL bone - `ModelPart.getChild` only looks at
        direct children of the baked root, so that is the PL-C002 crash.
        """
        if root_bone not in self.top_level():
            return None
        return {root_bone} | self.descendants(root_bone)

    def json_poser_root(self, poser_file_stem: str, declared_root: str | None) -> str | None:
        """Reproduce loadJsonPoser's root-bone pick.

        VaryingModelRepository.kt:156-157:
            rootBone field (if it names a top-level child)
            -> top-level child named after the poser file
            -> first non-locator top-level child
        """
        top = self.top_level()
        if declared_root and declared_root in top:
            return declared_root
        if poser_file_stem in top:
            return poser_file_stem
        non_locator = self.non_locator_top_level()
        if non_locator:
            return non_locator[0]
        return None  # `.first()` would throw NoSuchElementException

    def json_poser_parts(self, root_bone: str) -> set[str]:
        """`relevantPartsByName` for a JSON poser.

        JsonModelAdapter.kt:31 registers the root bone under the literal name
        `__root` and then every DESCENDANT under its own name. The root bone's
        own name is deliberately not included - that difference from the
        built-in path is a real source of PL-C001s.
        """
        return {"__root"} | self.descendants(root_bone)


def _is_box_uv(uv: object) -> bool:
    """Cobblemon's `Cube.uv` is `List<Int>?` - per-face UV objects fail Gson.

    TexturedModel.kt:339. A per-face `uv` makes `from()` return null, so the
    model silently never registers (PL-F002).
    """
    if uv is None:
        return True
    return isinstance(uv, list)


def parse_geo(text: str) -> GeoModel:
    """Parse a geo the way Cobblemon does, raising GeoError where it would fail.

    `hard=False` -> `TexturedModel.from` returns null: model absent, warn logged.
    `hard=True`  -> `create()` throws out of registerModels: nothing loads.
    """
    try:
        doc = json.loads(text)
    except Exception as exc:  # Gson is lenient, but not this lenient.
        raise GeoError(f"invalid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise GeoError("top level is not a JSON object")

    geometry = doc.get("minecraft:geometry")
    if geometry is None:
        # Gson leaves `geometry` null; `from()` succeeds, `create()` then does
        # `this.geometry!![0]` -> NPE, caught and rethrown as
        # IllegalArgumentException out of registerModels.
        raise GeoError("no `minecraft:geometry` array", hard=True)
    if not isinstance(geometry, list) or not geometry:
        raise GeoError("`minecraft:geometry` is empty or not an array", hard=True)

    geo = geometry[0]
    if not isinstance(geo, dict):
        raise GeoError("`minecraft:geometry[0]` is not an object", hard=True)

    description = geo.get("description")
    if not isinstance(description, dict):
        # `lateinit var description` -> UninitializedPropertyAccessException
        # inside create()'s try, rethrown as IllegalArgumentException.
        raise GeoError("`description` block missing", hard=True)

    bones = geo.get("bones")
    if bones is None:
        raise GeoError("no `bones` array", hard=True)
    if not isinstance(bones, list):
        raise GeoError("`bones` is not an array", hard=True)

    model = GeoModel(identifier=description.get("identifier"))
    declared: set[str] = set()

    for raw in bones:
        if not isinstance(raw, dict):
            raise GeoError("`bones` contains a non-object entry", hard=True)
        name = raw.get("name")
        if not isinstance(name, str):
            raise GeoError("a bone has no string `name`", hard=True)

        for cube in raw.get("cubes") or []:
            if not isinstance(cube, dict):
                raise GeoError(f"bone `{name}` has a non-object cube")
            if not _is_box_uv(cube.get("uv")):
                raise GeoError(
                    f"bone `{name}` uses per-face UV; Cobblemon geo must be BOX UV "
                    "(TexturedModel.Cube.uv is List<Int>?)"
                )

        parent = raw.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise GeoError(f"bone `{name}` has a non-string `parent`", hard=True)
        if parent is not None and parent not in declared:
            # TexturedModel.kt:182 `parts[bone.parent]!!` - the parent must
            # already have been built, so declaration order matters.
            raise GeoError(
                f"bone `{name}` declares parent `{parent}` which is not declared "
                "earlier in the bones array",
                hard=True,
            )

        declared.add(name)
        model.parents[name] = parent
        model.children.setdefault(parent, []).append(name)
        model.children.setdefault(name, model.children.get(name, []))

        # Locators bake into empty child bones named internal_locator__<name>.
        # TexturedModel.kt:166-178.
        locators = raw.get("locators")
        if isinstance(locators, dict):
            for loc in locators:
                loc_name = LOCATOR_PREFIX + loc
                declared.add(loc_name)
                model.parents[loc_name] = name
                model.children.setdefault(name, []).append(loc_name)
                model.children.setdefault(loc_name, [])

    # ...plus a synthetic top-level `internal_locator__root`.
    root_locator = LOCATOR_PREFIX + "root"
    model.parents[root_locator] = None
    model.children.setdefault(None, []).append(root_locator)
    model.children.setdefault(root_locator, [])

    return model
