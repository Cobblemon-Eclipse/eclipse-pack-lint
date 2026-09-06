"""Which pack assets are Eclipse-owned (and therefore not residue).

The whole point of the residue class is to answer "what in our pack is now
Cobblemon's job?". That answer changes every Cobblemon release, and the exempt
set (radiant/gilded/plushie/cosmetic/ZA-mega/skin content we author) changes
every time we ship a feature - so it lives in an operator-editable JSON file,
never in the code.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "data", "eclipse-owned.json")


@dataclass
class Ownership:
    #: Whole namespaces we author. Nothing under them is ever residue.
    namespaces: set[str] = field(default_factory=set)
    #: Substrings that mark an Eclipse aspect when they appear in a filename or
    #: in a resolver's `aspects` list (radiant, gilded, plushie, ...).
    aspects: list[str] = field(default_factory=list)
    #: Glob patterns over the full archive path.
    path_globs: list[str] = field(default_factory=list)
    #: Species we deliberately fork (`cobblemon:pikachu`, or bare `pikachu`).
    species: set[str] = field(default_factory=set)
    #: Exact loader keys we own (`cobblemon:eclipse_hat.geo`).
    keys: set[str] = field(default_factory=set)
    source: str | None = None

    def owns_path(self, path: str) -> str | None:
        """Return the reason this path is Eclipse-owned, or None."""
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "assets" and parts[1] in self.namespaces:
            return f"namespace `{parts[1]}`"
        for glob in self.path_globs:
            if fnmatch.fnmatch(path, glob):
                return f"path glob `{glob}`"
        lowered = os.path.basename(path).lower()
        for aspect in self.aspects:
            if aspect.lower() in lowered:
                return f"aspect `{aspect}`"
        return None

    def owns_key(self, key: str) -> str | None:
        if key in self.keys:
            return f"key `{key}`"
        bare = key.split(":", 1)[-1].lower()
        for aspect in self.aspects:
            if aspect.lower() in bare:
                return f"aspect `{aspect}`"
        return None

    def owns_species(self, species: str) -> str | None:
        if species in self.species or species.split(":", 1)[-1] in self.species:
            return f"species `{species}`"
        return None

    def owns_aspects(self, aspects: list[str]) -> str | None:
        for value in aspects:
            low = value.lower()
            for aspect in self.aspects:
                if aspect.lower() in low:
                    return f"aspect `{value}`"
        return None


def load(path: str | None) -> Ownership:
    path = path or DEFAULT_CONFIG
    if not os.path.isfile(path):
        return Ownership(source=None)
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw: dict[str, Any] = json.load(fh)
    return Ownership(
        namespaces=set(raw.get("namespaces") or []),
        aspects=list(raw.get("aspects") or []),
        path_globs=list(raw.get("path_globs") or []),
        species=set(raw.get("species") or []),
        keys=set(raw.get("keys") or []),
        source=path,
    )
