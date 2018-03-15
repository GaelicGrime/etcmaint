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
from collections import namedtuple
from subprocess import check_output, STDOUT, CalledProcessError
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def list_files(path, suffixes=None, prefixes=None):
    """List of the relative paths of the regular files in path.

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
                rpath = os.path.normpath(os.path.join(root, fname))
                if os.path.isdir(rpath):
                    continue
                # Exclude files ending with one of the suffixes.
                if suffixes_len:
                    if (len(list(itertools.takewhile(lambda x: not x or
                            not rpath.endswith(x), suffixes))) !=
                                suffixes_len):
                        continue
                # Exclude files starting with one of the prefixes.
                if prefixes_len:
                    if (len(list(itertools.takewhile(lambda x: not x or
                            not rpath.startswith(x), prefixes))) !=
                                prefixes_len):
                        continue
                flist.append(rpath)
    return flist

def repository_dir():
    xdg_data_home = os.environ.get('XDG_DATA_HOME')
    if xdg_data_home is None:
        home = os.environ.get('HOME')
        if home is None:
            print('Error: HOME environment variable not set', file=sys.stderr)
            sys.exit(1)
        xdg_data_home = os.path.join(home, '.local/share')
    return os.path.join(xdg_data_home, 'etcmerger')

def run_cmd(cmd, dry_run=False):
    if dry_run:
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

@contextlib.contextmanager
def change_cwd(path):
    """Context manager that temporarily creates and changes the cwd."""
    saved_dir = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(saved_dir)

Sha1File = namedtuple('Sha1File', ['abspath', 'sha1'])

class GitRepo():
    """A git repository."""

    def __init__(self, repodir, dry_run):
        self.repodir = repodir
        self.dry_run = dry_run

    def init(self):
        """Create the git repository directory if it does not exist."""
        if os.path.isdir(self.repodir) and os.listdir(self.repodir):
            abort('%s is not empty' % self.repodir)
        if not os.path.isdir(self.repodir):
            os.makedirs(self.repodir)
        self.git_cmd('init')

    def assert_exists(self):
        self.git_cmd('status', do_print=False)

    def git_cmd(self, cmd, do_print=True):
        git_dir = os.path.join(self.repodir, '.git')
        cmd = 'git --git-dir=%s --work-tree=%s %s' % (git_dir, self.repodir,
                                                      cmd)
        output = run_cmd(cmd, self.dry_run)
        if do_print:
            output = output.strip('\n')
            if output:
                print(output)
        return output

    def add_file(self, fname, content, commit_msg):
        path = os.path.join(self.repodir, fname)
        with open(path, 'w') as f:
            f.write(dedent(content))
        self.git_cmd('add %s' % path)
        self.git_cmd('commit -m "%s"' % commit_msg)

    def tracked_files(self, branch, exclude=None, with_sha1=False):
        """A dictionary of the tracked files in this branch."""
        d = {}
        ls_tree = self.git_cmd('ls-tree -r --name-only --full-tree %s' %
                               branch, do_print=False)

        self.git_cmd('checkout %s' % branch, do_print=False)
        for fname in ls_tree.strip('\n').split('\n'):
            if exclude and fname in exclude:
                continue
            if with_sha1:
                path = os.path.join(self.repodir, fname)
                d[fname] = Sha1File(path, path_sha1(path))
            else:
                d[fname] = None
        return d

    @property
    def branches(self):
        branches = self.git_cmd('branch --list', do_print=False)
        return [x.strip(' *') for x in branches.split('\n')]

class Timestamp():
    def __init__(self, merger):
        self.merger = merger
        self.fname = '.etcmerger_timestamp'
        self.prefix = 'TIMESTAMP='
        self.path = os.path.join(merger.repodir, self.fname)

    def new(self):
        """Create the timestamp file."""
        content = """\
            # This file is created by etcmerger. Its purpose is to record the
            # time the master (resp. etc) branch has been fast-forwarded to
            # the master-tmp (resp. etc-tmp) branch.
            TIMESTAMP=0
        """
        self.merger.repo.add_file(self.fname, content, 'Add the timestamp')

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
        self.merger.repo.add_file(self.fname, content, 'Update the timestamp')

    @property
    def value(self):
        with open(self.path, 'r') as f:
            for line in f:
                if line.startswith(self.prefix):
                    return int(line[line.index('=')+1:])
        self.abort_corrupted()

class UpdateResults():
    def __init__(self):
        self.etc_remove = []
        self.new_master_user = []

        self.etc_add = []
        self.new_master = []
        self.cherry_pick = []

    def __str__(self):
        txt = ''
        if self.cherry_pick:
            txt += 'List of the files to sync to /etc:'
            txt += '\n'.join(self.cherry_pick)
        else:
            txt += 'No files to sync to /etc'
        return txt

class EtcMerger():
    """Provide methods to implement the commands."""

    def __init__(self):
        self.repodir = repository_dir()
        self.timestamp = Timestamp(self)
        self.results = UpdateResults()

    def init(self):
        if not hasattr(self, 'dry_run'):
            self.dry_run = False
        if hasattr(self, 'cachedir') and self.cachedir is None:
            cfg = configparser.ConfigParser(allow_no_value=True)
            with open('/etc/pacman.conf') as f:
                cfg.read_file(f)
            self.cachedir = cfg['options']['CacheDir']
        self.repo = GitRepo(self.repodir, self.dry_run)

    def run(self):
        """Run the etcmerger command."""
        self.init()
        self.func(self)

    def cmd_init(self):
        """Initialize the git repository."""
        self.repo.init()

        # Add .gitignore.
        gitignore = """\
            *.swp
        """
        self.repo.add_file('.gitignore', gitignore, 'Creation')

        # Create the etc branch.
        self.repo.git_cmd('checkout -b etc')

        # Create the timestamp file in the etc branch.
        self.timestamp.new()

        self.cmd_update()
        print('Init command terminated')

    def cmd_update(self):
        """Update the repository."""
        self.repo.assert_exists()
        self.create_tmp_branches()

        self.git_upgraded_pkgs()
        self.git_removed_pkgs()
        self.git_user_updates()

        if self.dry_run:
            self.remove_tmp_branches()
        elif not self.results.cherry_pick:
            self.finalize()

        print(self.results)
        self.repo.git_cmd('checkout master')
        print('The repository has been updated')

    def cmd_diff(self):
        """Print the /etc regular file names that are not in the etc branch.

        Exclude pacnew, pacsave and pacorig files.
        """

        self.repo.assert_exists()
        if self.use_etc_tmp:
            if 'etc-tmp' in self.repo.branches:
                self.repo.git_cmd('checkout etc-tmp', do_print=False)
            else:
                print('The etc-tmp branch does not exist')
                return
        else:
            self.repo.git_cmd('checkout etc', do_print=False)

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = list_files('/etc', suffixes=suffixes,
                                    prefixes=self.exclude)
        repo_files = list_files(os.path.join(self.repodir, 'etc'))
        print('\n'.join(sorted(set(etc_files).difference(repo_files))))
        self.repo.git_cmd('checkout master', do_print=False)

    def cmd_sync(self):
        """Synchronize /etc with the master branch."""
        self.repo.assert_exists()
        self.finalize()
        self.repo.git_cmd('checkout master')
        print('Sync command terminated')

    def create_tmp_branches(self):
        print('Create the master-tmp and etc-tmp branches')
        for branch in ('etc', 'master'):
            tmp_branch = '%s-tmp' % branch
            if tmp_branch in self.repo.branches:
                self.repo.git_cmd('branch --delete --force %s' % tmp_branch)
            self.repo.git_cmd('checkout %s' % branch, do_print=False)
            self.repo.git_cmd('branch %s' % tmp_branch)

    def remove_tmp_branches(self):
        if not 'master-tmp' in self.repo.branches:
            return False

        print('Remove the master-tmp and etc-tmp branches')
        # Do a fast-forward merge.
        for branch in ('master', 'etc'):
            tmp_branch = '%s-tmp' % branch
            self.repo.git_cmd('checkout %s' % branch, do_print=False)
            self.repo.git_cmd('merge %s' % tmp_branch)
            self.repo.git_cmd('branch --delete %s' % tmp_branch)
        return True

    def finalize(self):
        if self.remove_tmp_branches():
            print('Update the timestamp')
            self.timestamp.now()

    def git_removed_pkgs(self):
        """Remove files that do not exist in /etc."""

        res = self.results
        exclude=['.gitignore', self.timestamp.fname]
        etc_tracked = self.repo.tracked_files('etc-tmp', exclude=exclude)
        for fname in etc_tracked:
            etc_file = os.path.join('/', fname)
            if not os.path.isfile(etc_file):
                res.etc_remove.append(fname)

        # Remove the etc-tmp files that do not exist in /etc.
        # The last tracked_files() has set the current branch to etc-tmp.
        if res.etc_remove:
            self.repo.git_cmd('rm %s' % ' '.join(res.etc_remove))
            self.repo.git_cmd('commit -m "Update etc-tmp with removed'
                                          ' /etc files"')

        # Remove the master-tmp files that have been removed in the etc-tmp
        # branch.
        master_remove = []
        master_tracked = self.repo.tracked_files('master-tmp')
        for fname in res.etc_remove:
            if fname in master_tracked:
                master_remove.append(fname)
        if master_remove:
            self.repo.git_cmd('checkout master-tmp')
            self.repo.git_cmd('rm %s' % ' '.join(master_remove))
            self.repo.git_cmd('commit -m "Update master-tmp with removed'
                                          ' /etc files"')

    def git_user_updates(self):
        """Update master-tmp with the user changes."""

        def etc_sha1file(fname):
            etc_file = os.path.join('/etc', fname)
            try:
                sha1 = path_sha1(etc_file)
            except OSError:
                sha1 = None
            return Sha1File(etc_file, sha1)

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = {n: etc_sha1file(n) for
                         n in list_files('/etc', suffixes=suffixes)}
        etc_tracked = self.repo.tracked_files('etc-tmp', with_sha1=True)

        # Build the list of etc-tmp files that are different from their
        # counterpart in /etc.
        # The last tracked_files() has set the current branch to etc-tmp.
        to_check_in_master = []
        for fname in etc_files:
            name = etc_files[fname].abspath[1:]
            if name in etc_tracked:
                if etc_files[fname].sha1 != etc_tracked[name].sha1:
                    to_check_in_master.append(name)

        # tracked_files() sets the current branch to master-tmp.
        master_tracked = self.repo.tracked_files('master-tmp', with_sha1=True)

        # Build the list of master-tmp files:
        #   * To add when the file does not exist in master-tmp and its
        #     counterpart in etc-tmp is different from the /etc file.
        #   * To update when the file exists in master-tmp and is different
        #     from the /etc file.
        res = self.results
        for fname in to_check_in_master:
            if fname not in master_tracked:
                res.new_master_user.append(fname)
        for fname in etc_files:
            name = etc_files[fname].abspath[1:]
            if name in master_tracked:
                if etc_files[fname].sha1 != master_tracked[name].sha1:
                    res.new_master_user.append(name)

        if res.new_master_user:
            self.repo.git_cmd('add %s' % ' '.join(res.new_master_user))
            self.repo.git_cmd('commit -m "%s"' % 'Update with user changes')

    def git_upgraded_pkgs(self):
        """Update the repository with installed or upgraded packages."""

        self.scan_cachedir()
        res = self.results
        if res.etc_add:
            # The following statement does not incur a command line length
            # overflow. For example on an archlinux box:
            #   find /etc | wc -c   ->    57722
            #   getconf ARG_MAX'    ->  2097152
            self.repo.git_cmd('add %s' % ' '.join(res.etc_add))
            commit_msg = """\
            Update the etc-tmp branch with /etc files\n
            Update the etc-tmp branch with the files that are tracked and that
            do not differ from their /etc counterpart, and with the new
            extracted files.
            """
            self.repo.git_cmd('commit -m "%s"' % dedent(commit_msg))

        if res.cherry_pick:
            self.repo.git_cmd('add %s' % ' '.join(res.cherry_pick))
            self.repo.git_cmd('commit -m "%s"' %
                              'Update with changed /etc files')
            sha1 = self.repo.git_cmd('git log --pretty=tformat:%H',
                                     do_print=False)

        # Clean the working area.
        self.repo.git_cmd('clean -d -x -f')

        # Update the master-tmp branch with new files.
        self.repo.git_cmd('checkout master-tmp')
        for fname in res.new_master:
            repo_file = os.path.join(self.repodir, fname)
            if os.path.isfile(repo_file):
                warn('adding %s to the master-tmp branch but this file'
                     ' already exists' % fname)
            dirname = os.path.dirname(repo_file)
            if dirname and not os.path.isdir(dirname):
                os.makedirs(dirname)
            etc_file = os.path.join('/', fname)
            shutil.copy(etc_file, dirname)
        if res.new_master:
            self.repo.git_cmd('add %s' % ' '.join(res.new_master))
            self.repo.git_cmd('commit -m "%s"' % 'Add new files from /etc')

        # Cherry pick the sha1 changes commited in the etc-tmp branch to the
        # master-tmp branch.
        for fname in res.cherry_pick:
            repo_file = os.path.join(self.repodir, fname)
            if not os.path.isfile(repo_file):
                warn('cherry picking %s to the master-tmp branch but this'
                     ' file does not exist' % fname)
        if res.cherry_pick:
            self.repo.git_cmd('cherry-pick -x %s' % sha1)

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

        def extract_pkg(pkg):
            tar = tarfile.open(pkg.path, mode='r:xz', debug=1)
            for tarinfo in etc_files_filter(tar.getmembers()):
                # Remember the sha1 of the existing file, if it exists.
                try:
                    sha1_of_previous = path_sha1(os.path.join(self.repodir,
                                                              tarinfo.name))
                except OSError:
                    sha1_of_previous = b''
                extracted[tarinfo.name] = sha1_of_previous
            tar.extractall(self.repodir,
                           members=etc_files_filter(tar.getmembers()))

        extracted = {}
        # Setting max_workers to 5 seems to be faster than the default, i.e.
        # the number of processors on the machine, multiplied by 5.
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(extract_pkg, pkg):
                            pkg for pkg in packages}
            for future in as_completed(futures):
                pkg = futures[future]
                print('extracting', pkg.name)

        for fname in extracted:
            if fname not in tracked:
                # Ensure that the file can be overwritten on a next
                # 'update' command.
                path = os.path.join(self.repodir, fname)
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
        master_tracked = self.repo.tracked_files('master-tmp')
        etc_tracked = self.repo.tracked_files('etc-tmp')
        extracted = self.extract(self.new_packages(), etc_tracked)

        res = self.results
        for fname in extracted:
            pkg_file = os.path.join(self.repodir, fname)
            etc_file = os.path.join('/', fname)
            if not os.path.isfile(etc_file):
                warn('%s does not exist' % etc_file)
                continue
            sha1_pkg_file = path_sha1(pkg_file)
            try:
                sha1_etc_file = path_sha1(etc_file)
            except OSError:
                warn('skip %s: not readable' % etc_file)
                continue

            # A new package install.
            if fname not in etc_tracked:
                res.etc_add.append(fname)
                if sha1_etc_file != sha1_pkg_file:
                    res.new_master.append(fname)
            # A package upgrade.
            else:
                if sha1_etc_file == sha1_pkg_file:
                    if extracted[tarinfo] != sha1_pkg_file:
                        res.etc_add.append(fname)
                        if fname in master_tracked:
                            warn('%s exists in the master branch' % fname)
                else:
                    if extracted[tarinfo] != sha1_pkg_file:
                        res.cherry_pick.append(fname)
                        if fname not in master_tracked:
                            warn('%s does not exist in the master branch' %
                                 fname)

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
            parser.add_argument('--use-etc-tmp',
                help='Use the etc-tmp branch instead (default: %(default)s)',
                action='store_true', default=False)
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
