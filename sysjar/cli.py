"""Command line for sysjar."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from sysjar import __version__
from sysjar.build import prepare_ant, prepare_gradle, prepare_ivy, prepare_maven
from sysjar.index import load_index
from sysjar.install import install_auto, install_pom, parse_alias, write_filelist
from sysjar.ivy import write_ivy_settings
from sysjar.model import SysjarError, parse_coord
from sysjar.pom import rewrite_tree
from sysjar.repo import default_repo, materialize, toolchain_plugin_versions
from sysjar.rpmmeta import load_record, open_packaged, provides_for, requires_for
from sysjar.scan import build_requires, scan


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        passthrough = argv[cut + 1:]
        argv = argv[:cut]
    parser = argparse.ArgumentParser(
        prog="sysjar",
        description="Build Maven, Gradle, Ant and Ivy projects against system jars. "
                    "Arguments after -- are passed to mvn, gradle or ant.",
    )
    parser.add_argument("--version", action="version", version=f"sysjar {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def with_index(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
        command.add_argument("--root", action="append", default=[], metavar="DIR",
                             help="filesystem root that holds usr/share/sysjar and usr/share/maven-metadata. Repeatable. Default: /")
        command.add_argument("--bootstrap", action="store_true",
                             help="fill missing coordinates from javapackages-bootstrap metadata under each --root. A real package always wins")
        command.add_argument("--bootstrap-root", action="append", default=[], metavar="DIR",
                             help="extracted tree whose usr/share/javapackages-bootstrap is a fallback index. Repeatable")
        command.add_argument("-q", "--quiet", action="store_true")
        return command

    with_index(sub.add_parser("list", help="list system jars"))
    resolve = with_index(sub.add_parser("resolve", help="print the file for a coordinate"))
    resolve.add_argument("coord")
    classpath = with_index(sub.add_parser("classpath", help="print a classpath of system jars"))
    classpath.add_argument("coords", nargs="+")
    repo = with_index(sub.add_parser("repo", help="write a Maven repository of symlinks to system jars"))
    repo.add_argument("--dest", type=Path, default=None)
    rewrite = with_index(sub.add_parser("rewrite", help="rewrite POM and ivy versions to system jars"))
    rewrite.add_argument("path", type=Path)
    rewrite.add_argument("--drop", action="append", default=[], metavar="GROUP:ARTIFACT")
    check = with_index(sub.add_parser("check", help="report index entries whose files are missing"))

    mvn = with_index(sub.add_parser("mvn", help="rewrite the project and run Maven offline"))
    mvn.add_argument("--project", type=Path, default=None, help="project directory. Default: current directory")
    mvn.add_argument("--drop", action="append", default=[], metavar="GROUP:ARTIFACT")
    mvn.add_argument("--allow-missing", action="store_true", help="run Maven even if a required dependency is not installed")
    mvn.add_argument("--mvn", default="mvn", help="Maven launcher to run. Default: mvn on PATH")
    mvn.add_argument("--print", dest="print_cmd", action="store_true", help="print the mvn command and do not run it")

    gradle = with_index(sub.add_parser("gradle", help="run Gradle offline against system jars"))
    gradle.add_argument("--project", type=Path, default=None, help="project directory. Default: current directory")
    gradle.add_argument("--strict-repos", action="store_true", help="fail if the build declares its own repositories")
    gradle.add_argument("--keep-repos", action="store_true", help="leave project repositories in place")
    gradle.add_argument("--daemon", action="store_true")
    gradle.add_argument("--user-home", action="store_true", help="use the default Gradle user home instead of a private one")
    gradle.add_argument("--gradle", default="gradle", help="Gradle launcher to run. Default: gradle on PATH")
    gradle.add_argument("--print", dest="print_cmd", action="store_true")

    ant = with_index(sub.add_parser("ant", help="rewrite Ivy modules, if any, and run Ant against the system repo"))
    ant.add_argument("--project", type=Path, default=None, help="project directory. Default: current directory")
    ant.add_argument("--drop", action="append", default=[], metavar="GROUP:ARTIFACT")
    ant.add_argument("--allow-missing", action="store_true", help="run Ant even if a required dependency is not installed")
    ant.add_argument("--ant", default="ant", help="Ant launcher to run. Default: ant on PATH")
    ant.add_argument("--print", dest="print_cmd", action="store_true", help="print the ant command and do not run it")

    ivy = with_index(sub.add_parser("ivy", help="rewrite ivy.xml files and write ivysettings.xml"))
    ivy.add_argument("--project", type=Path, default=None)
    ivy.add_argument("--drop", action="append", default=[])
    ivy.add_argument("--print", dest="print_cmd", action="store_true")

    install = sub.add_parser("install", help="install jars and POMs into a buildroot")
    install.add_argument("--buildroot", type=Path, required=True)
    install.add_argument("--auto", action="store_true", help="discover pom.xml files, or .sysjar/reactor from a sysjar gradle build")
    install.add_argument("--artifact", action="append", default=[], metavar="POM[:JAR]")
    install.add_argument("--alias", action="append", default=[], metavar="GROUP:ARTIFACT")
    install.add_argument("--file-name", default="", help="jar and pom file name without extension. One artifact, or a one-module --auto install.")
    install.add_argument("--link", action="append", default=[], metavar="NAME", help="extra jar symlink, without .jar. One artifact. Not a Maven coordinate.")
    install.add_argument("--filelist", type=Path, help="write an rpm %%files list for the installed jars, poms and index files")
    install.add_argument("project", nargs="?", type=Path, default=Path("."))

    scan_cmd = with_index(sub.add_parser("scan", help="show which dependencies are system jars"))
    scan_cmd.add_argument("path", type=Path, nargs="?", default=Path("."))
    builddep = with_index(sub.add_parser("builddep", help="print dependency names for a project"))
    builddep.add_argument("--allow-missing", action="store_true",
                          help="omit requirements that are not installed, and exit 0")
    builddep.add_argument("--drop", action="append", default=[], metavar="GROUP:ARTIFACT",
                          help="do not require this coordinate. Repeatable")
    builddep.add_argument("path", type=Path, nargs="?", default=Path("."))

    provides = sub.add_parser("provides", help="print RPM Provides for a .sysjar file")
    provides.add_argument("file")
    requires = sub.add_parser("requires", help="print RPM Requires for a .sysjar file")
    requires.add_argument("file")

    ivy_settings = with_index(sub.add_parser("ivy-settings", help="write ivysettings.xml for the system repository"))
    ivy_settings.add_argument("--dest", type=Path, required=True)

    args = parser.parse_args(argv)
    args.passthrough = passthrough
    try:
        return _run(args)
    except SysjarError as exc:
        print(f"sysjar: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


def _run(args: argparse.Namespace) -> int:
    cmd = args.cmd
    if cmd == "provides":
        data = load_record(open_packaged(args.file))
        for line in provides_for(data):
            print(line)
        return 0
    if cmd == "requires":
        data = load_record(open_packaged(args.file))
        for line in requires_for(data):
            print(line)
        return 0
    if cmd == "install":
        return _install(args)

    index = load_index(_roots(args), _bootstrap_roots(args))
    quiet = getattr(args, "quiet", False)

    if cmd == "list":
        for art in index.artifacts():
            location = art.path or art.pom or "-"
            print(f"{art.coord.display()} {art.version} {location}")
        return 0
    if cmd == "resolve":
        coord, requested = parse_coord(args.coord)
        art = index.find(coord.group, coord.artifact, coord.classifier, coord.extension)
        if art is None:
            print(f"no system jar for {coord.display()}", file=sys.stderr)
            return 1
        if requested and requested != art.version and not quiet:
            print(f"{coord.ga()}: requested {requested}, system jar is {art.version}", file=sys.stderr)
        print(art.path or art.pom)
        return 0
    if cmd == "classpath":
        paths = []
        missing = False
        for text in args.coords:
            coord, _requested = parse_coord(text)
            art = index.find(coord.group, coord.artifact, coord.classifier, coord.extension)
            if art is None or not (art.path or art.pom):
                print(f"no system jar for {coord.display()}", file=sys.stderr)
                missing = True
                continue
            paths.append(art.path or art.pom)
        if missing:
            return 1
        print(":".join(paths))
        return 0
    if cmd == "repo":
        dest = args.dest or default_repo()
        warnings = materialize(index, dest)
        _warn(warnings, quiet)
        if not quiet:
            print(dest)
        return 0
    if cmd == "check":
        bad = 0
        for art in index.artifacts():
            path = art.path or art.pom
            if not path or not Path(path).is_file():
                print(f"missing {art.coord.display()} {art.version} {path or '(no path)'}")
                bad += 1
        return 1 if bad else 0
    if cmd == "rewrite":
        report = rewrite_tree(
            args.path, index, drop=set(args.drop), toolchain=toolchain_plugin_versions(),
        )
        return _report(report, quiet, fail_blocking=True)
    if cmd == "ivy-settings":
        dest = args.dest
        repo = default_repo()
        if not repo.is_dir():
            materialize(index, repo)
        write_ivy_settings(dest, repo)
        if not quiet:
            print(dest)
        return 0
    if cmd == "scan":
        findings = scan(args.path, index)
        for finding in findings:
            state = f"system {finding.system}" if finding.system else "MISSING"
            requested = f" requested {finding.version}" if finding.version else ""
            mark = " heuristic" if finding.heuristic else ""
            print(f"{finding.source}: {finding.kind} {finding.label} {state}{requested}{mark}")
        return 1 if any(item.missing and not item.optional and item.scope not in {"provided", "test", "system"} and item.kind != "plugin" for item in findings) else 0
    if cmd == "builddep":
        findings = scan(args.path, index)
        dropped = set(args.drop)
        if dropped:
            findings = [
                item for item in findings
                if item.label not in dropped and f"{item.label}:{item.classifier}" not in dropped
            ]
        for line in build_requires(findings, allow_missing=args.allow_missing):
            print(line)
        missing = [item for item in findings if item.missing and not item.optional and item.scope not in {"provided", "system"}]
        for item in missing:
            print(f"missing {item.capability()}", file=sys.stderr)
        if args.allow_missing:
            return 0
        return 1 if missing else 0
    if cmd == "mvn":
        return _mvn(args, index, quiet)
    if cmd == "gradle":
        return _gradle(args, index, quiet)
    if cmd == "ant":
        return _ant(args, index, quiet)
    if cmd == "ivy":
        return _ivy(args, index, quiet)
    raise SysjarError(f"unknown command {cmd}")


def _mvn(args: argparse.Namespace, index, quiet: bool) -> int:
    project = args.project or Path.cwd()
    work = project / ".sysjar" / "work"
    command, report, warnings = prepare_maven(project, index, work, drop=set(args.drop), mvn=args.mvn)
    command.extend(args.passthrough)
    status = _report(report, quiet, fail_blocking=not args.allow_missing)
    _warn(warnings, quiet)
    if status != 0:
        return status
    if args.print_cmd:
        print(shlex.join(command))
        return 0
    _exec(command)
    return 1


def _gradle(args: argparse.Namespace, index, quiet: bool) -> int:
    project = args.project or Path.cwd()
    work = project / ".sysjar" / "work"
    command, warnings = prepare_gradle(
        project, index, work,
        strict_repos=args.strict_repos,
        keep_repos=args.keep_repos,
        isolate_home=not args.user_home,
        daemon=args.daemon,
        gradle=args.gradle,
    )
    command.extend(args.passthrough)
    _warn(warnings, quiet)
    if args.print_cmd:
        print(shlex.join(command))
        return 0
    _exec(command)
    return 1


def _ant(args: argparse.Namespace, index, quiet: bool) -> int:
    project = args.project or Path.cwd()
    work = project / ".sysjar" / "work"
    command, report, warnings = prepare_ant(project, index, work, drop=set(args.drop), ant=args.ant)
    command.extend(args.passthrough)
    status = _report(report, quiet, fail_blocking=not args.allow_missing)
    _warn(warnings, quiet)
    if status != 0:
        return status
    if args.print_cmd:
        print(shlex.join(command))
        return 0
    _exec(command)
    return 1


def _ivy(args: argparse.Namespace, index, quiet: bool) -> int:
    project = args.project or Path.cwd()
    work = project / ".sysjar" / "work"
    settings, report, warnings = prepare_ivy(project, index, work, drop=set(args.drop))
    status = _report(report, quiet, fail_blocking=True)
    _warn(warnings, quiet)
    if status != 0:
        return status
    if not quiet or args.print_cmd:
        print(f"ivy -settings {settings}")
    return 0


def _install(args: argparse.Namespace) -> int:
    aliases = [parse_alias(item) for item in args.alias]
    links = list(args.link)
    if not args.auto and not args.artifact:
        raise SysjarError("pass --auto, or --artifact POM[:JAR]")
    if (args.file_name or aliases or links) and args.auto and args.artifact:
        raise SysjarError("--file-name, --alias and --link apply to one install, not --auto combined with --artifact")
    if (args.file_name or aliases or links) and not args.auto and len(args.artifact) != 1:
        raise SysjarError("--file-name, --alias and --link apply to a single --artifact")
    written = []
    if args.auto:
        written.extend(install_auto(
            args.buildroot, args.project,
            file_name=args.file_name, aliases=aliases, links=links,
        ))
    for item in args.artifact:
        pom_text, sep, jar_text = item.partition(":")
        pom = Path(pom_text)
        jar = Path(jar_text) if sep else None
        written.append(install_pom(
            args.buildroot, pom, jar,
            file_name=args.file_name,
            aliases=aliases,
            links=links,
        ))
    if args.filelist:
        write_filelist(args.filelist, written)
    for path in written:
        print(path)
    return 0


def _roots(args: argparse.Namespace) -> list[Path]:
    if args.root:
        return [Path(item) for item in args.root]
    return [Path("/")]


def _bootstrap_roots(args: argparse.Namespace) -> list[Path]:
    found = [Path(item) for item in getattr(args, "bootstrap_root", []) or []]
    if getattr(args, "bootstrap", False):
        for root in _roots(args):
            if root not in found:
                found.append(root)
    return found


def _report(report, quiet: bool, *, fail_blocking: bool) -> int:
    if not quiet:
        for line in report.dropped + report.changes:
            print(line)
    blocking = report.blocking
    for gap in report.gaps:
        stream = sys.stderr if gap.blocking else sys.stderr
        if gap.blocking or not quiet:
            print(gap.format(), file=stream)
    if fail_blocking and blocking:
        return 1
    return 0


def _warn(warnings: list[str], quiet: bool) -> None:
    if quiet:
        return
    for line in warnings:
        print(f"sysjar: {line}", file=sys.stderr)


def _exec(command: list[str]) -> None:
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        raise SysjarError(f"cannot run {command[0]}: {exc}") from exc
