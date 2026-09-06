"""Resource sources and the effective (overlaid) file view.

Minecraft resolves a resource by asking each pack in priority order and taking
the first hit; the mod jar is the lowest-priority pack. `EffectiveView` models
exactly that: `jar` first, then every `--include-dir` zip, then every `--pack`
in the order given, later winning.
"""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass
from typing import Iterable, Iterator

#: Roles a source can play. `residue` detection only looks at pack-role sources.
ROLE_JAR = "jar"
ROLE_PACK = "pack"
ROLE_INCLUDE = "include"


class Source:
    """A readable bundle of resource-pack entries, keyed by archive path."""

    def __init__(self, name: str, role: str) -> None:
        self.name = name
        self.role = role
        self._entries: dict[str, object] = {}

    # -- reading -----------------------------------------------------------

    def paths(self) -> Iterable[str]:
        return self._entries.keys()

    def has(self, path: str) -> bool:
        return path in self._entries

    def read(self, path: str) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        """Release any OS handle. Safe to call more than once."""

    def read_text(self, path: str) -> str:
        raw = self.read(path)
        # Packs authored on Windows show up as UTF-8-with-BOM often enough that
        # a bare utf-8 decode would report a bogus JSON syntax error.
        return raw.decode("utf-8-sig", errors="replace")

    def read_json(self, path: str) -> object:
        return json.loads(self.read_text(path))

    def __repr__(self) -> str:
        return f"<Source {self.role}:{self.name} {len(self._entries)} entries>"


class ZipSource(Source):
    def __init__(self, path: str, role: str, name: str | None = None) -> None:
        super().__init__(name or os.path.basename(path), role)
        self.path = path
        self._zip = zipfile.ZipFile(path)
        for info in self._zip.infolist():
            if info.is_dir():
                continue
            self._entries[info.filename.replace("\\", "/")] = info

    def read(self, path: str) -> bytes:
        return self._zip.read(self._entries[path])  # type: ignore[arg-type]

    def size(self, path: str) -> int:
        return self._entries[path].file_size  # type: ignore[union-attr]

    def close(self) -> None:
        self._zip.close()


class DirSource(Source):
    """An unpacked pack directory (handy for tests and for pre-zip linting)."""

    def __init__(self, path: str, role: str, name: str | None = None) -> None:
        super().__init__(name or os.path.basename(os.path.abspath(path)), role)
        self.path = path
        for root, _dirs, files in os.walk(path):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, path).replace("\\", "/")
                self._entries[rel] = full

    def read(self, path: str) -> bytes:
        with open(self._entries[path], "rb") as fh:  # type: ignore[arg-type]
            return fh.read()

    def size(self, path: str) -> int:
        return os.path.getsize(self._entries[path])  # type: ignore[arg-type]


def open_source(path: str, role: str) -> Source:
    if os.path.isdir(path):
        return DirSource(path, role)
    return ZipSource(path, role)


def open_include_dir(path: str) -> list[Source]:
    """Simulate polymer's merge of the include zips in a `polymer/` directory.

    polymer merges every include zip into one generated pack; later includes win
    on a path collision. We reproduce that as an ordered list of sources sorted
    by filename, which is the order polymer walks the directory in. Linting the
    include dir BEFORE `polymer generate-pack` is the cheap gate - it catches a
    bad include without a server round trip.
    """
    if not os.path.isdir(path):
        raise ValueError(f"--include-dir is not a directory: {path}")
    zips = sorted(
        f for f in os.listdir(path) if f.lower().endswith(".zip")
    )
    return [ZipSource(os.path.join(path, f), ROLE_INCLUDE) for f in zips]


@dataclass
class Entry:
    """One resolved file in the effective view."""

    path: str
    source: Source
    #: Priority index; higher wins.
    priority: int

    @property
    def from_pack(self) -> bool:
        return self.source.role != ROLE_JAR

    def read_text(self) -> str:
        return self.source.read_text(self.path)

    def read(self) -> bytes:
        return self.source.read(self.path)


class EffectiveView:
    """The merged file map the client would actually see."""

    def __init__(self, sources: list[Source]) -> None:
        #: In priority order, lowest first.
        self.sources = sources
        self._effective: dict[str, Entry] = {}
        #: Every source that provides a path, lowest priority first.
        self._all: dict[str, list[Entry]] = {}
        for priority, source in enumerate(sources):
            for path in source.paths():
                entry = Entry(path, source, priority)
                self._all.setdefault(path, []).append(entry)
                self._effective[path] = entry

    def get(self, path: str) -> Entry | None:
        return self._effective.get(path)

    def has(self, path: str) -> bool:
        return path in self._effective

    def all_versions(self, path: str) -> list[Entry]:
        return self._all.get(path, [])

    def entries(self) -> Iterator[Entry]:
        return iter(self._effective.values())

    def close(self) -> None:
        """Close every underlying archive. Call when the run is done - Windows
        will not delete a zip that still has an open handle."""
        for source in self.sources:
            source.close()

    def jar_sources(self) -> list[Source]:
        return [s for s in self.sources if s.role == ROLE_JAR]

    def pack_sources(self) -> list[Source]:
        return [s for s in self.sources if s.role != ROLE_JAR]

    # -- asset-path helpers ------------------------------------------------

    def assets(self) -> Iterator[tuple[str, str, Entry]]:
        """Yield (namespace, asset-relative path, entry) for every assets/ file."""
        for path, entry in self._effective.items():
            parts = path.split("/")
            if len(parts) < 3 or parts[0] != "assets":
                continue
            yield parts[1], "/".join(parts[2:]), entry

    def under(self, namespace_dir: str) -> Iterator[tuple[str, str, Entry]]:
        """Yield assets under a directory prefix, recursively.

        Mirrors `ResourceManager.listResources(dir, filter)`, which is recursive
        across every namespace.
        """
        prefix = namespace_dir.rstrip("/") + "/"
        for ns, rel, entry in self.assets():
            if rel.startswith(prefix):
                yield ns, rel, entry
