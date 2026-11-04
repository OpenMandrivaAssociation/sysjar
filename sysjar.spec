Name:		sysjar
Version:	0.1.0
Release:	1
Summary:	Build Maven and Gradle projects against system jars
Group:		Development/Java
License:	MIT
URL:		https://openmandriva.org/
BuildArch:	noarch

BuildRequires:	python
Requires:	python
Requires:	javapackages-filesystem

%description
sysjar builds Maven, Gradle and Ivy projects against jars installed as
system packages. A system jar is the Java equivalent of a shared library:
the distribution ships one copy of a library, and a build uses that copy
instead of downloading a version of its own.

The index is /usr/share/sysjar/index. Packages that still ship XMvn
metadata under /usr/share/maven-metadata are read as well. RPM provides
use the coordinate names already used by cooker packages, so a jar
packaged with sysjar satisfies those BuildRequires.

A spec can declare BuildSystem: maven, gradle, ant or ivy. That pulls
in this package through rpm-build(maven) (and the matching names for
the others) and fills in the build, install and check sections.

%prep
%setup -c -T
# %check runs the buildsystem tests, which read macros.sysjar and bin/sysjar
# next to the tests directory.
cp -a %{_sourcedir}/sysjar %{_sourcedir}/tests %{_sourcedir}/bin \
	%{_sourcedir}/macros.sysjar %{_sourcedir}/LICENSE .

%build

%check
python -m unittest discover -s tests -v

%install
install -d %{buildroot}%{_bindir}
install -d %{buildroot}%{_datadir}/sysjar/lib/sysjar
install -d %{buildroot}%{_datadir}/sysjar/index
install -d %{buildroot}%{_mandir}/man1
install -d %{buildroot}%{_rpmmacrodir}
install -d %{buildroot}%{_rpmconfigdir}/fileattrs
install -m0755 %{_sourcedir}/bin/sysjar %{buildroot}%{_bindir}/sysjar
install -m0644 %{_sourcedir}/sysjar/*.py %{buildroot}%{_datadir}/sysjar/lib/sysjar/
install -m0644 %{_sourcedir}/macros.sysjar %{buildroot}%{_rpmmacrodir}/macros.sysjar
install -m0644 %{_sourcedir}/sysjar.attr %{buildroot}%{_rpmconfigdir}/fileattrs/sysjar.attr
install -m0755 %{_sourcedir}/sysjardeps %{buildroot}%{_rpmconfigdir}/sysjardeps
install -m0644 %{_sourcedir}/sysjar.1 %{buildroot}%{_mandir}/man1/sysjar.1

%transfiletriggerin -- %{_datadir}/sysjar/index
/usr/bin/sysjar repo --dest /var/cache/sysjar/m2 -q || :

%files
%license LICENSE
%{_bindir}/sysjar
%dir %{_datadir}/sysjar
%dir %{_datadir}/sysjar/index
%dir %{_datadir}/sysjar/lib
%{_datadir}/sysjar/lib/sysjar
%{_rpmmacrodir}/macros.sysjar
%{_rpmconfigdir}/fileattrs/sysjar.attr
%{_rpmconfigdir}/sysjardeps
%{_mandir}/man1/sysjar.1*
