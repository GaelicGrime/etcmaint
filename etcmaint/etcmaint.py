#! /bin/env python
"""An Arch Linux tool that uses git for the maintenance of /etc files.

The /etc files installed or upgraded by Arch Linux packages are managed in the
``etc`` branch of a git repository. The /etc files customized or created by
the user are managed in the ``master`` branch. The upgraded package changes of
the user-customized files are merged (actually cherry-picked) with the
``update`` subcommand. Merge conflicts must be resolved and commited by the
user. After a merge, the ``sync`` subcommand is used to retrofit the merged
changes to /etc.
"""

import sys
import os
import stat
import argparse
import pathlib
import inspect
import configparser
import tarfile
import hashlib
import itertools
import shutil
import contextlib
import subprocess
import re
from time import time as _time
from textwrap import dedent, wrap
from subprocess import PIPE, STDOUT, CalledProcessError
from concurrent.futures import ThreadPoolExecutor, as_completed

__version__ = '0.1'
pgm = os.path.basename(sys.argv[0].rstrip(os.sep))
RW_ACCESS = stat.S_IWUSR | stat.S_IRUSR
FIRST_COMMIT_MSG = 'First etcmaint commit'
EXCLUDE_FILES = 'passwd, group, mtab, udev/hwdb.bin'
EXCLUDE_PKGS = ''
EXCLUDE_PREFIXES = 'ca-certificates, ssl/certs'

# The subdirectory of '--root-dir'.
ROOT_SUBDIR = 'etc'

class EmtError(Exception): pass

def warn(msg):
    print('*** warning:', msg, file=sys.stderr)

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
                flist.append(os.path.join(subdir, rpath))
    return flist

def repository_dir():
    xdg_data_home = os.environ.get('XDG_DATA_HOME')
    if xdg_data_home is not None:
        return os.path.join(xdg_data_home, 'etcmaint')

    sudo_user = os.environ.get('SUDO_USER')
    login_name = sudo_user if sudo_user and os.getuid() == 0 else ''
    return os.path.expanduser('~%s/.local/share/etcmaint' % login_name)

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
    shutil.copy(etc_file, repo_file, follow_symlinks=False)

def str_file_list(header, files):
    if files:
        lines = [header]
        lines.extend('  %s' % f for f in files)
        return '\n'.join(lines)

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

class EtcPath():
    def __init__(self, root_dir, *parts):
        assert len(parts) >= 2
        assert parts[-1].startswith(ROOT_SUBDIR)
        self.root_dir = root_dir
        self.parts = parts
        self.path = pathlib.Path(*parts)
        self._digest = None

    @property
    def digest(self):
        if self._digest is None:
            try:
                is_symlink = self.path.is_symlink()
            except OSError:
                self._digest = b''
            else:
                if is_symlink:
                    # The digest is the path to which the symbolic link
                    # points.
                    realpath = self.path.resolve()
                    basedir = pathlib.Path(*self.parts[:-1])
                    try:
                        # The symlink is a relative path.
                        self._digest = realpath.relative_to(basedir)
                    except ValueError:
                        try:
                            # The symlink is an absolute path.
                            self._digest = realpath.relative_to(self.root_dir)
                        except ValueError:
                            warn('%s links to %s not prefixed with %s' %
                                 (self.path, realpath, self.root_dir))
                            self._digest = realpath
                else:
                    try:
                        h = hashlib.sha1()
                        with self.path.open('rb') as f:
                            h.update(f.read())
                        self._digest = h.digest()
                    except OSError:
                        self._digest = b''
        return self._digest

    def __eq__(self, other):
        return (isinstance(other, EtcPath) and self.digest == other.digest and
                self.digest != b'')

class GitRepo():
    """A git repository."""

    def __init__(self, root_dir, repodir, verbose):
        self.root_dir = root_dir
        self.repodir = repodir.rstrip(os.sep)
        self.verbose = verbose
        self.curbranch = None
        self.initial_branch = None

        # When run with sudo, for example with the 'sync' command, force all
        # git commands to be run as the user who invoked sudo to avoid having
        # some files created by 'git checkout some_branch' with root ownership
        # when run as root, that cannot be unlinked later when checking out
        # another branch as the plain user.
        self.git = []
        sudo_uid = os.environ.get('SUDO_UID')
        if os.getuid() == 0 and sudo_uid is not None:
            self.git.extend(('sudo --user #%s' % sudo_uid).split())
        self.git.extend(('git -C %s' % repodir).split())

    def create(self):
        """Create the git repository."""
        if os.path.isdir(self.repodir) and os.listdir(self.repodir):
            raise EmtError('%s is not empty' % self.repodir)
        if not os.path.isdir(self.repodir):
            os.makedirs(self.repodir)
        self.git_cmd('init')

    def init(self):
        # Check the first commit message.
        proc = subprocess.run(self.git + ['rev-list', '--max-parents=0',
                       '--format=%s', 'master', '--'],
                       universal_newlines=True, stdout=PIPE, stderr=STDOUT)
        if proc.returncode != 0:
            raise EmtError('no git repository at %s' % self.repodir)
        commit, first_commit_msg = proc.stdout.splitlines()
        if first_commit_msg != FIRST_COMMIT_MSG:
            err_msg = f"""\
                this is not an etcmaint repository
                found as the first commit message:
                '{first_commit_msg}'
                instead of the expected '{FIRST_COMMIT_MSG}' message"""
            raise EmtError(dedent(err_msg))

        status = self.get_status()
        if status:
            raise EmtError('the %s repository is not clean:\n%s' %
                  (self.repodir, '\n'.join(status)))

        if os.path.isfile(os.path.join(
                          self.repodir, '.git', 'CHERRY_PICK_HEAD')):
            raise EmtError("The previous cherry-pick is empty,"
                           " please use 'git reset'")

        # Get the initial branch.
        proc = subprocess.run(self.git + ['symbolic-ref', '--short', 'HEAD'],
                       universal_newlines=True, stdout=PIPE, stderr=STDOUT)
        if proc.returncode == 0:
            self.initial_branch = proc.stdout.splitlines()[0]
            self.curbranch = self.initial_branch

    def close(self):
        branch = 'master'
        if self.initial_branch in self.branches:
            branch = self.initial_branch
        if not self.get_status():
            self.checkout(branch)

    def git_cmd(self, cmd):
        if type(cmd) == str:
            cmd = cmd.split()
        try:
            output = subprocess.check_output(self.git + cmd,
                                    universal_newlines=True, stderr=STDOUT)
        except CalledProcessError as e:
            output = str(e) + '\n' + e.output.strip()
            raise EmtError(output)
        output = output.strip()
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

    def tracked_files(self, branch, exclude=None):
        """A dictionary of the tracked files in this branch."""
        d = {}
        ls_tree = self.git_cmd('ls-tree -r --name-only --full-tree %s' %
                               branch)
        for fname in ls_tree.splitlines():
            if exclude and fname in exclude:
                continue
            if not fname.startswith(ROOT_SUBDIR):
                continue
            d[fname] = EtcPath(self.root_dir, self.repodir, fname)
        return d

    @property
    def branches(self):
        branches = self.git_cmd("for-each-ref --format=%(refname:short)")
        return branches.splitlines()

class Timestamp():
    def __init__(self, emt):
        self.emt = emt
        self.fname = '.etcmaint_timestamp'
        self.path = os.path.join(emt.repodir, self.fname)

    def new(self):
        """Create the timestamp file."""
        content = """\
            # This file is created by etcmaint. Its purpose is to record the
            # time the etc-tmp branch has been created and the time the etc
            # branch has been fast-forwarded to the etc-tmp branch.
            CREATE=0
            FAST-FORWARD=0
        """
        self.emt.repo.add_file(self.fname, content, 'Add the timestamp')

    def abort_corrupted(self):
        raise EmtError("the '%s' timestamp file is corrupted" % self.fname)

    def set(self, prefix, time=None):
        """Set the timestamp."""
        time = _time() if time is None else time
        prefix_found = False
        with open(self.path, 'r') as f:
            lines = []
            for line in f:
                if line.startswith(prefix):
                    prefix_found = True
                    line = '%s=%s\n' % (prefix, time)
                lines.append(line)
        if not prefix_found:
            self.abort_corrupted()
        content = ''.join(lines)
        self.emt.repo.add_file(self.fname, content,
                                  'Set the %s timestamp' % prefix)

    def value(self, prefix):
        with open(self.path, 'r') as f:
            for line in f:
                if line.startswith(prefix):
                    return float(line[line.index('=')+1:])
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
             f" 'etc{btype}' branch:")
        self.add_list(self.pkg_add_master,
         f"List of files extracted from a package and added to the"
             f" 'master{btype}' branch:")
        self.add_list(self.cherry_pick,
         'List of files to sync to /etc:')
        return self.result

class EtcMaint():
    """Provide methods to implement the commands."""

    def __init__(self):
        self.repodir = repository_dir()
        self.timestamp = Timestamp(self)
        self.results = UpdateResults()

    def init(self):
        if not hasattr(self, 'verbose'):
            self.verbose = False
        self.repo = GitRepo(self.root_dir, self.repodir, self.verbose)

        if not hasattr(self, 'dry_run'):
            self.dry_run = False
        if hasattr(self, 'cache_dir') and self.cache_dir is None:
            cfg = configparser.ConfigParser(allow_no_value=True)
            with open('/etc/pacman.conf') as f:
                cfg.read_file(f)
            self.cache_dir = cfg['options']['CacheDir']

    def run(self, command):
        """Run the etcmaint command."""
        self.init()
        method = getattr(self, command)
        if command != 'cmd_create':
            self.repo.init()
        try:
            res = method()
            if isinstance(res, str):
                print(res % ('[dry-run] ' if self.dry_run else ''))
        finally:
            self.repo.close()

    def cmd_create(self):
        """Create the git repository.

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
        gitignore = """\
            *.swp
        """
        self.repo.add_file('.gitignore', gitignore, FIRST_COMMIT_MSG)

        # Create the etc branch and the timestamp.
        self.repo.checkout('etc', create=True)
        self.timestamp.new()

        self.repo.checkout('master')
        self.repo.init()
        self.update_repository()
        print('Git repository created at %s' % self.repodir)

    def cmd_update(self):
        """Update the repository with packages and user changes.

        The changes are done in temporary branches named 'master-tmp' and
        'etc-tmp'. When the changes do not incur a merge, the 'master' (resp.
        'etc') branch is fast-forwarded to its temporary branch and the
        temporary branches deleted. The operation is complete. Otherwise the
        fast-forwarding is postponed until the merge is synced to /etc with
        the 'sync' subcommand and the fast-forwarding is done by this command.
        Until then it is still possible to start over with a new 'update'
        subcommand, the previous temporary branches are discarded in that
        case.

        When a merge is pending and if packages are being upgraded before the
        'sync' subcommand, the next 'update' subcommand following the 'sync'
        subcommand will take those packages into account.
        """
        res = self.update_repository()
        if isinstance(res, str):
            res += dedent("""\
            %s'update' command terminated, use the 'sync' command to
            copy the changes to /etc and fast-forward the changes to the
            master branch""")
        else:
            res = str(self.results)
            res += "\n%s'update' command terminated: no file to sync to /etc"
        return res

    def cmd_diff(self):
        """Print the list of /etc file names not tracked in the 'etc' branch.

        These are the /etc files not created from an Arch Linux package. Among
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
        """Synchronize /etc with the changes made in the last merge.

        This command must be run with sudo or as root when using the
        --root-dir default value.
        """
        if not 'master-tmp' in self.repo.branches:
            return '%sno file to sync to /etc'

        # Find the cherry-pick in the master-tmp branch.
        re_commit = re.compile('^commit (?P<commit>[0-9A-Fa-f]{40})$')
        re_cherry_pick = re.compile(
                        r'^\(cherry picked from commit [0-9A-Fa-f]{40}\)$')
        res = self.repo.git_cmd('rev-list --format=%b master...master-tmp')
        cherry_pick_commit = None
        commit = None
        for line in res.splitlines():
            matchobj = re_commit.match(line)
            if matchobj:
                commit = matchobj.group('commit')
                continue
            matchobj = re_cherry_pick.match(line)
            if matchobj:
                cherry_pick_commit = commit
                break
        if cherry_pick_commit is None:
            raise EmtError('cannot find a cherry-pick in master-tmp branch')

        # Copy the files commited in the cherry-pick to /etc.
        self.repo.checkout('master-tmp')
        res = self.repo.git_cmd('diff-tree --no-commit-id --name-only -r %s' %
                                cherry_pick_commit)
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
                    else:
                        stat = os.stat(etc_file)
                    try:
                        shutil.copy(path, etc_file, follow_symlinks=False)
                    except OSError as e:
                        warn(e)
                    if not os.path.islink(etc_file):
                        os.chmod(etc_file, stat.st_mode)
                except OSError as e:
                    raise EmtError(str(e))
            print(rpath)

        if not self.dry_run:
            # Remove the temporary branches and update the timestamp to the
            # time of the etc-tmp branch creation.
            self.repo.checkout('etc-tmp')
            time = self.timestamp.value('CREATE')
            self.fast_forward(time)
        return "%s'sync' command terminated"

    def create_tmp_branches(self):
        print('Create the master-tmp and etc-tmp branches')
        branches = self.repo.branches
        for branch in ('etc', 'master'):
            tmp_branch = '%s-tmp' % branch
            if tmp_branch in branches:
                self.repo.checkout('master')
                self.repo.git_cmd('branch --delete --force %s' % tmp_branch)
                print("Remove the previous unused '%s' branch" % tmp_branch)
            self.repo.checkout(branch)
            self.repo.checkout(tmp_branch, create=True)
            if tmp_branch == 'etc-tmp':
                self.timestamp.set('CREATE')

    def remove_tmp_branches(self, fast_forward=False):
        if not 'master-tmp' in self.repo.branches:
            return False

        print('Remove the master-tmp and etc-tmp branches')
        if self.repo.curbranch in ('master-tmp', 'etc-tmp'):
            self.repo.checkout('master')
        for branch in ('master', 'etc'):
            tmp_branch = '%s-tmp' % branch
            if fast_forward:
                self.repo.checkout(branch)
                self.repo.git_cmd('merge %s' % tmp_branch)
            self.repo.git_cmd('branch -D %s' % tmp_branch)
        return True

    def fast_forward(self, time=None):
        if self.remove_tmp_branches(fast_forward=True):
            print('Update the timestamp')
            self.repo.checkout('etc')
            self.timestamp.set('FAST-FORWARD', time=time)

    def update_repository(self):
        self.create_tmp_branches()

        cherry_pick_commit = self.git_upgraded_pkgs()
        self.git_removed_pkgs()
        self.git_user_updates()

        if cherry_pick_commit:
            res = self.git_cherry_pick(cherry_pick_commit)
            if self.dry_run:
                self.remove_tmp_branches()
            return res
        else:
            if self.dry_run:
                self.remove_tmp_branches()
            else:
                self.fast_forward()

    def git_cherry_pick(self, commit):
        self.repo.checkout('master-tmp')
        for fname in self.results.cherry_pick:
            repo_file = os.path.join(self.repodir, fname)
            if not os.path.isfile(repo_file):
                warn('cherry picking %s to the master-tmp branch but this'
                     ' file does not exist' % fname)

        self.results.btype = '-tmp'
        msg = '%s\n' % self.results

        # Use a temporary branch for the cherry-pick.
        try:
            self.repo.checkout('cherry-pick', create=True)
            proc = self.repo.cherry_pick(commit)
            if proc.returncode == 0:
                if not self.dry_run:
                    # Do a fast-forward merge.
                    self.repo.checkout('master-tmp')
                    self.repo.git_cmd('merge cherry-pick')
                return msg
            else:
                conflicts = [x[3:] for x in self.repo.get_status()
                             if 'U' in x[:2]]
                if conflicts:
                    self.repo.git_cmd('cherry-pick --abort')
                else:
                    self.repo.git_cmd('reset --hard HEAD')
                    err = proc.stdout
                    err += 'etcmaint internal error: no conflicts found'
                    raise EmtError(err)
        finally:
            self.repo.checkout('master-tmp')
            self.repo.git_cmd('branch -D cherry-pick')

        msg += '%s\n' % (str_file_list(
                'List of files with a conflict to resolve first:', conflicts))

        # Do the effective cherry-pick now after having printed the list of
        # files with a conflict to resolve.
        if not self.dry_run:
            proc = self.repo.cherry_pick(commit)
            cwd = os.getcwd()
            msg += ('Please resolve the conflict%s%s\n' %
                    ('s' if len(conflicts) > 1 else '',
                     '' if cwd.startswith(self.repodir) else ' in %s' %
                                                            self.repodir))
            msg += '*** WITHOUT CHANGING THE COMMIT MESSAGE ***\n'
            msg += 'This is the result of the cherry-pick command:\n'
            msg += '%s\n' % ('\n'.join('  %s' % l for l in
                             proc.stdout.splitlines()))
            msg += dedent("""\
                You may use 'git -C %s cherry-pick --abort'
                and start over later with another 'etcmaint update' command
            """ % self.repodir)

        return msg

    def git_removed_pkgs(self):
        """Remove files that do not exist in /etc."""

        rslt = self.results
        exclude=['.gitignore', self.timestamp.fname]
        etc_tracked = self.repo.tracked_files('etc-tmp', exclude=exclude)
        for fname in etc_tracked:
            etc_file = os.path.join(self.root_dir, fname)
            if not os.path.lexists(etc_file):
                rslt.etc_removed.append(fname)

        # Remove the etc-tmp files that do not exist in /etc.
        commit_msg = 'Remove files missing in /etc'
        if rslt.etc_removed:
            self.repo.checkout('etc-tmp')
            self.repo.remove(rslt.etc_removed, commit_msg)

        # Remove the master-tmp files that have been removed in the etc-tmp
        # branch.
        master_remove = []
        master_tracked = self.repo.tracked_files('master-tmp')
        for fname in rslt.etc_removed:
            if fname in master_tracked:
                master_remove.append(fname)
        if master_remove:
            self.repo.checkout('master-tmp')
            self.repo.remove(master_remove, commit_msg)

    def git_user_updates(self):
        """Update master-tmp with the user changes."""

        suffixes = ['.pacnew', '.pacsave', '.pacorig']
        etc_files = {n: EtcPath(self.root_dir, self.root_dir, n) for n in
                     list_rpaths(self.root_dir, ROOT_SUBDIR,
                                 suffixes=suffixes)}
        etc_tracked = self.repo.tracked_files('etc-tmp')

        # Build the list of etc-tmp files that are different from their
        # counterpart in /etc.
        self.repo.checkout('etc-tmp')
        to_check_in_master = []
        for fname in etc_files:
            if fname in etc_tracked:
                if etc_files[fname] != etc_tracked[fname]:
                    to_check_in_master.append(fname)

        master_tracked = self.repo.tracked_files('master-tmp')

        # Build the list of master-tmp files:
        #   * To add when the file does not exist in master-tmp and its
        #     counterpart in etc-tmp is different from the /etc file.
        #   * To update when the file exists in master-tmp and is different
        #     from the /etc file.
        rslt = self.results
        for fname in to_check_in_master:
            if fname not in master_tracked:
                rslt.user_added.append(fname)
        self.repo.checkout('master-tmp')
        for fname in etc_files:
            if fname in master_tracked and fname not in rslt.pkg_add_master:
                if etc_files[fname].digest == b'':
                    warn('cannot read %s' % etc_files[fname].path)
                elif etc_files[fname] != master_tracked[fname]:
                    rslt.user_updated.append(fname)

        for files, commit_msg in (
                (rslt.user_added, 'Add files with user changes'),
                (rslt.user_updated, 'Update files with user changes')):
            for name in files:
                copy_file(name, self.root_dir, self.repodir)
            if files:
                self.repo.commit(files, commit_msg)

    def git_upgraded_pkgs(self):
        """Update the repository with installed or upgraded packages."""

        self.scan_cachedir()
        rslt = self.results
        if rslt.pkg_add_etc:
            self.repo.commit(rslt.pkg_add_etc,
                             'Add or upgrade files extracted from a package')

        cherry_pick_commit = None
        if rslt.cherry_pick:
            self.repo.commit(rslt.cherry_pick,
                     'Merge with upgraded package files not copied to /etc')
            cherry_pick_commit = self.repo.git_cmd('rev-list -1 HEAD --')

        # Clean the working area.
        self.repo.git_cmd('clean -d -x -f')

        # Update the master-tmp branch with new files.
        if rslt.pkg_add_master:
            self.repo.checkout('master-tmp')
            for fname in rslt.pkg_add_master:
                repo_file = os.path.join(self.repodir, fname)
                if os.path.lexists(repo_file):
                    warn('adding %s to the master-tmp branch but this file'
                         ' already exists' % fname)
                copy_file(fname, self.root_dir, self.repodir,
                          repo_file=repo_file)
            self.repo.commit(rslt.pkg_add_master,
                         'Add files after scanning new or upgraded packages')

        return cherry_pick_commit

    def new_packages(self, cache_dir):
        """Return the packages newer than the timestamp."""
        timestamp = self.timestamp.value('FAST-FORWARD')
        exclude_pkgs_len = len(self.exclude_pkgs)
        packages = {}
        for root, *remain in os.walk(cache_dir):
            with os.scandir(root) as it:
                for pkg in it:
                    pkg_name = pkg.name
                    if not pkg_name.endswith('.pkg.tar.xz'):
                        continue
                    # We use st_ctime for the following reasons:
                    # * When downloading a package, pacman sets its access and
                    #   last modification time to the modification time of the
                    #   remote (see curl_download_internal() and utimes_long()
                    #   at git://projects.archlinux.org/pacman.git).
                    # * GNU libc documentation explains that "The attribute
                    #   modification time for the file is set to the current
                    #   time [by utime()] (since changing the time stamps is
                    #   itself a modification of the file attributes)".
                    if pkg.stat().st_ctime <= timestamp:
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
            if cache_dir == self.cache_dir:
                break
        return packages.values()

    def scan(self, packages, tracked):
        def etc_files_filter(members):
            for tinfo in members:
                fname = tinfo.name
                if (fname.startswith(ROOT_SUBDIR) and
                        (tinfo.isfile() or tinfo.issym() or tinfo.islnk()) and
                        fname not in self.exclude_files):
                    yield tinfo

        def extract_from(pkg):
            tar = tarfile.open(pkg.path, mode='r:xz', debug=1)
            tarinfos = list(etc_files_filter(tar.getmembers()))
            for tinfo in tarinfos:
                path = EtcPath(self.root_dir, self.repodir, tinfo.name)
                # Remember the sha1 of the existing file, if it exists, before
                # extracting it from the tarball.
                digest = path.digest
                extracted[tinfo.name] = path
            tar.extractall(self.repodir, members=tarinfos)
            print('scanned', pkg.name)

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
        for fname in extracted:
            if fname not in tracked:
                # Ensure that the file can be overwritten on a next
                # 'update' command.
                path = os.path.join(self.repodir, fname)
                mode = os.lstat(path).st_mode
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
        packages = self.new_packages(self.cache_dir)
        if self.aur_dir is not None:
            packages = itertools.chain(packages,
                                       self.new_packages(self.aur_dir))
        extracted = self.scan(packages, etc_tracked)

        rslt = self.results
        for fname in extracted:
            repo_path = EtcPath(self.root_dir, self.repodir, fname)
            etc_path = EtcPath(self.root_dir, self.root_dir, fname)
            if etc_path.digest == b'':
                warn('skip %s: not readable' % fname)
                continue

            # A new package install.
            if fname not in etc_tracked:
                rslt.pkg_add_etc.append(fname)
                if etc_path != repo_path:
                    rslt.pkg_add_master.append(fname)
            # A package upgrade.
            else:
                previous_dgst_fname = extracted[fname]
                if etc_path == repo_path:
                    if extracted[fname] != repo_path:
                        rslt.pkg_add_etc.append(fname)
                        if fname in master_tracked:
                            warn('%s exists in the master branch' % fname)
                else:
                    if extracted[fname] != repo_path:
                        rslt.cherry_pick.append(fname)
                        if fname not in master_tracked:
                            warn('%s does not exist in the master branch' %
                                 fname)

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
            parser.add_argument('--verbose', '-v', help='Print the output of'
                ' the git commands (default: %(default)s)',
                action='store_true', default=False)
            parser.add_argument('--cache-dir', help='Set pacman cache'
                ' directory (override the /etc/pacman.conf setting of the'
                ' CacheDir option)', type=isdir)
            parser.add_argument('--aur-dir', help='Set the path of the '
                'directory where to look recursively for built AUR packages',
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
    # Assign the parsed args to the EtcMaint instance.
    emt = EtcMaint()
    parse_args(argv, emt)

    # Run the command.
    if emt.command == 'dispatch_help':
        func = getattr(sys.modules[__name__], 'dispatch_help')
        func(emt)
    else:
        if emt.command != 'cmd_sync' and os.getuid() == 0:
            raise EmtError('cannot be executed as a root user')
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
