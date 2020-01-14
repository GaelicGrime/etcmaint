#! /bin/env python
"""An Arch Linux tool based on git for the maintenance of /etc files."""

import sys
import os
import io
import stat
import argparse
import pathlib
import inspect
import configparser
import hashlib
import itertools
import shutil
import contextlib
import subprocess
from textwrap import dedent, wrap
from collections import namedtuple

__version__ = '0.7'
pgm = os.path.basename(sys.argv[0].rstrip(os.sep))
EXTENSIONS = ('xz', 'zst', 'zstd')
RW_ACCESS = stat.S_IWUSR | stat.S_IRUSR
FIRST_COMMIT_MSG = 'First etcmaint commit'
CHERRY_PICK_COMMIT_MSG = ('Files updated from new packages versions and'
                          ' customized by user')
GIT_USER_CONFIG = ['-c', 'user.email="etcmaint email"',
                   '-c', 'user.name=etcmaint']
EXCLUDE_FILES = 'passwd, group, mtab, udev/hwdb.bin'
EXCLUDE_PKGS = ''
EXCLUDE_PREFIXES = 'ca-certificates, ssl/certs'
ETCMAINT_BRANCHES = ['etc', 'etc-tmp', 'master', 'master-tmp', 'timestamps',
                     'timestamps-tmp']
EMPTY_CHERRY_PICK_MSG = """An empty commit.
This may happen when all the changes cherry-picked from the 'etc' branch
have already been included in the 'master' branch as user changes."""

# The subdirectory of '--root-dir'.
ROOT_SUBDIR = 'etc'

class EmtError(Exception): pass

def warn(msg):
    print('*** warning:', msg, file=sys.stderr)

def run_cmd(cmd, error='', ignore_failure=False):
    proc = subprocess.run(cmd, universal_newlines=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0 and not ignore_failure:
        err_list = []
        if error:
            err_list += [error]
        err_list += [proc.stdout.strip()]
        err_list += ['Command line:\n%s' % cmd]
        raise EmtError('\n'.join(err_list))
    return proc

def list_rpaths(rootdir, subdir, suffixes=None, prefixes=None):
    """List of the relative paths of the files in rootdir/subdir.

    Exclude file names that are a match for one of the suffixes in
    'suffixes' and file names that are a match for one of the prefixes in
    'prefixes'.
    """

    flist = []
    suffixes_len = len(suffixes) if suffixes is not None else 0
    prefixes_len = len(prefixes) if prefixes is not None else 0
    with change_cwd(os.path.join(rootdir, subdir)):
        for root, dirs, files in os.walk('.'):
            for fname in files:
                rpath = os.path.normpath(os.path.join(root, fname))
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
                flist.append(os.path.join(subdir, rpath))
    return flist

def repository_dir():
    xdg_data_home = os.environ.get('XDG_DATA_HOME')
    if xdg_data_home is not None:
        return os.path.join(xdg_data_home, 'etcmaint')

    try:
        logname = os.getlogin()
    except OSError:
        # Cannot fall back to getpass.getuser() or use the pwd database as
        # they do not provide the correct value when etcmaint is run with
        # sudo.
        raise EmtError('etcmaint requires a controlling terminal') from None
    return os.path.expanduser('~%s/.local/share/etcmaint' % logname)

def copy_file(rpath, rootdir, repodir, repo_file=None):
    """Copy a file on 'rootdir' to the repository.

    'rpath' is the relative path to 'rootdir'.
    """

    if repo_file is None:
        repo_file = os.path.join(repodir, rpath)
    dirname = os.path.dirname(repo_file)
    if dirname and not os.path.isdir(dirname):
        os.makedirs(dirname)
    etc_file = os.path.join(rootdir, rpath)
    # Remove destination if source is a symlink or if destination is a symlink
    # (in the last case, source would be copied to the file pointed by
    # destination instead of having the symlink itself being copied).
    if os.path.lexists(repo_file) and (os.path.islink(etc_file) or
                                       os.path.islink(repo_file)):
        os.remove(repo_file)
    shutil.copy2(etc_file, repo_file, follow_symlinks=False)

@contextlib.contextmanager
def change_cwd(path):
    """Context manager that temporarily changes the cwd."""
    saved_dir = os.getcwd()
    os.chdir(path)
    try:
        yield os.getcwd()
    finally:
        os.chdir(saved_dir)

@contextlib.contextmanager
def threadsafe_makedirs():
    def _makedirs(*args, **kwds):
        kwds['exist_ok'] = True
        saved_makedirs(*args[:2], **kwds)

    saved_makedirs = os.makedirs
    try:
        os.makedirs = _makedirs
        yield
    finally:
        os.makedirs = saved_makedirs

@contextlib.contextmanager
def tarfile_open(name, comptype, mode='r'):
    import tarfile

    if comptype not in ('zst', 'zstd'):
        with tarfile.open(name, '%s:%s' % (mode, comptype)) as tar:
            yield tar
    else:
        import zstandard as zstd

        if mode == 'r':
            compressor = zstd.ZstdDecompressor().stream_reader
        elif mode == 'w':
            compressor = zstd.ZstdCompressor().stream_writer
        else:
            raise ValueError('mode must be "r" or "w"')

        # Currently z-standard only supports stream-like file objects.
        # See https://github.com/indygreg/python-zstandard/issues/23.
        with open(name, '%sb' % mode) as f:
            with compressor(f) as fobj:
                with tarfile.open(mode="%s|" % mode, fileobj=fobj) as tar:
                    yield tar

class EtcPath():
    def __init__(self, basedir, rpath):
        assert rpath.startswith(ROOT_SUBDIR)
        self.path = pathlib.PosixPath(basedir, rpath)
        self._digest = None

    @property
    def digest(self):
        if self._digest is None:
            try:
                self.st_mode = self.path.lstat().st_mode
            except (FileNotFoundError, PermissionError):
                self.st_mode = None
                self._digest = b''
            else:
                try:
                    if stat.S_ISLNK(self.st_mode):
                        # The digest is the path to which the symbolic link
                        # points.
                        self._digest = os.readlink(self.path)
                    else:
                        h = hashlib.sha1()
                        with self.path.open('rb') as f:
                            h.update(f.read())
                        self._digest = h.digest()
                except OSError:
                    self._digest = b''
        return self._digest

    def __eq__(self, other):
        if (isinstance(other, EtcPath) and self.digest == other.digest and
                self.digest != b''):
            # Fix issue #15.
            # Upgraded readonly files are reported as updated by the user.
            return ((self.st_mode & stat.S_IXUSR) ==
                   (other.st_mode & stat.S_IXUSR))
        return False

class GitRepo():
    """A git repository."""

    def __init__(self, root_dir, repodir):
        self.root_dir = root_dir
        self.repodir = repodir.rstrip(os.sep)
        self.curbranch = None
        self.initial_branch = None
        self.initialized = False

        self.git = []
        self.root_not_repo_owner = False
        if os.geteuid() == 0:
            gitdir = os.path.join(repodir, '.git')
            try:
                statinfo = os.stat(gitdir)
            except FileNotFoundError:
                pass
            else:
                # When running as root and the git repository is not owned by
                # root, then run git commands as the owner of the repository.
                if statinfo.st_uid != 0:
                    if shutil.which('setpriv') is None:
                        raise EmtError('setpriv is missing from the '
                                       'ArchLinux util-linux package')

                    # Some tests mock os.geteuid() to return 0 and setpriv
                    # fails when attempting to set groups as a plain user.
                    set_priv = ['setpriv', '--reuid=%d' % statinfo.st_uid]
                    setgroups = ['--clear-groups', '--regid=%d' %
                                 statinfo.st_gid]
                    proc = run_cmd(['setpriv'] + setgroups + ['true'],
                                   ignore_failure=True)
                    if proc.returncode == 0:
                        set_priv += setgroups
                    self.root_not_repo_owner = True
                    self.git.extend(set_priv)

        self.git.extend(['git', '-C', repodir])

    def create(self):
        """Create the git repository."""
        if os.path.isdir(self.repodir):
            if os.listdir(self.repodir):
                raise EmtError('%s is not empty' % self.repodir)
        else:
            os.makedirs(self.repodir)
        self.git_cmd('init')
        self.initialized = True

    def init(self):
        # Check the first commit message.
        cmd = self.git + ['rev-list', '--max-parents=0', '--format=%s',
                          'master', '--']
        proc = run_cmd(cmd, error='no git repository at %s' % self.repodir)
        commit, first_commit_msg = proc.stdout.splitlines()
        if first_commit_msg != FIRST_COMMIT_MSG:
            err_msg = ("""\
                this is not an etcmaint repository
                First commit message found: '%s',
                instead of the expected commit message: '%s'""" %
                (first_commit_msg, FIRST_COMMIT_MSG))
            raise EmtError(dedent(err_msg))

        status = self.get_status()
        if status:
            tracked = untracked = False
            for line in status:
                if line[:2] == '??':
                    untracked = True
                else:
                    tracked = True
            msg = 'the %s repository is not clean:\n' % self.repodir
            msg += '\n'.join(status)
            msg += '\n'
            if tracked:
                msg += dedent("""
                Run 'git reset --hard' to discard any change in the working
                tree and in the index.""")
            if untracked:
                msg += dedent("""
                Run 'git clean -d -x -f' to clean the working tree by
                recursively removing files not under version control.""")
            raise EmtError(msg)

        # Get the initial branch.
        proc = run_cmd(self.git + ['symbolic-ref', '--short', 'HEAD'],
                       ignore_failure=True)
        if proc.returncode == 0:
            self.initial_branch = proc.stdout.splitlines()[0]
            self.curbranch = self.initial_branch
        self.initialized = True

    def close(self):
        if self.initialized:
            branch = 'master'
            if self.initial_branch in self.branches:
                branch = self.initial_branch
            if not self.get_status():
                self.checkout(branch)

    def git_cmd(self, cmd):
        if type(cmd) == str:
            cmd = cmd.split()
        proc = run_cmd(self.git + cmd)
        output = proc.stdout.rstrip()
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

    def commit(self, commit_msg):
        self.git_cmd(GIT_USER_CONFIG + ['commit', '-m', commit_msg])

    def add_files(self, files, commit_msg):
        """Add and commit a list of files.

        'files' is a dictionary mapping filename to the file content that must
        be written before the commit.
        """
        paths = []
        for rpath in files:
            path = os.path.join(self.repodir, rpath)
            paths.append(path)
            with open(path, 'w') as f:
                f.write(files[rpath])
        if paths:
            self.git_cmd(['add'] + paths)
            self.commit(commit_msg)

    def cherry_pick(self, sha):
        # --keep-redundant-commits:
        # If a commit being cherry picked duplicates a commit already in the
        # current history, it  will become empty. By default these redundant
        # commits cause cherry-pick to stop so the user can examine the
        # commit. This option overrides that behavior and creates an empty
        # commit object.
        return run_cmd(self.git + GIT_USER_CONFIG +
                    ['cherry-pick', '-x', '--keep-redundant-commits',
                     sha], ignore_failure=True)

    def tracked_files(self, branch):
        """A dictionary of the tracked files in this branch."""
        d = {}
        ls_tree = self.git_cmd('ls-tree -r --name-only --full-tree %s' %
                               branch)
        for rpath in ls_tree.splitlines():
            if rpath == '.gitignore':
                continue
            if branch.startswith('timestamps'):
                d[rpath] = pathlib.PosixPath(self.repodir, rpath)
            else:
                if not rpath.startswith(ROOT_SUBDIR):
                    continue
                d[rpath] = EtcPath(self.repodir, rpath)
        return d

    def check_fast_forward(self, branch):
        """Is a fast-forward merge allowed."""
        proc = run_cmd(self.git + ['rev-list', '%s-tmp..%s' %
                                   (branch, branch), '--'])
        if proc.stdout.strip():
            # Commits have been made on the main branch since the last update
            # command.
            raise EmtError('cannot fast-forward the %s branch, please '
            'run again the update command' % branch)

    @property
    def branches(self):
        branches = self.git_cmd("for-each-ref --format=%(refname:short)")
        return [b for b in branches.splitlines() if b in ETCMAINT_BRANCHES]

class Commit():
    """A commit to add/update or to remove a list of files."""

    def __init__(self, repo, branch, commit_msg, add=True):
        self.repo = repo
        self.branch = branch
        self.commit_msg = commit_msg
        self.add = add
        self.rpaths = []

    def commit(self):
        if not self.rpaths:
            return
        self.repo.checkout(self.branch)

        # Command line length overflow is not expected.
        # For example on an archlinux box:
        #   find /etc | wc -c   ->    57722
        #   getconf ARG_MAX'    ->  2097152
        cmd = 'add' if self.add else 'rm'
        self.repo.git_cmd([cmd] + self.rpaths)
        self.repo.commit(self.commit_msg)

class EtcMaint():
    """Provide methods to implement the commands."""

    def __init__(self, results):
        self.results = results

    def init(self):
        self.repodir = repository_dir()
        self.repo = GitRepo(self.root_dir, self.repodir)

        if not hasattr(self, 'dry_run'):
            self.dry_run = False
        self.mode = '[dry-run] ' if self.dry_run else ''

        if hasattr(self, 'cache_dir') and self.cache_dir is None:
            cfg = configparser.ConfigParser(allow_no_value=True)
            with open('/etc/pacman.conf') as f:
                cfg.read_file(f)
            self.cache_dir = cfg['options']['CacheDir']

        Etc_commits = namedtuple('Etc_commits',
                                 ['added', 'cherry_pick', 'removed'])
        self.etc_commits = Etc_commits(
            added=Commit(self.repo, 'etc-tmp',
                    'Files added or updated from new package versions'),
            cherry_pick=Commit(self.repo, 'etc-tmp', CHERRY_PICK_COMMIT_MSG),
            removed=Commit(self.repo, 'etc-tmp',
                    'Files removed that do not exist in /etc', add=False),
            )

        Master_commits = namedtuple('Master_commits',
                        ['added', 'removed', 'user_updated'])
        self.master_commits = Master_commits(
            added=Commit(self.repo, 'master-tmp',
                     'Files added from /etc after extracting new packages'),
            removed=Commit(self.repo, 'master-tmp',
                     'Files removed that do not exist in /etc', add=False),
            user_updated=Commit(self.repo, 'master-tmp',
                     'Files updated by the user in /etc'),
            )

    def run(self, command):
        """Run the etcmaint command."""
        assert command.startswith('cmd_')
        self.cmd = command[4:]
        self.init()
        method = getattr(self, command)

        # The sync subcommand is the only command that can be run as root
        # except when the repository has been created by root.
        if self.repo.root_not_repo_owner and command != 'cmd_sync':
            raise EmtError('cannot be executed as root')

        if command != 'cmd_create':
            self.repo.init()
        try:
            method()
        finally:
            self.repo.close()

    def print(self, text=''):
        print(text, file=self.results)

    def print_commits(self, suffix=''):
        print_footer = False
        for tmp_branch in ('etc-tmp', 'master-tmp'):
            branch = tmp_branch[:tmp_branch.index('-tmp')]
            rev_list = self.repo.git_cmd(
                                'rev-list --format=%%b%%n%%s %s..%s' %
                                (branch, tmp_branch))
            if not rev_list:
                continue
            self.print('Commits in the %s%s branch:' % (branch, suffix))
            print_footer = True
            # Print the commits in chronological order.
            for line in reversed(rev_list.splitlines()):
                if line.startswith('commit '):
                    sha = line[len('commit '):]
                    diff_tree = self.repo.git_cmd(
                           'diff-tree --no-commit-id --name-only -r %s' % sha)
                    rpaths = diff_tree.splitlines()
                    lines = (sorted(rpaths) if rpaths else
                             EMPTY_CHERRY_PICK_MSG.splitlines())
                    self.print('\n'.join((' ' * 4 + l) for l in lines))
                elif line:
                    self.print('  %s' % line)
        if print_footer:
            self.print('---')

    def cmd_create(self):
        """Create the git repository and populate the etc and master branches.

        The git repository is located at $XDG_DATA_HOME/etcmaint if the
        XDG_DATA_HOME environment variable is set and at
        $HOME/.local/share/etcmaint otherwise.

        The 'diff' subcommand may be used now to list the files added to /etc
        by the user. If any of those files is added (and commited) to the
        'master' branch, the 'update' subcommand will track future changes
        made to those files in /etc and include these changes to the 'master'
        branch.
        """
        self.repo.create()

        # Add .gitignore.
        self.repo.add_files({'.gitignore': '.swp\n'}, FIRST_COMMIT_MSG)

        # Create the etc and timestamps branches.
        self.repo.checkout('etc', create=True)
        self.repo.checkout('timestamps', create=True)

        self.repo.checkout('master')
        self.repo.init()
        self.update_repository()
        print('Git repository created at %s' % self.repodir)

    def cmd_update(self):
        """Update the repository with packages and user changes.

        The changes are made in temporary branches named 'master-tmp' and
        'etc-tmp'. When those changes do not incur a cherry-pick, the
        'master-tmp' (resp.  'etc-tmp') branch is merged as a fast-forward
        into its main branch and the temporary branches deleted. The operation
        is then complete and the changes can be examined with the git diff
        command run on the differences between the git tag set at the previous
        'update' command, named '<branch name>-prev', and the branch itself.
        For example, to list the names of the files that have been changed in
        the master branch:

            git diff --name-only master-prev...master

        Otherwise the fast-forwarding is postponed until the 'sync' command is
        run and until then it is still possible to start over with a new
        'update' command, the previous temporary branches being discarded in
        that case. To examine the changes that will be merged into each branch
        by the 'sync' command, use the git diff command run on the differences
        between the branch itself and the corresponding temporary branch. For
        example, to list all the changes that will be made by the 'sync'
        command to the master branch:

            git diff master...master-tmp

        """
        self.update_repository()
        results = self.results.getvalue()
        if results:
            print('---')
            print(results, end='')

    def cmd_diff(self):
        """Print the list of the /etc files not tracked in the etc branch.

        These are the /etc files not extracted from an Arch Linux package. Among
        them and of interest are the files created by a user that one may want
        to manually add and commit to the 'master' branch of the etcmaint
        repository so that their changes start being tracked by etcmaint (for
        example the netctl configuration files).

        pacnew, pacsave and pacorig files are excluded from this list.
        """
        if self.use_etc_tmp:
            if 'etc-tmp' in self.repo.branches:
                self.repo.checkout('etc-tmp')
            else:
                print('The etc-tmp branch does not exist')
                return
        else:
            self.repo.checkout('etc')

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = list_rpaths(self.root_dir, ROOT_SUBDIR,
                           suffixes=suffixes, prefixes=self.exclude_prefixes)
        repo_files = list_rpaths(self.repodir, ROOT_SUBDIR)
        print('\n'.join(sorted(set(etc_files).difference(repo_files))))

    def cmd_sync(self):
        """Synchronize /etc with changes made by the previous update command.

        To print the changes that are going to be made to /etc by the 'sync'
        command, first print the list of files that will be copied:

            etcmaint sync --dry-run

        Then for each file in the list, run the following git command where
        'rpath' is the relative path name as output by the previous command
        and that starts with 'etc/':

            git diff master...master-tmp -- rpath

        This command must be run as root when using the --root-dir default
        value.
        """
        if not 'etc-tmp' in self.repo.branches:
            print(self.mode + 'no file to sync to /etc')
            return

        for branch in ('master', 'etc'):
            self.repo.check_fast_forward(branch)

        # Find the cherry-pick in the etc-tmp branch.
        rev_list = self.repo.git_cmd('rev-list --format=%s etc..etc-tmp')
        cherry_pick_sha = None
        for line in rev_list.splitlines():
            if line.startswith('commit '):
                sha = line[len('commit '):]
            elif line == CHERRY_PICK_COMMIT_MSG:
                cherry_pick_sha =  sha
                break
        if cherry_pick_sha is None:
            raise EmtError('cannot find a cherry-pick in the etc-tmp branch')

        # Copy the files commited in the cherry-pick to /etc.
        self.repo.checkout('master-tmp')
        res = self.repo.git_cmd('diff-tree --no-commit-id --name-only -r %s' %
                                cherry_pick_sha)
        print_header = True
        for rpath in (f for f in res.splitlines() if
                      f not in self.exclude_files):
            etc_file = os.path.join(self.root_dir, rpath)
            if not os.path.lexists(etc_file):
                warn('%s not synced, does not exist on /etc' % rpath)
                continue
            if not self.dry_run:
                path = os.path.join(self.repodir, rpath)
                try:
                    if os.path.islink(path) or os.path.islink(etc_file):
                        os.remove(etc_file)
                    shutil.copyfile(path, etc_file, follow_symlinks=False)
                except OSError as e:
                    raise EmtError(e) from None
            if print_header:
                print_header = False
                print('Files copied from the master-tmp branch to %s:' %
                      self.root_dir)
            print('  %s' % rpath)

        if not self.dry_run:
            self.remove_tmp_branches()
        print(self.mode + "'sync' command terminated.")

    def create_tmp_branches(self):
        print('Creating the temporary branches')
        branches = self.repo.branches
        for branch in ('etc', 'master', 'timestamps'):
            tmp_branch = '%s-tmp' % branch
            if tmp_branch in branches:
                self.repo.checkout('master')
                self.repo.git_cmd('branch --delete --force %s' % tmp_branch)
                print("Remove the previous unused '%s' branch" % tmp_branch)
            self.repo.checkout(branch)
            self.repo.checkout(tmp_branch, create=True)

    def remove_tmp_branches(self):
        """Delete tmp branches, but merge first if not dry run."""
        if 'master-tmp' in self.repo.branches:
            print('Removing the temporary branches')
            if self.repo.curbranch in ('master-tmp', 'etc-tmp',
                                       'timestamps-tmp'):
                self.repo.checkout('master')
            for branch in ('master', 'etc', 'timestamps'):
                tmp_branch = '%s-tmp' % branch
                if not self.dry_run:
                    if branch in ('master', 'etc'):
                        # If there is a merge to be done then tag the branch
                        # before the merge.
                        if (self.repo.git_cmd('rev-list %s..%s' %
                                (branch, tmp_branch))):
                            self.repo.git_cmd('tag -f %s-prev %s' %
                                              (branch, branch))
                    self.repo.checkout(branch)
                    self.repo.git_cmd('merge %s' % tmp_branch)
                self.repo.git_cmd('branch -D %s' % tmp_branch)

    def update_repository(self):
        self.create_tmp_branches()

        self.git_removed_files()
        cherry_pick_sha = self.git_upgraded_pkgs()
        self.git_user_updates()

        if cherry_pick_sha is not None:
            if self.git_cherry_pick(cherry_pick_sha):
                self.print_commits(suffix='-tmp')
                self.print(self.mode + dedent("""\
                'update' command terminated, use the 'sync' command to
                copy the changes to /etc and fast-forward the changes
                to the master and etc branches."""))

            if self.dry_run:
                self.remove_tmp_branches()

        else:
            self.print_commits()
            self.remove_tmp_branches()
            self.print(self.mode +
                    "'update' command terminated: no file to sync to /etc.")

    def git_cherry_pick(self, cherry_pick_sha):
        self.repo.checkout('master-tmp')
        for rpath in self.etc_commits.cherry_pick.rpaths:
            repo_file = os.path.join(self.repodir, rpath)
            if not os.path.isfile(repo_file):
                assert False, ('cherry picking %s to the master-tmp branch'
                               ' but this file does not exist' % rpath)

        # Use first a temporary branch for the cherry-pick.
        try:
            self.repo.checkout('cherry-pick', create=True)
            proc = self.repo.cherry_pick(cherry_pick_sha)
            if proc.returncode == 0:
                # Do a fast-forward merge.
                self.repo.checkout('master-tmp')
                self.repo.git_cmd('merge cherry-pick')
                return True
            else:
                conflicts = [x[3:] for x in self.repo.get_status()
                             if 'U' in x[:2]]
                if conflicts:
                    assert os.path.exists(os.path.join(self.repodir,
                                          '.git', 'CHERRY_PICK_HEAD'))
                    self.repo.git_cmd('cherry-pick --abort')
                else:
                    self.repo.git_cmd('reset --hard HEAD')
                    raise EmtError(proc.stdout)
        finally:
            self.repo.checkout('master-tmp')
            self.repo.git_cmd('branch -D cherry-pick')

        self.print_commits(suffix='-tmp')

        self.print('List of files with a conflict to resolve:')
        self.print('\n'.join('  %s' % c for c in sorted(conflicts)))

        # Do the effective cherry-pick now after having printed the list of
        # files with a conflict to resolve.
        if not self.dry_run:
            self.print()
            proc = self.repo.cherry_pick(cherry_pick_sha)
            self.print('This is the output of the git cherry-pick command:')
            self.print('%s' % ('\n'.join('  %s' % l for l in
                             proc.stdout.splitlines())))
            self.print()
            self.print('Please resolve the conflict%s.' %
                       ('s' if len(conflicts) > 1 else ''))
            if not os.getcwd().startswith(self.repodir):
                self.print('You MUST change the current working directory'
                           ' to %s.' % self.repodir)
            self.print(dedent("""\
                At any time you may run 'git cherry-pick --abort' and
                start over later with another 'etcmaint update' command.""" ))
        else:
            self.print(self.mode +
                "'update' command terminated with conflict%s to resolve." %
                ('s' if len(conflicts) > 1 else ''))

        return False

    def git_removed_files(self):
        """Remove files that do not exist in /etc."""

        etc_tracked = self.repo.tracked_files('etc-tmp')
        for rpath in etc_tracked:
            etc_file = os.path.join(self.root_dir, rpath)
            if not os.path.lexists(etc_file):
                self.etc_commits.removed.rpaths.append(rpath)
        self.etc_commits.removed.commit()

        master_tracked = self.repo.tracked_files('master-tmp')
        for rpath in master_tracked:
            etc_file = os.path.join(self.root_dir, rpath)
            if not os.path.lexists(etc_file):
                self.master_commits.removed.rpaths.append(rpath)
        self.master_commits.removed.commit()

    def git_user_updates(self):
        """Update master-tmp with the user changes."""

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = {n: EtcPath(self.root_dir, n) for n in
                     list_rpaths(self.root_dir, ROOT_SUBDIR,
                                 suffixes=suffixes)}
        etc_tracked = self.repo.tracked_files('etc-tmp')

        # Build the list of etc-tmp files that are different from their
        # counterpart in /etc.
        self.repo.checkout('etc-tmp')
        to_check_in_master = []
        for rpath in etc_files:
            if rpath in etc_tracked:
                # Issue #16. Do not add an /etc file that has been made not
                # readable after a pacman upgrade.
                if (etc_files[rpath].digest != b'' and
                        etc_files[rpath] != etc_tracked[rpath]):
                    to_check_in_master.append(rpath)

        master_tracked = self.repo.tracked_files('master-tmp')

        # Build the list of master-tmp files:
        #   * To add when the file does not exist in master-tmp and its
        #     counterpart in etc-tmp is different from the /etc file.
        #   * To update when the file exists in master-tmp and is different
        #     from the /etc file.
        for rpath in to_check_in_master:
            if rpath not in master_tracked:
                self.master_commits.user_updated.rpaths.append(rpath)
        self.repo.checkout('master-tmp')
        for rpath in etc_files:
            if (rpath in master_tracked and rpath not in
                    self.master_commits.added.rpaths):
                if etc_files[rpath].digest == b'':
                    warn('cannot read %s' % etc_files[rpath].path)
                elif etc_files[rpath] != master_tracked[rpath]:
                    self.master_commits.user_updated.rpaths.append(rpath)

        for rpath in self.master_commits.user_updated.rpaths:
            copy_file(rpath, self.root_dir, self.repodir)
        self.master_commits.user_updated.commit()

    def git_upgraded_pkgs(self):
        """Update the repository with installed or upgraded packages."""

        self.extract_from_cachedir()
        self.etc_commits.added.commit()

        cherry_pick_sha = None
        if self.etc_commits.cherry_pick.rpaths:
            self.etc_commits.cherry_pick.commit()
            cherry_pick_sha = self.repo.git_cmd('rev-list -1 HEAD --')

        # Clean the working area of the files that are not under version
        # control.
        self.repo.git_cmd('clean -d -x -f')

        # Update the master-tmp branch with new files.
        if self.master_commits.added.rpaths:
            self.repo.checkout('master-tmp')
            for rpath in self.master_commits.added.rpaths:
                repo_file = os.path.join(self.repodir, rpath)
                if os.path.lexists(repo_file):
                    warn('adding %s to the master-tmp branch but this file'
                         ' already exists' % rpath)
                copy_file(rpath, self.root_dir, self.repodir,
                          repo_file=repo_file)
            self.master_commits.added.commit()

        return cherry_pick_sha

    def list_new_packages(self, cache_dir):
        """Build the list of new package files."""
        import re

        def newer_exists_in(packages, name, st_mtime, read_content=True):
            if name in packages:
                if read_content:
                    # A 'tracked' timestamps file.
                    with packages[name].open() as f:
                        timestamp = f.read()
                else:
                    # A 'new_packages' file.
                    timestamp = packages[name].stat().st_mtime
                return float(st_mtime) <= float(timestamp)
            return False

        re_validext = re.compile(r'.*\.pkg\.tar\.(%s)' % '|'.join(EXTENSIONS))
        exclude_pkgs_len = len(self.exclude_pkgs)
        excluded = []
        # 'timestamps' and 'tracked:'
        # Dictionary {package name: PosixPath with timestamp as content}
        timestamps = {}
        tracked = self.repo.tracked_files('timestamps-tmp')
        # Dictionary {package name: PosixPath of pacman file}
        new_pkgs = {}
        self.repo.checkout('timestamps-tmp')

        for root, *remain in os.walk(cache_dir):
            # scandir() started supporting the context manager protocol in
            # Python 3.6 (see issue bpo-25994) so we cannot use a context
            # manager here and must delete 'it' when not used anymore so that
            # the scandir() file descriptor is explicitly closed by Python.
            it = os.scandir(root)
            for direntry in it:
                if not direntry.is_file():
                    continue

                fullname = direntry.name
                if not re_validext.match(fullname):
                    continue

                # "Version tags may not include hyphens!" quoting from
                # https://wiki.archlinux.org/index.php/Arch_package_guidelines
                name, *remain = fullname.rsplit('-', maxsplit=3)
                if len(remain) != 3:
                    warn('ignoring incorrect package name: %s' % fullname)
                    continue

                st_mtime = direntry.stat().st_mtime
                if (newer_exists_in(tracked, name, st_mtime) or
                        newer_exists_in(new_pkgs, name, st_mtime, False)):
                    continue

                # Exclude packages.
                if (name in excluded or
                        len(list(itertools.takewhile(lambda x: not x or
                            not name.startswith(x),
                            self.exclude_pkgs))) != exclude_pkgs_len):
                    if name not in excluded:
                        excluded.append(name)
                    continue

                timestamps[name] = str(st_mtime)
                new_pkgs[name] = pathlib.PosixPath(direntry.path)
            del it
            # Look the full cache_dir tree only when scanning the 'aur-dir'
            # directory.
            if cache_dir != self.aur_dir:
                break

        # Commit the new timestamps.
        if timestamps:
            # Add files to the timestamps-tmp branch whose name are the
            # package name and whose content are the modification time.
            self.repo.add_files(timestamps,
                                'Add the timestamps of the new packages')
            self.new_packages = list(pkg.name for pkg in new_pkgs.values())

        return new_pkgs.values()

    def extract(self, packages, tracked):
        """ Extract configuration files from packages.

        Return a dictionary mapping extracted configuration file names to the
        EtcPath instance of the 'original' file before the extraction.
        """
        from concurrent.futures import ThreadPoolExecutor

        def extract_from(pkg):
            with tarfile_open(str(pkg), pkg.suffix[1:]) as tar:
                for tinfo in tar:
                    fname = tinfo.name
                    if (fname.startswith(ROOT_SUBDIR) and
                            (tinfo.isfile() or tinfo.issym() or tinfo.islnk())
                            and fname not in self.exclude_files):
                        path = EtcPath(self.repodir, tinfo.name)
                        # Remember the sha1 of the existing file, if it
                        # exists, before extracting it from the tarball
                        # (EtcPath.digest is lazily evaluated).
                        not_used = path.digest
                        extracted[tinfo.name] = path

                        # The Python tarfile implementation fails to create
                        # symlinks, see also issue bpo-10761.
                        if tinfo.issym():
                            abspath = os.path.join(self.repodir, tinfo.name)
                            try:
                                if os.path.lexists(abspath):
                                    os.unlink(abspath)
                            except OSError as err:
                                warn(err)
                        tar.extract(tinfo, self.repodir)
            print(pkg.name)

        extracted = {}
        max_workers = len(os.sched_getaffinity(0)) or 4
        # Extracting from tarfiles is not thread safe (see msg315067 in bpo
        # issue https://bugs.python.org/issue23649).
        with threadsafe_makedirs():
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(extract_from, pkg) for
                           pkg in packages]
        for f in futures:
            exc = f.exception()
            if exc is not None:
                raise exc
        for rpath in extracted:
            if rpath not in tracked:
                # Ensure that the file can be overwritten on a next
                # 'update' command.
                path = os.path.join(self.repodir, rpath)
                mode = os.lstat(path).st_mode
                if mode & RW_ACCESS != RW_ACCESS:
                    os.chmod(path, mode | RW_ACCESS)
        return extracted

    def extract_from_cachedir(self):
        """ Extract configuration files from pacman cachedir.

        The pacman man pages at section 'HANDLING CONFIG FILES describes the
        logic used to handle configuration files extracted from packages.
        The pacman terminology:
            original        file in the etc-tmp branch
            current         file in /etc
            new             packaged file

        State before                             State after logic applied and
        applying pacman logic                    upon entering
                                                 extract_from_cachedir()

        case 1: original=X, current=X, new=X     unchanged
        case 2: original=X, current=X, new=Y     current=Y, new=Y
        case 3: original=X, current=Y, new=X     unchanged
        case 4: original=X, current=Y, new=Y     unchanged
        case 5: original=X, current=Y, new=Z     cherry-pick changes between
                                                 new and current into master
        case 6: original=NULL, current=Y, new=Z  idem
        """

        # Extract the configuration files from each new package into the
        # etc-tmp branch.
        master_tracked = self.repo.tracked_files('master-tmp')
        etc_tracked = self.repo.tracked_files('etc-tmp')
        packages = self.list_new_packages(self.cache_dir)
        print('Extracting configuration files from %d new package files' %
              len(packages), end='')
        if self.aur_dir is not None:
            aur_packages = self.list_new_packages(self.aur_dir)
            print(' and %d new AUR package files' % len(aur_packages))
            packages = itertools.chain(packages, aur_packages)
        else:
            print()
        self.repo.checkout('etc-tmp')
        original_files = self.extract(packages, etc_tracked)

        for rpath in original_files:
            new = EtcPath(self.repodir, rpath)
            current = EtcPath(self.root_dir, rpath)
            if current.digest == b'':
                path = current.path
                exists = True
                try:
                    if not path.exists():
                        exists = False
                except PermissionError:
                    pass
                if not exists:
                    # Do not warn on 'create' subcommand to avoid the noise of
                    # all the /etc files removed after packages removal while
                    # their package file is still present in pacman CacheDir.
                    if self.cmd != 'create':
                        warn('skip %s does not exist' % path)
                else:
                    warn('skip %s not readable' % path)
                continue

            # A new package has been installed.
            if rpath not in etc_tracked:
                # Add the file to the etc-tmp branch.
                self.etc_commits.added.rpaths.append(rpath)
                if new != current:
                    # Case 6.
                    # Add the file name to the list of files to add to the
                    # master-tmp branch (from /etc).
                    self.master_commits.added.rpaths.append(rpath)
            # A package upgrade.
            else:
                if new == current:
                    if new != original_files[rpath]:
                        # Case 2 and 4.
                        # Stage the file in the etc-tmp branch.
                        self.etc_commits.added.rpaths.append(rpath)
                        if rpath in master_tracked:
                            warn('%s should not exist in the master branch'
                                 % rpath)
                    else:
                        # Case 1.
                        pass
                else:
                    # Case 5.
                    # A specific commit is used for the configuration files
                    # whose changes must be cherry-picked into the master
                    # branch.
                    if new != original_files[rpath]:
                        self.etc_commits.cherry_pick.rpaths.append(rpath)
                    else:
                        # Case 3.
                        pass

def dispatch_help(args):
    """Get help on a command."""
    command = args.subcommand
    if command is None:
        command = 'help'
    args.parsers[command].print_help()

    cmd_func = getattr(EtcMaint, 'cmd_%s' % command, None)
    if cmd_func:
        lines = cmd_func.__doc__.splitlines()
        print('\n%s\n' % lines[0])
        paragraph = []
        for l in dedent('\n'.join(lines[2:])).splitlines():
            if l == '':
                if paragraph:
                    print('\n'.join(wrap(' '.join(paragraph), width=78)))
                    print()
                    paragraph = []
                continue
            paragraph.append(l)
        if paragraph:
            print('\n'.join(wrap(' '.join(paragraph), width=78)))

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
    main_parser.prog = 'etcmaint'

    # The help subparser handles the help for each command.
    subparsers = main_parser.add_subparsers(title='etcmaint subcommands')
    parsers = { 'help': main_parser }
    parser = subparsers.add_parser('help', add_help=False,
                                   help=dispatch_help.__doc__.splitlines()[0])
    parser.add_argument('subcommand', choices=parsers, nargs='?',
                        default=None)
    parser.set_defaults(command='dispatch_help', parsers=parsers)

    # Add the command subparsers.
    d = dict(inspect.getmembers(EtcMaint, inspect.isfunction))
    for command in sorted(d):
        if not command.startswith('cmd_'):
            continue
        cmd = command[4:]
        func = d[command]
        parser = subparsers.add_parser(cmd, help=func.__doc__.splitlines()[0],
                                       add_help=False)
        parser.set_defaults(command=command)
        if cmd in ('update', 'sync'):
            parser.add_argument('--dry-run', '-n', help='Perform a trial run'
                ' with no changes made (default: %(default)s)',
                action='store_true', default=False)
        if cmd in ('create', 'update'):
            parser.add_argument('--cache-dir', help='Set pacman cache'
                ' directory (override the /etc/pacman.conf setting of the'
                ' CacheDir option)', type=isdir)
            parser.add_argument('--aur-dir', help='Set the path of the root '
                'of the directory tree where to look for built AUR packages',
                type=isdir)
            parser.add_argument('--exclude-pkgs', default=EXCLUDE_PKGS,
                type=lambda x: list(y.strip() for y in x.split(',')),
                help='A comma separated list of prefix of package names'
                     ' to be ignored (default: "%(default)s")',
                metavar='PFXS')
        if cmd in ('create', 'update', 'sync'):
            parser.add_argument('--exclude-files', default=EXCLUDE_FILES,
                type=lambda x: list(os.path.join(ROOT_SUBDIR, y.strip()) for
                y in x.split(',')), metavar='FILES',
                help='A comma separated list of /etc path names to be ignored'
                     ' (default: "%(default)s")')
        if cmd == 'diff':
            parser.add_argument('--exclude-prefixes',
                default=EXCLUDE_PREFIXES, metavar='PFXS',
                type=lambda x: list(y.strip() for y in x.split(',')),
                help='A comma separated list of prefixes of /etc path'
                ' names to be ignored (default: "%(default)s")')
            parser.add_argument('--use-etc-tmp',
                help='Use the etc-tmp branch instead (default: %(default)s)',
                action='store_true', default=False)
        parser.add_argument('--root-dir', default='/',
            help='Set the root directory of the etc files, mostly used for'
            ' testing (default: "%(default)s")', type=isdir)
        parsers[cmd] = parser

    main_parser.parse_args(argv[1:], namespace=namespace)
    if not hasattr(namespace, 'command'):
        main_parser.error('a command is required')

def etcmaint(argv):
    with io.StringIO() as results:
        # Assign the parsed args to the EtcMaint instance.
        emt = EtcMaint(results)
        parse_args(argv, emt)

        # Run the command.
        if emt.command == 'dispatch_help':
            func = getattr(sys.modules[__name__], 'dispatch_help')
            func(emt)
        else:
            emt.run(emt.command)
    return emt

def main():
    try:
        etcmaint(sys.argv)
    except EmtError as e:
        print('*** %s: error:' % pgm, e, file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
