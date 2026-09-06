"""Tiny synthetic jar/pack builders.

Every fixture is a handful of files, so a test can state exactly which loader
rule it exercises. Nothing here touches the real 150MB Cobblemon jar - the
regression tests against the real corpus are opt-in (see test_regression.py).
"""

from __future__ import annotations

import json
import os
import zipfile
from typing import Any


def geo(root: str, bones: list[str], *, parent_of: dict[str, str] | None = None,
        identifier: str | None = None, per_face_uv: bool = False) -> str:
    """A minimal, valid Bedrock geo with `root` on top and `bones` under it."""
    parent_of = parent_of or {}
    entries: list[dict[str, Any]] = [{"name": root, "pivot": [0, 0, 0]}]
    for bone in bones:
        cube: dict[str, Any] = {
            "origin": [0, 0, 0],
            "size": [1, 1, 1],
            "uv": {"north": {"uv": [0, 0], "uv_size": [1, 1]}} if per_face_uv else [0, 0],
        }
        entries.append(
            {
                "name": bone,
                "parent": parent_of.get(bone, root),
                "pivot": [0, 0, 0],
                "cubes": [cube],
            }
        )
    return json.dumps(
        {
            "format_version": "1.12.0",
            "minecraft:geometry": [
                {
                    "description": {
                        "identifier": identifier or f"geometry.{root}",
                        "texture_width": 64,
                        "texture_height": 64,
                        "visible_bounds_width": 2,
                        "visible_bounds_height": 2,
                        "visible_bounds_offset": [0, 0, 0],
                    },
                    "bones": entries,
                }
            ],
        },
        indent=1,
    )


def poser(poses: dict[str, Any], root_bone: str | None = None) -> str:
    body: dict[str, Any] = {"poses": poses}
    if root_bone:
        body["rootBone"] = root_bone
    return json.dumps(body, indent=1)


def pose(
    pose_types: list[str] | None = None,
    transformed: list[str] | None = None,
    animations: list[str] | None = None,
    quirks: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"poseTypes": pose_types or ["STAND"]}
    if transformed:
        out["transformedParts"] = [{"part": p, "isVisible": False} for p in transformed]
    if animations:
        out["animations"] = animations
    if quirks:
        out["quirks"] = quirks
    return out


def resolver(species: str, variations: list[dict[str, Any]], order: int = 0) -> str:
    return json.dumps({"species": species, "order": order, "variations": variations}, indent=1)


def animation_group(group: str, names: list[str]) -> str:
    return json.dumps(
        {
            "format_version": "1.8.0",
            "animations": {
                f"animation.{group}.{name}": {"loop": True, "animation_length": 1.0}
                for name in names
            },
        },
        indent=1,
    )


def write_zip(path: str, files: dict[str, str]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def a(namespace: str, rel: str) -> str:
    return f"assets/{namespace}/{rel}"


def minimal_jar() -> dict[str, str]:
    """A jar with one species (`nully`) wired end to end, plus fabric.mod.json.

    Every fixture layers on top of this so that "the jar is fine, the pack broke
    it" is the default shape - which is the shape every real finding has.
    """
    return {
        "fabric.mod.json": json.dumps({"id": "cobblemon", "version": "1.8.0+1.21.1"}),
        a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json"): geo(
            "nully", ["head", "ball", "leg_left", "leg_right"]
        ),
        a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): poser(
            {"standing": pose(["STAND"], transformed=["ball"],
                              animations=["q.bedrock('nully', 'ground_idle')"])}
        ),
        a("cobblemon", "bedrock/pokemon/animations/0001_nully/nully.animation.json"):
            animation_group("nully", ["ground_idle", "cry", "blink"]),
        a("cobblemon", "bedrock/pokemon/resolvers/0001_nully/0_nully.json"): resolver(
            "cobblemon:nully",
            [
                {
                    "aspects": [],
                    "poser": "cobblemon:nully",
                    "model": "cobblemon:nully.geo",
                    "texture": "cobblemon:textures/pokemon/nully/nully.png",
                }
            ],
        ),
        a("cobblemon", "textures/pokemon/nully/nully.png"): "PNG",
    }
