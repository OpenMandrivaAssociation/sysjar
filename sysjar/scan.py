"""List what a project needs from the system jar index."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sysjar.ivy import ivy_scope
from sysjar.model import Index
from sysjar.pom import _KEEP_MISSING_PLUGINS, collect_hits, discover_reactor, expand, pom_properties
from sysjar.rpmmeta import capability
from sysjar.xmlutil import local_name, parse_xml
import xml.etree.ElementTree as ET


_GRADLE_DEP = re.compile(
    r"""(?P<conf>implementation|api|compileOnly|runtimeOnly|testImplementation|testCompileOnly|testRuntimeOnly|compile|testCompile|runtime|classpath)"""
    r"""\s*\(?\s*['\"](?P<group>[A-Za-z0-9_.\-]+):(?P<artifact>[A-Za-z0-9_.\-]+)(?::(?P<version>[^'\"]+))?['\"]"""
)
_TOML_MODULE = re.compile(
    r"""module\s*=\s*['\"](?P<group>[A-Za-z0-9_.\-]+):(?P<artifact>[A-Za-z0-9_.\-]+)['\"]"""
)
_CONF_SCOPE = {
    "api": "compile",
    "implementation": "compile",
    "compile": "compile",
    "classpath": "compile",
    "runtime": "runtime",
    "runtimeOnly": "runtime",
    "compileOnly": "provided",
    "testImplementation": "test",
    "testCompile": "test",
    "testCompileOnly": "test",
    "testRuntimeOnly": "test",
}


@dataclass
class Finding:
    group: str
    artifact: str
    version: str
    scope: str
    kind: str
    source: str
    system: str = ""
    optional: bool = False
    classifier: str = ""
    extension: str = "jar"
    heuristic: bool = False
    # Set when this dependency is declared inside a plugin or an extension.
    # The plugin coordinate is what decides whether the dependency survives
    # the rewrite. An empty owner is a dependency of the project itself.
    owner_group: str = ""
    owner_artifact: str = ""

    @property
    def label(self) -> str:
        return f"{self.group}:{self.artifact}"

    @property
    def missing(self) -> bool:
        return not self.system

    def capability(self) -> str:
        from sysjar.model import Coord
        return capability(Coord(self.group, self.artifact, self.classifier, self.extension))


def scan(path: Path, index: Index) -> list[Finding]:
    findings: list[Finding] = []
    if path.is_file():
        findings.extend(_scan_file(path, index))
        return _dedup(findings)
    reactor = discover_reactor(path)
    seen_poms = set()
    for pom in reactor.values():
        seen_poms.add(pom.resolve())
        findings.extend(_scan_pom(pom, index))
    if not reactor and (path / "pom.xml").is_file():
        pom = path / "pom.xml"
        seen_poms.add(pom.resolve())
        findings.extend(_scan_pom(pom, index))
    skip = {"target", "build", ".git", ".sysjar", ".gradle"}
    for pattern in ("*.gradle", "*.gradle.kts", "libs.versions.toml", "ivy.xml"):
        for candidate in path.rglob(pattern):
            if any(part in skip for part in candidate.relative_to(path).parts):
                continue
            if candidate.resolve() in seen_poms:
                continue
            findings.extend(_scan_file(candidate, index))
    # A root pom defines the reactor. A module taken out of <modules>
    # (what %pom_disable_module does) is still on disk, and Maven will
    # not build it. Its dependencies are not build requirements.
    # A tree with no root pom is scanned in full.
    if not reactor:
        for candidate in path.rglob("pom.xml"):
            if any(part in skip for part in candidate.relative_to(path).parts):
                continue
            if candidate.resolve() in seen_poms:
                continue
            findings.extend(_scan_pom(candidate, index))
    return _dedup(findings)


def _scan_file(path: Path, index: Index) -> list[Finding]:
    if path.name == "pom.xml":
        return _scan_pom(path, index)
    if path.name == "ivy.xml" or path.suffix == ".xml" and _root_is(path, "ivy-module"):
        return _scan_ivy(path, index)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name == "libs.versions.toml":
        return _scan_toml(text, path, index)
    return _scan_gradle(text, path, index)


def _root_is(path: Path, name: str) -> bool:
    try:
        root = parse_xml(path).getroot()
    except (OSError, ET.ParseError):
        return False
    return local_name(root.tag) == name


def _scan_pom(path: Path, index: Index) -> list[Finding]:
    try:
        root = parse_xml(path).getroot()
    except (OSError, ET.ParseError):
        return []
    if local_name(root.tag) != "project":
        return []
    findings = []
    for hit in collect_hits(root):
        if "dependencyManagement" in hit.ancestors or "pluginManagement" in hit.ancestors:
            continue
        if "profile" in hit.ancestors:
            continue
        requested = expand(hit.version_raw, pom_properties(root)) if hit.version_raw else ""
        found = index.find(hit.group, hit.artifact, hit.classifier, hit.extension, requested)
        if found is None and (hit.extension == "pom" or hit.kind == "parent"):
            found = index.find_pom(hit.group, hit.artifact, requested)
        findings.append(Finding(
            hit.group, hit.artifact, hit.version_raw, hit.scope, hit.kind, str(path),
            system=found.version if found else "",
            optional=hit.optional,
            classifier=hit.classifier,
            extension=hit.extension or "jar",
            owner_group=hit.owner_group,
            owner_artifact=hit.owner_artifact,
        ))
    return findings


def _scan_ivy(path: Path, index: Index) -> list[Finding]:
    try:
        root = parse_xml(path).getroot()
    except (OSError, ET.ParseError):
        return []
    findings = []
    for dep in root.iter():
        if local_name(dep.tag) != "dependency":
            continue
        group = dep.get("org") or dep.get("organisation") or ""
        artifact = dep.get("name") or ""
        if not group or not artifact:
            continue
        found = index.find(group, artifact, dep.get("m:classifier") or dep.get("classifier") or "")
        findings.append(Finding(
            group, artifact, dep.get("rev") or "", ivy_scope(dep.get("conf") or ""),
            "dependency", str(path),
            system=found.version if found else "",
            classifier=dep.get("m:classifier") or dep.get("classifier") or "",
        ))
    return findings


def _scan_gradle(text: str, path: Path, index: Index) -> list[Finding]:
    findings = []
    for match in _GRADLE_DEP.finditer(text):
        scope = _CONF_SCOPE.get(match.group("conf"), "compile")
        group = match.group("group")
        artifact = match.group("artifact")
        found = index.find(group, artifact)
        findings.append(Finding(
            group, artifact, match.group("version") or "", scope, "dependency", str(path),
            system=found.version if found else "",
            heuristic=True,
        ))
    return findings


def _scan_toml(text: str, path: Path, index: Index) -> list[Finding]:
    findings = []
    for match in _TOML_MODULE.finditer(text):
        group = match.group("group")
        artifact = match.group("artifact")
        found = index.find(group, artifact)
        findings.append(Finding(
            group, artifact, "", "compile", "dependency", str(path),
            system=found.version if found else "",
            heuristic=True,
        ))
    return findings


def _dedup(findings: list[Finding]) -> list[Finding]:
    seen = set()
    out = []
    for finding in findings:
        key = (finding.group, finding.artifact, finding.classifier, finding.extension, finding.scope, finding.kind, finding.source)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def _missing_build_dependency(finding: Finding) -> bool:
    """A jar mock should install before %build.

    Parents and plugins are omitted. The rewrite drops a plugin the system
    does not have, and a parent POM is already satisfied by the index. A
    dependency nested in such a plugin goes away with it. A dependency of a
    plugin that stays (the compiler, the jar plugin, and the rest of the
    keep list) is still resolved, so that jar has to be installed.
    """
    if finding.kind != "dependency":
        return False
    if finding.scope not in {"compile", "runtime", ""}:
        return False
    if finding.owner_artifact and (finding.owner_group, finding.owner_artifact) not in _KEEP_MISSING_PLUGINS:
        return False
    return True


def build_requires(findings: list[Finding], *, allow_missing: bool = False) -> list[str]:
    """Dependency names for %generate_buildrequires.

    One bare name per line. rpm records a ``BuildRequires:`` prefix as its
    own dependency, and mock then looks for a package of that name.

    With allow_missing, a coordinate the index already resolved is left out.
    The file is already in the buildroot, and requesting the capability can
    pull a second provider that does not install. Missing parents and plugins
    are left out too. A missing compile or runtime dependency is printed, so
    mock installs that jar before %build.
    """
    lines = []
    seen = set()
    for finding in findings:
        if finding.optional:
            continue
        if finding.scope in {"provided", "system"}:
            continue
        if allow_missing:
            if not finding.missing or not _missing_build_dependency(finding):
                continue
        cap = finding.capability()
        if not cap or cap in seen:
            continue
        seen.add(cap)
        lines.append(cap)
    return lines
