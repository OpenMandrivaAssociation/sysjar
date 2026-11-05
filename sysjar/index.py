"""Load the system jar index from sysjar files and legacy XMvn metadata."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from sysjar.model import Artifact, Coord, Dependency, Index, JAR_LIKE
from sysjar.xmlutil import child, children, local_name, parse_xml, text_of


def load_index(roots: list[Path] | None = None, bootstrap_roots: list[Path] | None = None) -> Index:
    """Load every root. Later roots override earlier ones only through priority.

    Legacy XMvn fragments are priority 1. sysjar index files are priority 2,
    so a new package replaces a stale fragment of the same version.
    javapackages-bootstrap metadata is priority 0: it fills coordinates that
    no real package provides, and a packaged jar always wins.

    Without an explicit bootstrap root, only parent POMs are loaded from
    that tree. A model cannot be built without its parent. Jars stay out,
    because one bootstrap version would satisfy a dependency the
    distribution has not packaged.
    """
    index = Index()
    for root in bootstrap_roots or []:
        load_xmvn_tree(index, root, priority=0, bootstrap=True)
    for root in roots if roots is not None else [Path("/")]:
        load_xmvn_tree(index, root)
        load_sysjar_tree(index, root)
        if not bootstrap_roots:
            load_bootstrap_poms(index, root)
    return index


def load_bootstrap_poms(index: Index, root: Path) -> None:
    """Parent POMs from javapackages-bootstrap. Jars in that tree are ignored.

    A coordinate that also has a jar is a library or a plugin. Indexing its
    POM would publish a generated model at the same path as the lifecycle
    toolchain, and that model has no dependency versions.
    """
    directory = root / "usr/share/javapackages-bootstrap/maven-metadata"
    if not directory.is_dir():
        return
    found = []
    for path in sorted(directory.glob("*.xml")):
        found.extend(artifacts_from_xmvn(path, root, priority=0))
    jars = {
        (art.coord.group, art.coord.artifact)
        for art in found
        if not art.coord.classifier and (art.coord.extension or "jar") != "pom"
    }
    for art in found:
        extension = art.coord.extension or "jar"
        if extension != "pom" or art.coord.classifier:
            continue
        if (art.coord.group, art.coord.artifact) in jars:
            continue
        index.add(art)


def load_sysjar_tree(index: Index, root: Path) -> None:
    directory = root / "usr/share/sysjar/index"
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*.sysjar")):
        art = artifact_from_sysjar(path, root)
        if art is not None:
            index.add(art)


def load_xmvn_tree(index: Index, root: Path, priority: int = 1, *, bootstrap: bool = False) -> None:
    # Bootstrap metadata is a separate tree. Scanning maven-metadata here would
    # publish real packages at priority 0 and then again at priority 1.
    if bootstrap:
        directories = [root / "usr/share/javapackages-bootstrap/maven-metadata"]
    else:
        directories = [root / "usr/share/maven-metadata"]
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.xml")):
            for art in artifacts_from_xmvn(path, root, priority=priority):
                index.add(art)


def artifact_from_sysjar(path: Path, root: Path) -> Artifact | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    group = data.get("groupId") or ""
    artifact = data.get("artifactId") or ""
    version = data.get("version") or ""
    if not group or not artifact or not version:
        return None
    extension = data.get("extension") or "jar"
    classifier = data.get("classifier") or ""
    jar = data.get("jar") or data.get("path") or ""
    pom = data.get("pom") or ""
    if extension == "pom" and not pom:
        pom = jar
        jar = ""
    aliases = []
    for alias in data.get("aliases") or []:
        if alias.get("groupId") and alias.get("artifactId"):
            aliases.append(Coord(
                alias["groupId"],
                alias["artifactId"],
                alias.get("classifier") or "",
                alias.get("extension") or extension,
            ))
    deps = tuple(Dependency.from_json(item) for item in data.get("dependencies") or [] if item.get("groupId") and item.get("artifactId"))
    return Artifact(
        Coord(group, artifact, classifier, extension),
        version,
        path=_existing_path(root, jar) if jar else "",
        pom=_existing_path(root, pom) if pom else "",
        aliases=tuple(aliases),
        dependencies=deps,
        priority=2,
    )


def artifacts_from_xmvn(path: Path, root: Path, priority: int = 1) -> list[Artifact]:
    try:
        tree = parse_xml(path)
    except (OSError, ET.ParseError):
        return []
    found = []
    for node in tree.getroot().iter():
        if local_name(node.tag) != "artifact":
            continue
        # The metadata file nests artifacts only under <artifacts>. Dependencies
        # also contain no nested <artifact>, but be strict about the parent.
        parent = _parent(tree.getroot(), node)
        if parent is None or local_name(parent.tag) != "artifacts":
            continue
        group = text_of(node, "groupId")
        artifact = text_of(node, "artifactId")
        version = text_of(node, "version")
        if not group or not artifact or not version:
            continue
        extension = text_of(node, "extension") or _property_type(node) or "jar"
        classifier = text_of(node, "classifier")
        stored = text_of(node, "path")
        aliases = []
        alias_root = child(node, "aliases")
        for alias in children(alias_root, "alias"):
            ag = text_of(alias, "groupId")
            aa = text_of(alias, "artifactId")
            if ag and aa:
                aliases.append(Coord(
                    ag,
                    aa,
                    text_of(alias, "classifier"),
                    text_of(alias, "extension") or extension,
                ))
        deps = []
        dep_root = child(node, "dependencies")
        for dep in children(dep_root, "dependency"):
            dg = text_of(dep, "groupId")
            da = text_of(dep, "artifactId")
            if not dg or not da:
                continue
            optional = text_of(dep, "optional").lower() == "true"
            scope = text_of(dep, "scope") or "compile"
            dtype = text_of(dep, "extension") or text_of(dep, "type") or "jar"
            deps.append(Dependency(
                Coord(dg, da, text_of(dep, "classifier"), dtype),
                version=text_of(dep, "requestedVersion") or text_of(dep, "version"),
                scope=scope,
                optional=optional,
            ))
        resolved = _existing_path(root, stored) if stored else ""
        pom = resolved if extension == "pom" else ""
        jar = "" if extension == "pom" else resolved
        if extension in JAR_LIKE and resolved.endswith(".pom"):
            pom = resolved
            jar = ""
            extension = "pom"
        found.append(Artifact(
            Coord(group, artifact, classifier, extension),
            version,
            path=jar,
            pom=pom,
            aliases=tuple(aliases),
            dependencies=tuple(deps),
            priority=priority,
        ))
    return found


def _parent(root, node):
    for parent in root.iter():
        for child_node in list(parent):
            if child_node is node:
                return parent
    return None


def _property_type(node) -> str:
    props = child(node, "properties")
    return text_of(props, "type")


def _existing_path(root: Path, stored: str) -> str:
    if not stored:
        return ""
    direct = Path(stored)
    if direct.is_file():
        return str(direct)
    prefixed = root / stored.lstrip("/")
    if prefixed.is_file():
        return str(prefixed)
    # Keep the installed path. check() reports it missing.
    if root == Path("/"):
        return str(direct)
    return str(prefixed)
