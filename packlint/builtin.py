"""Built-in (Kotlin) poser requirements.

Cobblemon registers a few hundred posers in Kotlin rather than JSON
(`VaryingModelRepository.registerInBuiltPosers`, 339 of them in 1.8.0). Those
classes hard-require bones by name in their constructors, and a pack that
overrides the geo for one of those species without those bones crashes the
client at render time.

Bytecode does not carry `getPart("head")` in a form worth chasing (the string
constants are there but the call graph is not), so we extract the requirements
from the Kotlin source once per Cobblemon version with

    python -m packlint extract-builtin --source <cobblemon source dir> \\
        --out packlint/data/builtin_bones_<version>.json

and ship the generated file. See README, "Updating for a new Cobblemon version".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from packlint import molang

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

#: `inbuilt("azure_ball", ::PokeBallModel)` - VaryingModelRepository.kt:171-561.
_INBUILT = re.compile(r"""inbuilt\(\s*["']([^"']+)["']\s*,\s*::\s*(\w+)""")
#: `override val rootPart = root.registerChildWithAllChildren("sandslash")`
_ROOT_PART = re.compile(r"""root\.registerChildWithAllChildren\(\s*["']([^"']+)["']""")
_ANY_CHILD = re.compile(r"""registerChildWithAllChildren\(\s*["']([^"']+)["']""")
#: `getPart("head")` - PosableModel.kt:393, a `!!` lookup.
_GET_PART = re.compile(r"""(?<![\w.])getPart\(\s*["']([^"']+)["']\s*\)""")
#: `bedrock("sandslash", "ground_idle")` / `bedrockStateful(...)` - these THROW
#: on a miss (PosableModel.kt:824-836).
_BEDROCK_CALL = re.compile(
    r"""\bbedrock(?:Stateful)?\(\s*(?:animationGroup\s*=\s*)?["']([^"']+)["']\s*,"""
    r"""\s*(?:animation\s*=\s*)?["']([^"']+)["']"""
)
_CLASS_DECL = re.compile(r"""^\s*(?:open\s+)?class\s+(\w+)\s*\([^)]*\)\s*:\s*(\w+)""", re.M)


@dataclass
class BuiltinPoser:
    name: str
    cls: str
    root_bone: str | None = None
    parts: set[str] = field(default_factory=set)
    #: (group, animation) pairs resolved through `getAnimation`, which throws.
    animations: set[tuple[str, str]] = field(default_factory=set)

    def to_json(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "root": self.root_bone,
            "parts": sorted(self.parts),
            "animations": sorted([list(a) for a in self.animations]),
        }


def strip_comments(text: str) -> str:
    """Remove Kotlin comments, preserving string literals.

    Cobblemon's model classes are full of commented-out `getFaintAnimation`
    overrides that still reference `bedrockStateful("x", "faint")`. Reading
    those produced 240 phantom PL-C004s on the first run, so comment stripping
    is load-bearing, not cosmetic.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                end = n if end == -1 else end + 3
                out.append(text[i:end])
                i = end
                continue
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if ch == "'" and i + 1 < n:
            j = i + 1
            while j < n and text[j] != "'":
                if text[j] == "\\":
                    j += 1
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if text.startswith("/*", i):
            depth = 1
            j = i + 2
            while j < n and depth:  # Kotlin block comments nest.
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _kotlin_files(source_dir: str) -> dict[str, str]:
    """Map class name -> comment-stripped text for every Kotlin file."""
    out: dict[str, str] = {}
    for root, _dirs, files in os.walk(source_dir):
        for f in files:
            if f.endswith(".kt"):
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        out[f[:-3]] = strip_comments(fh.read())
                except OSError:
                    continue
    return out


def _requirements(cls: str, files: dict[str, str], seen: set[str]) -> BuiltinPoser:
    """Collect a class's bone/animation requirements, following its superclass."""
    poser = BuiltinPoser(name=cls, cls=cls)
    if cls in seen or cls not in files:
        return poser
    seen.add(cls)
    text = files[cls]

    root_match = _ROOT_PART.search(text)
    if root_match:
        poser.root_bone = root_match.group(1)
    poser.parts.update(_ANY_CHILD.findall(text))
    poser.parts.update(_GET_PART.findall(text))
    for group, name in _BEDROCK_CALL.findall(text):
        poser.animations.add((group, name))

    # MoLang strings inside the class (animations["physical"] = "q.bedrock_primary(...)")
    # resolve lazily and are swallowed on a miss, so they are only WARN material;
    # they are recorded so the report can mention them, not as crash claims.
    for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', text):
        for ref in molang.animation_refs(literal):
            if ref.hard:
                poser.animations.add((ref.group, ref.name))

    # Follow `class X(root: ModelPart) : YModel(root)` up the chain.
    for decl_cls, superclass in _CLASS_DECL.findall(text):
        if decl_cls != cls:
            continue
        if superclass.endswith("Model") and superclass in files:
            parent = _requirements(superclass, files, seen)
            poser.parts.update(parent.parts)
            poser.animations.update(parent.animations)
            if poser.root_bone is None:
                poser.root_bone = parent.root_bone
    return poser


def extract(source_dir: str) -> dict[str, Any]:
    """Parse `registerInBuiltPosers` and every model class it names."""
    files = _kotlin_files(source_dir)
    repo_text = files.get("VaryingModelRepository")
    if repo_text is None:
        raise SystemExit(
            f"VaryingModelRepository.kt not found under {source_dir!r} - point --source at the "
            "Cobblemon source tree (the directory containing common/src/main/kotlin/...)"
        )

    posers: dict[str, Any] = {}
    for name, cls in _INBUILT.findall(repo_text):
        req = _requirements(cls, files, set())
        req.name = name
        # A duplicate `inbuilt(...)` name is legal - the later call wins.
        posers[name] = req.to_json()

    version = _detect_version(source_dir)
    return {
        "cobblemon_version": version,
        "extracted_from": os.path.abspath(source_dir),
        "poser_count": len(posers),
        "posers": posers,
    }


def _detect_version(source_dir: str) -> str:
    """Walk up looking for the Cobblemon repo's `gradle.properties`."""
    current = os.path.abspath(source_dir)
    for _ in range(12):
        path = os.path.join(current, "gradle.properties")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                m = re.search(r"^mod_version\s*=\s*(\S+)", fh.read(), re.M)
                if m:
                    return m.group(1)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return "unknown"


# --------------------------------------------------------------------------


def available() -> list[str]:
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(
        f for f in os.listdir(DATA_DIR) if f.startswith("builtin_bones_") and f.endswith(".json")
    )


def load_builtin(version: str | None = None, path: str | None = None) -> dict[str, Any]:
    """Load the shipped built-in table, preferring an exact version match.

    Falls back to the newest shipped table when the jar's version has no file of
    its own; the report says which table was used so a version drift is visible
    rather than silent.
    """
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    files = available()
    if not files:
        return {"cobblemon_version": "none", "posers": {}, "source_file": None}

    chosen = None
    if version:
        exact = f"builtin_bones_{version}.json"
        if exact in files:
            chosen = exact
        else:
            # `1.8.0+1.21.1` should still match `builtin_bones_1.8.0.json`.
            base = version.split("+")[0]
            for f in files:
                if f == f"builtin_bones_{base}.json":
                    chosen = f
                    break
    if chosen is None:
        chosen = files[-1]

    with open(os.path.join(DATA_DIR, chosen), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["source_file"] = chosen
    return data
