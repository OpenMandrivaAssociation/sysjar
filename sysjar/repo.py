"""Materialize a Maven repository made of system jars."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from sysjar.model import Artifact, Coord, Dependency, Index, JAR_LIKE
from sysjar.pom import rewrite_path
from sysjar.xmlutil import local_name, parse_xml, upsert, write_xml
import xml.etree.ElementTree as ET


def default_repo() -> Path:
    return Path("/var/cache/sysjar/m2")


def materialize(index: Index, dest: Path, *, drop: set[str] | None = None) -> list[str]:
    """Write a Maven repository at dest. Jars are symlinks. POMs are rewritten copies.

    drop removes those coordinates from the published POMs as well as from
    the project. A parent profile is how a plugin reaches a build that never
    names it, and --drop has to catch it there.

    Returns warnings. Missing files are skipped so a stale index entry cannot
    plant a broken symlink that Maven treats as a successful download.
    """
    dest.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    versions: dict[tuple[str, str], set[str]] = {}
    for art in index.publishable():
        if not art.version:
            continue
        directory = _version_dir(dest, art)
        if directory is None:
            warnings.append(f"skipped {art.coord.display()} {art.version}: unsafe coordinate")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        versions.setdefault((art.coord.group, art.coord.artifact), set()).add(art.version)
        extension = art.coord.extension or "jar"
        if extension == "pom" or (art.pom and extension == "pom"):
            _write_pom(art, directory, index, warnings, drop=drop)
            continue
        if extension in JAR_LIKE or extension == "jar":
            target = art.path
            if not target or not Path(target).is_file():
                warnings.append(f"missing jar for {art.coord.ga()} at {target or '(none)'}")
            else:
                link = directory / _file_name(art, "jar" if extension in JAR_LIKE else extension)
                _symlink(link, Path(target))
                _checksums(link)
            pom_source = _sibling_pom(art, index)
            # XMvn ships the POM and the jar as two artifacts. A generated POM
            # must not replace the real one, whichever artifact is written last.
            if pom_source:
                _write_pom(art, directory, index, warnings, source=pom_source, drop=drop)
            elif not art.coord.classifier and not (directory / _file_name(art, "pom")).exists():
                _write_generated_pom(art, directory, index)
        else:
            target = art.path or art.pom
            if target and Path(target).is_file():
                link = directory / _file_name(art, extension)
                _symlink(link, Path(target))
                _checksums(link)
            else:
                warnings.append(f"missing file for {art.coord.display()}")
    _publish_aliases(index, dest, versions, warnings)
    for (group, artifact), vers in sorted(versions.items()):
        _write_metadata(dest, group, artifact, sorted(vers, key=_metadata_sort))
    return warnings


def _publish_aliases(index: Index, dest: Path, versions: dict[tuple[str, str], set[str]], warnings: list[str]) -> None:
    """Publish alias coordinates beside the canonical artifact.

    A classifier alias is how javapackages-bootstrap exposes guice's classes
    jar: one file, two names. Maven looks the alias up as its own path, so the
    repository has to contain that path too.
    """
    for art in index.publishable():
        for alias in art.aliases:
            if alias.key() == art.coord.key() or alias.key() in index.by_key:
                continue
            extension = alias.extension or art.coord.extension or "jar"
            alias_art = Artifact(Coord(alias.group, alias.artifact, alias.classifier, extension), art.version, art.path, art.pom)
            directory = _version_dir(dest, alias_art)
            if directory is None:
                warnings.append(f"skipped alias {alias.display()} {art.version}: unsafe coordinate")
                continue
            same_component = alias.group == art.coord.group and alias.artifact == art.coord.artifact
            if same_component and not alias.classifier:
                continue
            directory.mkdir(parents=True, exist_ok=True)
            versions.setdefault((alias.group, alias.artifact), set()).add(art.version)
            if extension in JAR_LIKE or extension == "jar":
                target = art.path
                if target and Path(target).is_file():
                    link = directory / _file_name(alias_art, "jar" if extension in JAR_LIKE else extension)
                    _symlink(link, Path(target))
                    _checksums(link)
                elif not same_component and (art.coord.extension or "jar") != "pom":
                    warnings.append(f"missing jar for alias {alias.display()}")
            if same_component:
                continue
            pom_dest = directory / _file_name(alias_art, "pom")
            if pom_dest.exists():
                continue
            canonical = _version_dir(dest, art)
            canonical_pom = canonical / _file_name(art, "pom") if canonical is not None else None
            if canonical_pom is not None and canonical_pom.is_file():
                _copy_pom_as(canonical_pom, pom_dest, alias.group, alias.artifact, art.version)
            else:
                _write_generated_pom(alias_art, directory, index)


def _copy_pom_as(source: Path, dest: Path, group: str, artifact: str, version: str) -> None:
    tree = parse_xml(source)
    root = tree.getroot()
    upsert(root, "groupId", group, after=("modelVersion",))
    upsert(root, "artifactId", artifact, after=("groupId", "modelVersion"))
    upsert(root, "version", version, after=("artifactId", "groupId", "modelVersion"))
    write_xml(tree, dest)
    _checksums(dest)


def _component_ok(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value and "\x00" not in value


def _version_dir(dest: Path, art: Artifact) -> Path | None:
    parts = art.coord.group.split(".")
    if not parts or any(not _component_ok(part) for part in parts):
        return None
    if not _component_ok(art.coord.artifact) or not _component_ok(art.version):
        return None
    return dest.joinpath(*parts, art.coord.artifact, art.version)


def _sibling_pom(art: Artifact, index: Index) -> str:
    """POM to publish for a jar. Prefer this artifact's POM, else the POM artifact XMvn stores beside it."""
    if art.pom and Path(art.pom).is_file():
        return art.pom
    sibling = index.find_pom(art.coord.group, art.coord.artifact)
    if sibling is None or sibling is art or sibling.version != art.version:
        return ""
    if sibling.pom and Path(sibling.pom).is_file():
        return sibling.pom
    return ""


def _file_name(art: Artifact, extension: str) -> str:
    # A classified jar shares the unclassified POM. Maven asks for
    # artifact-version.pom even when the jar is artifact-version-classifier.jar.
    classifier = ""
    if extension != "pom" and art.coord.classifier:
        classifier = f"-{art.coord.classifier}"
    return f"{art.coord.artifact}-{art.version}{classifier}.{extension}"


def _symlink(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.resolve())


def _checksums(path: Path) -> None:
    data = path.read_bytes()
    for algo in ("sha1", "md5"):
        digest = hashlib.new(algo, data).hexdigest()
        path.with_name(path.name + "." + algo).write_text(digest + "\n", encoding="ascii")


def _write_pom(art: Artifact, directory: Path, index: Index, warnings: list[str], source: str = "", drop: set[str] | None = None) -> None:
    if not source:
        source = art.pom or (art.path if (art.coord.extension or "") == "pom" else "")
    source_path = Path(source) if source else None
    if source_path is None or not source_path.is_file():
        _write_generated_pom(art, directory, index)
        return
    # javapackages-bootstrap ships files whose contents are the sentence
    # "Dummy POM file for …". Those are not models. The dependency list lives
    # on the index record, so generate a POM instead of dropping the artifact.
    if not _looks_like_xml(source_path):
        _write_generated_pom(art, directory, index)
        return
    dest = directory / _file_name(art, "pom")
    try:
        rewrite_path(
            source_path,
            index,
            inplace=False,
            dest=dest,
            force=(art.coord.group, art.coord.artifact, art.version),
            drop=drop,
            strip_uninstalled=True,
        )
    except (OSError, ET.ParseError) as exc:
        warnings.append(f"could not rewrite {source}: {exc}; generating a POM from the index")
        _write_generated_pom(art, directory, index)
        return
    _checksums(dest)
    parent_gap = _parent_warning(dest, index)
    if parent_gap:
        warnings.append(parent_gap)


def _looks_like_xml(path: Path) -> bool:
    try:
        head = path.read_bytes()[:200].lstrip()
    except OSError:
        return False
    return head.startswith((b"<?xml", b"<project", b"<metadata"))


def _parent_warning(pom_path: Path, index: Index) -> str:
    try:
        root = parse_xml(pom_path).getroot()
    except (OSError, ET.ParseError):
        return ""
    parent = None
    for node in list(root):
        if node.tag.endswith("parent") and (node.tag == "parent" or node.tag.endswith("}parent")):
            parent = node
            break
    if parent is None:
        return ""
    group = ""
    artifact = ""
    for node in list(parent):
        if node.tag.endswith("groupId"):
            group = (node.text or "").strip()
        elif node.tag.endswith("artifactId"):
            artifact = (node.text or "").strip()
    if group and artifact and index.find_pom(group, artifact) is None and index.find(group, artifact) is None:
        return f"{pom_path.name}: parent {group}:{artifact} is not installed, so Maven cannot build an effective POM"
    return ""


def _dependency_list(art: Artifact, index: Index) -> tuple[Dependency, ...]:
    """Deps to publish. A jar record often has none; the POM record beside it has the list."""
    if art.dependencies:
        return art.dependencies
    sibling = index.find_pom(art.coord.group, art.coord.artifact)
    if sibling is None or sibling is art or sibling.version != art.version:
        return ()
    return sibling.dependencies


def _jar_file(art: Artifact, index: Index) -> str:
    extension = art.coord.extension or "jar"
    if art.path and extension != "pom" and Path(art.path).is_file():
        return art.path
    found = index.find(art.coord.group, art.coord.artifact, art.coord.classifier, "jar")
    if found is None or found.version != art.version or not found.path:
        return ""
    if Path(found.path).is_file():
        return found.path
    return ""


_EXPORT_CACHE: dict[str, frozenset[str]] = {}


def _maven_exported(index: Index) -> frozenset[str]:
    """group:artifact values Maven's core class realm already provides.

    The list is META-INF/maven/extension.xml inside the installed maven-core
    jar, the same list Maven uses when it builds a plugin realm. A generated
    POM has no scopes. Listing one of these as compile makes the realm load a
    second copy of a Maven internal, and the two copies no longer share the
    session.
    """
    found: set[str] = set()
    for art in index.publishable():
        if art.coord.artifact != "maven-core" or art.coord.classifier:
            continue
        path = _jar_file(art, index)
        if path:
            found.update(_exported_from_jar(path))
    return frozenset(found)


def _exported_from_jar(path: str) -> frozenset[str]:
    cached = _EXPORT_CACHE.get(path)
    if cached is not None:
        return cached
    names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("META-INF/maven/extension.xml")
    except (OSError, zipfile.BadZipFile, KeyError):
        _EXPORT_CACHE[path] = frozenset()
        return _EXPORT_CACHE[path]
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        _EXPORT_CACHE[path] = frozenset()
        return _EXPORT_CACHE[path]
    for node in root.iter():
        if local_name(node.tag) == "exportedArtifact" and node.text and ":" in node.text:
            names.add(node.text.strip())
    _EXPORT_CACHE[path] = frozenset(names)
    return _EXPORT_CACHE[path]


def _is_maven_plugin(path: str) -> bool:
    if not path:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return "META-INF/maven/plugin.xml" in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _packaging(art: Artifact, index: Index) -> str:
    extension = art.coord.extension or "jar"
    jar_path = _jar_file(art, index)
    if not jar_path:
        return "pom"
    if extension == "maven-plugin" or _is_maven_plugin(jar_path):
        return "maven-plugin"
    return "jar"


def _write_generated_pom(art: Artifact, directory: Path, index: Index) -> None:
    ns = "http://maven.apache.org/POM/4.0.0"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}project")
    ET.SubElement(root, f"{{{ns}}}modelVersion").text = "4.0.0"
    ET.SubElement(root, f"{{{ns}}}groupId").text = art.coord.group
    ET.SubElement(root, f"{{{ns}}}artifactId").text = art.coord.artifact
    ET.SubElement(root, f"{{{ns}}}version").text = art.version
    ET.SubElement(root, f"{{{ns}}}packaging").text = _packaging(art, index)
    dependencies = _dependency_list(art, index)
    # maven-core's own POM must keep its compile graph. Everyone else that
    # was published from a scope-less index record must not re-export jars the
    # running Maven already puts on the plugin realm's parent classpath.
    exported = _maven_exported(index)
    publishing_runtime = art.coord.ga() in exported
    if dependencies:
        deps = ET.SubElement(root, f"{{{ns}}}dependencies")
        for dep in dependencies:
            if not publishing_runtime and dep.coord.ga() in exported:
                continue
            if dep.optional or dep.scope in {"test", "provided", "system"}:
                continue
            node = ET.SubElement(deps, f"{{{ns}}}dependency")
            ET.SubElement(node, f"{{{ns}}}groupId").text = dep.coord.group
            ET.SubElement(node, f"{{{ns}}}artifactId").text = dep.coord.artifact
            version = dep.version
            found = index.find(dep.coord.group, dep.coord.artifact, dep.coord.classifier, dep.coord.extension)
            if found is not None:
                version = found.version
            if version:
                ET.SubElement(node, f"{{{ns}}}version").text = version
            if dep.coord.classifier:
                ET.SubElement(node, f"{{{ns}}}classifier").text = dep.coord.classifier
            if dep.coord.extension and dep.coord.extension != "jar":
                ET.SubElement(node, f"{{{ns}}}type").text = dep.coord.extension
            if dep.scope and dep.scope != "compile":
                ET.SubElement(node, f"{{{ns}}}scope").text = dep.scope
        if not len(deps):
            root.remove(deps)
    dest = directory / _file_name(art, "pom")
    write_xml(ET.ElementTree(root), dest)
    _checksums(dest)


def _write_metadata(dest: Path, group: str, artifact: str, versions: list[str]) -> None:
    latest = versions[-1]
    root = ET.Element("metadata")
    ET.SubElement(root, "groupId").text = group
    ET.SubElement(root, "artifactId").text = artifact
    versioning = ET.SubElement(root, "versioning")
    ET.SubElement(versioning, "latest").text = latest
    ET.SubElement(versioning, "release").text = latest
    box = ET.SubElement(versioning, "versions")
    for version in versions:
        ET.SubElement(box, "version").text = version
    parts = group.split(".")
    if not parts or any(not _component_ok(part) for part in parts) or not _component_ok(artifact):
        return
    path = dest.joinpath(*parts, artifact, "maven-metadata.xml")
    write_xml(ET.ElementTree(root), path)
    _checksums(path)


def _metadata_sort(version: str):
    from sysjar.model import version_key
    return version_key(version)


def write_settings(path: Path, repo: Path) -> None:
    url = repo.resolve().as_uri()
    root = ET.Element("settings")
    # Maven's offline switch skips every repository, including a file repository.
    # The mirror below is what keeps the build off the network.
    mirrors = ET.SubElement(root, "mirrors")
    mirror = ET.SubElement(mirrors, "mirror")
    ET.SubElement(mirror, "id").text = "sysjar"
    ET.SubElement(mirror, "url").text = url
    ET.SubElement(mirror, "mirrorOf").text = "*"
    profiles = ET.SubElement(root, "profiles")
    profile = ET.SubElement(profiles, "profile")
    ET.SubElement(profile, "id").text = "sysjar"
    for box_name, repo_name in (("repositories", "repository"), ("pluginRepositories", "pluginRepository")):
        box = ET.SubElement(profile, box_name)
        node = ET.SubElement(box, repo_name)
        ET.SubElement(node, "id").text = "sysjar"
        ET.SubElement(node, "url").text = url
        releases = ET.SubElement(node, "releases")
        ET.SubElement(releases, "enabled").text = "true"
        snapshots = ET.SubElement(node, "snapshots")
        ET.SubElement(snapshots, "enabled").text = "true"
    active = ET.SubElement(root, "activeProfiles")
    ET.SubElement(active, "activeProfile").text = "sysjar"
    write_xml(ET.ElementTree(root), path)
