"""Read and rewrite Maven POMs so they resolve against the system index."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET

from sysjar.model import Coord, Dependency, Index
from sysjar.xmlutil import child, children, local_name, parse_xml, qname, remove_node, text_of, upsert, write_xml


PROP_RE = re.compile(r"\$\{([^}]+)\}")
VERSIONED = {"parent", "dependency", "plugin", "extension"}
SKIP_SUBTREES = {"exclusions", "configuration", "resources", "testResources", "filters", "pluginRepositories", "repositories"}
BUILTIN_PROPS = {
    "project.groupId", "pom.groupId", "groupId",
    "project.artifactId", "pom.artifactId", "artifactId",
    "project.version", "pom.version", "version",
    "project.parent.groupId", "project.parent.artifactId", "project.parent.version",
}
# javac in every JDK OpenMandriva still ships accepts 8 and rejects 7 and below.
COMPILER_FLOOR = 8
_COMPILER_PROPS = {
    "maven.compiler.source",
    "maven.compiler.target",
    "maven.compiler.release",
    "maven.compiler.testSource",
    "maven.compiler.testTarget",
}
_COMPILER_KEYS = ("source", "target", "release", "testSource", "testTarget")
# Missing copies of these stay in the POM. Dropping them would publish a jar
# that silently skipped code generation. Anything else Maven would try to
# resolve (site, javadoc, enforcer, an SCM extension) is removed instead.
_KEEP_MISSING_PLUGINS = {
    ("org.apache.maven.plugins", "maven-compiler-plugin"),
    ("org.apache.maven.plugins", "maven-resources-plugin"),
    ("org.apache.maven.plugins", "maven-jar-plugin"),
    ("org.apache.maven.plugins", "maven-war-plugin"),
    ("org.apache.maven.plugins", "maven-ear-plugin"),
    ("org.apache.maven.plugins", "maven-rar-plugin"),
    ("org.apache.maven.plugins", "maven-assembly-plugin"),
    ("org.apache.maven.plugins", "maven-shade-plugin"),
    ("org.apache.maven.plugins", "maven-dependency-plugin"),
    ("org.apache.maven.plugins", "maven-antrun-plugin"),
    ("org.apache.maven.plugins", "maven-plugin-plugin"),
    ("org.apache.felix", "maven-bundle-plugin"),
    ("biz.aQute.bnd", "bnd-maven-plugin"),
    ("org.codehaus.mojo", "build-helper-maven-plugin"),
    ("org.moditect", "moditect-maven-plugin"),
}
# tools.jar ceased to be an artifact on JDK 9. The classes are in the JDK.
_JDK_TOOLS = {("com.sun", "tools"), ("jdk.tools", "jdk.tools")}


@dataclass
class Gap:
    path: str
    coord: str
    requested: str
    kind: str
    scope: str = "compile"
    optional: bool = False
    blocking: bool = False

    def format(self) -> str:
        where = f"{self.path}: " if self.path else ""
        requested = f" requested {self.requested}" if self.requested else ""
        extra = []
        if self.optional:
            extra.append("optional")
        if self.scope and self.scope not in {"compile", ""}:
            extra.append(self.scope)
        if not self.blocking:
            extra.append("not required to compile")
        suffix = f" ({', '.join(extra)})" if extra else ""
        return f"{where}no system jar for {self.coord}{requested}{suffix}"


@dataclass
class Report:
    changes: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def extend(self, other: "Report") -> None:
        self.changes.extend(other.changes)
        self.gaps.extend(other.gaps)
        self.dropped.extend(other.dropped)

    @property
    def blocking(self) -> list[Gap]:
        return [gap for gap in self.gaps if gap.blocking]


@dataclass
class _Hit:
    elem: ET.Element
    kind: str
    group: str
    artifact: str
    version_raw: str
    classifier: str
    extension: str
    scope: str
    optional: bool
    prop: str
    blocking: bool
    ancestors: tuple[str, ...]


def pom_properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    box = child(root, "properties")
    if box is not None:
        for node in list(box):
            if node.text and local_name(node.tag):
                props[local_name(node.tag)] = node.text.strip()
    parent = child(root, "parent")
    pg = text_of(parent, "groupId")
    pa = text_of(parent, "artifactId")
    pv = text_of(parent, "version")
    group = text_of(root, "groupId") or pg
    artifact = text_of(root, "artifactId")
    version = text_of(root, "version") or pv
    builtin = {
        "project.groupId": group,
        "pom.groupId": group,
        "groupId": group,
        "project.artifactId": artifact,
        "pom.artifactId": artifact,
        "artifactId": artifact,
        "project.version": version,
        "pom.version": version,
        "version": version,
        "project.parent.groupId": pg,
        "project.parent.artifactId": pa,
        "project.parent.version": pv,
    }
    for key, value in builtin.items():
        if value:
            props.setdefault(key, value)
    return props


def expand(value: str, props: dict[str, str], depth: int = 0) -> str:
    if not value or depth > 10 or "${" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key in props:
            return expand(props[key], props, depth + 1)
        return match.group(0)

    return PROP_RE.sub(repl, value)


def project_identity(root: ET.Element) -> tuple[str, str, str]:
    props = pom_properties(root)
    return props.get("project.groupId", ""), props.get("project.artifactId", ""), props.get("project.version", "")


def _scope_of(elem: ET.Element, kind: str) -> str:
    if kind != "dependency":
        return "compile"
    return text_of(elem, "scope") or "compile"


def _blocking(kind: str, scope: str, optional: bool, ancestors: tuple[str, ...]) -> bool:
    if optional:
        return False
    if "dependencyManagement" in ancestors or "pluginManagement" in ancestors:
        return False
    if "profile" in ancestors and "activeByDefault" not in ancestors:
        # Refined below by the caller when the profile really is active by default.
        return False
    if kind == "parent":
        return True
    if kind == "dependency" and scope in {"compile", "runtime", "import", ""}:
        return True
    return False


def _profile_default(ancestors: tuple[ET.Element, ...]) -> bool:
    for node in ancestors:
        if local_name(node.tag) != "profile":
            continue
        activation = child(node, "activation")
        return text_of(activation, "activeByDefault") == "true"
    return False


def collect_hits(root: ET.Element) -> list[_Hit]:
    hits: list[_Hit] = []

    def walk(elem: ET.Element, ancestors: tuple[ET.Element, ...]) -> None:
        name = local_name(elem.tag)
        if name in SKIP_SUBTREES:
            return
        if name in VERSIONED and ancestors:
            props = pom_properties(root)
            group = expand(text_of(elem, "groupId"), props)
            artifact = expand(text_of(elem, "artifactId"), props)
            # Plugins may omit groupId. Maven's default is org.apache.maven.plugins.
            if name == "plugin" and artifact and not group:
                group = "org.apache.maven.plugins"
            version_raw = text_of(elem, "version")
            classifier = expand(text_of(elem, "classifier"), props)
            extension = expand(text_of(elem, "type") or text_of(elem, "extension"), props) or "jar"
            scope = _scope_of(elem, name)
            optional = text_of(elem, "optional").lower() == "true"
            prop = ""
            match = PROP_RE.fullmatch(version_raw.strip()) if version_raw else None
            if match:
                prop = match.group(1).strip()
            names = tuple(local_name(item.tag) for item in ancestors)
            blocking = _blocking(name, scope, optional, names)
            if "profile" in names and _profile_default(ancestors) and not optional:
                if name == "parent" or (name == "dependency" and scope in {"compile", "runtime", "import", ""}):
                    if "dependencyManagement" not in names and "pluginManagement" not in names:
                        blocking = True
            if group and artifact and "${" not in group and "${" not in artifact:
                hits.append(_Hit(
                    elem, name, group, artifact, version_raw, classifier, extension,
                    scope, optional, prop, blocking, names,
                ))
        for item in list(elem):
            walk(item, ancestors + (elem,))

    walk(root, ())
    return hits


def _property_assignments(root: ET.Element, prop: str) -> list[ET.Element]:
    """Project and profile values of one property.

    A JDK-activated profile replaces the project value. Rewriting only the
    project value leaves Maven requesting the profile's version.
    """
    found: list[ET.Element] = []

    def walk(elem: ET.Element, parent_name: str) -> None:
        name = local_name(elem.tag)
        if name in SKIP_SUBTREES:
            return
        if name == "properties" and parent_name in {"project", "profile"}:
            found.extend(node for node in list(elem) if local_name(node.tag) == prop)
            return
        for item in list(elem):
            walk(item, name)

    walk(root, "")
    return found


def _installed_version(
    index: Index,
    consumers: set[tuple[str, str, str, str, str]],
    requested: str,
) -> str | None:
    """System version shared by every artifact that reads this property value.

    None for a range or an unexpanded placeholder, when a consumer is absent,
    or when two consumers resolve to different jars. The caller then stamps
    the dependency itself, so the profile value cannot win.
    """
    if not requested or "${" in requested or requested[0] in "[(":
        return None
    versions: set[str] = set()
    for group, artifact, classifier, extension, kind in consumers:
        found = index.find(group, artifact, classifier, extension, requested)
        if found is None and (extension == "pom" or kind == "parent"):
            found = index.find_pom(group, artifact, requested)
        if found is None:
            return None
        versions.add(found.version)
    if len(versions) == 1:
        return next(iter(versions))
    return None


def _declared_management(root: ET.Element) -> set[tuple[str, str, str]]:
    """Coordinates whose version this POM already selects.

    A child that omits <version> is using this selection. Stamping the
    highest installed version on the child would hide it.
    """
    keys: set[tuple[str, str, str]] = set()
    props = pom_properties(root)
    managed = child(root, "dependencyManagement")
    for dep in children(child(managed, "dependencies"), "dependency"):
        group = expand(text_of(dep, "groupId"), props)
        artifact = expand(text_of(dep, "artifactId"), props)
        if group and artifact and "${" not in group and "${" not in artifact:
            keys.add(("dependency", group, artifact))
    plugins = child(child(root, "build"), "pluginManagement")
    for plugin in children(child(plugins, "plugins"), "plugin"):
        group = expand(text_of(plugin, "groupId"), props) or "org.apache.maven.plugins"
        artifact = expand(text_of(plugin, "artifactId"), props)
        if artifact and "${" not in group and "${" not in artifact:
            keys.add(("plugin", group, artifact))
    return keys


def _javac_level(value: str) -> int | None:
    match = re.fullmatch(r"(?:1\.)?(\d+)", value.strip())
    if not match:
        return None
    return int(match.group(1))


def _raise_level_text(node: ET.Element, floor: int, source: str, report: Report, seen: set[int]) -> None:
    if id(node) in seen:
        return
    text = (node.text or "").strip()
    if not text or "${" in text:
        return
    level = _javac_level(text)
    if level is None or level >= floor:
        return
    seen.add(id(node))
    node.text = str(floor)
    report.changes.append(f"{source}: compiler {local_name(node.tag)} {text} -> {floor}")


def _follow_compiler_property(
    root: ET.Element, prop: str, floor: int, source: str, report: Report, seen: set[int], depth: int = 0,
) -> None:
    if depth > 5 or not prop or prop in BUILTIN_PROPS:
        return
    for node in _property_assignments(root, prop):
        text = (node.text or "").strip()
        match = PROP_RE.fullmatch(text)
        if match:
            _follow_compiler_property(root, match.group(1).strip(), floor, source, report, seen, depth + 1)
            continue
        _raise_level_text(node, floor, source, report, seen)


def raise_compiler_level(root: ET.Element, floor: int = COMPILER_FLOOR, source: str = "") -> Report:
    """Raise javac source, target and release values this JDK will not accept.

    Java 5 and 6 sources are a subset of 8, and 8 is the oldest level both
    java-1.8.0-openjdk and the current JDK still compile. A parent POM is
    rewritten the same way when it is copied into the build repository, so a
    child inherits the raised level.
    """
    report = Report()
    seen: set[int] = set()
    for prop in _COMPILER_PROPS:
        _follow_compiler_property(root, prop, floor, source, report, seen)
    for plugin in root.iter():
        if local_name(plugin.tag) != "plugin":
            continue
        if text_of(plugin, "artifactId") != "maven-compiler-plugin":
            continue
        for node in plugin.iter():
            if local_name(node.tag) != "configuration":
                continue
            for key in _COMPILER_KEYS:
                elem = child(node, key)
                if elem is None:
                    continue
                text = (elem.text or "").strip()
                match = PROP_RE.fullmatch(text) if text else None
                if match:
                    _follow_compiler_property(root, match.group(1).strip(), floor, source, report, seen)
                else:
                    _raise_level_text(elem, floor, source, report, seen)
    return report


def drop_raw_compiler_levels(root: ET.Element, source: str = "") -> Report:
    """Remove raw javac -source and -target passed through compilerArguments.

    The compiler plugin already applies the language level. A parent that also
    forces -source, combined with a child that sets release, makes javac fail:
    --source cannot be used together with --release.
    """
    report = Report()
    for plugin in root.iter():
        if local_name(plugin.tag) != "plugin":
            continue
        if text_of(plugin, "artifactId") != "maven-compiler-plugin":
            continue
        for elem in list(plugin.iter()):
            name = local_name(elem.tag)
            if name == "compilerArguments":
                removed = [
                    local_name(node.tag)
                    for node in list(elem)
                    if local_name(node.tag) in {"source", "target"}
                ]
                for node in list(elem):
                    if local_name(node.tag) in {"source", "target"}:
                        elem.remove(node)
                if removed:
                    report.changes.append(
                        f"{source}: dropped compilerArguments {', '.join(removed)}"
                    )
                if not list(elem):
                    remove_node(plugin, elem)
            elif name == "compilerArgs":
                _drop_compiler_arg_levels(plugin, elem, source, report)
    return report


def _drop_compiler_arg_levels(plugin: ET.Element, elem: ET.Element, source: str, report: Report) -> None:
    args = [node for node in list(elem) if local_name(node.tag) == "arg"]
    drop: set[int] = set()
    index = 0
    while index < len(args):
        text = (args[index].text or "").strip()
        if text in {"-source", "--source", "-target", "--target"} and index + 1 < len(args):
            drop.add(id(args[index]))
            drop.add(id(args[index + 1]))
            index += 2
            continue
        if re.fullmatch(r"(?:-source|-target|--source|--target)(?:[= ].*)?", text):
            drop.add(id(args[index]))
        index += 1
    if not drop:
        return
    for node in list(elem):
        if id(node) in drop:
            elem.remove(node)
    report.changes.append(f"{source}: dropped compilerArgs source/target")
    if not list(elem):
        remove_node(plugin, elem)


def fix_snapshot_versions(root: ET.Element, source: str = "") -> Report:
    """Maven 4 rejects a version written as exactly ``SNAPSHOT``."""
    report = Report()
    for elem in root.iter():
        if local_name(elem.tag) != "version":
            continue
        if (elem.text or "").strip() == "SNAPSHOT":
            elem.text = "0-SNAPSHOT"
            report.changes.append(f"{source}: version SNAPSHOT -> 0-SNAPSHOT")
    return report


def rewrite_element(
    root: ET.Element,
    index: Index,
    *,
    reactor: set[tuple[str, str]] | None = None,
    drop: set[str] | None = None,
    source: str = "",
    managed: set[tuple[str, str, str]] | None = None,
    strip_uninstalled: bool = False,
) -> Report:
    """Rewrite dependency, plugin, extension and parent versions to the index.

    The project's own version is left alone. Reactor modules are left alone so
    a multi-module build still compiles its own jars. A dropped coordinate is
    removed, except a parent, which is reported instead. ``strip_uninstalled``
    removes plugins that are not installed, except plugins that produce the
    main artifact, and removes a test-scoped dependency that is not installed.
    Maven resolves that dependency even when tests are skipped. An installed
    test dependency stays, and a provided dependency stays because the
    compiler can import it. An import-scoped BOM is removed when it is not
    installed: Maven resolves it while building the model, unlike other
    dependencyManagement entries. A pluginManagement entry that declares
    executions is removed too, because Maven resolves that plugin while
    loading the project. A managed plugin that only pins a version stays.
    The project and the dependency POMs copied into
    the build repository are both rewritten that way, so a child does not
    inherit a site plugin the system does not have. ``--drop`` still removes
    a plugin that this leaves in place.
    """
    reactor = reactor or set()
    drop = drop or set()
    covered = set(managed or ()) | _declared_management(root)
    report = Report()
    hits = collect_hits(root)
    props_elem = child(root, "properties")
    prop_targets: dict[str, set[str]] = {}
    prop_consumers: dict[str, set[tuple[str, str, str, str, str]]] = {}
    literal: list[tuple[_Hit, str]] = []
    removed: set[int] = set()

    for hit in hits:
        if id(hit.elem) in removed:
            continue
        label = f"{hit.group}:{hit.artifact}"
        if label in drop or f"{hit.group}:{hit.artifact}:{hit.classifier}" in drop:
            if hit.kind == "parent":
                report.gaps.append(Gap(source, label, hit.version_raw, hit.kind, hit.scope, False, True))
                report.changes.append(f"{source}: refused to drop parent {label}")
                continue
            _mark_removed(hit.elem, removed)
            remove_node(root, hit.elem)
            report.dropped.append(f"{source}: dropped {hit.kind} {label}")
            continue
        # A module built in this reactor keeps the version in its source, parent included.
        if (hit.group, hit.artifact) in reactor:
            continue
        if not hit.version_raw and (hit.kind, hit.group, hit.artifact) in covered:
            # pluginManagement supplies the version. Maven still resolves a
            # plugin that sits in build/plugins, so an uninstalled managed
            # version fails the build. Drop that use. The managed entry stays.
            if _drop_uninstalled_managed_plugin(hit, root, index, strip_uninstalled):
                _mark_removed(hit.elem, removed)
                remove_node(root, hit.elem)
                report.dropped.append(f"{source}: dropped {hit.kind} {label} (not installed)")
                report.gaps.append(Gap(
                    source, _hit_coord(hit), hit.version_raw,
                    hit.kind, hit.scope, hit.optional, False,
                ))
            elif _drop_uninstalled_test_dependency(hit, root, index, strip_uninstalled):
                _mark_removed(hit.elem, removed)
                remove_node(root, hit.elem)
                report.dropped.append(f"{source}: dropped {hit.kind} {label} (test, not installed)")
                report.gaps.append(Gap(
                    source, _hit_coord(hit), hit.version_raw,
                    hit.kind, hit.scope, hit.optional, False,
                ))
            continue
        requested = expand(hit.version_raw, pom_properties(root)) if hit.version_raw else ""
        found = index.find(hit.group, hit.artifact, hit.classifier, hit.extension, requested)
        if found is None and (hit.extension == "pom" or hit.kind == "parent"):
            found = index.find_pom(hit.group, hit.artifact, requested)
        if found is None:
            # An import BOM is resolved while Maven builds the model. Other
            # dependencyManagement entries are not, and stay as defaults.
            if _drop_uninstalled_import(hit, root, strip_uninstalled):
                _mark_removed(hit.elem, removed)
                remove_node(root, hit.elem)
                report.dropped.append(f"{source}: dropped {hit.kind} {label} (import, not installed)")
                report.gaps.append(Gap(
                    source, _hit_coord(hit), hit.version_raw,
                    hit.kind, hit.scope, hit.optional, False,
                ))
                continue
            # Maven resolves a pluginManagement plugin that has executions.
            # One that only pins a version is left for children to inherit.
            if _drop_resolved_managed_plugin(hit, strip_uninstalled):
                _mark_removed(hit.elem, removed)
                remove_node(root, hit.elem)
                report.dropped.append(f"{source}: dropped {hit.kind} {label} (not installed)")
                report.gaps.append(Gap(
                    source, _hit_coord(hit), hit.version_raw,
                    hit.kind, hit.scope, hit.optional, False,
                ))
                continue
            # Managed entries are defaults. A direct dependency that needs one
            # is reported on its own. Listing every BOM line would hide the real gaps.
            if "dependencyManagement" in hit.ancestors or "pluginManagement" in hit.ancestors:
                continue
            # A versionless plugin has nothing Maven can resolve. The stage-0
            # launcher then asks for version SYSTEM and fails the build.
            # On the project being built, a versioned plugin that is not
            # installed is removed too, including one listed only for the
            # site report. Maven resolves both while loading the project.
            # Plugins that produce the jar stay, so a missing code generator
            # is still an error.
            if hit.kind in {"plugin", "extension"} and (
                "reporting" not in hit.ancestors or strip_uninstalled
            ):
                packaging = (hit.group, hit.artifact) in _KEEP_MISSING_PLUGINS
                if not hit.version_raw or (strip_uninstalled and not packaging):
                    _mark_removed(hit.elem, removed)
                    remove_node(root, hit.elem)
                    report.dropped.append(f"{source}: dropped {hit.kind} {label} (not installed)")
                    report.gaps.append(Gap(
                        source, _hit_coord(hit), hit.version_raw,
                        hit.kind, hit.scope, hit.optional, False,
                    ))
                    continue
            # tools.jar is not a Maven artifact on JDK 9 and newer.
            if hit.kind == "dependency" and (hit.group, hit.artifact) in _JDK_TOOLS:
                _mark_removed(hit.elem, removed)
                remove_node(root, hit.elem)
                report.dropped.append(f"{source}: dropped {hit.kind} {label} (provided by the JDK)")
                continue
            # -Dmaven.test.skip still resolves test dependencies. Drop one the
            # system does not have. A provided dependency stays: the sources
            # can import it, and removing it only hides that.
            if _drop_uninstalled_test_dependency(hit, root, index, strip_uninstalled):
                _mark_removed(hit.elem, removed)
                remove_node(root, hit.elem)
                report.dropped.append(f"{source}: dropped {hit.kind} {label} (test, not installed)")
                report.gaps.append(Gap(
                    source, _hit_coord(hit), hit.version_raw,
                    hit.kind, hit.scope, hit.optional, False,
                ))
                continue
            report.gaps.append(Gap(
                source, _hit_coord(hit), hit.version_raw,
                hit.kind, hit.scope, hit.optional, hit.blocking,
            ))
            continue
        if hit.prop and hit.prop not in BUILTIN_PROPS and props_elem is not None and child(props_elem, hit.prop) is not None:
            prop_targets.setdefault(hit.prop, set()).add(found.version)
            prop_consumers.setdefault(hit.prop, set()).add(
                (hit.group, hit.artifact, hit.classifier, hit.extension, hit.kind)
            )
            literal.append((hit, found.version))
        else:
            literal.append((hit, found.version))

    applied: set[str] = set()
    if props_elem is not None:
        for prop, versions in prop_targets.items():
            # One property shared by two artifacts that resolve apart cannot
            # be a single value. Those elements are stamped below instead.
            if len(versions) != 1:
                continue
            nodes = _property_assignments(root, prop)
            if not nodes:
                continue
            pending: list[tuple[ET.Element, str, str]] = []
            failed = False
            for node in nodes:
                current = (node.text or "").strip()
                target = _installed_version(index, prop_consumers[prop], current)
                if target is None:
                    failed = True
                    break
                pending.append((node, current, target))
            if failed:
                continue
            for node, current, target in pending:
                if current != target:
                    node.text = target
                    report.changes.append(f"{source}: property {prop} {current} -> {target}")
            applied.add(prop)

    for hit, version in literal:
        if hit.prop in applied:
            if hit.kind == "parent" and (hit.group, hit.artifact) not in reactor:
                _clear_relative_path(hit.elem)
            continue
        current = text_of(hit.elem, "version")
        if current != version:
            upsert(hit.elem, "version", version, after=("artifactId", "groupId"))
            report.changes.append(
                f"{source}: {hit.kind} {hit.group}:{hit.artifact} {current or '(managed)'} -> {version}"
            )
        if hit.kind == "parent" and (hit.group, hit.artifact) not in reactor:
            _clear_relative_path(hit.elem)
    return report


def _managed_plugin_versions(root: ET.Element, group: str, artifact: str) -> list[str]:
    versions: list[str] = []

    def walk(elem: ET.Element, ancestors: tuple[str, ...]) -> None:
        name = local_name(elem.tag)
        if name == "plugin" and "pluginManagement" in ancestors:
            plugin_group = text_of(elem, "groupId") or "org.apache.maven.plugins"
            plugin_artifact = text_of(elem, "artifactId")
            if plugin_group == group and plugin_artifact == artifact:
                versions.append(text_of(elem, "version"))
        for item in list(elem):
            walk(item, ancestors + (name,))

    walk(root, ())
    return versions


def _drop_uninstalled_import(hit: _Hit, root: ET.Element, strip_uninstalled: bool) -> bool:
    """An import-scoped BOM Maven resolves while building the project model."""
    if not strip_uninstalled or hit.kind != "dependency":
        return False
    return expand(hit.scope, pom_properties(root)) == "import"


def _drop_resolved_managed_plugin(hit: _Hit, strip_uninstalled: bool) -> bool:
    """A pluginManagement entry Maven resolves because it has executions."""
    if not strip_uninstalled or hit.kind not in {"plugin", "extension"}:
        return False
    if "pluginManagement" not in hit.ancestors:
        return False
    if (hit.group, hit.artifact) in _KEEP_MISSING_PLUGINS:
        return False
    return child(hit.elem, "executions") is not None


def _drop_uninstalled_test_dependency(hit: _Hit, root: ET.Element, index: Index, strip_uninstalled: bool) -> bool:
    """A project test dependency Maven would resolve even though tests are skipped.

    The caller has already failed to find a versioned coordinate. A versionless
    use takes its version from dependencyManagement, so this checks the index
    itself. plugin dependencies and dependencyManagement entries stay.
    """
    if not strip_uninstalled or hit.kind != "dependency":
        return False
    if "dependencyManagement" in hit.ancestors or "plugin" in hit.ancestors:
        return False
    if expand(hit.scope, pom_properties(root)) != "test":
        return False
    if hit.version_raw:
        return True
    return index.find(hit.group, hit.artifact, hit.classifier, hit.extension or "jar") is None


def _drop_uninstalled_managed_plugin(hit: _Hit, root: ET.Element, index: Index, strip_uninstalled: bool) -> bool:
    """A versionless build plugin whose managed version is not installed.

    A version that this POM does not declare comes from a reactor parent.
    That parent is rewritten first, so the use stays and Maven inherits the
    installed version. Dropping it skips code generation.
    """
    if not strip_uninstalled or hit.kind not in {"plugin", "extension"}:
        return False
    if "pluginManagement" in hit.ancestors or (hit.group, hit.artifact) in _KEEP_MISSING_PLUGINS:
        return False
    versions = _managed_plugin_versions(root, hit.group, hit.artifact)
    if not versions:
        return False
    props = pom_properties(root)
    for raw in versions:
        requested = expand(raw, props) if raw else ""
        if not requested or "${" in requested or requested[0] in "[(":
            continue
        if index.find(hit.group, hit.artifact, hit.classifier, hit.extension or "jar", requested):
            return False
    return True


def _mark_removed(elem: ET.Element, removed: set[int]) -> None:
    removed.add(id(elem))
    for item in list(elem):
        _mark_removed(item, removed)


def _hit_coord(hit: _Hit) -> str:
    coord = Coord(hit.group, hit.artifact, hit.classifier, hit.extension)
    return coord.display()


def _clear_relative_path(parent: ET.Element) -> None:
    node = child(parent, "relativePath")
    if node is None:
        upsert(parent, "relativePath", "", after=("version", "artifactId", "groupId"))
        node = child(parent, "relativePath")
    if node is not None:
        node.text = ""
        # Drop nested text so the element is empty. Maven treats that as "no relative parent".
        for item in list(node):
            node.remove(item)


def make_standalone(root: ET.Element, group: str, artifact: str, version: str) -> None:
    """Fill in coordinates inherited from a parent so the POM resolves on its own."""
    if group and not text_of(root, "groupId"):
        upsert(root, "groupId", group, after=("modelVersion",))
    if artifact and not text_of(root, "artifactId"):
        upsert(root, "artifactId", artifact, after=("groupId", "modelVersion"))
    if version:
        upsert(root, "version", version, after=("artifactId", "groupId", "modelVersion"))


def runtime_dependencies(root: ET.Element) -> list[Dependency]:
    """Direct compile/runtime dependencies. Plugins and managed deps are excluded."""
    deps: list[Dependency] = []
    for hit in collect_hits(root):
        if hit.kind != "dependency":
            continue
        if "plugin" in hit.ancestors or "extension" in hit.ancestors:
            continue
        if "dependencyManagement" in hit.ancestors:
            continue
        if "profile" in hit.ancestors:
            continue
        deps.append(Dependency(
            Coord(hit.group, hit.artifact, hit.classifier, hit.extension or "jar"),
            version=expand(hit.version_raw, pom_properties(root)) if hit.version_raw else "",
            scope=hit.scope or "compile",
            optional=hit.optional,
        ))
    return deps


def discover_reactor(root: Path) -> dict[tuple[str, str], Path]:
    pom = root / "pom.xml"
    found: dict[tuple[str, str], Path] = {}
    if not pom.is_file():
        return found

    def visit(path: Path, inherited_group: str, inherited_version: str) -> None:
        if not path.is_file():
            return
        try:
            tree = parse_xml(path)
        except (OSError, ET.ParseError):
            return
        element = tree.getroot()
        if local_name(element.tag) != "project":
            return
        group, _artifact, version = project_identity(element)
        group = group or inherited_group
        version = version or inherited_version
        artifact = text_of(element, "artifactId")
        if group and artifact:
            found[(group, artifact)] = path
        modules = child(element, "modules")
        for module in children(modules, "module"):
            name = (module.text or "").strip()
            if name:
                visit(path.parent / name / "pom.xml", group, version)

    visit(pom, "", "")
    return found


def iter_build_files(root: Path):
    if root.is_file():
        yield root
        return
    skip = {"target", "build", ".git", ".sysjar", ".gradle"}
    for pattern in ("pom.xml", "ivy.xml"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if any(part in skip for part in relative.parts):
                continue
            yield path


def _reactor_management(path: Path, reactor_map: dict[tuple[str, str], Path]) -> set[tuple[str, str, str]]:
    """Management declared by reactor parents of path. path itself is handled later."""
    covered: set[tuple[str, str, str]] = set()
    seen: set[Path] = set()
    current = path.resolve()
    while current not in seen:
        seen.add(current)
        try:
            root = parse_xml(current).getroot()
        except (OSError, ET.ParseError):
            break
        parent = child(root, "parent")
        if parent is None:
            break
        nxt = reactor_map.get((text_of(parent, "groupId"), text_of(parent, "artifactId")))
        if nxt is None:
            break
        current = nxt.resolve()
        if current in seen:
            break
        try:
            covered |= _declared_management(parse_xml(current).getroot())
        except (OSError, ET.ParseError):
            break
    return covered


def rewrite_path(
    path: Path,
    index: Index,
    *,
    reactor: set[tuple[str, str]] | None = None,
    drop: set[str] | None = None,
    inplace: bool = True,
    dest: Path | None = None,
    force: tuple[str, str, str] | None = None,
    managed: set[tuple[str, str, str]] | None = None,
    strip_uninstalled: bool = False,
) -> Report:
    tree = parse_xml(path)
    root = tree.getroot()
    name = local_name(root.tag)
    source = str(path)
    if name == "ivy-module":
        from sysjar.ivy import rewrite_ivy_element
        report = rewrite_ivy_element(root, index, drop=drop, source=source)
    elif name == "project":
        report = rewrite_element(
            root, index, reactor=reactor, drop=drop, source=source, managed=managed,
            strip_uninstalled=strip_uninstalled,
        )
        report.extend(raise_compiler_level(root, source=source))
        report.extend(drop_raw_compiler_levels(root, source=source))
        report.extend(fix_snapshot_versions(root, source=source))
        if force is not None:
            make_standalone(root, *force)
    else:
        report = Report()
        report.gaps.append(Gap(source, "", "", "file", blocking=False))
        report.changes.append(f"{source}: skipped unrecognized root <{name}>")
        return report
    if inplace:
        write_xml(tree, path)
    elif dest is not None:
        write_xml(tree, dest)
    return report


def pin_toolchain_plugins(
    root: ET.Element,
    index: Index,
    versions: dict[tuple[str, str], str],
    *,
    source: str = "",
) -> Report:
    """Point plugins that are not installed at the toolchain copy.

    A project often pins maven-resources-plugin to 3.3.1. The toolchain
    ships 3.3.1.MBI, and Maven looks up the pin before its own binding.
    An installed jar wins, so this does not hide a packaged plugin.
    """
    report = Report()
    if not versions:
        return report
    props = pom_properties(root)
    for plugin in root.iter():
        if local_name(plugin.tag) != "plugin":
            continue
        group = expand(text_of(plugin, "groupId"), props) or "org.apache.maven.plugins"
        artifact = expand(text_of(plugin, "artifactId"), props)
        if not artifact or "${" in group or "${" in artifact:
            continue
        want = versions.get((group, artifact))
        if not want:
            continue
        if index.find(group, artifact, "", "jar") is not None:
            continue
        current = expand(text_of(plugin, "version"), props)
        if current == want:
            continue
        upsert(plugin, "version", want, after=("artifactId", "groupId"))
        shown = current or "(unset)"
        report.changes.append(f"{source}: toolchain {group}:{artifact} {shown} -> {want}")
    return report


def pin_lifecycle_plugins(
    root: ET.Element,
    index: Index,
    defaults: dict[tuple[str, str], str],
    *,
    source: str = "",
    managed: set[tuple[str, str, str]] | None = None,
) -> Report:
    """Select the installed jar for Maven's built-in lifecycle plugins.

    The binding inside Maven names an exact upstream version. A bootstrap jar
    is published as that version plus ``.MBI``, so the binding misses it.
    pluginManagement is consulted before the binding. A version this POM or
    its reactor parent already selects is left alone.
    """
    report = Report()
    if not defaults:
        return report
    covered = set(managed or ()) | _declared_management(root)
    declared = _build_plugin_ids(root)
    additions: list[tuple[str, str, str]] = []
    for (group, artifact), requested in defaults.items():
        if ("plugin", group, artifact) in covered or (group, artifact) in declared:
            continue
        found = index.find(group, artifact, "", "jar", requested)
        if found is None:
            continue
        additions.append((group, artifact, found.version))
    if not additions:
        return report
    build = child(root, "build")
    if build is None:
        build = ET.SubElement(root, qname(root, "build"))
    management = child(build, "pluginManagement")
    if management is None:
        management = ET.SubElement(build, qname(build, "pluginManagement"))
    plugins = child(management, "plugins")
    if plugins is None:
        plugins = ET.SubElement(management, qname(management, "plugins"))
    for group, artifact, version in additions:
        plugin = ET.SubElement(plugins, qname(plugins, "plugin"))
        upsert(plugin, "groupId", group)
        upsert(plugin, "artifactId", artifact, after=("groupId",))
        upsert(plugin, "version", version, after=("artifactId", "groupId"))
        report.changes.append(
            f"{source}: lifecycle plugin {group}:{artifact} -> {version}"
        )
    return report


def _build_plugin_ids(root: ET.Element) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    props = pom_properties(root)
    for plugin in children(child(child(root, "build"), "plugins"), "plugin"):
        group = expand(text_of(plugin, "groupId"), props) or "org.apache.maven.plugins"
        artifact = expand(text_of(plugin, "artifactId"), props)
        if artifact and "${" not in group and "${" not in artifact:
            found.add((group, artifact))
    return found


def drop_versionless_managed_plugins(
    root: ET.Element,
    index: Index,
    *,
    source: str = "",
) -> Report:
    """Remove a managed plugin that still has no concrete version.

    Maven resolves the version of every pluginManagement entry. One that
    names no version, or only a property that did not expand, asks the
    repository for maven-metadata.xml and fails offline. A toolchain pin
    runs first, so a plugin shipped there already has a version. An
    installed jar keeps its configuration.
    """
    report = Report()
    props = pom_properties(root)
    doomed: list[tuple[ET.Element, str, str]] = []

    def walk(elem: ET.Element, ancestors: tuple[str, ...]) -> None:
        name = local_name(elem.tag)
        if name == "plugin" and "pluginManagement" in ancestors:
            group = expand(text_of(elem, "groupId"), props) or "org.apache.maven.plugins"
            artifact = expand(text_of(elem, "artifactId"), props)
            version = expand(text_of(elem, "version"), props)
            concrete = bool(version) and "${" not in version and version[:1] not in {"[", "("}
            if (
                artifact
                and "${" not in group
                and "${" not in artifact
                and not concrete
                and index.find(group, artifact, "", "jar") is None
            ):
                doomed.append((elem, group, artifact))
        for child_node in list(elem):
            walk(child_node, ancestors + (name,))

    walk(root, ())
    for elem, group, artifact in doomed:
        remove_node(root, elem)
        report.dropped.append(f"{source}: dropped plugin {group}:{artifact} (no version)")
        report.gaps.append(Gap(
            source, f"{group}:{artifact}", "", "plugin", "compile", False, False,
        ))
    return report


def rewrite_tree(
    root: Path,
    index: Index,
    *,
    drop: set[str] | None = None,
    reactor_only: bool = False,
    lifecycle: dict[tuple[str, str], str] | None = None,
    toolchain: dict[tuple[str, str], str] | None = None,
) -> Report:
    reactor_map = discover_reactor(root if root.is_dir() else root.parent)
    reactor = set(reactor_map)
    report = Report()
    start = root if root.is_dir() else root.parent
    # A Maven build must not rewrite the POMs it ships as test data. Those
    # files describe projects that are supposed to be missing or broken.
    if reactor_only and root.is_dir() and reactor_map:
        files = list(reactor_map.values())
    else:
        files = [root] if root.is_file() else list(iter_build_files(start))
    for path in files:
        try:
            managed = _reactor_management(path, reactor_map) if reactor_map else None
            report.extend(rewrite_path(
                path, index, reactor=reactor, drop=drop, inplace=True, managed=managed,
                strip_uninstalled=True,
            ))
            tree = parse_xml(path)
            if local_name(tree.getroot().tag) != "project":
                continue
            changed = False
            if lifecycle:
                pinned = pin_lifecycle_plugins(
                    tree.getroot(), index, lifecycle, source=str(path), managed=managed,
                )
                changed = bool(pinned.changes)
                report.extend(pinned)
            if toolchain:
                pinned = pin_toolchain_plugins(
                    tree.getroot(), index, toolchain, source=str(path),
                )
                changed = changed or bool(pinned.changes)
                report.extend(pinned)
            dropped = drop_versionless_managed_plugins(
                tree.getroot(), index, source=str(path),
            )
            changed = changed or bool(dropped.dropped)
            report.extend(dropped)
            if changed:
                write_xml(tree, path)
        except (OSError, ET.ParseError) as exc:
            # A fixture POM that is not well-formed XML must not abort the build.
            report.changes.append(f"{path}: skipped unreadable file ({exc})")
    return report
