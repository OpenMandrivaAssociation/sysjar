"""Declarative BuildSystem: maven, gradle, ant and ivy."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MACROS = ROOT / "macros.sysjar"
SYSJAR_BIN = ROOT / "bin"


def macro_path() -> str:
    target = subprocess.check_output(["rpm", "--eval", "%{_target}"], text=True).strip()
    parts = [
        "/usr/lib/rpm/macros",
        "/usr/lib/rpm/macros.d/*.macros",
        "/usr/lib/rpm/macros.d/macros.*",
        f"/usr/lib/rpm/platform/{target}/macros",
        "/usr/lib/rpm/fileattrs/*.attr",
        "/usr/lib/rpm/openmandriva/macros",
        "/etc/rpm/macros.d/*.macros",
        "/etc/rpm/macros.*",
        "/etc/rpm/macros",
        f"/etc/rpm/{target}/macros",
        str(MACROS),
    ]
    return ":".join(parts)


class MacroProvidesTests(unittest.TestCase):
    def test_buildsystem_provides_and_launchers_are_not_double_dashed(self):
        text = MACROS.read_text(encoding="utf-8")
        for launcher in ("%sysjar_mvn", "%sysjar_gradle", "%sysjar_ant", "%sysjar_ivy"):
            self.assertNotIn(f"{launcher} --", text)
        script = Path("/usr/lib/rpm/rpm_macros_provides.sh")
        if not script.is_file():
            self.skipTest("rpm macro provides generator is not installed")
        proc = subprocess.run(
            ["bash", str(script)],
            input=str(MACROS) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        provides = {line for line in proc.stdout.splitlines() if line.startswith("rpm-build(")}
        self.assertEqual(provides, {
            "rpm-build(maven)",
            "rpm-build(gradle)",
            "rpm-build(ant)",
            "rpm-build(ivy)",
        })
        self.assertNotIn("rpm_macro(__", proc.stdout)

    def test_static_buildrequires(self):
        if shutil.which("rpmspec") is None:
            self.skipTest("rpmspec is not installed")
        # rpmspec --buildrequires runs %prep, and the default prep is %autosetup.
        top = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top)
        sources = top / "SOURCES"
        src = top / "src" / "demo-1"
        src.mkdir(parents=True)
        sources.mkdir()
        (src / "pom.xml").write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<project>\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>com.example</groupId>\n"
            "  <artifactId>demo</artifactId>\n"
            "  <version>1</version>\n"
            "</project>\n",
            encoding="utf-8",
        )
        subprocess.check_call(
            ["tar", "-C", str(top / "src"), "-czf", str(sources / "demo-1.tar.gz"), "demo-1"],
        )
        env = os.environ.copy()
        env["PATH"] = f"{SYSJAR_BIN}:{env.get('PATH', '/usr/bin')}"
        expected = {
            "maven": "maven",
            "gradle": "gradle",
            "ant": "ant",
            "ivy": "ant",
        }
        for name, tool in expected.items():
            spec_path = top / f"{name}.spec"
            spec_path.write_text(
                "Name: demo\nVersion: 1\nRelease: 1\nSummary: demo\nLicense: MIT\n"
                "Source0: demo-1.tar.gz\n"
                f"BuildSystem: {name}\n%description\ndemo\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    "rpmspec", "--macros", macro_path(), "-q", "--buildrequires",
                    "--define", f"_sourcedir {sources}",
                    "--define", f"_builddir {top / name}-build",
                    str(spec_path),
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
            self.assertIn(tool, lines, proc.stdout)
            self.assertIn("java-current-devel", lines, proc.stdout)
            self.assertIn(f"rpm-build({name})", lines, proc.stdout)

    def test_bootstrap_switches_the_launcher_and_the_static_requirement(self):
        if shutil.which("rpm") is None:
            self.skipTest("rpm is not installed")
        proc = subprocess.run(
            [
                "rpm", "--macros", macro_path(),
                "--define", "sysjar_bootstrap 1",
                "--define", "sysjar_bootstrap_root /tmp/jpb",
                "--define", "_sysjar_mvn /tmp/jpb/bin/mvn",
                "--define", "sysjar_drops --drop org.apache.rat:apache-rat-plugin",
                "--eval", "%sysjar_mvn",
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = " ".join(proc.stdout.split())
        self.assertEqual(
            line,
            "sysjar mvn --bootstrap --bootstrap-root /tmp/jpb "
            "--drop org.apache.rat:apache-rat-plugin --mvn /tmp/jpb/bin/mvn --",
        )
        extra = subprocess.run(
            [
                "rpm", "--macros", macro_path(),
                "--define", "sysjar_extra --root / --root /tmp/resolver-root",
                "--eval", "%sysjar_mvn",
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(extra.returncode, 0, extra.stderr)
        self.assertEqual(
            " ".join(extra.stdout.split()),
            "sysjar mvn --root / --root /tmp/resolver-root --",
        )
        plain = subprocess.run(
            ["rpm", "--macros", macro_path(), "--eval", "%sysjar_mvn"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(" ".join(plain.stdout.split()), "sysjar mvn --")
        if shutil.which("rpmspec") is None:
            return
        top = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top)
        sources = top / "SOURCES"
        src = top / "src" / "demo-1"
        src.mkdir(parents=True)
        sources.mkdir()
        (src / "pom.xml").write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<project>\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>com.example</groupId>\n"
            "  <artifactId>demo</artifactId>\n"
            "  <version>1</version>\n</project>\n",
            encoding="utf-8",
        )
        subprocess.check_call(
            ["tar", "-C", str(top / "src"), "-czf", str(sources / "demo-1.tar.gz"), "demo-1"],
        )
        spec_path = top / "boot.spec"
        spec_path.write_text(
            "%global sysjar_bootstrap 1\n"
            "Name: demo\nVersion: 1\nRelease: 1\nSummary: demo\nLicense: MIT\n"
            "Source0: demo-1.tar.gz\nBuildSystem: maven\n%description\ndemo\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PATH"] = f"{SYSJAR_BIN}:{env.get('PATH', '/usr/bin')}"
        proc = subprocess.run(
            [
                "rpmspec", "--macros", macro_path(), "-q", "--buildrequires",
                "--define", f"_sourcedir {sources}",
                "--define", f"_builddir {top / 'BUILD'}",
                str(spec_path),
            ],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        self.assertIn("javapackages-bootstrap", lines, proc.stdout)
        self.assertNotIn("maven", lines, proc.stdout)
        self.assertIn("rpm-build(maven)", lines)

    def test_java_home_keeps_the_environment_unless_pinned(self):
        if shutil.which("rpm") is None:
            self.skipTest("rpm is not installed")
        plain = subprocess.run(
            ["rpm", "--macros", macro_path(), "--eval", "%__sysjar_use_java_home"],
            text=True, capture_output=True,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), 'export JAVA_HOME="$JAVA_HOME"')
        pinned = subprocess.run(
            [
                "rpm", "--macros", macro_path(),
                "--define", "_sysjar_java_home /usr/lib/jvm/java-21-openjdk",
                "--eval", "%__sysjar_use_java_home",
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(
            pinned.stdout.strip(),
            'export JAVA_HOME="/usr/lib/jvm/java-21-openjdk"',
        )
        build = subprocess.run(
            ["rpm", "--macros", macro_path(), "--eval", "%buildsystem_maven_build"],
            text=True, capture_output=True,
        )
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertIn('export JAVA_HOME="$JAVA_HOME"', build.stdout)
        self.assertNotIn("/usr/lib/jvm/java\n", build.stdout)
        self.assertNotIn("%{_jvmdir}", build.stdout)
        jvmdir = subprocess.check_output(["rpm", "--eval", "%{_jvmdir}"], text=True).strip()
        versioned = subprocess.run(
            [
                "rpm", "--macros", macro_path(),
                "--define", "_sysjar_java_version 21",
                "--eval", "%__sysjar_use_java_home",
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(versioned.returncode, 0, versioned.stderr)
        self.assertEqual(
            versioned.stdout.strip(),
            f'export JAVA_HOME="{jvmdir}/java-21-openjdk"',
        )
        override = subprocess.run(
            [
                "rpm", "--macros", macro_path(),
                "--define", "_sysjar_java_version 21",
                "--define", "_sysjar_java_home /opt/jdk",
                "--eval", "%__sysjar_use_java_home",
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(override.returncode, 0, override.stderr)
        self.assertEqual(override.stdout.strip(), 'export JAVA_HOME="/opt/jdk"')
        for macro, plain_value, pinned_value in (
            ("%_sysjar_java_buildrequires", "java-current-devel", "java-21-openjdk-devel"),
            ("%_sysjar_java_requires", "java-current", "java-21-openjdk"),
        ):
            plain_req = subprocess.run(
                ["rpm", "--macros", macro_path(), "--eval", macro],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(plain_req.stdout.strip(), plain_value)
            pinned_req = subprocess.run(
                [
                    "rpm", "--macros", macro_path(),
                    "--define", "_sysjar_java_version 21",
                    "--eval", macro,
                ],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(pinned_req.stdout.strip(), pinned_value)
        if shutil.which("rpmspec") is None:
            return
        top = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top)
        sources = top / "SOURCES"
        src = top / "src" / "demo-1"
        src.mkdir(parents=True)
        sources.mkdir()
        (src / "pom.xml").write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<project>\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>com.example</groupId>\n"
            "  <artifactId>demo</artifactId>\n"
            "  <version>1</version>\n</project>\n",
            encoding="utf-8",
        )
        subprocess.check_call(
            ["tar", "-C", str(top / "src"), "-czf", str(sources / "demo-1.tar.gz"), "demo-1"],
        )
        spec_path = top / "pin.spec"
        spec_path.write_text(
            "%global _sysjar_java_version 21\n"
            "Name: demo\nVersion: 1\nRelease: 1\nSummary: demo\nLicense: MIT\n"
            "Source0: demo-1.tar.gz\nBuildSystem: maven\n"
            "Requires: %{_sysjar_java_requires}\n%description\ndemo\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PATH"] = f"{SYSJAR_BIN}:{env.get('PATH', '/usr/bin')}"
        proc = subprocess.run(
            [
                "rpmspec", "--macros", macro_path(), "-q", "--buildrequires",
                "--define", f"_sourcedir {sources}",
                "--define", f"_builddir {top / 'BUILD'}",
                str(spec_path),
            ],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        self.assertIn("java-21-openjdk-devel", lines, proc.stdout)
        self.assertNotIn("java-current-devel", lines, proc.stdout)
        parsed = subprocess.run(
            [
                "rpmspec", "--macros", macro_path(), "-P",
                "--define", f"_sourcedir {sources}",
                "--define", f"_builddir {top / 'BUILD'}",
                str(spec_path),
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn("Requires: java-21-openjdk", parsed.stdout)
        self.assertIn(f'export JAVA_HOME="{jvmdir}/java-21-openjdk"', parsed.stdout)


class DeclarativeBuildTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("rpmbuild") is None:
            self.skipTest("rpmbuild is not installed")
        self.top = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.top)
        for name in ("SOURCES", "SPECS", "BUILD", "RPMS", "SRPMS"):
            (self.top / name).mkdir()
        self.bindir = self.top / "bin"
        self.bindir.mkdir()
        self.record = self.top / "tool-args"
        self._fake("mvn", "mkdir -p target; echo jar > target/demo-1.0.0.jar\n")
        self._fake("gradle", "")
        self._fake("ant", "")

    def _fake(self, name, extra):
        path = self.bindir / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {self.record}\n"
            f"{extra}"
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _source(self, name, version, files):
        src = self.top / "src" / f"{name}-{version}"
        src.mkdir(parents=True)
        for rel, text in files.items():
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        tarball = self.top / "SOURCES" / f"{name}-{version}.tar.gz"
        subprocess.check_call(
            ["tar", "-C", str(self.top / "src"), "-czf", str(tarball), f"{name}-{version}"],
        )

    def _build(self, spec, mode="-bc"):
        spec_path = self.top / "SPECS" / "demo.spec"
        spec_path.write_text(spec, encoding="utf-8")
        env = os.environ.copy()
        env["PATH"] = f"{SYSJAR_BIN}:{self.bindir}:{env.get('PATH', '/usr/bin')}"
        proc = subprocess.run(
            [
                "rpmbuild", "--macros", macro_path(), mode, "--nodeps", "--noclean",
                "--define", f"_topdir {self.top}",
                "--define", "debug_package %{nil}",
                "--define", "__spec_install_post %{nil}",
                "--define", "__os_install_post %{nil}",
                str(spec_path),
            ],
            text=True,
            capture_output=True,
            env=env,
        )
        log = proc.stdout + "\n" + proc.stderr
        return proc.returncode, log

    def _args(self):
        if not self.record.is_file():
            return ""
        return self.record.read_text(encoding="utf-8")

    def test_maven_package_and_optional_check(self):
        self._source("demo", "1.0.0", {
            "pom.xml": """<?xml version="1.0" encoding="UTF-8"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
</project>
""",
        })
        code, log = self._build(
            """
Name: demo
Version: 1.0.0
Release: 1
Summary: demo
License: MIT
Source0: demo-1.0.0.tar.gz
BuildArch: noarch
BuildSystem: maven
BuildOption: -DskipTests

%description
demo
""",
        )
        self.assertEqual(code, 0, log)
        args = self._args()
        self.assertIn("-Dmaven.test.skip=true", args)
        self.assertIn("-DskipTests", args)
        self.assertIn(" package", args)
        # %check is off unless the spec defines %bcond_without tests.
        self.assertEqual(len([line for line in args.splitlines() if line.strip()]), 1, args)

        self.record.unlink()
        code, log = self._build(
            """
%bcond_without tests
Name: demo
Version: 1.0.0
Release: 1
Summary: demo
License: MIT
Source0: demo-1.0.0.tar.gz
BuildArch: noarch
BuildSystem: maven
BuildOption(build): compile
BuildOption(check): verify

%description
demo
%files
/usr/share/java/demo.jar
/usr/share/maven-poms/demo.pom
/usr/share/sysjar/index/com.example/demo.sysjar
""",
            mode="-bb",
        )
        self.assertEqual(code, 0, log)
        lines = [line for line in self._args().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2, self._args())
        self.assertIn(" compile", lines[0])
        self.assertNotIn(" package", lines[0])
        self.assertIn("-Dmaven.test.skip=false", lines[1])
        self.assertIn(" verify", lines[1])
        self.assertNotIn(" test", lines[1])
        self.assertTrue(any((self.top / "RPMS").rglob("demo-*.rpm")))

    def test_gradle_defaults_to_assemble(self):
        self._source("demo", "1", {"build.gradle": "\n"})
        code, log = self._build(
            """
Name: demo
Version: 1
Release: 1
Summary: demo
License: MIT
Source0: demo-1.tar.gz
BuildSystem: gradle
BuildOption: -Pextra=1

%description
demo
""",
        )
        self.assertEqual(code, 0, log)
        args = self._args()
        self.assertIn("--offline", args)
        self.assertIn("-Pextra=1", args)
        self.assertIn(" assemble", args)

    def test_ant_and_ivy_use_the_system_repo(self):
        ivy = """<ivy-module version="2.0">
  <info organisation="com.example" module="demo" revision="1.0.0"/>
  <dependencies/>
</ivy-module>
"""
        build_xml = '<project name="demo" default="jar"><target name="jar"/></project>\n'
        self._source("demo", "1", {"build.xml": build_xml, "ivy.xml": ivy})
        code, log = self._build(
            """
Name: demo
Version: 1
Release: 1
Summary: demo
License: MIT
Source0: demo-1.tar.gz
BuildSystem: ant
BuildOption: -Dfoo=1

%description
demo
""",
        )
        self.assertEqual(code, 0, log)
        args = self._args()
        self.assertIn("-Divy.settings.file=", args)
        self.assertIn("build.xml", args)
        self.assertIn("-Dfoo=1", args)
        unpacked = next((self.top / "BUILD").rglob("ivysettings.xml"))
        self.assertIn("sysjar", unpacked.read_text(encoding="utf-8"))

        self.record.unlink()
        code, log = self._build(
            """
Name: demo
Version: 1
Release: 1
Summary: demo
License: MIT
Source0: demo-1.tar.gz
BuildSystem: ivy
BuildOption(build): dist

%description
demo
""",
        )
        self.assertEqual(code, 0, log)
        args = self._args()
        self.assertIn("-Divy.settings.file=", args)
        self.assertIn(" dist", args)


if __name__ == "__main__":
    unittest.main()
