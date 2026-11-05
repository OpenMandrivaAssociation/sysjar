"""Prepare an offline Maven or Gradle command against the system index."""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from sysjar.gradle import write_init_script
from sysjar.ivy import find_ivy_files, write_ivy_settings
from sysjar.model import Index, version_key
from sysjar.pom import Report, rewrite_path, rewrite_tree
from sysjar.repo import TOOLCHAIN_REPO, materialize, overlay_repo, write_settings

# group:artifact:version:goal, as Maven writes it in default-bindings.xml.
_PHASE_PLUGIN = re.compile(
    r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)"
)


def lifecycle_defaults(mvn: str) -> dict[tuple[str, str], str]:
    """Plugin versions named by this Maven's built-in lifecycle bindings.

    An empty dict means the launcher could not be read. The build then keeps
    Maven's own binding, which misses a bootstrap jar whose version ends in
    ``.MBI``.
    """
    binary = Path(mvn)
    if not binary.is_file():
        found = shutil.which(mvn)
        binary = Path(found) if found else binary
    if not binary.is_file():
        return {}
    home = binary.resolve().parent.parent
    chosen: dict[tuple[str, str], str] = {}
    jars: list[Path] = []
    for directory in (home, home / "lib", home / "lib" / "ext"):
        if directory.is_dir():
            jars.extend(directory.glob("*.jar"))
    for jar in jars:
        try:
            with zipfile.ZipFile(jar) as archive:
                name = "META-INF/plexus/default-bindings.xml"
                if name not in archive.namelist():
                    continue
                text = archive.read(name).decode("utf-8", "replace")
        except (OSError, zipfile.BadZipFile):
            continue
        for group, artifact, version, _goal in _PHASE_PLUGIN.findall(text):
            key = (group, artifact)
            current = chosen.get(key)
            if current is None or version_key(version) > version_key(current):
                chosen[key] = version
    return chosen


def prepare_maven(
    project: Path,
    index: Index,
    work: Path,
    *,
    drop: set[str] | None = None,
    mvn: str = "mvn",
) -> tuple[list[str], Report, list[str]]:
    report = rewrite_tree(
        project, index, drop=drop, reactor_only=True, lifecycle=lifecycle_defaults(mvn),
    )
    repo = work / "repo"
    warnings = materialize(index, repo, drop=drop)
    # Lifecycle plugins are not indexed. Link the ones Maven itself binds
    # after the index, so an indexed jar of the same path wins.
    overlay_repo(TOOLCHAIN_REPO, repo)
    settings = work / "settings.xml"
    write_settings(settings, repo)
    local = work / "local"
    local.mkdir(parents=True, exist_ok=True)
    pom = project / "pom.xml" if project.is_dir() else project
    command = [
        mvn, "--batch-mode",
        "-s", str(settings),
        f"-Dmaven.repo.local={local}",
        "-f", str(pom),
    ]
    return command, report, warnings


def prepare_gradle(
    project: Path,
    index: Index,
    work: Path,
    *,
    strict_repos: bool = False,
    keep_repos: bool = False,
    isolate_home: bool = True,
    daemon: bool = False,
    gradle: str = "gradle",
) -> tuple[list[str], list[str]]:
    repo = work / "repo"
    warnings = materialize(index, repo)
    script = write_init_script(
        work / "init.gradle", index, repo,
        strict_repos=strict_repos, keep_repos=keep_repos,
    )
    command = [gradle, "--offline", "--project-dir", str(project), "-I", str(script)]
    if not daemon:
        command.append("--no-daemon")
    if isolate_home:
        home = work / "gradle-home"
        home.mkdir(parents=True, exist_ok=True)
        command.extend(["-g", str(home)])
    return command, warnings


def prepare_ant(
    project: Path,
    index: Index,
    work: Path,
    *,
    drop: set[str] | None = None,
    ant: str = "ant",
) -> tuple[list[str], Report, list[str]]:
    """Rewrite Ivy modules, point Ant at the system repo, and build the ant command.

    A later ``-f`` on the command line overrides the build file. Ant keeps
    the last one. Projects that already ship ivysettings.xml are left alone.
    Ivy loads a settings file next to ivy.xml when the build does not name one.
    """
    root = project if project.is_dir() else project.parent
    files = find_ivy_files(root)
    report = Report()
    warnings: list[str] = []
    command = [ant]
    if files:
        for path in files:
            report.extend(rewrite_path(path, index, drop=drop, inplace=True))
        repo = work / "repo"
        warnings = materialize(index, repo, drop=drop)
        settings = work / "ivysettings.xml"
        settings.parent.mkdir(parents=True, exist_ok=True)
        write_ivy_settings(settings, repo)
        for path in files:
            sibling = path.with_name("ivysettings.xml")
            if not sibling.exists():
                write_ivy_settings(sibling, repo)
        command.append(f"-Divy.settings.file={settings.resolve()}")
    build_file = project if project.is_file() else project / "build.xml"
    if build_file.is_file():
        command.extend(["-f", str(build_file.resolve())])
    return command, report, warnings


def prepare_ivy(project: Path, index: Index, work: Path, *, drop: set[str] | None = None) -> tuple[Path, Report, list[str]]:
    report = rewrite_tree(project, index, drop=drop)
    repo = work / "repo"
    warnings = materialize(index, repo, drop=drop)
    settings = work / "ivysettings.xml"
    write_ivy_settings(settings, repo)
    return settings, report, warnings
