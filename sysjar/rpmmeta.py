"""RPM Provides and Requires for one system jar.

The mvn() name is the one already used by cooker packages, so a new jar
satisfies existing BuildRequires.
"""

from __future__ import annotations

import json
from pathlib import Path

from sysjar.model import Coord, Dependency


def capability(coord: Coord, namespace: str = "mvn") -> str:
    extension = coord.extension or "jar"
    classifier = coord.classifier or ""
    if extension == "jar":
        extension = ""
    if not extension and not classifier:
        return f"{namespace}({coord.group}:{coord.artifact})"
    return f"{namespace}({coord.group}:{coord.artifact}:{extension}:{classifier}:)"


def provides_for(data: dict) -> list[str]:
    version = data.get("version") or ""
    coords = [_coord_from_record(data)]
    for alias in data.get("aliases") or []:
        if alias.get("groupId") and alias.get("artifactId"):
            coords.append(Coord(
                alias["groupId"],
                alias["artifactId"],
                alias.get("classifier") or "",
                alias.get("extension") or data.get("extension") or "jar",
            ))
    lines = []
    seen = set()
    for coord in coords:
        for namespace in ("mvn", "sysjar"):
            cap = capability(coord, namespace)
            line = f"{cap} = {version}" if version else cap
            if line not in seen:
                seen.add(line)
                lines.append(line)
    return lines


def requires_for(data: dict) -> list[str]:
    lines = []
    seen = set()
    for item in data.get("dependencies") or []:
        dep = Dependency.from_json(item)
        if not dep.coord.group or not dep.coord.artifact:
            continue
        if dep.optional:
            continue
        if dep.scope in {"test", "provided", "system", "import"}:
            continue
        cap = capability(dep.coord, "mvn")
        if cap not in seen:
            seen.add(cap)
            lines.append(cap)
    return lines


def _coord_from_record(data: dict) -> Coord:
    return Coord(
        data.get("groupId", ""),
        data.get("artifactId", ""),
        data.get("classifier") or "",
        data.get("extension") or "jar",
    )


def load_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def open_packaged(path: str) -> Path:
    """Open a packaged path, preferring RPM_BUILD_ROOT during a build."""
    import os
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    root = os.environ.get("RPM_BUILD_ROOT", "")
    if root:
        prefixed = Path(root) / path.lstrip("/")
        if prefixed.is_file():
            return prefixed
    return candidate
