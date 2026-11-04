import io
import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from sysjar.cli import main
from sysjar.gradle import render_init_script
from sysjar.index import load_index
from sysjar.install import filelist_lines, install_auto, install_pom
from sysjar.ivy import rewrite_ivy_element
from sysjar.model import Artifact, Coord, Dependency, Index, SysjarError, version_key
from sysjar.build import lifecycle_defaults
from sysjar.pom import (
    drop_raw_compiler_levels, fix_snapshot_versions, pin_lifecycle_plugins, raise_compiler_level,
    rewrite_element, runtime_dependencies,
)
from sysjar.repo import materialize, write_settings
from sysjar.rpmmeta import provides_for, requires_for
from sysjar.scan import build_requires, scan
from sysjar.xmlutil import POM_NS, child, children, parse_xml, text_of, write_xml


def ns(tag):
    return f"{{{POM_NS}}}{tag}"


def pom(body):
    root = ET.fromstring(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        f"{body}</project>\n"
    )
    return root


def artifact(group, name, version, path, pom_path="", **kwargs):
    return Artifact(Coord(group, name, kwargs.pop("classifier", ""), kwargs.pop("extension", "jar")), version, path, pom_path, **kwargs)


class IndexTests(unittest.TestCase):
    def test_version_order_and_priority(self):
        self.assertGreater(version_key("1.10.0"), version_key("1.9.0"))
        index = Index()
        index.add(artifact("org.slf4j", "slf4j-api", "1.7.36", "/old.jar", priority=1))
        index.add(artifact("org.slf4j", "slf4j-api", "1.7.36", "/new.jar", priority=2))
        self.assertEqual(index.find("org.slf4j", "slf4j-api").path, "/new.jar")
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17", "/newer.jar", priority=1))
        # A newer legacy fragment does not replace a sysjar record of an older build.
        self.assertEqual(index.find("org.slf4j", "slf4j-api").path, "/new.jar")

    def test_alias_and_classifier(self):
        index = Index()
        art = artifact(
            "commons-logging", "commons-logging", "1.3.5", "/usr/share/java/commons-logging.jar",
            aliases=(Coord("org.apache.commons", "commons-logging"),),
        )
        index.add(art)
        side = artifact(
            "commons-logging", "commons-logging", "1.3.5", "/usr/share/java/commons-logging-api.jar",
            classifier="api",
        )
        index.add(side)
        self.assertEqual(index.find("org.apache.commons", "commons-logging").path, art.path)
        self.assertEqual(index.find("commons-logging", "commons-logging", "api").path, side.path)
        self.assertEqual(index.version_map()["commons-logging:commons-logging"], "1.3.5")

    def test_xmvn_metadata(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "usr/share/java/commons-logging.jar"
        pom_path = root / "usr/share/maven-poms/commons-logging.pom"
        jar.parent.mkdir(parents=True)
        pom_path.parent.mkdir(parents=True)
        jar.write_bytes(b"jar")
        pom_path.write_text("<project/>", encoding="utf-8")
        meta = root / "usr/share/maven-metadata"
        meta.mkdir()
        (meta / "commons-logging.xml").write_text("""<?xml version="1.0" ?>
<metadata><artifacts>
  <artifact>
    <groupId>commons-logging</groupId>
    <artifactId>commons-logging</artifactId>
    <version>1.3.5</version>
    <path>%s</path>
    <aliases><alias><groupId>org.apache.commons</groupId><artifactId>commons-logging</artifactId></alias></aliases>
    <dependencies>
      <dependency>
        <groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId>
        <requestedVersion>2.0.16</requestedVersion><optional>true</optional>
      </dependency>
    </dependencies>
  </artifact>
  <artifact>
    <groupId>commons-logging</groupId>
    <artifactId>commons-logging</artifactId>
    <extension>pom</extension>
    <version>1.3.5</version>
    <path>%s</path>
  </artifact>
</artifacts></metadata>
""" % (jar, pom_path), encoding="utf-8")
        index = load_index([root])
        found = index.find("org.apache.commons", "commons-logging")
        self.assertEqual(found.version, "1.3.5")
        self.assertTrue(found.dependencies[0].optional)
        self.assertIsNotNone(index.find_pom("commons-logging", "commons-logging"))

    def test_bootstrap_loses_to_a_packaged_jar(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        system = root / "usr/share/java/demo.jar"
        system.parent.mkdir(parents=True)
        system.write_bytes(b"system")
        boot_jar = root / "usr/share/javapackages-bootstrap/demo.jar"
        boot_jar.parent.mkdir(parents=True)
        boot_jar.write_bytes(b"boot")
        only = root / "usr/share/javapackages-bootstrap/only.jar"
        only.write_bytes(b"only")
        meta = root / "usr/share/maven-metadata"
        meta.mkdir(parents=True)
        (meta / "demo.xml").write_text(f"""<?xml version="1.0" ?>
<metadata><artifacts>
  <artifact>
    <groupId>org.example</groupId><artifactId>demo</artifactId>
    <version>1.2.3</version><path>{system}</path>
  </artifact>
</artifacts></metadata>
""", encoding="utf-8")
        boot = root / "usr/share/javapackages-bootstrap/maven-metadata"
        boot.mkdir(parents=True)
        (boot / "demo.xml").write_text(f"""<?xml version="1.0" ?>
<metadata><artifacts>
  <artifact>
    <groupId>org.example</groupId><artifactId>demo</artifactId>
    <version>9.0.0.MBI</version><path>{boot_jar}</path>
  </artifact>
  <artifact>
    <groupId>org.example</groupId><artifactId>only-boot</artifactId>
    <version>1.0.0.MBI</version><path>{only}</path>
  </artifact>
</artifacts></metadata>
""", encoding="utf-8")
        plain = load_index([root])
        self.assertIsNone(plain.find("org.example", "only-boot"))
        self.assertEqual(plain.find("org.example", "demo").version, "1.2.3")
        index = load_index([root], [root])
        found = index.find("org.example", "demo")
        self.assertEqual(found.version, "1.2.3")
        self.assertEqual(found.path, str(system))
        self.assertEqual(index.find("org.example", "only-boot").version, "1.0.0.MBI")
        self.assertEqual(index.find("org.example", "only-boot").priority, 0)

    def test_bootstrap_keeps_the_version_the_build_asked_for(self):
        index = Index()
        index.add(artifact("org.slf4j", "slf4j-api", "1.7.36.MBI", "/slf4j-17.jar", priority=0))
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17.MBI", "/slf4j-20.jar", priority=0))
        self.assertEqual(index.find("org.slf4j", "slf4j-api").path, "/slf4j-20.jar")
        self.assertEqual(index.find("org.slf4j", "slf4j-api", requested="1.7.36").path, "/slf4j-17.jar")
        self.assertEqual(len(index.publishable()), 2)
        index.add(artifact("org.slf4j", "slf4j-api", "1.7.36", "/system.jar", priority=1))
        self.assertEqual(index.find("org.slf4j", "slf4j-api", requested="2.0.17").path, "/system.jar")
        self.assertEqual(len(index.publishable()), 1)

    def test_scan_treats_a_pom_parent_as_present(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        parent_pom = root / "apache.pom"
        parent_pom.write_text("<project/>", encoding="utf-8")
        index = Index()
        index.add(artifact("org.apache", "apache", "35", "", str(parent_pom), extension="pom"))
        project = root / "proj"
        project.mkdir()
        write_xml(ET.ElementTree(pom("""
  <parent>
    <groupId>org.apache</groupId>
    <artifactId>apache</artifactId>
    <version>31</version>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
""")), project / "pom.xml")
        findings = [item for item in scan(project, index) if item.kind == "parent"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].system, "35")
        self.assertFalse(findings[0].missing)

    def test_scan_skips_modules_the_reactor_does_not_list(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        unused = project / "unused"
        unused.mkdir(parents=True)
        write_xml(ET.ElementTree(pom("""
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <dependencies>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>2.0.17</version>
    </dependency>
  </dependencies>
""")), project / "pom.xml")
        write_xml(ET.ElementTree(pom("""
  <groupId>com.example</groupId>
  <artifactId>unused</artifactId>
  <version>1</version>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>missing</artifactId>
      <version>1</version>
    </dependency>
  </dependencies>
""")), unused / "pom.xml")
        labels = {item.label for item in scan(project, Index())}
        self.assertIn("org.slf4j:slf4j-api", labels)
        self.assertNotIn("com.acme:missing", labels)
        self.assertNotIn("mvn(com.acme:missing)", "\n".join(build_requires(scan(project, Index()))))
        loose = root / "loose"
        nested = loose / "mod"
        nested.mkdir(parents=True)
        write_xml(ET.ElementTree(pom("""
  <groupId>com.acme</groupId>
  <artifactId>missing</artifactId>
  <version>1</version>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>also-missing</artifactId>
      <version>1</version>
    </dependency>
  </dependencies>
""")), nested / "pom.xml")
        loose_labels = {item.label for item in scan(loose, Index())}
        self.assertIn("com.acme:also-missing", loose_labels)


class PomTests(unittest.TestCase):
    def index(self):
        index = Index()
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17", "/slf4j.jar"))
        index.add(artifact("com.google.guava", "guava", "33.4.0", "/guava.jar"))
        index.add(artifact("org.apache", "apache", "35", "/apache.pom", extension="pom"))
        index.add(artifact("org.apache.maven.plugins", "maven-compiler-plugin", "3.13.0", "/compiler.jar"))
        return index

    def test_rewrites_versions_properties_parent_and_plugins(self):
        root = pom("""
  <parent>
    <groupId>org.apache</groupId>
    <artifactId>apache</artifactId>
    <version>31</version>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <properties>
    <guava.version>32.0.0</guava.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>${guava.version}</version>
    </dependency>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>1.7.25</version>
    </dependency>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>demo-core</artifactId>
      <version>0.9.0</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
      </plugin>
    </plugins>
  </build>
""")
        report = rewrite_element(root, self.index(), reactor={("com.example", "demo-core")})
        self.assertEqual(text_of(child(root, "properties"), "guava.version"), "33.4.0")
        deps = {text_of(item, "artifactId"): text_of(item, "version") for item in child(root, "dependencies")}
        self.assertEqual(deps["slf4j-api"], "2.0.17")
        self.assertEqual(deps["demo-core"], "0.9.0")
        self.assertEqual(deps["guava"], "${guava.version}")
        parent = child(root, "parent")
        self.assertEqual(text_of(parent, "version"), "35")
        self.assertEqual(text_of(parent, "relativePath"), "")
        plugin = child(child(child(root, "build"), "plugins"), "plugin")
        self.assertEqual(text_of(plugin, "version"), "3.13.0")
        self.assertEqual(text_of(root, "version"), "1.0.0")
        self.assertTrue(any(gap.coord.startswith("junit:junit") and not gap.blocking for gap in report.gaps))
        self.assertFalse(report.blocking)

    def test_missing_build_plugin_is_removed_and_management_stays(self):
        root = pom("""
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
      </plugin>
      <plugin>
        <artifactId>maven-javadoc-plugin</artifactId>
        <executions>
          <execution>
            <goals><goal>jar</goal></goals>
          </execution>
        </executions>
      </plugin>
    </plugins>
    <pluginManagement>
      <plugins>
        <plugin>
          <artifactId>maven-site-plugin</artifactId>
          <version>3.12.1</version>
        </plugin>
      </plugins>
    </pluginManagement>
  </build>
""")
        report = rewrite_element(root, self.index())
        built = [text_of(item, "artifactId") for item in children(child(child(root, "build"), "plugins"), "plugin")]
        managed = [
            text_of(item, "artifactId")
            for item in children(child(child(child(root, "build"), "pluginManagement"), "plugins"), "plugin")
        ]
        self.assertEqual(built, ["maven-compiler-plugin"])
        self.assertEqual(managed, ["maven-site-plugin"])
        self.assertTrue(any("maven-javadoc-plugin" in line for line in report.dropped))
        self.assertFalse(report.blocking)

    def test_build_drops_uninstalled_plugins_and_jdk_tools(self):
        body = """
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <dependencies>
    <dependency>
      <groupId>com.sun</groupId>
      <artifactId>tools</artifactId>
      <version>any</version>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-site-plugin</artifactId>
        <version>3.3</version>
      </plugin>
      <plugin>
        <artifactId>maven-antrun-plugin</artifactId>
        <version>3.1.0</version>
      </plugin>
      <plugin>
        <artifactId>maven-release-plugin</artifactId>
      </plugin>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
      </plugin>
      <plugin>
        <artifactId>maven-scm-plugin</artifactId>
        <version>1.7</version>
        <dependencies>
          <dependency>
            <groupId>org.apache.maven.scm</groupId>
            <artifactId>maven-scm-provider-gitexe</artifactId>
            <version>1.7</version>
          </dependency>
        </dependencies>
      </plugin>
    </plugins>
    <pluginManagement>
      <plugins>
        <plugin>
          <artifactId>maven-javadoc-plugin</artifactId>
          <version>3.0.0</version>
        </plugin>
        <plugin>
          <artifactId>maven-release-plugin</artifactId>
          <version>2.5.3</version>
        </plugin>
        <plugin>
          <artifactId>maven-compiler-plugin</artifactId>
          <version>3.11.0</version>
        </plugin>
      </plugins>
    </pluginManagement>
    <extensions>
      <extension>
        <groupId>org.sonatype.central</groupId>
        <artifactId>central-publishing-maven-plugin</artifactId>
        <version>0.10.0</version>
      </extension>
    </extensions>
  </build>
  <reporting>
    <plugins>
      <plugin>
        <artifactId>maven-project-info-reports-plugin</artifactId>
        <version>3.0.0</version>
      </plugin>
    </plugins>
  </reporting>
"""
        original = pom(body)
        untouched = rewrite_element(original, self.index())
        still = [text_of(item, "artifactId") for item in children(child(child(original, "build"), "plugins"), "plugin")]
        self.assertIn("maven-site-plugin", still)
        self.assertIn("maven-release-plugin", still)
        self.assertTrue(untouched.blocking)

        root = pom(body)
        report = rewrite_element(root, self.index(), strip_uninstalled=True)
        built = [text_of(item, "artifactId") for item in children(child(child(root, "build"), "plugins"), "plugin")]
        self.assertEqual(built, ["maven-antrun-plugin", "maven-compiler-plugin"])
        self.assertEqual(
            [text_of(item, "artifactId") for item in children(child(child(root, "build"), "extensions"), "extension")],
            [],
        )
        managed = [
            text_of(item, "artifactId")
            for item in children(child(child(child(root, "build"), "pluginManagement"), "plugins"), "plugin")
        ]
        self.assertEqual(managed, ["maven-javadoc-plugin", "maven-release-plugin", "maven-compiler-plugin"])
        reporting = [text_of(item, "artifactId") for item in children(child(child(root, "reporting"), "plugins"), "plugin")]
        self.assertEqual(reporting, [])
        self.assertIsNone(child(child(root, "dependencies"), "dependency"))
        self.assertTrue(any("com.sun:tools" in line and "JDK" in line for line in report.dropped))
        self.assertTrue(any("maven-site-plugin" in line for line in report.dropped))
        self.assertFalse(report.blocking)

    def test_uninstalled_import_bom_and_executed_managed_plugin_are_removed(self):
        root = pom("""
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.junit</groupId>
        <artifactId>junit-bom</artifactId>
        <version>5.14.4</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
      <dependency>
        <groupId>junit</groupId>
        <artifactId>junit</artifactId>
        <version>4.13.2</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <build>
    <pluginManagement>
      <plugins>
        <plugin>
          <artifactId>maven-site-plugin</artifactId>
          <version>3.6</version>
          <executions>
            <execution>
              <id>attach-descriptor</id>
              <goals><goal>attach-descriptor</goal></goals>
            </execution>
          </executions>
        </plugin>
        <plugin>
          <artifactId>maven-javadoc-plugin</artifactId>
          <version>3.0.0</version>
        </plugin>
      </plugins>
    </pluginManagement>
  </build>
""")
        report = rewrite_element(root, self.index(), strip_uninstalled=True)
        managed_deps = [
            text_of(item, "artifactId")
            for item in children(child(child(root, "dependencyManagement"), "dependencies"), "dependency")
        ]
        managed_plugins = [
            text_of(item, "artifactId")
            for item in children(child(child(child(root, "build"), "pluginManagement"), "plugins"), "plugin")
        ]
        self.assertEqual(managed_deps, ["junit"])
        self.assertEqual(managed_plugins, ["maven-javadoc-plugin"])
        self.assertTrue(any("junit-bom" in line and "import" in line for line in report.dropped))
        self.assertTrue(any("maven-site-plugin" in line for line in report.dropped))
        self.assertFalse(report.blocking)

    def test_uninstalled_test_dependency_is_removed_and_provided_stays(self):
        body = """
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>junit</groupId>
        <artifactId>junit</artifactId>
        <version>4.13.2</version>
      </dependency>
      <dependency>
        <groupId>org.jboss.shrinkwrap</groupId>
        <artifactId>shrinkwrap-impl-base</artifactId>
        <version>1.2.6</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>org.jboss.shrinkwrap</groupId>
      <artifactId>shrinkwrap-impl-base</artifactId>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>org.jboss.shrinkwrap</groupId>
      <artifactId>shrinkwrap-api</artifactId>
      <version>${missing.version}</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>javax.interceptor</groupId>
      <artifactId>interceptor-api</artifactId>
      <version>1.2</version>
      <scope>provided</scope>
    </dependency>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>needed</artifactId>
      <version>1</version>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
        <dependencies>
          <dependency>
            <groupId>com.acme</groupId>
            <artifactId>plugin-test</artifactId>
            <version>1</version>
            <scope>test</scope>
          </dependency>
        </dependencies>
      </plugin>
    </plugins>
  </build>
"""
        index = self.index()
        index.add(artifact("junit", "junit", "4.13.2", "/junit.jar"))
        original = pom(body)
        kept = rewrite_element(original, index)
        kept_ids = [text_of(item, "artifactId") for item in children(child(original, "dependencies"), "dependency")]
        self.assertEqual(
            kept_ids,
            ["junit", "shrinkwrap-impl-base", "shrinkwrap-api", "interceptor-api", "needed"],
        )
        self.assertFalse(any(gap.coord.startswith("junit:junit") for gap in kept.gaps))
        self.assertTrue(any(gap.coord.startswith("org.jboss.shrinkwrap:shrinkwrap-api") and not gap.blocking for gap in kept.gaps))
        self.assertTrue(kept.blocking)

        root = pom(body)
        report = rewrite_element(root, index, strip_uninstalled=True)
        deps = children(child(root, "dependencies"), "dependency")
        self.assertEqual(
            [text_of(item, "artifactId") for item in deps],
            ["junit", "interceptor-api", "needed"],
        )
        junit = deps[0]
        self.assertEqual(text_of(junit, "version"), "")
        self.assertEqual(text_of(junit, "scope"), "test")
        managed = [
            text_of(item, "artifactId")
            for item in children(child(child(root, "dependencyManagement"), "dependencies"), "dependency")
        ]
        self.assertEqual(managed, ["junit", "shrinkwrap-impl-base"])
        plugin = child(child(child(root, "build"), "plugins"), "plugin")
        plugin_dep = child(child(plugin, "dependencies"), "dependency")
        self.assertEqual(text_of(plugin_dep, "artifactId"), "plugin-test")
        self.assertTrue(any("shrinkwrap-impl-base" in line and "test" in line for line in report.dropped))
        self.assertTrue(any("shrinkwrap-api" in line and "test" in line for line in report.dropped))
        self.assertFalse(any("interceptor-api" in line for line in report.dropped))
        self.assertTrue(any(gap.coord.startswith("javax.interceptor:interceptor-api") and not gap.blocking for gap in report.gaps))
        self.assertTrue(report.blocking)

    def test_raw_compiler_source_args_are_removed(self):
        root = pom("""
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>
          <release>11</release>
          <compilerArguments>
            <source>8</source>
            <target>8</target>
            <Xlint>all</Xlint>
          </compilerArguments>
          <compilerArgs>
            <arg>-source</arg>
            <arg>8</arg>
            <arg>-Werror</arg>
          </compilerArgs>
        </configuration>
      </plugin>
      <plugin>
        <artifactId>maven-jar-plugin</artifactId>
        <configuration>
          <compilerArguments>
            <source>1.5</source>
          </compilerArguments>
        </configuration>
      </plugin>
    </plugins>
  </build>
""")
        report = drop_raw_compiler_levels(root)
        compiler = children(child(child(root, "build"), "plugins"), "plugin")[0]
        configuration = child(compiler, "configuration")
        self.assertEqual(text_of(configuration, "release"), "11")
        arguments = child(configuration, "compilerArguments")
        self.assertEqual(text_of(arguments, "Xlint"), "all")
        self.assertEqual(text_of(arguments, "source"), "")
        self.assertEqual(text_of(arguments, "target"), "")
        self.assertEqual(
            [(node.text or "").strip() for node in children(child(configuration, "compilerArgs"), "arg")],
            ["-Werror"],
        )
        jar = children(child(child(root, "build"), "plugins"), "plugin")[1]
        jar_args = child(child(jar, "configuration"), "compilerArguments")
        self.assertEqual(text_of(jar_args, "source"), "1.5")
        self.assertTrue(report.changes)

        empty = pom("""
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>
          <compilerArguments>
            <source>${maven.compiler.argument.source}</source>
            <target>${maven.compiler.argument.target}</target>
          </compilerArguments>
        </configuration>
      </plugin>
    </plugins>
  </build>
""")
        drop_raw_compiler_levels(empty)
        plugin = child(child(child(empty, "build"), "plugins"), "plugin")
        self.assertIsNone(child(child(plugin, "configuration"), "compilerArguments"))

    def test_compiler_level_below_8_is_raised_and_snapshot_is_renamed(self):
        root = pom("""
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>SNAPSHOT</version>
  <properties>
    <maven.compiler.release>7</maven.compiler.release>
    <project.source.level>1.6</project.source.level>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>
          <source>1.5</source>
          <target>${project.source.level}</target>
        </configuration>
        <executions>
          <execution>
            <configuration>
              <release>6</release>
            </configuration>
          </execution>
        </executions>
      </plugin>
      <plugin>
        <artifactId>maven-jar-plugin</artifactId>
        <configuration>
          <source>1.5</source>
        </configuration>
      </plugin>
    </plugins>
  </build>
  <dependencies>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>old</artifactId>
      <version>1.0-SNAPSHOT</version>
    </dependency>
  </dependencies>
""")
        report = raise_compiler_level(root, source="pom")
        props = child(root, "properties")
        self.assertEqual(text_of(props, "maven.compiler.release"), "8")
        self.assertEqual(text_of(props, "project.source.level"), "8")
        self.assertEqual(text_of(props, "maven.compiler.target"), "17")
        compiler = children(child(child(root, "build"), "plugins"), "plugin")[0]
        configuration = child(compiler, "configuration")
        self.assertEqual(text_of(configuration, "source"), "8")
        self.assertEqual(text_of(configuration, "target"), "${project.source.level}")
        execution = child(child(child(compiler, "executions"), "execution"), "configuration")
        self.assertEqual(text_of(execution, "release"), "8")
        jar = children(child(child(root, "build"), "plugins"), "plugin")[1]
        self.assertEqual(text_of(child(jar, "configuration"), "source"), "1.5")
        self.assertTrue(any("1.5 -> 8" in line for line in report.changes))
        self.assertTrue(any("1.6 -> 8" in line for line in report.changes))
        snap = fix_snapshot_versions(root, source="pom")
        self.assertEqual(text_of(root, "version"), "0-SNAPSHOT")
        dependency = child(child(root, "dependencies"), "dependency")
        self.assertEqual(text_of(dependency, "version"), "1.0-SNAPSHOT")
        self.assertTrue(snap.changes)

    def test_lifecycle_plugin_is_pinned_to_the_installed_jar(self):
        root = pom("""
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1</version>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
      </plugin>
    </plugins>
  </build>
""")
        index = Index()
        index.add(artifact("org.apache.maven.plugins", "maven-resources-plugin", "3.3.1.MBI", "/resources.jar"))
        index.add(artifact("org.apache.maven.plugins", "maven-compiler-plugin", "3.12.1.MBI", "/compiler.jar"))
        report = pin_lifecycle_plugins(root, index, {
            ("org.apache.maven.plugins", "maven-resources-plugin"): "3.3.1",
            ("org.apache.maven.plugins", "maven-compiler-plugin"): "3.13.0",
            ("org.apache.maven.plugins", "maven-jar-plugin"): "3.4.1",
        })
        managed = {
            text_of(item, "artifactId"): text_of(item, "version")
            for item in children(child(child(child(root, "build"), "pluginManagement"), "plugins"), "plugin")
        }
        self.assertEqual(managed, {"maven-resources-plugin": "3.3.1.MBI"})
        self.assertTrue(any("maven-resources-plugin -> 3.3.1.MBI" in line for line in report.changes))
        compiler = child(child(child(root, "build"), "plugins"), "plugin")
        self.assertEqual(text_of(compiler, "version"), "3.11.0")

    def test_lifecycle_defaults_reads_bindings_from_the_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lib = home / "lib"
            lib.mkdir()
            binary = home / "bin" / "mvn"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            payload = (
                "<phases>"
                "<process-resources>org.apache.maven.plugins:maven-resources-plugin:3.3.1:resources</process-resources>"
                "<compile>org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile</compile>"
                "</phases>"
            )
            with zipfile.ZipFile(lib / "maven-core.jar", "w") as archive:
                archive.writestr("META-INF/plexus/default-bindings.xml", payload)
            found = lifecycle_defaults(str(binary))
        self.assertEqual(found[("org.apache.maven.plugins", "maven-resources-plugin")], "3.3.1")
        self.assertEqual(found[("org.apache.maven.plugins", "maven-compiler-plugin")], "3.13.0")

    def test_parent_built_in_this_reactor_keeps_its_source_version(self):
        root = pom("""
  <parent>
    <groupId>org.apache.maven</groupId>
    <artifactId>maven</artifactId>
    <version>3.9.11</version>
    <relativePath>../pom.xml</relativePath>
  </parent>
  <artifactId>maven-core</artifactId>
""")
        index = Index()
        index.add(artifact("org.apache.maven", "maven", "3.9.9.MBI", "/maven.pom", extension="pom"))
        rewrite_element(root, index, reactor={("org.apache.maven", "maven")})
        parent = child(root, "parent")
        self.assertEqual(text_of(parent, "version"), "3.9.11")
        self.assertEqual(text_of(parent, "relativePath"), "../pom.xml")

    def test_versionless_dependency_keeps_the_managed_version(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        module = project / "mod"
        module.mkdir(parents=True)
        write_xml(ET.ElementTree(pom("""
  <groupId>org.apache.maven</groupId>
  <artifactId>maven</artifactId>
  <version>3.9.11</version>
  <properties>
    <version.plexus-utils>3.6.0</version.plexus-utils>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.codehaus.plexus</groupId>
        <artifactId>plexus-utils</artifactId>
        <version>${version.plexus-utils}</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <modules>
    <module>mod</module>
  </modules>
""")), project / "pom.xml")
        write_xml(ET.ElementTree(pom("""
  <parent>
    <groupId>org.apache.maven</groupId>
    <artifactId>maven</artifactId>
    <version>3.9.11</version>
  </parent>
  <artifactId>maven-model</artifactId>
  <dependencies>
    <dependency>
      <groupId>org.codehaus.plexus</groupId>
      <artifactId>plexus-utils</artifactId>
    </dependency>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
    </dependency>
  </dependencies>
""")), module / "pom.xml")
        index = Index()
        index.add(artifact("org.codehaus.plexus", "plexus-utils", "3.5.1.MBI", "/utils-3.jar", priority=0))
        index.add(artifact("org.codehaus.plexus", "plexus-utils", "4.0.2.MBI", "/utils-4.jar", priority=0))
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17", "/slf4j.jar"))
        from sysjar.pom import rewrite_tree
        report = rewrite_tree(project, index, reactor_only=True)
        self.assertFalse(report.blocking)
        parent = parse_xml(project / "pom.xml").getroot()
        self.assertEqual(text_of(child(parent, "properties"), "version.plexus-utils"), "3.5.1.MBI")
        deps = child(parse_xml(module / "pom.xml").getroot(), "dependencies")
        by_name = {text_of(item, "artifactId"): item for item in children(deps, "dependency")}
        self.assertEqual(text_of(by_name["plexus-utils"], "version"), "")
        self.assertEqual(text_of(by_name["slf4j-api"], "version"), "2.0.17")

    def _felix_parent(self, profile_version: str):
        return pom(f"""
  <groupId>org.apache.commons</groupId>
  <artifactId>commons-parent</artifactId>
  <version>89</version>
  <properties>
    <commons.felix.version>5.1.9</commons.felix.version>
  </properties>
  <build>
    <pluginManagement>
      <plugins>
        <plugin>
          <groupId>org.apache.felix</groupId>
          <artifactId>maven-bundle-plugin</artifactId>
          <version>${{commons.felix.version}}</version>
        </plugin>
      </plugins>
    </pluginManagement>
  </build>
  <profiles>
    <profile>
      <id>java-17-up</id>
      <activation><jdk>[17,)</jdk></activation>
      <properties>
        <commons.felix.version>{profile_version}</commons.felix.version>
        <commons.release.dryRun>true</commons.release.dryRun>
      </properties>
    </profile>
  </profiles>
""")

    def _felix_profile_version(self, root):
        profile = child(child(root, "profiles"), "profile")
        return text_of(child(profile, "properties"), "commons.felix.version")

    def _felix_plugin_version(self, root):
        plugin = child(child(child(child(root, "build"), "pluginManagement"), "plugins"), "plugin")
        return text_of(plugin, "version")

    def test_jdk_profile_property_is_rewritten_to_the_installed_plugin(self):
        root = self._felix_parent("6.0.0")
        index = Index()
        index.add(artifact("org.apache.felix", "maven-bundle-plugin", "5.1.9.MBI", "/bundle.jar", priority=0))
        rewrite_element(root, index)
        self.assertEqual(text_of(child(root, "properties"), "commons.felix.version"), "5.1.9.MBI")
        self.assertEqual(self._felix_profile_version(root), "5.1.9.MBI")
        self.assertEqual(self._felix_plugin_version(root), "${commons.felix.version}")
        profile = child(child(root, "profiles"), "profile")
        self.assertEqual(text_of(child(profile, "properties"), "commons.release.dryRun"), "true")

    def test_jdk_profile_keeps_a_bootstrap_plugin_that_matches_its_version(self):
        root = self._felix_parent("6.0.0")
        index = Index()
        index.add(artifact("org.apache.felix", "maven-bundle-plugin", "5.1.9.MBI", "/bundle-5.jar", priority=0))
        index.add(artifact("org.apache.felix", "maven-bundle-plugin", "6.0.0.MBI", "/bundle-6.jar", priority=0))
        rewrite_element(root, index)
        self.assertEqual(text_of(child(root, "properties"), "commons.felix.version"), "5.1.9.MBI")
        self.assertEqual(self._felix_profile_version(root), "6.0.0.MBI")
        self.assertEqual(self._felix_plugin_version(root), "${commons.felix.version}")

    def test_unreadable_profile_property_does_not_hide_the_installed_version(self):
        root = self._felix_parent("[5.1,6.0)")
        index = Index()
        index.add(artifact("org.apache.felix", "maven-bundle-plugin", "5.1.9.MBI", "/bundle.jar", priority=0))
        rewrite_element(root, index)
        self.assertEqual(self._felix_profile_version(root), "[5.1,6.0)")
        self.assertEqual(self._felix_plugin_version(root), "5.1.9.MBI")

    def test_reactor_rewrite_leaves_test_fixtures_alone(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        fixture = project / "src" / "test" / "resources"
        fixture.mkdir(parents=True)
        write_xml(ET.ElementTree(pom("""
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>1.7.25</version>
    </dependency>
  </dependencies>
""")), project / "pom.xml")
        write_xml(ET.ElementTree(pom("""
  <parent>
    <groupId>org.example</groupId>
    <artifactId>missing-parent</artifactId>
    <version>1</version>
  </parent>
  <artifactId>fixture</artifactId>
""")), fixture / "pom.xml")
        from sysjar.pom import rewrite_tree
        report = rewrite_tree(project, self.index(), reactor_only=True)
        self.assertFalse(report.blocking)
        main_dep = child(child(parse_xml(project / "pom.xml").getroot(), "dependencies"), "dependency")
        self.assertEqual(text_of(main_dep, "version"), "2.0.17")
        parent = child(parse_xml(fixture / "pom.xml").getroot(), "parent")
        self.assertEqual(text_of(parent, "version"), "1")

    def test_drop_and_runtime_deps(self):
        root = pom("""
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>1.7.25</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>optional</artifactId>
      <version>1</version>
      <optional>true</optional>
    </dependency>
  </dependencies>
""")
        report = rewrite_element(root, self.index(), drop={"com.acme:optional"})
        self.assertTrue(any("dropped" in line for line in report.dropped))
        deps = runtime_dependencies(root)
        self.assertEqual([dep.coord.artifact for dep in deps], ["slf4j-api", "junit"])
        self.assertEqual(deps[1].scope, "test")

    def test_ivy_rewrite(self):
        root = ET.fromstring("""<ivy-module version="2.0">
          <dependencies>
            <dependency org="org.slf4j" name="slf4j-api" rev="1.7.25"/>
          </dependencies>
        </ivy-module>""")
        report = rewrite_ivy_element(root, self.index())
        dep = list(root.iter("dependency"))[0]
        self.assertEqual(dep.get("rev"), "2.0.17")
        self.assertTrue(report.changes)


class RepoTests(unittest.TestCase):
    def test_repository_layout(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "slf4j.jar"
        pom_path = root / "slf4j.pom"
        jar.write_bytes(b"not a real jar")
        pom_src = pom("""
  <groupId>org.slf4j</groupId>
  <artifactId>slf4j-api</artifactId>
  <version>2.0.17</version>
  <dependencies>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>1.7.36</version>
    </dependency>
  </dependencies>
""")
        write_xml(ET.ElementTree(pom_src), pom_path)
        index = Index()
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17", str(jar), str(pom_path)))
        side = root / "api.jar"
        side.write_bytes(b"api")
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17", str(side), classifier="api"))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertEqual(warnings, [])
        stored = dest / "org/slf4j/slf4j-api/2.0.17/slf4j-api-2.0.17.jar"
        self.assertTrue(stored.is_symlink())
        self.assertEqual(stored.resolve(), jar.resolve())
        self.assertTrue(stored.with_name(stored.name + ".sha1").is_file())
        classified = dest / "org/slf4j/slf4j-api/2.0.17/slf4j-api-2.0.17-api.jar"
        self.assertTrue(classified.is_symlink())
        rewritten = parse_xml(dest / "org/slf4j/slf4j-api/2.0.17/slf4j-api-2.0.17.pom")
        dep = child(child(rewritten.getroot(), "dependencies"), "dependency")
        self.assertEqual(text_of(dep, "version"), "2.0.17")
        meta = (dest / "org/slf4j/slf4j-api/maven-metadata.xml").read_text(encoding="utf-8")
        self.assertIn(">2.0.17<", meta)
        settings = root / "settings.xml"
        write_settings(settings, dest)
        text = settings.read_text(encoding="utf-8")
        self.assertNotIn("<offline>true</offline>", text)
        self.assertIn("<mirrorOf>*</mirrorOf>", text)

    def test_drop_removes_a_parent_profile_plugin_from_the_published_pom(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        pom_path = root / "commons-parent.pom"
        write_xml(ET.ElementTree(pom("""
  <groupId>org.apache.commons</groupId>
  <artifactId>commons-parent</artifactId>
  <version>89</version>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.felix</groupId>
        <artifactId>maven-bundle-plugin</artifactId>
        <version>5.1.9</version>
      </plugin>
    </plugins>
  </build>
  <profiles>
    <profile>
      <id>java-9-up</id>
      <build>
        <plugins>
          <plugin>
            <groupId>org.moditect</groupId>
            <artifactId>moditect-maven-plugin</artifactId>
            <version>1.1.0</version>
          </plugin>
        </plugins>
      </build>
    </profile>
  </profiles>
""")), pom_path)
        index = Index()
        index.add(artifact("org.apache.commons", "commons-parent", "89", str(pom_path), extension="pom"))
        kept = root / "m2-kept"
        materialize(index, kept)
        kept_text = (kept / "org/apache/commons/commons-parent/89/commons-parent-89.pom").read_text(encoding="utf-8")
        self.assertIn("moditect-maven-plugin", kept_text)
        self.assertIn("maven-bundle-plugin", kept_text)
        dropped = root / "m2-dropped"
        materialize(index, dropped, drop={"org.moditect:moditect-maven-plugin"})
        dropped_text = (dropped / "org/apache/commons/commons-parent/89/commons-parent-89.pom").read_text(encoding="utf-8")
        self.assertNotIn("moditect-maven-plugin", dropped_text)
        self.assertIn("maven-bundle-plugin", dropped_text)

    def test_published_pom_raises_compiler_level_and_keeps_plugins(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        pom_path = root / "specs.pom"
        write_xml(ET.ElementTree(pom("""
  <groupId>org.apache.geronimo.specs</groupId>
  <artifactId>specs</artifactId>
  <version>1.6</version>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
        <configuration>
          <source>1.5</source>
          <target>1.5</target>
        </configuration>
      </plugin>
      <plugin>
        <artifactId>maven-site-plugin</artifactId>
        <version>3.3</version>
      </plugin>
    </plugins>
  </build>
""")), pom_path)
        index = Index()
        index.add(artifact(
            "org.apache.geronimo.specs", "specs-parent", "1.6", "", str(pom_path), extension="pom",
            aliases=(Coord("org.apache.geronimo.specs", "specs"),),
        ))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertEqual(warnings, [])
        published = parse_xml(dest / "org/apache/geronimo/specs/specs-parent/1.6/specs-parent-1.6.pom")
        compiler = children(child(child(published.getroot(), "build"), "plugins"), "plugin")[0]
        configuration = child(compiler, "configuration")
        self.assertEqual(text_of(configuration, "source"), "8")
        self.assertEqual(text_of(configuration, "target"), "8")
        names = [text_of(item, "artifactId") for item in children(child(child(published.getroot(), "build"), "plugins"), "plugin")]
        self.assertNotIn("maven-site-plugin", names)
        self.assertIn("maven-compiler-plugin", names)
        alias_pom = dest / "org/apache/geronimo/specs/specs/1.6/specs-1.6.pom"
        self.assertTrue(alias_pom.is_file())

    def test_xmvn_pom_artifact_is_published_with_the_jar(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "commons-logging.jar"
        pom_path = root / "commons-logging.pom"
        jar.write_bytes(b"jar")
        write_xml(ET.ElementTree(pom("""
  <groupId>commons-logging</groupId>
  <artifactId>commons-logging</artifactId>
  <version>1.3.5</version>
  <dependencies>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
""")), pom_path)
        index = Index()
        index.add(artifact("commons-logging", "commons-logging", "1.3.5", "", str(pom_path), extension="pom"))
        index.add(artifact("commons-logging", "commons-logging", "1.3.5", str(jar)))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertEqual(warnings, [])
        rewritten = parse_xml(dest / "commons-logging/commons-logging/1.3.5/commons-logging-1.3.5.pom")
        # Test scope is not transitive. An uninstalled test dependency is removed
        # so a later build does not try to resolve it.
        dep = child(child(rewritten.getroot(), "dependencies"), "dependency")
        self.assertIsNone(dep)
        stored = dest / "commons-logging/commons-logging/1.3.5/commons-logging-1.3.5.jar"
        self.assertEqual(stored.resolve(), jar.resolve())

    def test_unsafe_coordinate_is_not_written_outside_the_repo(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "ok.jar"
        jar.write_bytes(b"jar")
        index = Index()
        index.add(artifact("org.example", "ok", "1", str(jar)))
        index.add(Artifact(Coord("..", "evil"), "1", str(jar)))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertTrue(any("unsafe" in line for line in warnings))
        self.assertTrue((dest / "org/example/ok/1/ok-1.jar").is_symlink())
        self.assertFalse((root / "evil").exists())

    def test_dummy_pom_is_generated_from_the_index(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "compiler.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("META-INF/maven/plugin.xml", "<plugin/>")
        dummy = root / "compiler.pom"
        dummy.write_text("Dummy POM file for maven-compiler-plugin module\n", encoding="utf-8")
        utils = root / "utils.jar"
        utils.write_bytes(b"utils")
        index = Index()
        index.add(artifact("org.apache.maven.shared", "maven-shared-utils", "3.4.2", str(utils)))
        index.add(artifact(
            "org.apache.maven.plugins", "maven-compiler-plugin", "3.12.1.MBI",
            "", str(dummy), extension="pom",
            dependencies=(Dependency(Coord("org.apache.maven.shared", "maven-shared-utils"), version=""),),
        ))
        index.add(artifact(
            "org.apache.maven.plugins", "maven-compiler-plugin", "3.12.1.MBI", str(jar),
        ))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertEqual(warnings, [])
        written = parse_xml(dest / "org/apache/maven/plugins/maven-compiler-plugin/3.12.1.MBI/maven-compiler-plugin-3.12.1.MBI.pom")
        self.assertEqual(text_of(written.getroot(), "packaging"), "maven-plugin")
        dep = child(child(written.getroot(), "dependencies"), "dependency")
        self.assertEqual(text_of(dep, "artifactId"), "maven-shared-utils")
        self.assertEqual(text_of(dep, "version"), "3.4.2")
        stored = dest / "org/apache/maven/plugins/maven-compiler-plugin/3.12.1.MBI/maven-compiler-plugin-3.12.1.MBI.jar"
        self.assertEqual(stored.resolve(), jar.resolve())

    def test_generated_pom_does_not_reexport_maven_runtime(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        core = root / "maven-core.jar"
        with zipfile.ZipFile(core, "w") as archive:
            archive.writestr(
                "META-INF/maven/extension.xml",
                "<extension><exportedArtifacts>"
                "<exportedArtifact>org.apache.maven:maven-core</exportedArtifact>"
                "<exportedArtifact>org.apache.maven:maven-model</exportedArtifact>"
                "</exportedArtifacts></extension>",
            )
        core_pom = root / "maven-core.pom"
        core_pom.write_text("Dummy POM file for maven-core\n", encoding="utf-8")
        plugin_jar = root / "plugin.jar"
        with zipfile.ZipFile(plugin_jar, "w") as archive:
            archive.writestr("META-INF/maven/plugin.xml", "<plugin/>")
        plugin_pom = root / "plugin.pom"
        plugin_pom.write_text("Dummy POM file for the plugin\n", encoding="utf-8")
        archiver = root / "archiver.jar"
        archiver.write_bytes(b"archiver")
        archiver_pom = root / "archiver.pom"
        archiver_pom.write_text("Dummy POM file for maven-archiver\n", encoding="utf-8")
        utils = root / "utils.jar"
        utils.write_bytes(b"utils")
        index = Index()
        index.add(artifact("org.apache.maven.shared", "maven-shared-utils", "3.4.2", str(utils)))
        index.add(artifact(
            "org.apache.maven", "maven-core", "4.0.0.rc.4.MBI", "", str(core_pom), extension="pom",
            dependencies=(
                Dependency(Coord("org.apache.maven", "maven-model"), version="4"),
                Dependency(Coord("org.apache.maven", "maven-impl"), version="4"),
            ),
        ))
        index.add(artifact("org.apache.maven", "maven-core", "4.0.0.rc.4.MBI", str(core)))
        index.add(artifact(
            "org.apache.maven.plugins", "maven-compiler-plugin", "3.12.1.MBI", "", str(plugin_pom), extension="pom",
            dependencies=(
                Dependency(Coord("org.apache.maven", "maven-core"), version="4"),
                Dependency(Coord("org.apache.maven.shared", "maven-shared-utils"), version=""),
            ),
        ))
        index.add(artifact("org.apache.maven.plugins", "maven-compiler-plugin", "3.12.1.MBI", str(plugin_jar)))
        index.add(artifact(
            "org.apache.maven", "maven-archiver", "3.6.2.MBI", "", str(archiver_pom), extension="pom",
            dependencies=(Dependency(Coord("org.apache.maven", "maven-core"), version="4"),),
        ))
        index.add(artifact("org.apache.maven", "maven-archiver", "3.6.2.MBI", str(archiver)))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertEqual(warnings, [])
        core_written = parse_xml(dest / "org/apache/maven/maven-core/4.0.0.rc.4.MBI/maven-core-4.0.0.rc.4.MBI.pom")
        core_ids = [text_of(node, "artifactId") for node in children(child(core_written.getroot(), "dependencies"), "dependency")]
        self.assertEqual(core_ids, ["maven-model", "maven-impl"])
        plugin_written = parse_xml(dest / "org/apache/maven/plugins/maven-compiler-plugin/3.12.1.MBI/maven-compiler-plugin-3.12.1.MBI.pom")
        plugin_ids = [text_of(node, "artifactId") for node in children(child(plugin_written.getroot(), "dependencies"), "dependency")]
        self.assertEqual(plugin_ids, ["maven-shared-utils"])
        archiver_written = parse_xml(dest / "org/apache/maven/maven-archiver/3.6.2.MBI/maven-archiver-3.6.2.MBI.pom")
        self.assertIsNone(child(archiver_written.getroot(), "dependencies"))

    def test_alias_is_published_under_its_own_path(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "guice.jar"
        jar.write_bytes(b"guice")
        pom_path = root / "guice.pom"
        write_xml(ET.ElementTree(pom("""
  <groupId>com.google.inject</groupId>
  <artifactId>guice</artifactId>
  <version>5.1.0.MBI</version>
""")), pom_path)
        index = Index()
        index.add(artifact(
            "com.google.inject", "guice", "5.1.0.MBI", str(jar), str(pom_path),
            aliases=(
                Coord("com.google.inject", "guice", "classes", "jar"),
                Coord("org.example", "guice-alias"),
            ),
        ))
        dest = root / "m2"
        warnings = materialize(index, dest)
        self.assertEqual(warnings, [])
        classified = dest / "com/google/inject/guice/5.1.0.MBI/guice-5.1.0.MBI-classes.jar"
        self.assertEqual(classified.resolve(), jar.resolve())
        alias_jar = dest / "org/example/guice-alias/5.1.0.MBI/guice-alias-5.1.0.MBI.jar"
        self.assertEqual(alias_jar.resolve(), jar.resolve())
        alias_pom = parse_xml(dest / "org/example/guice-alias/5.1.0.MBI/guice-alias-5.1.0.MBI.pom")
        self.assertEqual(text_of(alias_pom.getroot(), "artifactId"), "guice-alias")
        self.assertEqual(text_of(alias_pom.getroot(), "groupId"), "org.example")


class GradleTests(unittest.TestCase):
    def test_init_script_forces_system_version(self):
        index = Index()
        index.add(artifact("org.slf4j", "slf4j-api", "2.0.17", "/slf4j.jar",
                            aliases=(Coord("org.slf4j", "slf4j"),)))
        script = render_init_script(index, Path("/var/cache/sysjar/m2"))
        self.assertIn("'org.slf4j:slf4j-api': '2.0.17'", script)
        self.assertIn("'org.slf4j:slf4j': '2.0.17'", script)
        self.assertIn("useTarget", script)
        self.assertIn("runtimeClasspath", script)
        self.assertIn("file:///var/cache/sysjar/m2", script)
        self.assertIn("PREFER_SETTINGS", script)
        self.assertIn("sysjarAddRepo(project.buildscript.repositories)", script)
        self.assertNotIn("uri(", script)


class InstallTests(unittest.TestCase):
    def test_install_provides_and_requires(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "src"
        target = project / "target"
        target.mkdir(parents=True)
        (target / "demo-1.2.0.jar").write_bytes(b"jar")
        source = pom("""
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.2.0</version>
  <dependencies>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>1.7.25</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
""")
        pom_path = project / "pom.xml"
        write_xml(ET.ElementTree(source), pom_path)
        buildroot = root / "buildroot"
        written = install_pom(
            buildroot, pom_path,
            aliases=[Coord("com.example", "demo-old")],
        )
        record = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(provides_for(record), [
            "mvn(com.example:demo) = 1.2.0",
            "sysjar(com.example:demo) = 1.2.0",
            "mvn(com.example:demo-old) = 1.2.0",
            "sysjar(com.example:demo-old) = 1.2.0",
        ])
        self.assertEqual(requires_for(record), ["mvn(org.slf4j:slf4j-api)"])
        self.assertTrue((buildroot / "usr/share/java/demo.jar").is_file())
        self.assertTrue((buildroot / "usr/share/java/demo-old.jar").is_symlink())
        lines = filelist_lines(written)
        self.assertIn("%dir /usr/share/sysjar/index/com.example", lines)
        self.assertIn("/usr/share/java/demo.jar", lines)
        self.assertIn("/usr/share/java/demo-old.jar", lines)
        self.assertIn("/usr/share/maven-poms/demo.pom", lines)
        self.assertIn("/usr/share/sysjar/index/com.example/demo.sysjar", lines)
        index = load_index([buildroot])
        self.assertEqual(index.find("com.example", "demo-old").version, "1.2.0")

    def test_auto_gradle_record_without_pom(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        reactor = project / ".sysjar" / "reactor"
        reactor.mkdir(parents=True)
        jar = project / "demo.jar"
        jar.write_bytes(b"jar")
        (reactor / "_root.json").write_text(json.dumps({
            "group": "com.example",
            "artifact": "demo",
            "version": "9",
            "jar": str(jar),
            "pom": "",
            "dependencies": [{"group": "org.slf4j", "artifact": "slf4j-api", "version": "2.0.17", "scope": "runtime"}],
        }), encoding="utf-8")
        written = install_auto(root / "buildroot", project)
        record = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(record["version"], "9")
        self.assertEqual(record["dependencies"][0]["artifactId"], "slf4j-api")
        self.assertTrue((root / "buildroot/usr/share/maven-poms/demo.pom").is_file())

    def test_file_name_rejects_several_reactor_records(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        reactor = project / ".sysjar" / "reactor"
        reactor.mkdir(parents=True)
        jar = project / "demo.jar"
        jar.write_bytes(b"jar")
        payload = {
            "group": "com.example", "artifact": "demo", "version": "9",
            "jar": str(jar), "pom": "", "dependencies": [],
        }
        (reactor / "a.json").write_text(json.dumps(payload), encoding="utf-8")
        payload["artifact"] = "other"
        (reactor / "b.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(SysjarError):
            install_auto(root / "buildroot", project, file_name="one")


class CliTests(unittest.TestCase):
    def project(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        jar = root / "usr/share/java/slf4j-api.jar"
        jar.parent.mkdir(parents=True)
        jar.write_bytes(b"jar")
        pom_path = root / "usr/share/maven-poms/slf4j-api.pom"
        pom_path.parent.mkdir(parents=True)
        write_xml(ET.ElementTree(pom("""
  <groupId>org.slf4j</groupId>
  <artifactId>slf4j-api</artifactId>
  <version>2.0.17</version>
""")), pom_path)
        index_dir = root / "usr/share/sysjar/index/org.slf4j"
        index_dir.mkdir(parents=True)
        (index_dir / "slf4j-api.sysjar").write_text(json.dumps({
            "sysjar": 1,
            "groupId": "org.slf4j",
            "artifactId": "slf4j-api",
            "version": "2.0.17",
            "extension": "jar",
            "jar": str(jar),
            "pom": str(pom_path),
            "dependencies": [],
        }), encoding="utf-8")
        project = root / "proj"
        project.mkdir()
        write_xml(ET.ElementTree(pom("""
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>1.7.25</version>
    </dependency>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>missing</artifactId>
      <version>1</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build><plugins>
    <plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
  </plugins></build>
""")), project / "pom.xml")
        (project / "build.gradle").write_text(
            "dependencies { implementation 'org.slf4j:slf4j-api:1.7.25'\n"
            "testImplementation 'junit:junit:4.13.2' }\n",
            encoding="utf-8",
        )
        return root, project

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def test_resolve_scan_builddep_and_mvn_print(self):
        root, project = self.project()
        status, out, err = self.invoke(["resolve", "--root", str(root), "org.slf4j:slf4j-api:1.7.25"])
        self.assertEqual(status, 0, err)
        self.assertTrue(out.strip().endswith("slf4j-api.jar"))
        self.assertIn("system jar is 2.0.17", err)

        status, out, _err = self.invoke(["scan", "--root", str(root), str(project)])
        self.assertEqual(status, 0)
        self.assertIn("slf4j-api system 2.0.17", out)
        self.assertIn("junit:junit MISSING", out)
        self.assertIn("heuristic", out)

        status, out, err = self.invoke(["builddep", "--root", str(root), str(project)])
        self.assertEqual(status, 1)
        self.assertIn("BuildRequires:\tmvn(org.slf4j:slf4j-api)", out)
        self.assertIn("BuildRequires:\tmvn(junit:junit)", out)
        self.assertIn("missing mvn(junit:junit)", err)
        status, out, err = self.invoke(["builddep", "--allow-missing", "--root", str(root), str(project)])
        self.assertEqual(status, 0, err)
        self.assertIn("BuildRequires:\tmvn(junit:junit)", out)
        status, out, err = self.invoke([
            "builddep", "--allow-missing", "--drop", "junit:junit", "--root", str(root), str(project),
        ])
        self.assertEqual(status, 0, err)
        self.assertNotIn("junit:junit", out)
        self.assertIn("BuildRequires:\tmvn(org.slf4j:slf4j-api)", out)

        status, out, err = self.invoke([
            "mvn", "--root", str(root), "--project", str(project), "--print", "--", "-DskipTests", "package",
        ])
        self.assertEqual(status, 0, err)
        self.assertIn("mvn --batch-mode", out)
        self.assertNotIn(" -o", out)
        self.assertIn("-DskipTests", out)
        self.assertIn("-f ", out)
        status, out, err = self.invoke([
            "mvn", "--root", str(root), "--project", str(project),
            "--mvn", "/opt/mvn", "--print", "--", "-DskipTests", "package",
        ])
        self.assertEqual(status, 0, err)
        self.assertIn("/opt/mvn --batch-mode", out)
        rewritten = text_of(
            child(child(parse_xml(project / "pom.xml").getroot(), "dependencies"), "dependency"),
            "version",
        )
        self.assertEqual(rewritten, "2.0.17")

        buildroot = root / "buildroot"
        (project / "target").mkdir()
        shutil.copy(root / "usr/share/java/slf4j-api.jar", project / "target/demo-1.0.0.jar")
        # The rewritten pom still builds demo.
        status, out, err = self.invoke([
            "install", "--buildroot", str(buildroot), "--artifact", f"{project / 'pom.xml'}",
        ])
        self.assertEqual(status, 0, err)
        self.assertTrue((buildroot / "usr/share/java/demo.jar").is_file())

    def test_missing_required_dependency_stops_mvn(self):
        root, project = self.project()
        pom_path = project / "pom.xml"
        text = pom_path.read_text(encoding="utf-8")
        text = text.replace('<scope>test</scope>', '<scope>compile</scope>')
        pom_path.write_text(text, encoding="utf-8")
        status, _out, err = self.invoke([
            "mvn", "--root", str(root), "--project", str(project), "--print",
        ])
        self.assertEqual(status, 1)
        self.assertIn("com.acme:missing", err)

    def test_gradle_print_and_check(self):
        root, project = self.project()
        status, out, err = self.invoke([
            "gradle", "--root", str(root), "--project", str(project), "--print", "--", "build",
        ])
        self.assertEqual(status, 0, err)
        self.assertIn("gradle --offline", out)
        self.assertIn("--project-dir", out)
        self.assertIn(" build", out)
        self.assertTrue((project / ".sysjar/work/init.gradle").is_file())
        status, out, _err = self.invoke(["check", "--root", str(root)])
        self.assertEqual(status, 0, out)
        os.remove(root / "usr/share/java/slf4j-api.jar")
        status, out, _err = self.invoke(["check", "--root", str(root)])
        self.assertEqual(status, 1)
        self.assertIn("slf4j-api", out)

    def test_install_auto_file_name(self):
        root, project = self.project()
        (project / "target").mkdir()
        (project / "target" / "demo-1.0.0.jar").write_bytes(b"jar")
        buildroot = root / "buildroot"
        status, out, err = self.invoke([
            "install", "--buildroot", str(buildroot), "--auto", "--file-name", "renamed", str(project),
        ])
        self.assertEqual(status, 0, err)
        self.assertTrue((buildroot / "usr/share/java/renamed.jar").is_file())
        self.assertTrue((buildroot / "usr/share/maven-poms/renamed.pom").is_file())
        self.assertFalse((buildroot / "usr/share/java/demo.jar").exists())
        record = json.loads((buildroot / "usr/share/sysjar/index/com.example/demo.sysjar").read_text(encoding="utf-8"))
        self.assertEqual(record["jar"], "/usr/share/java/renamed.jar")
        self.assertIn("demo.sysjar", out)
        listed = root / "listed.mfiles"
        status, _out, err = self.invoke([
            "install", "--buildroot", str(root / "buildroot2"), "--auto",
            "--file-name", "renamed", "--link", "demo-compat",
            "--filelist", str(listed), str(project),
        ])
        self.assertEqual(status, 0, err)
        text = listed.read_text(encoding="utf-8")
        self.assertIn("/usr/share/java/renamed.jar\n", text)
        self.assertIn("/usr/share/java/demo-compat.jar\n", text)
        self.assertTrue((root / "buildroot2/usr/share/java/demo-compat.jar").is_symlink())

    def test_ant_print_rewrites_ivy_and_stops_when_required_jar_is_missing(self):
        root, project = self.project()
        (project / "build.xml").write_text(
            '<project name="demo" default="jar"><target name="jar"/></project>\n',
            encoding="utf-8",
        )
        (project / "ivy.xml").write_text(
            """<ivy-module version="2.0">
              <info organisation="com.example" module="demo" revision="1.0.0"/>
              <dependencies>
                <dependency org="org.slf4j" name="slf4j-api" rev="1.7.25"/>
                <dependency org="junit" name="junit" rev="4.13.2" conf="test->default"/>
                <dependency org="com.acme" name="missing" rev="1"/>
              </dependencies>
            </ivy-module>
            """,
            encoding="utf-8",
        )
        status, out, err = self.invoke([
            "ant", "--root", str(root), "--project", str(project), "--print", "--", "-Dfoo=1",
        ])
        self.assertEqual(status, 1, out)
        self.assertIn("com.acme:missing", err)
        self.assertIn("junit:junit", err)
        self.assertIn("not required to compile", err)
        status, out, err = self.invoke([
            "ant", "--root", str(root), "--project", str(project), "--drop", "com.acme:missing",
            "--ant", "/opt/ant", "--print", "--", "-Dfoo=1", "jar",
        ])
        self.assertEqual(status, 0, err)
        self.assertIn("/opt/ant", out)
        self.assertIn("-Divy.settings.file=", out)
        self.assertIn("build.xml", out)
        self.assertIn("-Dfoo=1", out)
        self.assertIn(" jar", out)
        self.assertNotIn(" -o", out)
        ivy = parse_xml(project / "ivy.xml").getroot()
        revs = [dep.get("rev") for dep in ivy.iter("dependency")]
        self.assertIn("2.0.17", revs)
        self.assertTrue((project / "ivysettings.xml").is_file())
        self.assertTrue((project / ".sysjar/work/ivysettings.xml").is_file())

    def test_ant_without_ivy_only_selects_the_build_file(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        project.mkdir()
        (project / "build.xml").write_text("<project/>\n", encoding="utf-8")
        status, out, err = self.invoke([
            "ant", "--root", str(root), "--project", str(project), "--print",
        ])
        self.assertEqual(status, 0, err)
        self.assertIn("build.xml", out)
        self.assertNotIn("ivy.settings.file", out)
        self.assertFalse((project / "ivysettings.xml").exists())


class InstallIvyTests(unittest.TestCase):
    def test_install_ivy_module_skips_test_conf(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        dist = project / "dist"
        dist.mkdir(parents=True)
        (dist / "demo.jar").write_bytes(b"jar")
        (project / "ivy.xml").write_text(
            """<ivy-module version="2.0">
              <info organisation="com.example" module="demo" revision="1.2.0"/>
              <publications>
                <artifact name="demo" type="jar"/>
              </publications>
              <dependencies>
                <dependency org="org.slf4j" name="slf4j-api" rev="1.7.36"/>
                <dependency org="junit" name="junit" rev="4.13.2" conf="test->default"/>
              </dependencies>
            </ivy-module>
            """,
            encoding="utf-8",
        )
        written = install_auto(root / "buildroot", project)
        record = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(provides_for(record), [
            "mvn(com.example:demo) = 1.2.0",
            "sysjar(com.example:demo) = 1.2.0",
        ])
        self.assertEqual(requires_for(record), ["mvn(org.slf4j:slf4j-api)"])
        self.assertTrue((root / "buildroot/usr/share/java/demo.jar").is_file())
        pom_text = (root / "buildroot/usr/share/maven-poms/demo.pom").read_text(encoding="utf-8")
        self.assertIn("slf4j-api", pom_text)
        self.assertIn(">test<", pom_text)

    def test_ivy_revision_must_be_fixed(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        project = root / "proj"
        project.mkdir()
        (project / "ivy.xml").write_text(
            '<ivy-module version="2.0"><info organisation="com.example" module="demo" revision="latest.integration"/></ivy-module>\n',
            encoding="utf-8",
        )
        with self.assertRaises(SysjarError) as caught:
            install_auto(root / "buildroot", project)
        self.assertIn("fixed version", str(caught.exception))


class CapabilityTests(unittest.TestCase):
    def test_matches_cooker_mvn_provides(self):
        from sysjar.rpmmeta import capability
        self.assertEqual(capability(Coord("org.slf4j", "slf4j-api")), "mvn(org.slf4j:slf4j-api)")
        self.assertEqual(
            capability(Coord("commons-logging", "commons-logging", "api")),
            "mvn(commons-logging:commons-logging::api:)",
        )
        self.assertEqual(
            capability(Coord("org.slf4j", "slf4j-api", "consumer", "pom")),
            "mvn(org.slf4j:slf4j-api:pom:consumer:)",
        )
        self.assertEqual(
            capability(Coord("com.google.inject", "guice", "classes")),
            "mvn(com.google.inject:guice::classes:)",
        )


class SystemMetadataTests(unittest.TestCase):
    def test_installed_xmvn_fragments_parse(self):
        directory = Path("/usr/share/maven-metadata")
        if not directory.is_dir() or not any(directory.glob("*.xml")):
            self.skipTest("no system maven metadata")
        index = load_index([Path("/")])
        self.assertGreater(len(index.artifacts()), 0)
        logging = index.find("commons-logging", "commons-logging")
        if logging is not None:
            self.assertEqual(logging.version, "1.3.5")


if __name__ == "__main__":
    unittest.main()
