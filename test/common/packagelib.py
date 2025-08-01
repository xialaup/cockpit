# This file is part of Cockpit.
#
# Copyright (C) 2017 Red Hat, Inc.
#
# Cockpit is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# Cockpit is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Cockpit; If not, see <https://www.gnu.org/licenses/>.

import logging
import os
import re
import textwrap
from collections.abc import Mapping

from testlib import MachineCase


class PackageCase(MachineCase):
    def setUp(self) -> None:
        super().setUp()

        self.repo_dir = os.path.join(self.vm_tmpdir, "repo")

        if self.machine.ostree_image:
            logging.warning("PackageCase: OSTree images can't install additional packages")
            return

        # expected backend; hardcode this on image names to check the auto-detection
        if self.machine.image.startswith("debian") or self.machine.image.startswith("ubuntu"):
            self.backend = "apt"
            self.primary_arch = "all"
            self.secondary_arch = "amd64"
        elif re.match(r"fedora-(39|40)|(centos|rhel)-(8|9|10).*", self.machine.image):
            self.backend = "dnf4"
            self.primary_arch = "noarch"
            self.secondary_arch = "x86_64"
        elif self.machine.image.startswith(("fedora", "rhel-", "centos-")):
            self.backend = "dnf5"
            self.primary_arch = "noarch"
            self.secondary_arch = "x86_64"
        elif "suse" in self.machine.image:
            self.backend = "zypper"
            self.primary_arch = "noarch"
            self.secondary_arch = "x86_64"
        elif self.machine.image == "arch":
            self.backend = "alpm"
            self.primary_arch = "any"
            self.secondary_arch = "x86_64"
        else:
            raise NotImplementedError("unknown image " + self.machine.image)

        if "debian" in self.image or "ubuntu" in self.image:
            # PackageKit refuses to work when offline, and main interface is not managed by NM on these images
            self.machine.execute("nmcli con add type dummy con-name fake ifname fake0 ip4 1.2.3.4/24 gw4 1.2.3.1")
            self.addCleanup(self.machine.execute, "nmcli con delete fake")

        # HACK: packagekit often hangs on shutdown; https://bugzilla.redhat.com/show_bug.cgi?id=1717185
        self.write_file("/etc/systemd/system/packagekit.service.d/timeout.conf", "[Service]\nTimeoutStopSec=5\n")
        self.addCleanup(self.machine.execute, "systemctl stop packagekit; systemctl reset-failed packagekit || true")

        # disable all existing repositories to avoid hitting the network
        if self.backend == "apt":
            self.restore_dir("/var/lib/apt", reboot_safe=True)
            self.restore_dir("/var/cache/apt", reboot_safe=True)
            self.restore_dir("/etc/apt", reboot_safe=True)
            self.machine.execute(
                "echo > /etc/apt/sources.list; rm -f /etc/apt/sources.list.d/*; apt-get clean; apt-get update")
        elif self.backend == "alpm":
            self.restore_dir("/var/lib/pacman", reboot_safe=True)
            self.restore_dir("/var/cache/pacman", reboot_safe=True)
            self.restore_dir("/etc/pacman.d", reboot_safe=True)
            self.restore_dir("/var/lib/PackageKit/alpm", reboot_safe=True)
            self.restore_file("/etc/pacman.conf")
            self.restore_file("/etc/pacman.d/mirrorlist")
            self.restore_file("/usr/share/libalpm/hooks/90-packagekit-refresh.hook")

            self.machine.execute("rm /etc/pacman.conf /etc/pacman.d/mirrorlist /var/lib/pacman/sync/* "
                                 "/usr/share/libalpm/hooks/90-packagekit-refresh.hook")
            # Drop alpm state directory as it interferes with running offline
            self.machine.execute("test -d /var/lib/PackageKit/alpm && rm -r /var/lib/PackageKit/alpm || true")
            # Clean up possible leftover lockfile
            self.machine.execute("""
                if [ -f /var/lib/pacman/db.lck ]; then
                    fuser -k /var/lib/pacman/db.lck || true;
                    rm /var/lib/pacman/db.lck;
                fi
            """)

            # Initial config for installation
            empty_repo_dir = '/var/lib/cockpittest/empty'
            config = f"""
[options]
Architecture = auto
HoldPkg     = pacman glibc

[empty]
SigLevel = Never
Server = file://{empty_repo_dir}
"""
            # HACK: Setup empty repo for packagekit
            self.machine.execute(f"mkdir -p {empty_repo_dir} || true")
            self.machine.execute(f"repo-add {empty_repo_dir}/empty.db.tar.gz")
            self.machine.write("/etc/pacman.conf", config)
            self.machine.execute("pacman -Sy")
        else:
            self.restore_dir("/etc/yum.repos.d", reboot_safe=True)
            self.restore_dir("/var/cache/dnf", reboot_safe=True)
            self.machine.execute("rm -rf /etc/yum.repos.d/* /var/cache/dnf/*")

        # have PackageKit start from a clean slate
        self.machine.execute("systemctl stop packagekit")
        self.machine.execute("systemctl kill --signal=SIGKILL packagekit || true; rm -rf /var/cache/PackageKit")
        self.machine.execute("systemctl reset-failed packagekit || true")
        self.restore_file("/var/lib/PackageKit/transactions.db")

        if self.image in ["debian-stable", "debian-testing"]:
            # PackageKit tries to resolve some DNS names, but our test VM is offline;
            # temporarily disable the name server to fail quickly
            self.machine.execute("mv /etc/resolv.conf /etc/resolv.conf.test")
            self.addCleanup(self.machine.execute, "mv /etc/resolv.conf.test /etc/resolv.conf")

        # reset automatic updates
        if self.backend == 'dnf4':
            self.restore_file("/etc/dnf/automatic.conf")
            self.machine.execute("systemctl disable --now dnf-automatic dnf-automatic-install "
                                 "dnf-automatic.service dnf-automatic-install.timer")
            self.machine.execute("rm -r /etc/systemd/system/dnf-automatic* && systemctl daemon-reload || true")

        if self.backend == 'dnf5':
            self.restore_file("/etc/dnf/dnf5-plugins/automatic.conf")
            self.addCleanup(self.machine.execute, "systemctl disable --now dnf5-automatic.timer 2>/dev/null || true")
            self.addCleanup(self.machine.execute,
                            "rm -r /etc/systemd/system/dnf5-automatic*.d && systemctl daemon-reload || true")

        self.updateInfo: dict[tuple[str, str, str], Mapping[str, str | list[str]]] = {}

        # HACK: kpatch check sometimes complains that we don't set up a full repo in unrelated tests
        self.allow_browser_errors("Could not determine kpatch packages:.*repodata updates was not complete")

    #
    # Helper functions for creating packages/repository
    #

    def createPackage(self, name: str, version: str, release: str, *, install: bool = False,
                      postinst: str | None = None, depends: str = "",
                      content: Mapping[str, Mapping[str, str] | str] | None = None,
                      arch: str | None = None, provides: str | None = None, **updateinfo: str | list[str]) -> None:
        """Create a dummy package in repo_dir on self.machine

        If install is True, install the package. Otherwise, update the package
        index in repo_dir.
        """
        if provides:
            provides = f"Provides: {provides}"
        else:
            provides = ""

        if self.backend == "apt":
            self.createDeb(name, version + '-' + release, depends, postinst, install=install, content=content,
                           arch=arch, provides=provides)
        elif self.backend == "alpm":
            self.createPacmanPkg(name, version, release, depends, postinst, install=install, content=content,
                                 arch=arch, provides=provides)
        else:
            self.createRpm(name, version, release, depends, postinst, install=install, content=content,
                           arch=arch, provides=provides)
        if updateinfo:
            self.updateInfo[name, version, release] = updateinfo

    def createDeb(self, name: str, version: str, depends: str, postinst: str | None = None, *, install: bool,
                  content: Mapping[str, Mapping[str, str] | str] | None = None,
                  arch: str | None = None, provides: str | None = None) -> None:
        """Create a dummy deb in repo_dir on self.machine

        If install is True, install the package. Otherwise, update the package
        index in repo_dir.
        """
        m = self.machine

        if arch is None:
            arch = self.primary_arch
        deb = f"{self.repo_dir}/{name}_{version}_{arch}.deb"
        if postinst:
            postinstcode = f"printf '#!/bin/sh\n{postinst}' > /tmp/b/DEBIAN/postinst; chmod 755 /tmp/b/DEBIAN/postinst"
        else:
            postinstcode = ''
        if content is not None:
            for path, data in content.items():
                dest = "/tmp/b/" + path
                m.execute(f"mkdir -p '{os.path.dirname(dest)}'")
                if isinstance(data, Mapping):
                    m.execute(f"cp '{data['path']}' '{dest}'")
                else:
                    m.write(dest, data)
        m.execute(f"mkdir -p {self.repo_dir}")
        m.write("/tmp/b/DEBIAN/control", textwrap.dedent(f"""
            Package: {name}
            Version: {version}
            Priority: optional
            Section: test
            Maintainer: foo
            Depends: {depends}
            Architecture: {arch}
            Description: dummy {name}
            {provides}
            """))

        cmd = f"""set -e
                  {postinstcode}
                  touch /tmp/b/stamp-{name}-{version}
                  dpkg -b /tmp/b {deb}
                  rm -r /tmp/b
              """
        if install:
            cmd += "dpkg -i " + deb
        m.execute(cmd)
        self.addCleanup(m.execute, f"dpkg -P --force-depends --force-remove-reinstreq {name} 2>/dev/null || true")

    def createRpm(self, name: str, version: str, release: str, requires: str, post: str | None = None, *,
                  install: bool,
                  content: Mapping[str, Mapping[str, str] | str] | None = None,
                  arch: str | None = None,
                  provides: str | None = None) -> None:
        """Create a dummy rpm in repo_dir on self.machine

        If install is True, install the package. Otherwise, update the package
        index in repo_dir.
        """
        if post:
            postcode = '\n%%post\n' + post
        else:
            postcode = ''
        if requires:
            requires = f"Requires: {requires}\n"
        if arch is None:
            arch = self.primary_arch
        installcmds = f"touch $RPM_BUILD_ROOT/stamp-{name}-{version}-{release}\n"
        installedfiles = f"/stamp-{name}-{version}-{release}\n"
        if content is not None:
            for path, data in content.items():
                installcmds += f'mkdir -p $(dirname "$RPM_BUILD_ROOT/{path}")\n'
                if isinstance(data, Mapping):
                    installcmds += f"cp {data['path']} \"$RPM_BUILD_ROOT/{path}\"\n"
                else:
                    installcmds += f'cat >"$RPM_BUILD_ROOT/{path}" <<\'EOF\'\n' + data + '\nEOF\n'
                installedfiles += f"{path}\n"

        architecture = ""
        if arch == self.primary_arch:
            architecture = f"BuildArch: {self.primary_arch}"
        spec = f"""
Summary: dummy {name}
Name: {name}
Version: {version}
Release: {release}
License: BSD
{provides}
{architecture}
{requires}

%define _build_id_links none

%%install
{installcmds}

%%description
Test package.

%%files
{installedfiles}

{postcode}
"""
        self.machine.write("/tmp/spec", spec)
        cmd = """
rpmbuild --quiet -bb /tmp/spec
mkdir -p {0}
cp ~/rpmbuild/RPMS/{4}/*.rpm {0}
rm -rf ~/rpmbuild
"""
        if install:
            cmd += "rpm -i {0}/{1}-{2}-{3}.*.rpm"
        self.machine.execute(cmd.format(self.repo_dir, name, version, release, arch))
        self.addCleanup(self.machine.execute, f"rpm -e --nodeps {name} 2>/dev/null || true")

    def createPacmanPkg(self, name: str, version: str, release: str, requires: str, postinst: str | None = None, *,
                        install: bool,
                        content: Mapping[str, Mapping[str, str] | str] | None = None,
                        arch: str | None = None,
                        provides: str | None = None) -> None:
        """Create a dummy pacman package in repo_dir on self.machine

        If install is True, install the package. Otherwise, update the package
        index in repo_dir.
        """

        if arch is None:
            arch = 'any'

        sources = ""
        installcmds = 'package() {\n'
        if content is not None:
            sources = "source=("
            files = 0
            for path, data in content.items():
                p = os.path.dirname(path)
                installcmds += f'mkdir -p $pkgdir{p}\n'
                if isinstance(data, Mapping):
                    dpath = data["path"]

                    file = os.path.basename(dpath)
                    sources += file
                    files += 1
                    # TODO: hardcoded /tmp
                    self.machine.execute(f'cp {data["path"]} /tmp/{file}')
                    installcmds += f'cp {file} $pkgdir{path}\n'
                else:
                    installcmds += f'cat >"$pkgdir{path}" <<\'EOF\'\n' + data + '\nEOF\n'

            sources += ")"

        # Always stamp a file
        installcmds += f"touch $pkgdir/stamp-{name}-{version}-{release}\n"
        installcmds += '}'

        pkgbuild = f"""
pkgname={name}
pkgver={version}
pkgdesc="dummy {name}"
pkgrel={release}
arch=({arch})
depends=({requires})
options=(!debug)
{sources}

{installcmds}
"""

        if postinst:
            postinstcode = f"""
post_install() {{
    {postinst}
}}

post_upgrade() {{
    post_install $*
}}
"""
            self.machine.write(f"/tmp/{name}.install", postinstcode)
            pkgbuild += f"\ninstall={name}.install\n"

        self.machine.write("/tmp/PKGBUILD", pkgbuild)

        cmd = """
        cd /tmp/
        su builder -c "makepkg --cleanbuild --clean --force --nodeps --skipinteg --noconfirm"
"""

        if install:
            cmd += f"pacman -U --overwrite '*' --noconfirm {name}-{version}-{release}-{arch}.pkg.tar.zst\n"

        cmd += f"mkdir -p {self.repo_dir}\n"
        cmd += f"mv *.pkg.tar.zst {self.repo_dir}\n"
        # Clean up packaging files
        cmd += "rm PKGBUILD\n"
        if postinst:
            cmd += f"rm /tmp/{name}.install"
        self.machine.execute(cmd)
        self.addCleanup(self.machine.execute, f"pacman -Rdd --noconfirm {name} 2>/dev/null || true")

    def createAptChangelogs(self) -> None:
        # apt metadata has no formal field for bugs/CVEs, they are parsed from the changelog
        for ((pkg, ver, rel), info) in self.updateInfo.items():
            changes = info.get("changes", "some changes")
            assert isinstance(changes, str)
            if info.get("bugs"):
                assert isinstance(info["bugs"], list)
                changes += f" (Closes: {', '.join([('#' + str(b)) for b in info['bugs']])})"
            if info.get("cves"):
                assert isinstance(info["cves"], list)
                changes += "\n  * " + ", ".join(info["cves"])

            path = f"{self.repo_dir}/changelogs/{pkg[0]}/{pkg}/{pkg}_{ver}-{rel}"
            contents = f"""{pkg} ({ver}-{rel}) unstable; urgency=medium

  * {changes}

 -- Joe Developer <joe@example.com>  Wed, 31 May 2017 14:52:25 +0200
"""
            self.machine.execute(f"mkdir -p $(dirname {path}); echo '{contents}' > {path}")

    def createYumUpdateInfo(self) -> str:
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<updates>\n'
        for ((pkg, ver, rel), info) in self.updateInfo.items():
            refs = ""
            for b in info.get("bugs", []):
                refs += f'      <reference href="https://bugs.example.com?bug={b}" id="{b}" title="Bug#{b} Description" type="bugzilla"/>\n'
            for c in info.get("cves", []):
                refs += f'      <reference href="https://www.cve.org/CVERecord?id={c}" id="{c}" title="{c}" type="cve"/>\n'
            if info.get("securitySeverity"):
                refs += '      <reference href="https://access.redhat.com/security/updates/classification/#{0}" id="" title="" type="other"/>\n'.format(info[
                                                                                                                                                        "securitySeverity"])
            for e in info.get("errata", []):
                refs += f'      <reference href="https://access.redhat.com/errata/{e}" id="{e}" title="{e}" type="self"/>\n'

            xml += """  <update from="test@example.com" status="stable" type="{severity}" version="2.0">
    <id>UPDATE-{pkg}-{ver}-{rel}</id>
    <title>{pkg} {ver}-{rel} update</title>
    <issued date="2017-01-01 12:34:56"/>
    <description>{desc}</description>
    <references>
{refs}
    </references>
    <pkglist>
     <collection short="0815">
        <package name="{pkg}" version="{ver}" release="{rel}" epoch="0" arch="noarch">
          <filename>{pkg}-{ver}-{rel}.noarch.rpm</filename>
        </package>
      </collection>
    </pkglist>
  </update>
""".format(pkg=pkg, ver=ver, rel=rel, refs=refs,
                desc=info.get("changes", ""), severity=info.get("severity", "bugfix"))

        xml += '</updates>\n'
        return xml

    def addPackageSet(self, name: str) -> None:
        self.machine.execute(f"mkdir -p {self.repo_dir}; cp /var/lib/package-sets/{name}/* {self.repo_dir}")

    def enableRepo(self) -> None:
        if self.backend == "apt":
            self.createAptChangelogs()
            self.machine.execute(f"""echo 'deb [trusted=yes] file://{self.repo_dir} /' > /etc/apt/sources.list.d/test.list
                                    cd {self.repo_dir}; apt-ftparchive packages . > Packages
                                    xz -c Packages > Packages.xz
                                    O=$(apt-ftparchive -o APT::FTPArchive::Release::Origin=cockpittest release .); echo "$O" > Release
                                    echo 'Changelogs: http://localhost:12345/changelogs/@CHANGEPATH@' >> Release
                                    """)
            pid = self.machine.spawn(f"cd {self.repo_dir}; exec python3 -m http.server 12345", "changelog")
            # pid will not be present for rebooting tests
            self.addCleanup(self.machine.execute, "kill %i || true" % pid)
            self.machine.wait_for_cockpit_running(port=12345)  # wait for changelog HTTP server to start up
        elif self.backend == "alpm":
            self.machine.execute(f"""cd {self.repo_dir}
                                     repo-add {self.repo_dir}/testrepo.db.tar.gz *.pkg.tar.zst
                    """)

            config = f"""
[testrepo]
SigLevel = Never
Server = file://{self.repo_dir}
            """
            if 'testrepo' not in self.machine.execute('grep testrepo /etc/pacman.conf || true'):
                self.machine.write("/etc/pacman.conf", config, append=True)
                # packagekit does not detect new repositories without a restart and refresh
                self.machine.execute("systemctl restart packagekit; pkcon refresh force")

        else:
            self.machine.execute(f"""printf '[updates]\nname=cockpittest\nbaseurl=file://{self.repo_dir}\nenabled=1\ngpgcheck=0\n' > /etc/yum.repos.d/cockpittest.repo
                                     echo '{self.createYumUpdateInfo()}' > /tmp/updateinfo.xml
                                     createrepo_c {self.repo_dir}
                                     modifyrepo_c /tmp/updateinfo.xml {self.repo_dir}/repodata
                                     dnf clean all""")

    def removePackages(self, packages: list[str]) -> None:
        packages_str = ' '.join(packages)
        if self.backend == "alpm":
            self.machine.execute(f"pacman -Rdd --noconfirm {packages_str}")
        elif self.backend == "apt":
            self.machine.execute(f"dpkg --purge {packages_str}")
        elif self.backend.startswith('dnf') or self.backend == "zypper":
            self.machine.execute(f"rpm --erase {packages_str}")
