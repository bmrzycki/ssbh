#!/usr/bin/env python3

import argparse
import json
import os

from csv import reader as csv_reader
from getpass import getuser
from pathlib import Path
from platform import architecture
from shlex import quote
from shutil import which, copy, copymode
from signal import signal, SIGPIPE, SIG_DFL
from subprocess import run as sp_run
from sys import argv

def ps_bins(path='/proc'):
    s = set()
    for p in Path(path).glob('[0-9]*/cmdline'):
        with open(p) as fh:
            cmdline = fh.readline().split('\0')
            if cmdline[0]:
                s.add(cmdline[0])
    return s


class OSrelease:
    def __init__(self, root='/', case_sensitive=False, exact=False):
        self._root = root
        self._case_sensitive = case_sensitive
        self._exact = exact
        self._fname = ''
        self._data = {  # Defaults from "man 5 os-release"
            'NAME'        : 'Linux',
            'ID'          : 'linux',
            'PRETTY_NAME' : 'Linux' }
        # Filename order matters, see "man 5 os-release"
        for fname in ("etc/os-release", "usr/lib/os-release"):
            p = Path(self._root, fname)
            if p.exists():
                with open(p) as fh:
                    # This isn't an os-release validator and expects
                    # adherence to the spec.
                    self._data = dict(csv_reader(fh, delimiter="="))
                self._fname = str(p)
                break  # Do not parse both files as per spec

    def file(self):
        return self._fname

    def parms(self):
        return sorted(list(self._data.keys()))

    def __getattr__(self, attr):
        if not self._case_sensitive:
            attr = attr.upper()
        if self._exact and attr not in self._data:
            raise AttributeError(
                f"module 'OSrelease' has no attribute '{attr}'")
        return self._data.get(attr, '')


class Singularity:
    def __init__(self, image_path):
        if os.sep not in image_path:
            image_path = Path(os.getcwd(), image_path)
        self.image_path = Path(image_path).resolve()
        if self.image_path.exists():
            raise FileExistsError(
                f"refusing to overwrite '{self.image_path}'")
        self.prog = which('singularity')
        if self.prog is None:
            raise FileNotFoundError(
                "'singularity' binary not in PATH")

    def _run(self, cmd, cwd):
        s = ''
        for t in cmd:
            s += f"{quote(t)} "
        print(f"# cmd: {s[:-1]}")
        print(f"# cwd: {quote(str(cwd))}")
        sp_run(cmd, cwd=cwd, check=True)

    def build(self, spec):
        self._run([self.prog, 'build', '--sandbox', '--fix-perms',
                   str(self.image_path), spec],
                  cwd=self.image_path.parent)

    def localtime(self):
        dst = self.image_path.joinpath('etc/localtime')
        dst.unlink(missing_ok=True)
        copy('/etc/localtime', dst)
        copymode('/etc/localtime', dst)

    def mkfile(self, rel_name, data, perms=0o644):
        p = Path(self.image_path, rel_name)
        if p.exists():
            raise FileExistsError(f"refusing to overwrite '{p}'")
        if isinstance(data, list):
            data = '\n'.join(data) + '\n'
        with open(p, 'w') as fh:
            fh.write(data)
        p.chmod(perms)

    def mkenv(self, name, tag='', editor='vi', editor_args=''):
        data = [ '#!/bin/sh',
                 f"export EDITOR='{editor}'",
                 f"export EDITOR_ARGUMENTS='{editor_args}'",
                 "prompt='#'",
                 "grep -q '^overlay /' /proc/mounts && prompt='$'" ]
        ps1 = f'export PS1="({name}'
        if tag:
            ps1 += f':{tag}'
        ps1 += ') $USER@$SINGULARITY_NAME \w $prompt "'
        data.append(ps1)
        # The file in env is named to be the last file sourced ensuring
        # the contents are not changed or overwritten by another file.
        self.mkfile('.singularity.d/env/99-zzzzzzzz-final-env.sh', data,
                    perms=0o755)

    def exec(self, cmd, admin=True):
        if isinstance(cmd, str):
            cmd = cmd.split()
        pre = [ self.prog, 'exec' ]
        if admin:
            pre += ['--fakeroot', '--writable']
        self._run(pre + [str(self.image_path)] + cmd, cwd='/')

    def helpers(self):
        self.mkfile(
            'usr/local/bin/ll',
            ['#!/bin/sh',
             '/bin/ls --color=auto -lFh "$@"'],
            perms=0o755)
        self.mkfile(
            'usr/local/bin/la',
            ['#!/bin/sh',
             '/bin/ls --color=auto -lFha "$@"'],
            perms=0o755)
        self.mkfile(
            'usr/local/bin/e',
            ['#!/bin/sh',
             'test -z "$EDITOR" && EDITOR="vi"',
             '$EDITOR $EDITOR_ARGUMENTS "$@"'],
            perms=0o755)


class DistroDocker(Singularity):
    def __init__(self, name, image_path='', tag='latest', editor='vi',
                 editor_args='', extras='localtime helpers mkenv',
                 pkgs='', upgrade=False):
        self._dd_editor = editor
        self._dd_editor_args = editor_args
        self._dd_updated = False
        if editor != 'vi':
            pkgs += f" {editor}"
        extras = extras.split()
        if pkgs and "install" not in extras:
            extras.append("install")
        if upgrade:
            extras.append("upgrade")
        self._dd_pkgs = pkgs.split()
        if not image_path:
            image_path = name
        super().__init__(image_path)

        self.build(f"docker://{name}:{tag}")
        for fn_name in extras:
            print(f"# extra: {fn_name}()")
            getattr(self, fn_name)()

    def update(self):
        if not self._dd_updated:
            self.update_distro()
            self._dd_updated = True

    def update_distro(self):
        "Update the distro package database"
        pass  # Subclasses may override

    def upgrade(self):
        self.update()
        self.upgrade_distro()

    def upgrade_distro(self):
        "Upgrade the distro installed packages"
        pass  # Subclasses may override

    def install(self):
        if self._dd_pkgs:
            self.update()
            self.install_distro(self._dd_pkgs)

    def install_distro(self, pkg_list):
        "install packages in pkg_list"
        pass  # Subclasses may override

    def mkenv(self):
        o = OSrelease(self.image_path)
        super().mkenv(name=o.id, tag=o.version_id, editor=self._dd_editor,
                      editor_args=self._dd_editor_args)


class Alpine(DistroDocker):
    def __init__(self, image_path='', tag='latest', pkgs='', upgrade=False):
        super().__init__(name='alpine', image_path=image_path, tag=tag,
                         editor='nano', pkgs=pkgs, upgrade=upgrade)

    def update_distro(self):
        self.exec('apk update')

    def upgrade_distro(self):
        self.exec('apk upgrade')

    def install_distro(self, pkg_list):
        self.exec(['apk', 'add'] + pkg_list)


class Fedora(DistroDocker):
    def __init__(self, image_path='', tag='latest', pkgs='', upgrade=False):
        super().__init__(name='fedora', image_path=image_path, tag=tag,
                         editor='nano', pkgs=pkgs, upgrade=upgrade)

    def upgrade_distro(self):
        self.exec('dnf -y upgrade')

    def install_distro(self, pkg_list):
        self.exec(['dnf', '-y', 'install'] + pkg_list)


class Ubuntu(DistroDocker):
    def __init__(self, image_path='', tag='latest', pkgs='', upgrade=False):
        super().__init__(name='ubuntu', image_path=image_path, tag=tag,
                         editor='nano', pkgs=pkgs, upgrade=upgrade)

    def update_distro(self):
        self.exec('apt -y update')

    def upgrade_distro(self):
        self.exec('apt -y upgrade')

    def install_distro(self, pkg_list):
        self.exec(['apt', '-y', 'install'] + pkg_list)


class UnifiNetworkController(Ubuntu):
    def __init__(self, image_path=''):
        # ubiquiti: https://bit.ly/39FE3AS
        #   docker: https://github.com/linuxserver/docker-unifi-controller
        if getuser() == 'root':
            raise RuntimeError(
                "UNC does not support root-owned containers")
        ps_haveged, ps_unc = False, False
        for name in ps_bins():
            if name == 'unifi':
                ps_unc = True
            elif name.endswith('/haveged'):
                ps_haveged = True
            if ps_haveged and ps_unc:
                break
        if ps_unc:
            raise RuntimeError(
                'UNC requires all existing running versions '
                'on this host to be stopped before installing')
        pkgs  = 'dialog less logrotate procps apt-transport-https '
        pkgs += 'ca-certificates gnupg wget mongodb-server'
        if not image_path:
            image_path = 'unc'
        tag = 'focal'  # 20.04.x
        if (architecture()[0] == '32bit' and
            os.uname().machine.startswith('arm')):
            tag = 'xenial'  # 16.04.x for mongo issues on armhf
        super().__init__(image_path=image_path, tag=tag, pkgs=pkgs,
                         upgrade=True)
        print('# Adding UniFi key and the UNC apt repository')
        self.exec('apt-key adv --keyserver keyserver.ubuntu.com '
                  '--recv 06E85760C0A52C50')
        self.mkfile('etc/apt/sources.list.d/100-ubnt-unifi.list',
                    ['deb https://www.ui.com/downloads/unifi/debian '
                     'stable ubiquiti'])
        self.update_distro()  # Force update for Unifi packages
        if tag != 'xenial':
            self.exec('apt-mark hold openjdk-1?-*')
        self.install_distro(['openjdk-8-jre-headless', 'unifi'])
        self.exec('apt autoremove -y')

        print("")
        if not ps_haveged:
            print('WARNING: UNC requires the "haveged" daemon on '
                  'headless hosts to generate sufficient entropy')
        print("# UNC runs on https://localhost:8443")
        print("# Start:")
        print(f"#   singularity exec -fw {self.image_path} "
              "/etc/init.d/unifi start")


class Homebridge(Ubuntu):
    def __init__(self, image_path=''):
        # Homebridge: https://bit.ly/3fJxiRk
        ps_avahid = False
        for name in ps_bins():
            if 'avahi-daemon:' in name:
                ps_avahid = True
                break
        pkgs = 'dialog less gcc g++ make python net-tools wget'
        if not ps_avahid:
            pkgs += ' avahi-daemon'
        if not image_path:
            image_path = 'hb'
        tag = 'focal'  # 20.04.x
        super().__init__(image_path=image_path, tag=tag, pkgs=pkgs,
                         upgrade=True)
        print('# Node.js install LTS release')
        self.exec('wget -O /nodejs_lts_installer.bash '
                  'https://deb.nodesource.com/setup_16.x')
        self.exec('bash /nodejs_lts_installer.bash')
        self.install_distro(['nodejs'])
        self.exec('npm install --global --unsafe-perm '
                  'homebridge homebridge-config-ui-x')
        self.exec('hb-service install --user root')
        hb_p = self.image_path.joinpath('root/.homebridge')
        hb_p.mkdir(exist_ok=True)

        # Manual log setup: https://bit.ly/33XqqNH
        print('# config.json setup and manual log addition')
        cfg_p = self.image_path.joinpath('var/lib/homebridge/config.json')
        found = False
        with open(cfg_p) as fh:
            data = json.load(fh)
        for pl in data['platforms']:
            if (pl.get('platform', '') == 'config'):
                pl['log'] = { 'method' : 'file',
                              'path'   : '/tmp/homebridge.log' }
                found = True
                break
        if not found:
            raise RuntimeError("unable to add logging to config.json")
        # Do the file creation and move in two separate steps. When
        # singularity runs it mounts /root as an overlay on $HOME. The
        # actual /root directory's contents is never used. The self.exec
        # method runs inside the container to move the file into the right
        # location.
        self.mkfile('/homebridge_config.json',
                    json.dumps(data, sort_keys=True, indent=4))
        self.exec('mv -v /homebridge_config.json '
                  '/root/.homebridge/config.json')

        print("")
        print("# Homebridge runs on http://localhost:8581/login")
        print("#   user: admin / password: admin")
        print("# Update:")
        print(f"#   singularity exec -fw {self.image_path} "
              "hb-service update-node")
        print("# Start:")
        if not ps_avahid:
            print(f"#   singularity exec -fw {self.image_path} "
                  "/etc/init.d/dbus restart")
            print(f"#   singularity exec -fw {self.image_path} "
                  "/etc/init.d/avahi-daemon restart")
        print(f"#   (singularity exec -fw {self.image_path} "
              "homebridge | tee -a /tmp/homebridge.log) &")


def main(args_raw):
    valid = {
        'alpine' : 'tag=str, pkgs=str, upgrade=bool',
        'fedora' : 'tag=str, pkgs=str, upgrade=bool',
        'ubuntu' : 'tag=str, pkgs=str, upgrade=bool',
        'unc'    : '',
        'hb'     : '' }
    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description='Singularity sandbox build helper')
    p.add_argument(
        '-l', '--list',
        default=False, action='store_true',
        help='list all supported containers + options')
    p.add_argument(
        '-i', '--image-path',
        default='',
        help='path and name to Singularity sandbox'
        'defaults to cwd + name if unset')
    p.add_argument(
        '-n', '--name',
        default='alpine',
        help='image name to create')
    p.add_argument(
        '-o', '--option',
        default=[], action='append',
        help='key=value options depnding on the image')
    args = p.parse_args(args_raw)
    if args.list:
        for k in sorted(valid.keys()):
            print(f"{k:20} {valid[k]}")
        return
    image_path = args.image_path
    if not image_path:
        image_path = args.name
    sb_args = { 'image_path' : image_path }
    for kv in args.option:
        if '=' in kv:
            k, _, v = kv.partition('=')
            if k == 'upgrade':
                v = v.lower()
                if v in ('y', 'yes', 't', 'true'):
                    v = True
                else:
                    v = False
            sb_args[k] = v

    try:
        { 'alpine' : Alpine,
          'fedora' : Fedora,
          'ubuntu' : Ubuntu,
          'unc'    : UnifiNetworkController,
          'hb'     : Homebridge }[args.name](**sb_args)
    except Exception as e:
        p.error(f"{args.name}, {str(e)}")


if __name__ == '__main__':
    signal(SIGPIPE, SIG_DFL)  # Avoid exceptions for broken pipes
    main(argv[1:])
