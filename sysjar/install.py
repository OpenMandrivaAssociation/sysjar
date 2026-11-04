"""Install a built jar into a buildroot as a system library."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from sysjar.ivy import concrete_revision, find_ivy_files, ivy_dependencies, ivy_identity, ivy_jar_names
from sysjar.model import Artifact, Coord, Dependency, SysjarError, require_safe_filename
from sysjar.pom import discover_reactor, project_identity, runtime_dependencies
from sysjar.xmlutil import local_name, parse_xml, text_of, write_xml

_SKIP_JAR_SUFFIXES = ("-sources.jar", "-javadoc.jar", "-javadocs.jar", "-tests.jar", "-test-sources.jar")
_OUTPUT_FOLDERS = ("dist", "build/lib", "build/jars", "build", "target", "build/libs")


def index_name(coord: Coord) -> str:
    name = require_safe_filename(coord.artifact)
    if coord.classifier:
        name += "~" + require_safe_filename(coord.classifier)
    extension = coord.extension or "jar"
    if extension != "jar":
        name += "@" + require_safe_filename(extension)
    return name + ".sysjar"


def install_artifact(
    buildroot: Path,
    *,
    group: str,
    artifact: str,
    version: str,
    jar: Path | None = None,
    pom: Path | None = None,
    classifier: str = "",
    extension: str = "jar",
    file_name: str = "",
    aliases: list[Coord] | None = None,
    dependencies: list[Dependency] | None = None,
    links: list[str] | None = None,
) -> Path:
    if not group or not artifact or not version:
        raise SysjarError("install needs groupId, artifactId and version")
    require_safe_filename(group)
    extension = extension or "jar"
    coord = Coord(group, artifact, classifier, extension)
    stem = file_name or (f"{artifact}-{classifier}" if classifier else artifact)
    require_safe_filename(stem)

    java_dir = buildroot / "usr/share/java"
    pom_dir = buildroot / "usr/share/maven-poms"
    index_dir = buildroot / "usr/share/sysjar/index" / group
    java_dir.mkdir(parents=True, exist_ok=True)
    pom_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    jar_path = ""
    pom_path = ""
    written_links: list[str] = []
    if jar is not None:
        if not jar.is_file():
            raise SysjarError(f"jar not found: {jar}")
        dest = java_dir / f"{stem}.jar"
        shutil.copyfile(jar, dest)
        dest.chmod(0o644)
        jar_path = f"/usr/share/java/{stem}.jar"
        for alias in aliases or []:
            if alias.artifact and alias.artifact != stem and not alias.classifier:
                _symlink_jar(java_dir, dest, require_safe_filename(alias.artifact))
        for name in links or []:
            name = require_safe_filename(name)
            if name == stem:
                continue
            _symlink_jar(java_dir, dest, name)
            written_links.append(name)
    if pom is not None:
        if not pom.is_file():
            raise SysjarError(f"pom not found: {pom}")
        dest_pom = pom_dir / f"{stem}.pom"
        shutil.copyfile(pom, dest_pom)
        dest_pom.chmod(0o644)
        pom_path = f"/usr/share/maven-poms/{stem}.pom"
    elif dependencies:
        dest_pom = pom_dir / f"{stem}.pom"
        _write_synth_pom(dest_pom, coord, version, dependencies)
        pom_path = f"/usr/share/maven-poms/{stem}.pom"

    record = Artifact(
        coord, version, path=jar_path, pom=pom_path,
        aliases=tuple(aliases or []),
        dependencies=tuple(dependencies or []),
    ).to_json()
    # Store the paths the installed system will see, not the buildroot.
    if jar_path:
        record["jar"] = jar_path
    if pom_path:
        record["pom"] = pom_path
    if written_links:
        record["links"] = written_links
    dest_index = index_dir / index_name(coord)
    dest_index.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    dest_index.chmod(0o644)
    return dest_index


def filelist_lines(index_path: Path) -> list[str]:
    """rpm %files lines for one installed artifact. Paths are buildroot-relative."""
    data = json.loads(index_path.read_text(encoding="utf-8"))
    group = data.get("groupId") or ""
    lines = []
    if group:
        lines.append(f"%dir /usr/share/sysjar/index/{group}")
        lines.append(f"/usr/share/sysjar/index/{group}/{index_path.name}")
    jar = data.get("jar") or ""
    if jar:
        lines.append(jar)
        stem = Path(jar).name
        if stem.endswith(".jar"):
            stem = stem[:-4]
        for alias in data.get("aliases") or []:
            artifact = alias.get("artifactId") or ""
            if artifact and not alias.get("classifier") and artifact != stem:
                lines.append(f"/usr/share/java/{artifact}.jar")
        for name in data.get("links") or []:
            if name and name != stem:
                lines.append(f"/usr/share/java/{name}.jar")
    pom_path = data.get("pom") or ""
    if pom_path and pom_path != jar:
        lines.append(pom_path)
    return lines


def write_filelist(path: Path, index_files: list[Path]) -> None:
    lines = []
    seen = set()
    for index_path in index_files:
        for line in filelist_lines(index_path):
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _symlink_jar(java_dir: Path, dest: Path, name: str) -> None:
    link = java_dir / f"{name}.jar"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(dest.name)


def install_pom(buildroot: Path, pom: Path, jar: Path | None = None, *, file_name: str = "", aliases: list[Coord] | None = None, links: list[str] | None = None) -> Path:
    tree = parse_xml(pom)
    root = tree.getroot()
    if local_name(root.tag) != "project":
        raise SysjarError(f"{pom} is not a Maven POM")
    group, artifact, version = project_identity(root)
    if not artifact:
        raise SysjarError(f"{pom} has no artifactId")
    if not group or not version:
        raise SysjarError(f"{pom} has no groupId or version. A parent POM must be in this reactor, or the coordinates must be written in the POM.")
    packaging = text_of(root, "packaging") or "jar"
    extension = "pom" if packaging == "pom" else "jar"
    if extension == "jar" and jar is None:
        jar = locate_jar(pom, artifact, version)
        if jar is None:
            raise SysjarError(f"no jar for {group}:{artifact}. Pass POM:JAR to --artifact.")
    deps = runtime_dependencies(root)
    return install_artifact(
        buildroot,
        group=group,
        artifact=artifact,
        version=version,
        jar=None if extension == "pom" else jar,
        pom=pom,
        extension=extension,
        file_name=file_name,
        aliases=aliases,
        dependencies=deps,
        links=links,
    )


def locate_jar(pom: Path, artifact: str, version: str) -> Path | None:
    base = pom.parent
    names = [f"{artifact}-{version}.jar", f"{artifact}.jar"]
    # Exact names may be produced by Ant into dist/ or build/ as well as target/.
    for relative in ("target", "build/libs", "build", "dist"):
        folder = _folder(base, relative)
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    loose = []
    for relative in ("target", "build/libs"):
        loose.extend(_loose_jars(_folder(base, relative)))
    if len(loose) == 1:
        return loose[0]
    return None


def locate_built_jar(base: Path, names: list[str]) -> Path | None:
    """Find a jar Ant or Ivy just built. The first output directory with a hit wins."""
    folders = [_folder(base, relative) for relative in _OUTPUT_FOLDERS]
    for folder in folders:
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    for folder in folders:
        found = _loose_jars(folder)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None


def _folder(base: Path, relative: str) -> Path:
    path = base
    for part in relative.split("/"):
        path = path / part
    return path


def _loose_jars(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [
        candidate for candidate in sorted(folder.glob("*.jar"))
        if not candidate.name.endswith(_SKIP_JAR_SUFFIXES)
    ]


def install_auto(buildroot: Path, project: Path, *, file_name: str = "", aliases: list[Coord] | None = None, links: list[str] | None = None) -> list[Path]:
    reactor = project / ".sysjar" / "reactor"
    records = sorted(reactor.glob("*.json")) if reactor.is_dir() else []
    written = []
    if records:
        if (file_name or aliases or links) and len(records) != 1:
            raise SysjarError("--file-name, --alias and --link need a single artifact; this build produced several")
        for record_path in records:
            data = json.loads(record_path.read_text(encoding="utf-8"))
            written.append(_install_reactor_record(buildroot, data, file_name=file_name, aliases=aliases, links=links))
        return written
    mapping = discover_reactor(project)
    if not mapping and (project / "pom.xml").is_file():
        mapping = {("", ""): project / "pom.xml"}
    if mapping:
        if (file_name or aliases or links) and len(mapping) != 1:
            raise SysjarError("--file-name, --alias and --link need a single artifact; this reactor has several modules")
        for pom in mapping.values():
            written.append(install_pom(buildroot, pom, file_name=file_name, aliases=aliases, links=links))
        return written
    ivy_files = find_ivy_files(project) if project.is_dir() else []
    if ivy_files:
        if (file_name or aliases or links) and len(ivy_files) != 1:
            raise SysjarError("--file-name, --alias and --link need a single artifact; this build has several ivy modules")
        for ivy in ivy_files:
            written.append(install_ivy(buildroot, ivy, file_name=file_name, aliases=aliases, links=links))
        return written
    raise SysjarError(f"no pom.xml, ivy.xml, or .sysjar/reactor metadata under {project}")


def install_ivy(buildroot: Path, ivy: Path, *, file_name: str = "", aliases: list[Coord] | None = None, links: list[str] | None = None) -> Path:
    root = parse_xml(ivy).getroot()
    if local_name(root.tag) != "ivy-module":
        raise SysjarError(f"{ivy} is not an ivy module")
    group, artifact, revision = ivy_identity(root)
    if not group or not artifact:
        raise SysjarError(f"{ivy} has no organisation or module")
    version = concrete_revision(revision)
    names = []
    for stem in ivy_jar_names(root, artifact):
        names.append(f"{stem}-{version}.jar")
        names.append(f"{stem}.jar")
    jar = locate_built_jar(ivy.parent, names)
    if jar is None:
        raise SysjarError(
            f"no jar for {group}:{artifact} next to {ivy}. "
            "Looked in dist/, build/ and target/. Pass --artifact POM:JAR, or set the jar name in <publications>."
        )
    return install_artifact(
        buildroot,
        group=group,
        artifact=artifact,
        version=version,
        jar=jar,
        pom=None,
        file_name=file_name,
        aliases=aliases,
        dependencies=ivy_dependencies(root),
        links=links,
    )


def _install_reactor_record(buildroot: Path, data: dict, *, file_name: str, aliases: list[Coord] | None, links: list[str] | None) -> Path:
    group = data.get("group") or ""
    artifact = data.get("artifact") or ""
    version = data.get("version") or ""
    jar = Path(data["jar"]) if data.get("jar") else None
    pom = Path(data["pom"]) if data.get("pom") else None
    deps = []
    for item in data.get("dependencies") or []:
        if item.get("group") and item.get("artifact"):
            deps.append(Dependency(
                Coord(item["group"], item["artifact"], "", "jar"),
                version=item.get("version") or "",
                scope=item.get("scope") or "runtime",
            ))
    if pom is not None and pom.is_file():
        return install_pom(buildroot, pom, jar, file_name=file_name, aliases=aliases, links=links)
    return install_artifact(
        buildroot,
        group=group,
        artifact=artifact,
        version=version,
        jar=jar,
        pom=None,
        file_name=file_name,
        aliases=aliases,
        dependencies=deps,
        links=links,
    )


def _write_synth_pom(path: Path, coord: Coord, version: str, dependencies: list[Dependency]) -> None:
    ns = "http://maven.apache.org/POM/4.0.0"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}project")
    ET.SubElement(root, f"{{{ns}}}modelVersion").text = "4.0.0"
    ET.SubElement(root, f"{{{ns}}}groupId").text = coord.group
    ET.SubElement(root, f"{{{ns}}}artifactId").text = coord.artifact
    ET.SubElement(root, f"{{{ns}}}version").text = version
    if dependencies:
        box = ET.SubElement(root, f"{{{ns}}}dependencies")
        for dep in dependencies:
            node = ET.SubElement(box, f"{{{ns}}}dependency")
            ET.SubElement(node, f"{{{ns}}}groupId").text = dep.coord.group
            ET.SubElement(node, f"{{{ns}}}artifactId").text = dep.coord.artifact
            if dep.version:
                ET.SubElement(node, f"{{{ns}}}version").text = dep.version
            if dep.scope and dep.scope != "compile":
                ET.SubElement(node, f"{{{ns}}}scope").text = dep.scope
    write_xml(ET.ElementTree(root), path)
    path.chmod(0o644)


def parse_alias(text: str) -> Coord:
    from sysjar.model import parse_coord
    coord, _version = parse_coord(text)
    return coord
