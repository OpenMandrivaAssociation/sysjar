"""Coordinates and the system jar index."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class SysjarError(Exception):
    """A problem the user can fix by packaging a jar or adjusting the build."""


JAR_LIKE = ("jar", "bundle", "maven-plugin", "eclipse-plugin", "ejb")

@dataclass(frozen=True)
class Coord:
    group: str
    artifact: str
    classifier: str = ""
    extension: str = "jar"

    def key(self) -> tuple[str, str, str, str]:
        return (self.group, self.artifact, self.classifier or "", self.extension or "jar")

    def ga(self) -> str:
        return f"{self.group}:{self.artifact}"

    def display(self) -> str:
        ext = "" if self.extension in ("", "jar") else f"@{self.extension}"
        clas = f":{self.classifier}" if self.classifier else ""
        return f"{self.group}:{self.artifact}{clas}{ext}"


def parse_coord(text: str) -> tuple[Coord, str]:
    """Parse g:a, g:a:version, g:a:version:classifier, g:a:version:type:classifier, or g:a@version."""
    raw = text.strip()
    if not raw:
        raise SysjarError("empty coordinate")
    extension = "jar"
    if "@" in raw:
        raw, extension = raw.rsplit("@", 1)
        if not extension:
            extension = "jar"
    parts = raw.split(":")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise SysjarError(f"coordinate needs groupId:artifactId: {text}")
    group, artifact = parts[0], parts[1]
    version = ""
    classifier = ""
    if len(parts) == 3:
        version = parts[2]
    elif len(parts) == 4:
        version, classifier = parts[2], parts[3]
    elif len(parts) == 5:
        version = parts[2]
        if parts[3]:
            extension = parts[3]
        classifier = parts[4]
    else:
        if len(parts) != 2:
            raise SysjarError(f"cannot parse coordinate: {text}")
    return Coord(group, artifact, classifier, extension or "jar"), version


def version_key(version: str) -> tuple:
    """Rough Maven-ish ordering. Used only when two packages claim one coordinate."""
    parts = re.split(r"[.\-+_]", version or "")
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            # Qualifiers sort before a release that omits them only when compared
            # as a trailing tag. A shorter release still loses to a longer one
            # here; packagers should not ship two builds of the same coordinate.
            key.append((1, part))
    return tuple(key)


def _upstream_version(version: str) -> str:
    if version.endswith(".MBI"):
        return version[:-4]
    return version


def _closest(alts: list[Artifact], requested: str) -> Artifact:
    """Pick the bootstrap jar whose real version is the one the build asked for."""
    wanted = _upstream_version(requested)
    exact = [art for art in alts if _upstream_version(art.version) == wanted]
    if exact:
        return exact[-1]
    wanted_key = version_key(wanted)
    below = [art for art in alts if version_key(_upstream_version(art.version)) <= wanted_key]
    pool = below or list(alts)
    return max(pool, key=lambda art: version_key(_upstream_version(art.version)))


def groovy_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def require_safe_filename(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise SysjarError(f"unsafe file name: {name!r}")
    return name


@dataclass
class Dependency:
    coord: Coord
    version: str = ""
    scope: str = "compile"
    optional: bool = False

    def to_json(self) -> dict:
        return {
            "groupId": self.coord.group,
            "artifactId": self.coord.artifact,
            "version": self.version,
            "classifier": self.coord.classifier,
            "type": self.coord.extension or "jar",
            "scope": self.scope or "compile",
            "optional": self.optional,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Dependency":
        return cls(
            Coord(
                data.get("groupId", ""),
                data.get("artifactId", ""),
                data.get("classifier") or "",
                data.get("type") or data.get("extension") or "jar",
            ),
            version=data.get("version") or data.get("requestedVersion") or "",
            scope=data.get("scope") or "compile",
            optional=bool(data.get("optional")),
        )


@dataclass
class Artifact:
    coord: Coord
    version: str
    path: str = ""
    pom: str = ""
    aliases: tuple[Coord, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    priority: int = 1

    def to_json(self) -> dict:
        data = {
            "sysjar": 1,
            "groupId": self.coord.group,
            "artifactId": self.coord.artifact,
            "version": self.version,
            "extension": self.coord.extension or "jar",
            "classifier": self.coord.classifier,
            "aliases": [
                {
                    "groupId": alias.group,
                    "artifactId": alias.artifact,
                    "classifier": alias.classifier,
                    "extension": alias.extension or "jar",
                }
                for alias in self.aliases
            ],
            "dependencies": [dep.to_json() for dep in self.dependencies],
        }
        if self.path:
            data["jar" if (self.coord.extension or "jar") in JAR_LIKE else "path"] = self.path
        if self.pom:
            data["pom"] = self.pom
        if (self.coord.extension or "jar") == "pom" and self.path and "pom" not in data:
            data["pom"] = self.path
        return data


@dataclass
class Index:
    by_key: dict[tuple[str, str, str, str], Artifact] = field(default_factory=dict)
    alias: dict[tuple[str, str, str, str], tuple[str, str, str, str]] = field(default_factory=dict)
    # Every priority-0 version. A bootstrap tree can ship two builds of one
    # coordinate; a real package still replaces all of them.
    alternates: dict[tuple[str, str, str, str], list[Artifact]] = field(default_factory=dict)

    def add(self, art: Artifact) -> None:
        if not art.coord.group or not art.coord.artifact or not art.version:
            raise SysjarError(f"artifact needs group, artifact and version: {art}")
        key = art.coord.key()
        if art.priority == 0:
            kept = [old for old in self.alternates.get(key, []) if old.version != art.version]
            kept.append(art)
            self.alternates[key] = kept
        old = self.by_key.get(key)
        if old is not None:
            better = (art.priority, version_key(art.version)) >= (old.priority, version_key(old.version))
            if not better:
                return
            self.alias = {k: v for k, v in self.alias.items() if v != key}
        self.by_key[key] = art
        for alias in art.aliases:
            if alias.key() == key:
                continue
            self.alias[alias.key()] = key

    def artifacts(self) -> list[Artifact]:
        return sorted(self.by_key.values(), key=lambda art: (art.coord.group, art.coord.artifact, art.coord.classifier, art.coord.extension, art.version))

    def publishable(self) -> list[Artifact]:
        """Winners, plus every other bootstrap version when no real package took the coordinate."""
        chosen: list[Artifact] = list(self.by_key.values())
        have = {(art.coord.key(), art.version) for art in chosen}
        for key, alts in self.alternates.items():
            winner = self.by_key.get(key)
            if winner is None or winner.priority != 0:
                continue
            for alt in alts:
                mark = (alt.coord.key(), alt.version)
                if mark not in have:
                    chosen.append(alt)
                    have.add(mark)
        return sorted(chosen, key=lambda art: (art.coord.group, art.coord.artifact, art.coord.classifier, art.coord.extension, version_key(art.version)))

    def _lookup(self, key: tuple[str, str, str, str]) -> Artifact | None:
        if key in self.by_key:
            return self.by_key[key]
        canonical = self.alias.get(key)
        if canonical is not None:
            return self.by_key.get(canonical)
        return None

    def find(self, group: str, artifact: str, classifier: str = "", extension: str = "jar", requested: str = "") -> Artifact | None:
        classifier = classifier or ""
        extension = extension or "jar"
        found = self._lookup((group, artifact, classifier, extension))
        if found is None and (extension in JAR_LIKE or extension == "jar"):
            for ext in JAR_LIKE:
                found = self._lookup((group, artifact, classifier, ext))
                if found is not None:
                    break
        if found is None:
            return None
        return self._prefer_requested(found, requested)

    def find_pom(self, group: str, artifact: str, requested: str = "") -> Artifact | None:
        found = self._lookup((group, artifact, "", "pom"))
        if found is None:
            return self.find(group, artifact, "", "jar", requested)
        return self._prefer_requested(found, requested)

    def _prefer_requested(self, found: Artifact, requested: str) -> Artifact:
        """A packaged jar wins. Bootstrap jars can be several versions; use the one the build asked for."""
        if found.priority != 0 or not requested or requested[0] in "[($" :
            return found
        alts = self.alternates.get(found.coord.key()) or [found]
        if len(alts) < 2:
            return found
        return _closest(alts, requested)

    def version_map(self) -> dict[str, str]:
        """group:artifact -> system version. An unclassified jar outranks a POM or a classified side artifact."""
        ranked: dict[str, tuple[int, str]] = {}

        def put(group: str, artifact: str, version: str, rank: int) -> None:
            key = f"{group}:{artifact}"
            current = ranked.get(key)
            if current is None or rank > current[0]:
                ranked[key] = (rank, version)

        for art in self.artifacts():
            extension = art.coord.extension or "jar"
            # A bootstrap parent POM is not a library. Gradle must not
            # substitute a jar dependency onto that version.
            if art.priority == 0 and extension == "pom" and not art.coord.classifier:
                continue
            if art.coord.classifier == "" and extension in JAR_LIKE:
                rank = 2
            elif extension == "pom" and art.coord.classifier == "":
                rank = 1
            else:
                rank = 0
            put(art.coord.group, art.coord.artifact, art.version, rank)
            for alias in art.aliases:
                put(alias.group, alias.artifact, art.version, rank if alias.classifier == "" else 0)
        return {key: version for key, (_rank, version) in ranked.items()}
