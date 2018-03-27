#! /bin/env python
"""An Arch Linux tool for the maintenance of /etc files.

The /etc files installed or upgraded by Arch Linux packages are managed in
the 'etc' branch of a git repository. The /etc files customized or created by
a user are managed in the 'master' branch. The git repository is located at
$XDG_DATA_HOME/etcmaint if the XDG_DATA_HOME environment variable is set and
at $HOME/.local/share/etcmaint otherwise.

The upgraded packages changes of user-customized files are merged (actually
cherry-picked) by etcmaint. Merge conflicts must be resolved by the user.
After the merge, the 'sync' subcommand is used to retrofit those changes to
the files on /etc.
"""

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
import subprocess
from textwrap import dedent
from collections import namedtuple
from subprocess import PIPE, STDOUT, CalledProcessError
from concurrent.futures import ThreadPoolExecutor, as_completed

__version__ = '0.1'
pgm = os.path.basename(sys.argv[0])
RW_ACCESS = stat.S_IWUSR | stat.S_IRUSR
FIRST_COMMIT_MSG = 'First etcmaint commit'
EXCLUDE_FILES = 'passwd, group, udev/hwdb.bin'
EXCLUDE_PKGS = ''
EXCLUDE_ETC = 'ca-certificates, fonts, ssl/certs'

def abort(msg):
    print('*** %s: error:' % pgm, msg, file=sys.stderr)
    sys.exit(1)

def warn(msg):
    print('*** warning:', msg, file=sys.stderr)

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
    return os.path.join(xdg_data_home, 'etcmaint')

def copy_from_etc(rpath, repodir, repo_file=None):
    """Copy a file on /etc to the repository.

    'rpath' is the relative path to the repository directory.
    """

    if repo_file is None:
        repo_file = os.path.join(repodir, rpath)
    dirname = os.path.dirname(repo_file)
    if dirname and not os.path.isdir(dirname):
        os.makedirs(dirname)
    etc_file = os.path.join('/', rpath)
    if os.path.islink(repo_file):
        os.remove(repo_file)
    shutil.copy(etc_file, dirname, follow_symlinks=False)

def str_file_list(header, files):
    if files:
        lines = [header]
        lines.extend('  %s' % f for f in files)
        return '\n'.join(lines)

@contextlib.contextmanager
def change_cwd(path):
    """Context manager that temporarily creates and changes the cwd."""
    saved_dir = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(saved_dir)

FileDigest = namedtuple('FileDigest', ['abspath', 'digest'])

class GitRepo():
    """A git repository."""

    def __init__(self, repodir, verbose):
        self.repodir = repodir.rstrip(os.sep)
        self.verbose = verbose
        self.curbranch = None
        self.initial_branch = None
        self.git = ('git -C %s' % repodir).split()

    def create(self):
        """Create the git repository."""
        if os.path.isdir(self.repodir) and os.listdir(self.repodir):
            abort('%s is not empty' % self.repodir)
        if not os.path.isdir(self.repodir):
            os.makedirs(self.repodir)
        self.git_cmd('init')

    def init(self):
        # Check the first commit message.
        commit = self.git_cmd('rev-list --max-parents=0 --format=%s master --')
        first_commit_msg = commit.splitlines()[1]
        if first_commit_msg != FIRST_COMMIT_MSG:
            err_msg = f"""\
                this is not an etcmaint repository
                found as the first commit message:
                '{first_commit_msg}'
                instead of the expected '{FIRST_COMMIT_MSG}' message"""
            abort(dedent(err_msg))

        status = self.get_status()
        if status:
            abort('the %s repository is not clean:\n%s' %
                  (self.repodir, '\n'.join(status)))

        if os.path.isfile(os.path.join(
                          self.repodir, '.git', 'CHERRY_PICK_HEAD')):
            abort("The previous cherry-pick is empty, please use 'git reset'")

        # Get the initial branch.
        proc = subprocess.run(self.git + ['symbolic-ref', '--short', 'HEAD'],
                       universal_newlines=True, stdout=PIPE, stderr=STDOUT)
        if proc.returncode == 0:
            self.initial_branch = proc.stdout.splitlines()[0]

        self.checkout('master')

    def close(self):
        if self.initial_branch is not None:
            self.checkout(self.initial_branch)
        else:
            self.checkout('master')

    def git_cmd(self, cmd):
        if type(cmd) == str:
            cmd = cmd.split()
        try:
            output = subprocess.check_output(self.git + cmd,
                                    universal_newlines=True, stderr=STDOUT)
        except CalledProcessError as e:
            output = str(e) + '\n' + e.output.strip('\n')
            abort(output)
        output = output.strip('\n')
        if self.verbose and output:
                print(output)
        return output

    def get_status(self):
        output = self.git_cmd('status --porcelain')
        return output.splitlines()

    def checkout(self, branch, create=False):
        if create:
            self.git_cmd('checkout -b %s' % branch)
        else:
            if branch == self.curbranch:
                return
            self.git_cmd('checkout %s' % branch)
        self.curbranch = branch

    def commit(self, files, msg):
        """Commit changes to a list of files."""

        # Command line length overflow is not expected.
        # For example on an archlinux box:
        #   find /etc | wc -c   ->    57722
        #   getconf ARG_MAX'    ->  2097152
        self.git_cmd('add %s' % ' '.join(files))
        self.git_cmd(['commit', '-m', msg])

    def remove(self, files, msg):
        self.git_cmd('rm %s' % ' '.join(files))
        self.git_cmd(['commit', '-m', msg])

    def add_file(self, fname, content, commit_msg):
        path = os.path.join(self.repodir, fname)
        with open(path, 'w') as f:
            f.write(dedent(content))
        self.commit([path], commit_msg)

    def cherry_pick(self, commit):
        return subprocess.run(self.git + ['cherry-pick', '-x', commit],
                       universal_newlines=True, stdout=PIPE, stderr=STDOUT)

    def tracked_files(self, branch, exclude=None, with_digest=False):
        """A dictionary of the tracked files in this branch."""
        d = {}
        ls_tree = self.git_cmd('ls-tree -r --name-only --full-tree %s' %
                               branch)
        if with_digest:
            self.checkout(branch)
        for fname in ls_tree.splitlines():
            if exclude and fname in exclude:
                continue
            if with_digest:
                path = os.path.join(self.repodir, fname)
                d[fname] = FileDigest(path, self.digest(path))
            else:
                d[fname] = None
        return d

    @property
    def branches(self):
        branches = self.git_cmd("for-each-ref --format=%(refname:short)")
        return branches.splitlines()

    def digest(self, path):
        if os.path.islink(path):
            # The digest is the canonical path of the /etc file linked to the
            # symlink.
            path = os.path.realpath(path)
            if path.startswith(self.repodir):
                path = path[len(self.repodir):]
            return path

        try:
            h = hashlib.sha1()
            with open(path, 'rb') as f:
                h.update(f.read())
            return h.digest()
        except OSError:
            return None

class Timestamp():
    def __init__(self, merger):
        self.merger = merger
        self.fname = '.etcmaint_timestamp'
        self.prefix = 'TIMESTAMP='
        self.path = os.path.join(merger.repodir, self.fname)

    def new(self):
        """Create the timestamp file."""
        content = """\
            # This file is created by etcmaint. Its purpose is to record the
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
        self.etc_removed = []
        self.user_added = []
        self.user_updated = []
        self.pkg_add_etc = []
        self.pkg_add_master = []
        self.cherry_pick = []
        self.result = ''
        self.btype = ''

    def add_list(self, files, header):
        lines = str_file_list(header, files)
        if lines:
            if self.result:
                self.result += '\n'
            self.result += lines

    def __str__(self):
        btype = self.btype
        self.add_list(self.etc_removed,
         "List of files missing in /etc and removed from both branches:")
        self.add_list(self.user_added,
         f"List of files added to the 'master{btype}' branch:")
        self.add_list(self.user_updated,
         f"List of files updated in the 'master{btype}' branch:")
        self.add_list(self.pkg_add_etc,
         f"List of files extracted from a package and added to the"
         " 'etc{btype}' branch:")
        self.add_list(self.pkg_add_master,
         f"List of files extracted from a package and added to the"
         " 'master{btype}' branch:")
        self.add_list(self.cherry_pick,
         'List of files to sync to /etc:')
        return self.result

class EtcMerger():
    """Provide methods to implement the commands."""

    def __init__(self):
        self.repodir = repository_dir()
        self.timestamp = Timestamp(self)
        self.results = UpdateResults()

    def init(self):
        if not hasattr(self, 'verbose'):
            self.verbose = False
        self.repo = GitRepo(self.repodir, self.verbose)

        if not hasattr(self, 'dry_run'):
            self.dry_run = False
        if hasattr(self, 'cachedir') and self.cachedir is None:
            cfg = configparser.ConfigParser(allow_no_value=True)
            with open('/etc/pacman.conf') as f:
                cfg.read_file(f)
            self.cachedir = cfg['options']['CacheDir']

    def run(self):
        """Run the etcmaint command."""
        self.init()
        self.func(self)

    def cmd_create(self):
        """Create the git repository."""
        self.repo.create()

        # Add .gitignore.
        gitignore = """\
            *.swp
        """
        self.repo.add_file('.gitignore', gitignore, FIRST_COMMIT_MSG)

        # Create the etc branch and the timestamp.
        self.repo.checkout('etc', create=True)
        self.timestamp.new()

        self.repo.checkout('master')
        self.update_repository()
        self.repo.close()
        print('Git repository created at %s' % self.repodir)

    def cmd_update(self):
        """Update the repository with packages and user changes.

        The changes are done in temporary branches named 'master-tmp' and
        'etc-tmp'. When the changes do not incur a merge, the 'master' (resp.
        'etc') branch is fast-forwarded to its temporary branch and the
        temporary branch deleted. Otherwise this fast-forwarding is postponed
        until the merge is synced to /etc with the 'sync' subcommand. Until
        then it is still possible to start over with a new 'update'
        subcommand, the previous temporary branches being discarded in that
        case.
        """
        if self.update_repository():
            if self.results:
                print(self.results)
            self.repo.close()
            print("'update' command terminated: no file to sync to /etc")

    def cmd_diff(self):
        """Print the list of /etc file names not in the 'etc' branch.

        These are the /etc files not created from an Arch Linux package. Among
        them and of interest are the files created by a user that one may want
        to manually add and commit to the 'master' branch of the etcmaint
        repository so that their changes start being tracked by etcmaint (for
        example the netctl configuration files).

        pacnew, pacsave and pacorig files are excluded from this list.
        """

        self.repo.init()
        if self.use_etc_tmp:
            if 'etc-tmp' in self.repo.branches:
                self.repo.checkout('etc-tmp')
            else:
                print('The etc-tmp branch does not exist')
                return
        else:
            self.repo.checkout('etc')

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = list_files('/etc', suffixes=suffixes,
                                    prefixes=self.exclude)
        repo_files = list_files(os.path.join(self.repodir, 'etc'))
        print('\n'.join(sorted(set(etc_files).difference(repo_files))))
        self.repo.close()

    def cmd_sync(self):
        """Synchronize /etc with the files in the 'master' branch.

        Use rsync to retrofit the last changes after a merge.
        """
        self.repo.init()
        self.finalize()
        self.repo.close()
        print("'sync' command terminated")

    def create_tmp_branches(self):
        print('Creating the master-tmp and etc-tmp branches')
        branches = self.repo.branches
        for branch in ('etc', 'master'):
            tmp_branch = '%s-tmp' % branch
            if tmp_branch in branches:
                self.repo.checkout('master')
                self.repo.git_cmd('branch --delete --force %s' % tmp_branch)
                print("Removing the previous unused '%s' branch" % tmp_branch)
            self.repo.checkout(branch)
            self.repo.checkout(tmp_branch, create=True)

    def remove_tmp_branches(self):
        if not 'master-tmp' in self.repo.branches:
            return False

        print('Removing the master-tmp and etc-tmp branches')
        # Do a fast-forward merge.
        for branch in ('master', 'etc'):
            tmp_branch = '%s-tmp' % branch
            self.repo.checkout(branch)
            self.repo.git_cmd('merge %s' % tmp_branch)
            self.repo.git_cmd('branch --delete %s' % tmp_branch)
        return True

    def finalize(self):
        if self.remove_tmp_branches():
            print('Updating the timestamp')
            self.repo.checkout('etc')
            self.timestamp.now()

    def update_repository(self):
        self.repo.init()
        self.create_tmp_branches()

        cherry_pick_commit = self.git_upgraded_pkgs()
        self.git_removed_pkgs()
        self.git_user_updates()

        if cherry_pick_commit:
            self.git_cherry_pick(cherry_pick_commit)
            return False
        else:
            if self.dry_run:
                self.remove_tmp_branches()
            else:
                self.finalize()
        return True

    def git_cherry_pick(self, commit):
        self.repo.checkout('master-tmp')
        for fname in self.results.cherry_pick:
            repo_file = os.path.join(self.repodir, fname)
            if not os.path.isfile(repo_file):
                warn('cherry picking %s to the master-tmp branch but this'
                     ' file does not exist' % fname)

        self.results.btype = '-tmp'
        print(self.results)

        # Use a temporary branch for the cherry-pick.
        try:
            self.repo.checkout('cherry-pick', create=True)
            proc = self.repo.cherry_pick(commit)
            if proc.returncode == 0:
                if not self.dry_run:
                    # Do a fast-forward merge.
                    self.repo.checkout('master-tmp')
                    self.repo.git_cmd('merge cherry-pick')
                return
            else:
                conflicts = [x[3:] for x in self.repo.get_status()
                             if 'U' in x[:2]]
                try:
                    if conflicts:
                        self.repo.git_cmd('cherry-pick --abort')
                    else:
                        self.repo.git_cmd('reset --hard HEAD')
                        err = proc.stdout
                        err += 'etcmaint internal error: no conflicts found'
                        abort(err)
                finally:
                    self.repo.checkout('master-tmp')
        finally:
            self.repo.git_cmd('branch --delete cherry-pick')

        print(str_file_list('List of files with a conflict to resolve:',
                            conflicts))

        # Do the effective cherry-pick now after having printed the list of
        # files with a conflict to resolve.
        if not self.dry_run:
            proc = self.repo.cherry_pick(commit)
            cwd = os.getcwd()
            print('Please resolve the conflict%s%s' %
                  ('s' if len(conflicts) > 1 else '',
                   '' if cwd.startswith(self.repodir) else ' in %s' %
                                                            self.repodir))
            print('This is the result of the cherry-pick command:')
            print('\n'.join('  %s' % l for l in
                            proc.stdout.splitlines()))
            msg = """\
            You may use 'git -C %s cherry-pick --abort'
            and start over later with another 'etcmaint update' command"""
            print(dedent(msg % self.repodir))

    def git_removed_pkgs(self):
        """Remove files that do not exist in /etc."""

        res = self.results
        exclude=['.gitignore', self.timestamp.fname]
        etc_tracked = self.repo.tracked_files('etc-tmp', exclude=exclude)
        for fname in etc_tracked:
            etc_file = os.path.join('/', fname)
            if not os.path.isfile(etc_file):
                res.etc_removed.append(fname)

        # Remove the etc-tmp files that do not exist in /etc.
        commit_msg = 'Remove files missing in /etc'
        if res.etc_removed:
            self.repo.checkout('etc-tmp')
            self.repo.remove(res.etc_removed, commit_msg)

        # Remove the master-tmp files that have been removed in the etc-tmp
        # branch.
        master_remove = []
        master_tracked = self.repo.tracked_files('master-tmp')
        for fname in res.etc_removed:
            if fname in master_tracked:
                master_remove.append(fname)
        if master_remove:
            self.repo.checkout('master-tmp')
            self.repo.remove(master_remove, commit_msg)

    def git_user_updates(self):
        """Update master-tmp with the user changes."""

        def etc_filedisgest(fname):
            etc_file = os.path.join('/etc', fname)
            return FileDigest(etc_file, self.repo.digest(etc_file))

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = {n: etc_filedisgest(n) for
                         n in list_files('/etc', suffixes=suffixes)}
        etc_tracked = self.repo.tracked_files('etc-tmp', with_digest=True)

        # Build the list of etc-tmp files that are different from their
        # counterpart in /etc.
        to_check_in_master = []
        for fname in etc_files:
            name = etc_files[fname].abspath[1:]
            if name in etc_tracked:
                if etc_files[fname].digest != etc_tracked[name].digest:
                    to_check_in_master.append(name)

        master_tracked = self.repo.tracked_files('master-tmp',
                                                 with_digest=True)

        # Build the list of master-tmp files:
        #   * To add when the file does not exist in master-tmp and its
        #     counterpart in etc-tmp is different from the /etc file.
        #   * To update when the file exists in master-tmp and is different
        #     from the /etc file.
        res = self.results
        for fname in to_check_in_master:
            if fname not in master_tracked:
                res.user_added.append(fname)
        for fname in etc_files:
            name = etc_files[fname].abspath[1:]
            if name in master_tracked and name not in res.pkg_add_master:
                if etc_files[fname].digest != master_tracked[name].digest:
                    res.user_updated.append(name)

        if res.user_added or res.user_updated:
            self.repo.checkout('master-tmp')
        for files, commit_msg in (
                (res.user_added, 'Add files with user changes'),
                (res.user_updated, 'Update files with user changes')):
            for name in files:
                copy_from_etc(name, self.repodir)
            if files:
                self.repo.commit(files, commit_msg)

    def git_upgraded_pkgs(self):
        """Update the repository with installed or upgraded packages."""

        self.scan_cachedir()
        res = self.results
        if res.pkg_add_etc:
            self.repo.commit(res.pkg_add_etc,
                             'Add or upgrade files extracted from a package')

        cherry_pick_commit = None
        if res.cherry_pick:
            self.repo.commit(res.cherry_pick,
                     'Update with upgraded package files not copied to /etc')
            cherry_pick_commit = self.repo.git_cmd('rev-list -1 HEAD --')

        # Clean the working area.
        self.repo.git_cmd('clean -d -x -f')

        # Update the master-tmp branch with new files.
        if res.pkg_add_master:
            self.repo.checkout('master-tmp')
            for fname in res.pkg_add_master:
                repo_file = os.path.join(self.repodir, fname)
                if os.path.isfile(repo_file):
                    warn('adding %s to the master-tmp branch but this file'
                         ' already exists' % fname)
                copy_from_etc(fname, self.repodir, repo_file=repo_file)
            self.repo.commit(res.pkg_add_master,
                         'Add files after scanning new or upgraded packages')

        return cherry_pick_commit

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

    def scan(self, packages, tracked):
        def etc_files_filter(members):
            for tarinfo in members:
                fname = tarinfo.name
                if (tarinfo.isfile() and fname.startswith('etc') and
                        fname not in self.exclude_files):
                    yield tarinfo

        def extract_from(pkg):
            tar = tarfile.open(pkg.path, mode='r:xz', debug=1)
            for tarinfo in etc_files_filter(tar.getmembers()):
                # Remember the sha1 of the existing file, if it exists.
                dgst_of_previous = self.repo.digest(os.path.join(self.repodir,
                                                    tarinfo.name))
                extracted[tarinfo.name] = dgst_of_previous
            tar.extractall(self.repodir,
                           members=etc_files_filter(tar.getmembers()))

        extracted = {}
        max_workers = len(os.sched_getaffinity(0)) or 4
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(extract_from, pkg):
                            pkg for pkg in packages}
            for future in as_completed(futures):
                pkg = futures[future]
                print('scanned', pkg.name)

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
        master_tracked = self.repo.tracked_files('master-tmp')
        etc_tracked = self.repo.tracked_files('etc-tmp')
        self.repo.checkout('etc-tmp')
        extracted = self.scan(self.new_packages(), etc_tracked)

        res = self.results
        for fname in extracted:
            dgst_fname = self.repo.digest(os.path.join(self.repodir, fname))
            etc_file = os.path.join('/', fname)
            dgst_etc_file = self.repo.digest(etc_file)
            if dgst_etc_file is None:
                warn('skip %s: not readable' % etc_file)
                continue

            # A new package install.
            if fname not in etc_tracked:
                res.pkg_add_etc.append(fname)
                if dgst_etc_file != dgst_fname:
                    res.pkg_add_master.append(fname)
            # A package upgrade.
            else:
                previous_dgst_fname = extracted[fname]
                if dgst_etc_file == dgst_fname:
                    if previous_dgst_fname != dgst_fname:
                        res.pkg_add_etc.append(fname)
                        if fname in master_tracked:
                            warn('%s exists in the master branch' % fname)
                else:
                    if previous_dgst_fname != dgst_fname:
                        res.cherry_pick.append(fname)
                        if fname not in master_tracked:
                            warn('%s does not exist in the master branch' %
                                 fname)

def dispatch_help(args):
    """Get help on a command."""
    command = args.subcommand
    if command is None:
        command = 'help'
    args.parsers[command].print_help()

    cmd_func = getattr(EtcMerger, 'cmd_%s' % command, None)
    if cmd_func:
        lines = cmd_func.__doc__.splitlines()
        print('\n' + lines[0])
        print(dedent('\n'.join(lines[1:])), end='')

def parse_args(argv, namespace):
    def isdir(path):
        if not os.path.isdir(path):
            raise argparse.ArgumentTypeError('%s is not a directory' % path)
        return path

    # Instantiate the main parser.
    main_parser = argparse.ArgumentParser(prog=pgm,
                    formatter_class=argparse.RawDescriptionHelpFormatter,
                    description=__doc__, add_help=False)
    main_parser.add_argument('--version', '-v', action='version',
                    version='%(prog)s ' + __version__)

    # The help subparser handles the help for each command.
    subparsers = main_parser.add_subparsers(title='etcmaint subcommands')
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
        if cmd in ('create', 'update'):
            parser.add_argument('--verbose', '-v', help='Print the output of'
                ' the git commands (default: %(default)s)',
                action='store_true', default=False)
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
                ' recursion if a link points to a parent directory of itself'
                ' (default: "%(default)s")',
                action='store_true', default=False)
        if cmd in ('create', 'update', 'sync'):
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
