#! /bin/env python
"""Arch Linux maintenance tool for merging /etc files."""

import sys
import os
import stat
import time
import argparse
import inspect
import configparser
import tarfile
import hashlib
import itertools
import shutil
import contextlib
from textwrap import dedent
from subprocess import check_output, STDOUT, CalledProcessError

__version__ = '0.1'
pgm = os.path.basename(sys.argv[0])
RW_ACCESS = stat.S_IWUSR | stat.S_IRUSR
EXCLUDE_FILES = 'passwd, group, udev/hwdb.bin'
EXCLUDE_PKGS = ''
EXCLUDE_ETC = 'ca-certificates, fonts, ssl/certs'

def abort(msg):
    print('*** %s: error:' % pgm, msg, file=sys.stderr)
    sys.exit(1)

def warn(msg):
    print('*** warning:', msg, file=sys.stderr)

def path_sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.digest()

def repo_name():
    xdg_data_home = os.environ.get('XDG_DATA_HOME')
    if xdg_data_home is None:
        home = os.environ.get('HOME')
        if home is None:
            print('Error: HOME environment variable not set', file=sys.stderr)
            sys.exit(1)
        xdg_data_home = os.path.join(home, '.local/share')
    repo = os.path.join(xdg_data_home, 'etcmerger')
    return repo

@contextlib.contextmanager
def change_cwd(path):
    """Context manager that temporarily creates and changes the cwd."""
    saved_dir = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(saved_dir)

class Timestamp():
    def __init__(self, merger):
        self.merger = merger
        self.fname = '.etcmerger_timestamp'
        self.prefix = 'TIMESTAMP='
        self.path = os.path.join(merger.repo, self.fname)

    def new(self):
        """Create the timestamp file."""
        content = """\
            # This file is created by etcmerger. Its purpose is to record the
            # time the master (resp. etc) branch has been fast-forwarded to
            # the master-tmp (resp. etc-tmp) branch.
            TIMESTAMP=0
        """
        self.merger.add_file(self.fname, content, 'Add the timestamp')

    def abort_corrupted(self):
        abort("the '%s' timestamp file is corrupted" % self.fname)

    def now(self):
        """Set the timestamp to the current time."""
        prefix_found = False
        with open(self.path, 'r') as f:
            lines = []
            for line in f:
                if line.startswith(self.prefix):
                    prefix_found = True
                    line = self.prefix + str(int(time.time()))
                lines.append(line)
        if not prefix_found:
            self.abort_corrupted()
        content = ''.join(lines)
        self.merger.add_file(self.fname, content, 'Update the timestamp')

    @property
    def value(self):
        with open(self.path, 'r') as f:
            for line in f:
                if line.startswith(self.prefix):
                    return int(line[line.index('=')+1:])
        self.abort_corrupted()

class EtcMerger():
    """Provide methods to implement the commands."""

    def __init__(self):
        self.repo = repo_name()
        self.timestamp = Timestamp(self)

    def init(self):
        if not hasattr(self, 'dry_run'):
            self.dry_run = False
        if hasattr(self, 'cachedir') and self.cachedir is None:
            cfg = configparser.ConfigParser(allow_no_value=True)
            with open('/etc/pacman.conf') as f:
                cfg.read_file(f)
            self.cachedir = cfg['options']['CacheDir']

    def run(self):
        """Run the etcmerger command."""
        self.init()
        self.func(self)

    def run_cmd(self, cmd):
        if self.dry_run:
            print(cmd)
            return

        # Assume there may be only one double quoted argument and that it is
        # at the end of the command.
        qs = cmd.split('"', maxsplit=1)
        if len(qs) == 2:
            cmd = qs[0].split()
            cmd.append(qs[1].strip('"'))
        else:
            cmd = cmd.split()

        try:
            output = check_output(cmd, universal_newlines=True, stderr=STDOUT)
        except CalledProcessError as e:
            output = str(e) + '\n' + e.output.strip('\n')
            abort(output)
        return output

    def git_cmd(self, cmd, do_print=True):
        git_dir = os.path.join(self.repo, '.git')
        cmd = 'git --git-dir=%s --work-tree=%s %s' % (git_dir, self.repo, cmd)
        output = self.run_cmd(cmd)
        if do_print:
            output = output.strip('\n')
            if output:
                print(output)
        return output

    def add_file(self, path, content, commit_msg):
        path = os.path.join(self.repo, path)
        with open(path, 'w') as f:
            f.write(dedent(content))
        self.git_cmd('add %s' % path)
        self.git_cmd('commit -m "%s"' % commit_msg)

    def tracked_files(self, branch):
        """Return a dictionary of the tracked files in this branch."""
        d = {}
        ls_tree = self.git_cmd('ls-tree -r --name-only --full-tree %s' %
                               branch, do_print=False)

        self.git_cmd('checkout %s' % branch, do_print=False)
        for fname in ls_tree.split('\n'):
            path = os.path.join(self.repo, fname)
            if os.path.isfile(path):
                d[fname] = path_sha1(path)
        return d

    def list_files(self, path, suffixes=None, prefixes=None):
        """List of names of regular files in path.

        Exclude file names that are a match for one of the suffixes in
        'suffixes' and file names that are a match for one of the prefixes in
        'prefixes'.
        """

        flist = []
        suffixes_len = len(suffixes) if suffixes is not None else 0
        prefixes_len = len(prefixes) if prefixes is not None else 0
        with change_cwd(path):
            for root, dirs, files in os.walk('.'):
                for fname in files:
                    if fname.endswith('.pacnew'):
                        continue
                    abspath = os.path.normpath(os.path.join(root, fname))
                    if os.path.isdir(abspath):
                        continue
                    # Exclude files ending with one of the suffixes.
                    if suffixes_len:
                        if (len(list(itertools.takewhile(lambda x: not x or
                                not abspath.endswith(x), suffixes))) !=
                                    suffixes_len):
                            continue
                    # Exclude files starting with one of the prefixes.
                    if prefixes_len:
                        if (len(list(itertools.takewhile(lambda x: not x or
                                not abspath.startswith(x), prefixes))) !=
                                    prefixes_len):
                            continue
                    flist.append(abspath)
        return flist

    def cmd_init(self):
        """Initialize the git repository."""
        # Create the git repository directory if it does not exist.
        if not os.path.isdir(self.repo):
            os.makedirs(self.repo)
        if os.listdir(self.repo):
            abort('%s is not empty' % self.repo)
        self.git_cmd('init')

        # Add .gitignore.
        gitignore = """\
            *.swp
        """
        self.add_file('.gitignore', gitignore, 'Creation')

        # Create the etc branch.
        self.git_cmd('checkout -b etc')

        # Create the timestamp file in the etc branch.
        self.timestamp.new()

        # Initialize both branches with the installed packages.
        # Add to the master branch the /etc files that are different from
        # their counterpart in pacman cachedir.
        self.create_tmp_branches()
        self.scan_cachedir()
        self.finalize()
        print('Init command ok.')

    def cmd_update(self):
        """Update the repository."""
        self.create_tmp_branches()
        self.scan_cachedir()
        print('Command merge ok')

    def cmd_diff(self):
        """Print the /etc regular file names that are not in the etc branch.

        Exclude pacnew, pacsave and pacorig files.
        """
        self.git_cmd('checkout etc')
        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = self.list_files('/etc', suffixes=suffixes,
                                    prefixes=self.exclude)
        repo_files = self.list_files(os.path.join(self.repo, 'etc'))
        print('\n'.join(sorted(set(etc_files).difference(repo_files))))
        self.git_cmd('checkout master')

    def cmd_sync(self):
        """Synchronize /etc with the master branch."""
        self.finalize()
        print('Command sync ok')

    @property
    def branches(self):
        branches = self.git_cmd('branch --list', do_print=False)
        return [x.strip(' *') for x in branches.split('\n')]

    def create_tmp_branches(self):
        print('Create the master-tmp and etc-tmp branches')
        for branch in ('etc', 'master'):
            tmp_branch = '%s-tmp' % branch
            if tmp_branch in self.branches:
                self.git_cmd('branch --delete --force %s' % tmp_branch)
            self.git_cmd('checkout %s' % branch, do_print=False)
            self.git_cmd('branch %s' % tmp_branch)

    def remove_tmp_branches(self):
        if not 'master-tmp' in self.branches:
            return False

        print('Remove the master-tmp and etc-tmp branches')
        # Do a fast-forward merge.
        for branch in ('master', 'etc'):
            tmp_branch = '%s-tmp' % branch
            self.git_cmd('checkout %s' % branch, do_print=False)
            self.git_cmd('merge %s' % tmp_branch)
            self.git_cmd('branch --delete %s' % tmp_branch)
        return True

    def finalize(self):
        if self.remove_tmp_branches():
            print('Update the timestamp')
            self.timestamp.now()
            self.git_cmd('checkout master')

    def new_packages(self):
        """Return the packages newer than the timestamp."""
        timestamp = self.timestamp.value
        exclude_pkgs_len = len(self.exclude_pkgs)
        packages = {}
        for root, *remain in os.walk(self.cachedir,
                                     followlinks=self.followlinks):
            with os.scandir(root) as it:
                for pkg in it:
                    pkg_name = pkg.name
                    if not pkg_name.endswith('.pkg.tar.xz'):
                        continue
                    if pkg.stat().st_mtime <= timestamp:
                        continue

                    # Exclude packages.
                    if (len(list(itertools.takewhile(lambda x: not x or not
                            pkg_name.startswith(x), self.exclude_pkgs))) !=
                                exclude_pkgs_len):
                        continue

                    name, *remain = pkg_name.rsplit('-', maxsplit=3)
                    if len(remain) != 3:
                        warn('ignoring incorrect package name: %s' % pkg_name)
                        continue
                    if name not in packages:
                        packages[name] = pkg
                    elif packages[name].name < pkg_name:
                        packages[name] = pkg
        return packages.values()

    def extract(self, packages, tracked):
        def etc_files_filter(members):
            for tarinfo in members:
                fname = tarinfo.name
                if (tarinfo.isfile() and fname.startswith('etc') and
                        fname not in self.exclude_files):
                    yield tarinfo

        extracted = {}
        for pkg in packages:
            print('extracting from package:', pkg.name)
            tar = tarfile.open(pkg.path, mode='r:xz', debug=1)
            for tarinfo in etc_files_filter(tar.getmembers()):
                try:
                    sha1_of_previous = path_sha1(os.path.join(self.repo,
                                                              tarinfo.name))
                except OSError:
                    sha1_of_previous = b''
                extracted[tarinfo.name] = sha1_of_previous
            tar.extractall(self.repo,
                           members=etc_files_filter(tar.getmembers()))
            for fname in extracted:
                if fname not in tracked:
                    # Ensure that the file can be overwritten on a next
                    # 'update' command.
                    path = os.path.join(self.repo, fname)
                    mode = os.stat(path).st_mode
                    if mode & RW_ACCESS != RW_ACCESS:
                        os.chmod(path, mode | RW_ACCESS)
        return extracted

    def scan_cachedir(self):
        """Scan pacman cachedir for newly installed or upgraded packages.

        Algorithm:
        ----------
        Build the list of package files newer than the timestamp.
        For each etc file in a package file:
          if the packaged file does not exist in etc-tmp    # pkg install
            add it to the etc-tmp branch
            if corresponding file in /etc and packaged file are different
              add the /etc file to the master-tmp branch
          else                                              # pkg upgrade
            if packaged file == corresponding file in /etc
              if packaged file != file in the etc-tmp branch
                update it in the etc-tmp branch
                  assert the file does not exist in master-tmp
            else                            # pkg uprade with a pacnew file
              if packaged file != file in the etc-tmp branch
                update it in the etc-tmp branch in a specific commit
                    whose <sha1> will be used for the cherry-pick
                  assert the file exists in master-tmp
        """

        # Extract the etc files from each package into the etc-tmp branch.
        # Note that tracked_files() has the side-effect of checking out the
        # corresponding branch.
        master_tracked = self.tracked_files('master-tmp')
        etc_tracked = self.tracked_files('etc-tmp')
        extracted = self.extract(self.new_packages(), etc_tracked)

        # Implement the algorithm.
        etc_add = []
        new_master = []
        cherry_pick = []
        for fname in extracted:
            pkg_file = os.path.join(self.repo, fname)
            etc_file = os.path.join('/', fname)
            if not os.path.isfile(etc_file):
                warn('%s does not exist' % etc_file)
            sha1_pkg_file = path_sha1(pkg_file)
            try:
                sha1_etc_file = path_sha1(etc_file)
            except OSError:
                warn('skip %s: not readable' % etc_file)
                continue

            # A new package install.
            if fname not in etc_tracked:
                etc_add.append(fname)
                if sha1_etc_file != sha1_pkg_file:
                    new_master.append(fname)
            # A package upgrade.
            else:
                if sha1_etc_file == sha1_pkg_file:
                    if extracted[tarinfo] != sha1_pkg_file:
                        etc_add.append(fname)
                        if fname in master_tracked:
                            warn('%s exists in the master branch' % fname)
                else:
                    if extracted[tarinfo] != sha1_pkg_file:
                        cherry_pick.append(fname)
                        if fname not in master_tracked:
                            warn('%s does not exist in the master branch' %
                                 fname)

        if etc_add:
            # The following statement does not incur a command line length
            # overflow. For example on an archlinux box:
            #   find /etc | wc -c   ->    57722
            #   getconf ARG_MAX'    ->  2097152
            self.git_cmd('add %s' % ' '.join(etc_add))
            commit_msg = """\
            Update the etc-tmp branch with /etc files\n
            Update the etc-tmp branch with the files that are tracked and that
            do not differ from their /etc counterpart, and with the new
            extracted files.
            """
            self.git_cmd('commit -m "%s"' % dedent(commit_msg))

        sha1 = None
        if cherry_pick:
            self.git_cmd('add %s' % ' '.join(cherry_pick))
            self.git_cmd('commit -m "%s"' % 'Update with changed /etc files')
            sha1 = self.git_cmd('git log --pretty=tformat:%H', do_print=False)

        # Clean the working area.
        self.git_cmd('clean -d -x -f')

        # Update the master-tmp branch with new files.
        self.git_cmd('checkout master-tmp')
        for fname in new_master:
            repo_file = os.path.join(self.repo, fname)
            if os.path.isfile(repo_file):
                warn('adding %s to the master-tmp branch but this file'
                     ' already exists' % fname)
            dirname = os.path.dirname(repo_file)
            if dirname and not os.path.isdir(dirname):
                os.makedirs(dirname)
            etc_file = os.path.join('/etc', fname)
            shutil.copy(etc_file, dirname)
        if new_master:
            self.git_cmd('add %s' % ' '.join(new_master))
            self.git_cmd('commit -m "%s"' % 'Add new files from /etc')

        # Cherry pick the sha1 changes commited in the etc-tmp branch to the
        # master-tmp branch.
        for fname in cherry_pick:
            repo_file = os.path.join(self.repo, fname)
            if not os.path.isfile(repo_file):
                warn('cherry picking %s to the master-tmp branch but this'
                     ' file does not exist' % fname)
        if sha1 is not None:
            self.git_cmd('cherry-pick -x %s' % sha1)

        if self.dry_run:
            self.remove_tmp_branches()
            self.git_cmd('checkout master')
        elif sha1 is None:
            self.finalize()
        else:
            self.git_cmd('checkout master')

def dispatch_help(args):
    """Use 'help <command>' to get help on the command."""
    command = args.subcommand
    if command is None:
        command = 'help'
    args.parsers[command].print_help()

    cmd_func = getattr(EtcMerger, 'cmd_%s' % command)
    print('\n' + cmd_func.__doc__)

def parse_args(argv, namespace):
    def isdir(path):
        if not os.path.isdir(path):
            raise argparse.ArgumentTypeError('%s is not a directory' % path)
        return path

    # Instantiate the main parser.
    main_parser = argparse.ArgumentParser(prog=pgm,
                                          description=__doc__, add_help=False)
    main_parser.add_argument('--version', '-v', action='version',
                             version='%(prog)s ' + __version__)

    # The help subparser handles the help for each command.
    subparsers = main_parser.add_subparsers(title='These are the etcmerger'
                                                  ' commands')
    parsers = { 'help': main_parser }
    parser = subparsers.add_parser('help', add_help=False,
                                   help=dispatch_help.__doc__.splitlines()[0])
    parser.add_argument('subcommand', choices=parsers, nargs='?',
                        default=None)
    parser.set_defaults(func=dispatch_help, parsers=parsers)

    # Add the command subparsers.
    d = dict(inspect.getmembers(EtcMerger, inspect.isfunction))
    for command in sorted(d):
        if not command.startswith('cmd_'):
            continue
        cmd = command[4:]
        func = d[command]
        parser = subparsers.add_parser(cmd, help=func.__doc__.splitlines()[0],
                                       add_help=False)
        parser.set_defaults(func=func)
        if cmd in ('update', 'sync'):
            parser.add_argument('--dry-run', '-n', help='Perform a trial run'
                ' with no changes made (default: %(default)s)',
                action='store_true', default=False)
        if cmd in ('init', 'update'):
            parser.add_argument('--cachedir', help='Set pacman cache'
                ' directory (override the /etc/pacman.conf setting)',
                type=isdir)
            parser.add_argument('--exclude-pkgs', default=EXCLUDE_PKGS,
                type=lambda x: list(y.strip() for y in x.split(',')),
                help='A comma separated list of prefix of package names'
                     ' to be ignored (default: "%(default)s")',
                metavar='PFXS')
            parser.add_argument('--followlinks',
                help='Visit directories pointed to by symlinks in cachedir.'
                ' Be aware that using this option can lead to infinite'
                ' recursion if a link points to a parent director of itself'
                ' (default: "%(default)s")',
                action='store_true', default=False)
        if cmd in ('init', 'update', 'sync'):
            parser.add_argument('--exclude-files', default=EXCLUDE_FILES,
                type=lambda x: list(os.path.join('etc', y.strip()) for
                y in x.split(',')), metavar='FILES',
                help='A comma separated list of file names to be ignored'
                     ' (default: "%(default)s")')
        if cmd == 'diff':
            parser.add_argument('--exclude', default=EXCLUDE_ETC,
                type=lambda x: list(y.strip() for y in x.split(',')),
                metavar='PFXS',
                help='A comma separated list of prefixes of /etc file'
                ' names to be ignored (default: "%(default)s")')
        parsers[cmd] = parser

    main_parser.parse_args(argv[1:], namespace=namespace)
    if not hasattr(namespace, 'func'):
        main_parser.error('a command is required')

def main():
    if os.geteuid() == 0:
        abort('cannot be executed as a root user')

    # Assign the parsed args to the EtcMerger instance.
    merger = EtcMerger()
    parse_args(sys.argv, merger)

    # Run the command.
    if merger.func == dispatch_help:
        merger.func(merger)
    else:
        merger.run()

if __name__ == '__main__':
    main()
