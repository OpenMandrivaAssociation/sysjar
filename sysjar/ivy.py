"""Ivy settings and a small ivy.xml rewriter."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from sysjar.model import Coord, Dependency, Index, SysjarError
from sysjar.pom import Gap, Report
from sysjar.xmlutil import child, children, local_name, write_xml


def rewrite_ivy_element(root: ET.Element, index: Index, *, drop: set[str] | None = None, source: str = "") -> Report:
    drop = drop or set()
    report = Report()
    for dep in list(root.iter()):
        if local_name(dep.tag) != "dependency":
            continue
        org = dep.get("org") or dep.get("organisation") or ""
        name = dep.get("name") or ""
        if not org or not name:
            continue
        label = f"{org}:{name}"
        if label in drop:
            parent = _owner(root, dep)
            if parent is not None:
                parent.remove(dep)
                report.dropped.append(f"{source}: dropped ivy dependency {label}")
            continue
        found = index.find(org, name, dep.get("m:classifier") or dep.get("classifier") or "")
        scope = ivy_scope(dep.get("conf") or "")
        if found is None:
            blocking = scope in {"compile", "runtime", ""}
            report.gaps.append(Gap(source, label, dep.get("rev") or "", "dependency", scope, False, blocking))
            continue
        current = dep.get("rev") or ""
        if current != found.version:
            dep.set("rev", found.version)
            report.changes.append(f"{source}: ivy {label} {current or '(unversioned)'} -> {found.version}")
    return report


def _owner(root: ET.Element, node: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        for child in list(parent):
            if child is node:
                return parent
    return None


_SKIP_DIRS = {"target", "build", ".git", ".sysjar", ".gradle"}


def ivy_scope(conf: str) -> str:
    """Map an Ivy conf to a Maven scope. Test and provided confs are not runtime requires."""
    if not conf:
        return "compile"
    left = conf.split("->", 1)[0]
    parts = [part.strip().lower() for part in left.replace(";", ",").split(",") if part.strip()]
    if not parts:
        return "compile"
    if all(part in {"test", "tests", "testing"} for part in parts):
        return "test"
    if all(part in {"provided", "compileonly", "compile-only"} for part in parts):
        return "provided"
    if all(part in {"runtime", "runtimeonly", "runtime-only"} for part in parts):
        return "runtime"
    return "compile"


def ivy_identity(root: ET.Element) -> tuple[str, str, str]:
    info = child(root, "info")
    if info is None:
        return "", "", ""
    group = info.get("organisation") or info.get("organization") or info.get("org") or ""
    module = info.get("module") or ""
    revision = info.get("revision") or ""
    return group, module, revision


def concrete_revision(revision: str) -> str:
    if not revision or revision.startswith("latest") or any(char in revision for char in "$/\\\t "):
        raise SysjarError(
            f"ivy revision {revision or '(empty)'} is not a fixed version. "
            "Set revision on the info element to the version this package builds."
        )
    return revision


def ivy_jar_names(root: ET.Element, module: str) -> list[str]:
    names: list[str] = []
    for art in children(child(root, "publications"), "artifact"):
        ext = (art.get("ext") or art.get("type") or "jar").lower()
        if ext not in {"jar", "bundle"}:
            continue
        name = art.get("name") or module
        if name and name not in names:
            names.append(name)
    if module and module not in names:
        names.append(module)
    return names


def ivy_dependencies(root: ET.Element) -> list[Dependency]:
    found: list[Dependency] = []
    for dep in root.iter():
        if local_name(dep.tag) != "dependency":
            continue
        group = dep.get("org") or dep.get("organisation") or ""
        name = dep.get("name") or ""
        if not group or not name:
            continue
        classifier = dep.get("classifier") or dep.get("m:classifier") or ""
        found.append(Dependency(
            Coord(group, name, classifier, "jar"),
            version=dep.get("rev") or "",
            scope=ivy_scope(dep.get("conf") or ""),
        ))
    return found


def find_ivy_files(project: Path) -> list[Path]:
    if project.is_file():
        return [project] if project.name == "ivy.xml" else []
    found: list[Path] = []
    for path in sorted(project.rglob("ivy.xml")):
        if any(part in _SKIP_DIRS for part in path.relative_to(project).parts):
            continue
        found.append(path)
    return found


def write_ivy_settings(path: Path, repo: Path) -> None:
    root = ET.Element("ivysettings")
    settings = ET.SubElement(root, "settings")
    settings.set("defaultResolver", "sysjar")
    resolvers = ET.SubElement(root, "resolvers")
    ibiblio = ET.SubElement(resolvers, "ibiblio")
    ibiblio.set("name", "sysjar")
    ibiblio.set("m2compatible", "true")
    ibiblio.set("useMavenMetadata", "true")
    ibiblio.set("root", repo.resolve().as_uri())
    write_xml(ET.ElementTree(root), path)
