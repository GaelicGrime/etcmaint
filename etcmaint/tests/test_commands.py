"""etcmaint tests."""

import sys
import os
import io
import tempfile
import tarfile
import time
from argparse import ArgumentError
from contextlib import contextmanager, ExitStack
from textwrap import dedent
from collections import namedtuple
from unittest import mock, TestCase

from etcmaint.etcmaint import (ETCMAINT_BRANCHES, change_cwd, etcmaint,
                               ROOT_SUBDIR, EtcPath, EmtError, EtcMaint)

ROOT_DIR = 'root'
REPO_DIR = 'repo'
CACHE_DIR = 'cache'
AUR_DIR = 'aur'
ROOT_SUBDIR_LEN = len(ROOT_SUBDIR)

# Set debug to True and:
#   * Print on stderr the stdout and stderr output of etcmaint.
#   * Do not remove the temporary directories where the tests are run.
debug = 0

@contextmanager
def temp_cwd():
    """Context manager that temporarily creates and changes the CWD."""
    with tempfile.TemporaryDirectory() as temp_path:
        with change_cwd(temp_path) as cwd_dir:
            yield cwd_dir

@contextmanager
def captured_output():
    _stdout = getattr(sys, 'stdout')
    _stderr = getattr(sys, 'stderr')
    strio = io.StringIO()
    setattr(sys, 'stdout', strio)
    setattr(sys, 'stderr', strio)
    try:
        yield strio, _stdout, _stderr
    finally:
        setattr(sys, 'stdout', _stdout)
        setattr(sys, 'stdout', _stderr)

def raise_context_of_exit(func, *args, **kwds):
    try:
        func(*args, **kwds)
    except SystemExit as e:
        e = e.__context__ if isinstance(e.__context__, Exception) else e
        raise e from None

SymLink = namedtuple('SymLink', ['linkto', 'abspath'])

class Command():
    """Helper to build an etcmaint command."""

    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.cache_dir = os.path.join(self.tmpdir, CACHE_DIR)
        self.root_dir = os.path.join(self.tmpdir, ROOT_DIR)

    def add_files(self, files, dir_path=''):
        """'files' dictionary of file names mapped to content or SymLink."""
        for fname in files:
            path = os.path.join(dir_path, ROOT_SUBDIR, fname)
            dirname = os.path.dirname(path)
            if not os.path.isdir(dirname):
                os.makedirs(dirname)
            val = files[fname]
            if isinstance(val, SymLink):
                linkto = val.linkto
                if val.abspath:
                    linkto =  os.path.join(self.root_dir, ROOT_SUBDIR, linkto)
                os.symlink(linkto, path)
            else:
                with open(path, 'w') as f:
                    f.write(val)

    def add_etc_files(self, files):
        self.add_files(files, self.root_dir)

    def add_package(self, name, files, version='1.0', release='1',
                    cache_dir=None, delta_mtime=0):
        """Add a package."""
        cache_dir = self.cache_dir if cache_dir is None else cache_dir
        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir)
        pkg_name = os.path.join(cache_dir, '%s-%s-%s-%s.pkg.tar.xz' %
                                (name, version, release, os.uname().machine))
        with temp_cwd():
            self.add_files(files)
            with tarfile.open(pkg_name, 'w|xz') as tar:
                tar.add(ROOT_SUBDIR)
        # Update the package modification and access times.
        if delta_mtime:
            st = os.stat(pkg_name)
            atime = mtime = st.st_mtime + delta_mtime
            os.utime(pkg_name, (atime, mtime))
        return pkg_name

    def etc_abspath(self, fname):
        return os.path.join(self.root_dir, ROOT_SUBDIR, fname)

    def remove_etc_file(self, fname):
        os.unlink(self.etc_abspath(fname))

    def run(self, command, *args, with_rootdir=True):
        argv = ['etcmaint', command]
        if command in ('create', 'update'):
            argv.extend(['--cache-dir', self.cache_dir])
        if with_rootdir:
            argv.extend(['--root-dir', self.root_dir])
        argv.extend(args)
        return etcmaint(argv)

class BaseTestCase(TestCase):
    """The base class of all TestCase classes."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stdout, self._stdout, self._stderr = self.stack.enter_context(
                                                          captured_output())
        if not debug:
            self.tmpdir = self.stack.enter_context(temp_cwd())
        else:
            self.tmpdir = tempfile.TemporaryDirectory().name
            os.makedirs(self.tmpdir)
            os.chdir(self.tmpdir)
            print('The temporary test directory %s must be removed manually' %
                  self.tmpdir, file =self._stderr)

        self.cmd = Command(self.tmpdir)

    def run_cmd(self, command, *args, with_rootdir=True):
        self.stdout.seek(0)
        self.stdout.truncate(0)
        try:
            self.emt = self.cmd.run(command, *args, with_rootdir=with_rootdir)
        finally:
            if debug:
                out = self.stdout.getvalue()
                if out:
                    print(out, file=self._stderr)

    def check_output(self, equal=None, is_in=None, is_notin=None):
        out = self.stdout.getvalue()
        if equal is not None:
            self.assertEqual(equal, out)
        if is_in is not None:
            self.assertIn(is_in, out)
        if is_notin is not None:
            self.assertNotIn(is_notin, out)

class CommandLineTestCase(BaseTestCase):
    """Test the command line."""

    def setUp(self):
        super().setUp()
        os.environ['XDG_DATA_HOME'] = os.path.join(self.tmpdir, REPO_DIR)

    def make_base_dirs(self):
        os.makedirs(os.path.join(self.tmpdir, ROOT_DIR, ROOT_SUBDIR))
        os.makedirs(os.path.join(self.tmpdir, CACHE_DIR))

    def test_cl_pacman_conf(self):
        # Check that CacheDir may be parsed in /etc/pacman.conf.
        emt = EtcMaint()
        emt.root_dir = '/'
        emt.cache_dir = None
        emt.init()
        self.assertEqual(os.path.isdir(emt.cache_dir), True)

    def test_cl_main_help(self):
        self.make_base_dirs()
        self.run_cmd('help', with_rootdir=False)
        self.assertIn('An Arch Linux tool based on git for the maintenance'
                      ' of /etc files.', self.stdout.getvalue())

    def test_cl_create_help(self):
        self.make_base_dirs()
        self.run_cmd('help', 'create', with_rootdir=False)
        self.assertIn('Create the git repository', self.stdout.getvalue())

    def test_cl_not_a_dir(self):
        # Check that ROOT_DIR exists.
        with self.assertRaisesRegex(ArgumentError,
                                    '--root-dir.*not a directory'):
            raise_context_of_exit(self.run_cmd, 'diff')

    def test_cl_no_repo(self):
        # Check that the repository exists.
        self.make_base_dirs()
        with self.assertRaisesRegex(EmtError, 'no git repository'):
            raise_context_of_exit(self.run_cmd, 'diff')

    def test_cl_invalid_command(self):
        self.make_base_dirs()
        with self.assertRaisesRegex(ArgumentError, 'invalid choice'):
            raise_context_of_exit(self.run_cmd, 'foo', with_rootdir=False)

class CommandsTestCase(BaseTestCase):
    """Test the etcmaint commands."""

    def setUp(self):
        super().setUp()
        pre_patch = mock.patch('etcmaint.etcmaint.repository_dir',
                           return_value=os.path.join(self.tmpdir, REPO_DIR))
        self.stack.enter_context(pre_patch)

    def check_results(self, master, etc, branches=None):
        def list_files(branch):
            return [f[ROOT_SUBDIR_LEN+1:] for f in
                    sorted(self.emt.repo.tracked_files(branch).keys())]

        self.assertEqual(list_files('master'), master)
        self.assertEqual(list_files('etc'), etc)
        if branches is not None:
            self.assertEqual(sorted(self.emt.repo.branches), branches)

    def check_content(self, branch, fname, expected):
        content = self.emt.repo.git_cmd('show %s:%s' %
                                (branch, os.path.join(ROOT_SUBDIR, fname)))
        self.assertEqual(content, expected)

    def check_status(self, expected):
        self.assertEqual(self.emt.repo.get_status(), expected)

    def check_curbranch(self, expected):
        self.assertEqual(self.emt.repo.curbranch, expected)

    def add_repo_file(self, branch, fname, content, commit_msg):
        self.emt.repo.checkout(branch)
        os.makedirs(os.path.join(self.tmpdir, REPO_DIR, ROOT_SUBDIR))
        self.emt.repo.add_files({os.path.join(ROOT_SUBDIR, fname): content},
                               commit_msg)

class CreateTestCase(CommandsTestCase):
    def test_create_plain(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'a', 'content')

    def test_create_aur_package(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        files = {'b': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('aur package', files,
                             cache_dir=os.path.join(self.tmpdir, AUR_DIR))
        self.run_cmd('create', '--aur-dir', AUR_DIR)
        self.check_results([], ['a', 'b'])

    def test_create_symlink_abspath(self):
        files = {'a': 'content', 'b': SymLink('a', True)}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a', 'b'])
        self.check_content('etc', 'b', self.cmd.etc_abspath('a'))

    def test_create_symlink_relpath(self):
        files = {'a': 'content', 'b': SymLink('a', False)}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a', 'b'])
        self.check_content('etc', 'b', 'a')

    def test_create_package_and_etc_differ(self):
        # 'b' in /etc and package differ and is added to the master branch.
        files = {'a': 'content', 'b': 'content'}
        self.cmd.add_etc_files(files)
        files['b'] = 'new content'
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results(['b'], ['a', 'b'])
        self.check_content('master', 'b', 'content')
        self.check_content('etc', 'a', 'content')
        self.check_content('etc', 'b', 'new content')

    def test_create_not_exists_in_package(self):
        # 'b' /etc file, non-existent in package, is not added to the etc
        # branch.
        files = {'a': 'content', 'b': 'content'}
        self.cmd.add_etc_files(files)
        del files['b']
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

    def test_create_newest_package(self):
        # Check that the newest package file is used.
        files = {'a': 'newest release X'}
        self.cmd.add_package('package', files, release='X')

        files['a'] = 'oldest release Y'
        self.cmd.add_etc_files(files)
        # Create the package in the past.
        pkg_a = self.cmd.add_package('package', files, release='Y',
                                     delta_mtime=-3600)

        self.run_cmd('create')
        self.assertNotIn('package-1.0-Y',
                        ('-'.join(p.rsplit('-', maxsplit=3)[:3]) for
                        p in self.emt.results.new_packages))
        self.check_results(['a'], ['a'])
        # The oldest release file is in master: it is the content of the /etc
        # file which differs from the content of the newest release (and
        # pacman would have written a pacnew file).
        self.check_content('master', 'a', 'oldest release Y')
        self.check_content('etc', 'a', 'newest release X')

    def test_create_exclude_packages(self):
        files = {'a': 'a content', 'b': 'b content', 'c': 'c content'}
        self.cmd.add_etc_files(files)
        pkg_a = self.cmd.add_package('a_package', {'a': 'a content'})
        pkg_b = self.cmd.add_package('b_package', {'b': 'b content'})
        pkg_c = self.cmd.add_package('c_package', {'c': 'c content'})
        self.run_cmd('create', '--exclude-pkgs', 'foo, b_, bar')
        self.check_results([], ['a', 'c'])
        out = self.stdout.getvalue()
        self.assertIn('scanned %s' % os.path.basename(pkg_a), out)
        self.assertNotIn('scanned %s' % os.path.basename(pkg_b), out)
        self.assertIn('scanned %s' % os.path.basename(pkg_c), out)

    def test_create_exclude_files(self):
        files = {'a': 'a content', 'b': 'b content', 'bbb': 'bbb content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create', '--exclude-files', 'foo, b, bar')
        self.check_results([], ['a', 'bbb'])

class UpdateSyncTestCase(CommandsTestCase):
    def simple_cherry_pick(self):
        content = ['line %d' % n for n in range(5)]
        user_content = content[:]; user_content[0] = 'user line 0'
        self.cmd.add_etc_files({'a': '\n'.join(user_content)})
        self.cmd.add_package('package_a', {'a': '\n'.join(content)})
        self.run_cmd('create')
        self.check_results(['a'], ['a'])

        # A cherry-pick occurs.
        package_content = content[:]; package_content[3] = 'package line 3'
        self.cmd.add_package('package_a', {'a': '\n'.join(package_content)})
        self.run_cmd('update')

    def check_simple_cherry_pick(self, branch, branches):
        self.check_results(['a'], ['a'], branches)
        self.check_content(branch, 'a', dedent("""\
                                               user line 0
                                               line 1
                                               line 2
                                               package line 3
                                               line 4"""))

    def test_update_plain(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

        files = {'a': 'new content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('update')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'a', 'new content')

    def test_update_etc_removed(self):
        # Remove 'b' /etc file and it is removed from the etc branch on
        # 'update'.
        files = {'a': 'content', 'b': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package_a', {'a': 'content'})
        self.cmd.add_package('package_b', {'b': 'content'})
        self.run_cmd('create')
        self.check_results([], ['a', 'b'])

        self.cmd.remove_etc_file('b')
        self.run_cmd('update')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

    def test_update_package_and_etc_differ_removed(self):
        # Remove 'a' /etc file and it is removed from the etc branch on
        # 'update', and removed from the master branch.
        files = {'a': 'content', 'b': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package_a', {'a': 'new content'})
        self.cmd.add_package('package_b', {'b': 'content'})
        self.run_cmd('create')
        self.check_results(['a'], ['a', 'b'])
        self.check_content('master', 'a', 'content')
        self.check_content('etc', 'a', 'new content')

        self.cmd.remove_etc_file('a')
        self.run_cmd('update')
        self.check_results([], ['b'])

    def test_update_with_upgraded_package_no_etc_change(self):
        # Check that a new released package, with no change in the /etc files,
        # does not add new files to the etc branch.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

        # Ensure that package 'X' does not have the same st_mtime.
        self.cmd.add_package('package', files, release='X', delta_mtime=1)
        self.run_cmd('update')
        self.check_results([], ['a'])
        self.assertFalse(self.emt.results.pkg_add_etc)

    def test_update_with_new_package(self):
        self.cmd.add_etc_files({'a': 'content'})
        self.cmd.add_package('package_a', {'a': 'content'})
        self.run_cmd('create')
        self.check_results([], ['a'])

        self.cmd.add_etc_files({'b': 'content'})
        self.cmd.add_package('package_b', {'b': 'content'})
        self.run_cmd('update')
        self.check_results([], ['a', 'b'])

    def test_update_old_package(self):
        # Check that old packages are not scanned on the next update.
        files = {'a': 'a content'}
        self.cmd.add_etc_files(files)
        pkg_a = self.cmd.add_package('package_a', files)
        self.run_cmd('create')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'a', 'a content')

        files = {'b': 'b content'}
        self.cmd.add_etc_files(files)
        pkg_b = self.cmd.add_package('package_b', files)
        self.run_cmd('update')
        self.check_results([], ['a', 'b'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'b', 'b content')
        self.check_output(is_in='scanned %s' % os.path.basename(pkg_b),
                           is_notin='scanned %s' % os.path.basename(pkg_a))

    def test_update_dry_run(self):
        # Check that two consecutive updates in dry-run mode give the same
        # output.
        self.cmd.add_etc_files({'a': 'content'})
        self.cmd.add_package('package_a', {'a': 'content'})
        self.run_cmd('create')
        self.check_results([], ['a'])

        self.cmd.add_etc_files({'b': 'content'})
        self.cmd.add_package('package_b', {'b': 'content'})
        out = []
        for n in range(2):
            self.stdout.seek(0)
            self.stdout.truncate(0)
            self.run_cmd('update', '--dry-run')
            out.append(self.stdout.getvalue())
        self.assertEqual(out[0], out[1])

    def test_update_symlink(self):
        # 'a' /etc file is changed to a symlink.
        files = {'a': 'content', 'b': 'content'}
        self.cmd.add_etc_files(files)
        files['a'] = 'package content'
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results(['a'], ['a', 'b'])

        self.cmd.remove_etc_file('a')
        self.cmd.add_etc_files({'a': SymLink('b', True)})
        self.run_cmd('update')
        self.check_results(['a'], ['a', 'b'])
        self.check_content('etc', 'a', 'package content')
        self.check_content('master', 'a', self.cmd.etc_abspath('b'))

    def test_update_user_customize(self):
        # File customized by user is added to the master branch upon 'update'.
        self.cmd.add_etc_files({'a': 'content'})
        self.cmd.add_package('package_a', {'a': 'content'})
        self.run_cmd('create')
        self.check_results([], ['a'])

        self.cmd.add_etc_files({'a': 'new user content'})
        self.run_cmd('update')
        self.check_results(['a'], ['a'])
        self.check_content('master', 'a', 'new user content')
        self.check_content('etc', 'a', 'content')

    def test_update_user_update_customized(self):
        # File customized by user and updated by user.
        self.cmd.add_etc_files({'a': 'user content'})
        self.cmd.add_package('package_a', {'a': 'package content'})
        self.run_cmd('create')
        self.check_results(['a'], ['a'])
        self.check_content('master', 'a', 'user content')
        self.check_content('etc', 'a', 'package content')

        self.cmd.add_etc_files({'a': 'new user content'})
        self.run_cmd('update')
        self.check_content('master', 'a', 'new user content')

    def test_update_user_add(self):
        # 'b' file not from a package, manually added to master and updated by
        # the user.
        self.cmd.add_etc_files({'a': 'content'})
        self.cmd.add_package('package_a', {'a': 'content'})
        self.run_cmd('create')
        self.check_results([], ['a'])

        self.cmd.add_etc_files({'b': 'content'})
        self.add_repo_file('master', 'b', 'content', 'commit msg')
        self.check_content('master', 'b', 'content')
        self.cmd.add_etc_files({'b': 'new content'})
        self.run_cmd('update')
        self.check_results(['b'], ['a'])
        self.check_content('master', 'b', 'new content')

    def test_update_cherry_pick(self):
        # File cherry-picked by git.
        self.simple_cherry_pick()
        self.check_simple_cherry_pick('master-tmp', ETCMAINT_BRANCHES)

    def test_update_cherry_pick_update(self):
        # Check that an update following an update with a cherry-pick, gives
        # the same result.
        self.simple_cherry_pick()
        self.run_cmd('update')
        self.check_simple_cherry_pick('master-tmp', ETCMAINT_BRANCHES)

    def test_update_cherry_pick_dry_run(self):
        # File cherry-picked by git in dry-run mode: no changes.
        content = ['line %d' % n for n in range(5)]
        user_content = content[:]; user_content[0] = 'user line 0'
        self.cmd.add_etc_files({'a': '\n'.join(user_content)})
        self.cmd.add_package('package_a', {'a': '\n'.join(content)})
        self.run_cmd('create')
        self.check_results(['a'], ['a'])

        package_content = content[:]; package_content[3] = 'package line 3'
        self.cmd.add_package('package_a', {'a': '\n'.join(package_content)})
        self.run_cmd('update', '--dry-run')
        self.check_results(['a'], ['a'], ['etc', 'master', 'timestamps'])

    def test_update_plain_conflict(self):
        # A plain conflict: a package upgrades the content of a user
        # customized file.
        self.cmd.add_etc_files({'a': 'user content'})
        self.cmd.add_package('package_a', {'a': 'package content'})
        self.run_cmd('create')
        self.check_results(['a'], ['a'])
        self.check_content('master', 'a', 'user content')
        self.check_content('etc', 'a', 'package content')

        self.cmd.add_package('package_a', {'a': 'new package content'})
        self.run_cmd('update')
        self.check_results(['a'], ['a'], ETCMAINT_BRANCHES)
        self.check_curbranch('master-tmp')
        self.check_status(['UU %s/a' % ROOT_SUBDIR])

    def test_update_conflict(self):
        # A conflict: the file is customized by the user and the package
        # upgrades its content at the same time.
        self.cmd.add_etc_files({'a': 'content'})
        self.cmd.add_package('package_a', {'a': 'content'})
        self.run_cmd('create')
        self.check_results([], ['a'])

        self.cmd.add_etc_files({'a': 'new user content'})
        self.cmd.add_package('package_a', {'a': 'new package content'})
        self.run_cmd('update')
        self.check_results([], ['a'], ETCMAINT_BRANCHES)
        self.check_curbranch('master-tmp')
        self.check_status(['UU %s/a' % ROOT_SUBDIR])

    def test_update_new_package(self):
        # Check that a package is updated with a new release.
        files = {'a': 'initial content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files, release='X')
        self.run_cmd('create')

        files['a'] = 'new content'
        self.cmd.add_etc_files(files)
        pkg_a = self.cmd.add_package('package', files, release='Y',
                                     delta_mtime=1)
        self.run_cmd('update')
        self.assertNotIn('package-1.0-X',
                        ('-'.join(p.rsplit('-', maxsplit=3)[:3]) for
                        p in self.emt.results.new_packages))
        self.check_results([], ['a'])
        self.check_content('etc', 'a', 'new content')

    def test_plain_sync(self):
        # Sync after a git cherry-pick.
        self.simple_cherry_pick()
        self.run_cmd('sync')
        self.check_simple_cherry_pick('master',
                                      ['etc', 'master', 'timestamps'])
        fname = os.path.join(ROOT_SUBDIR, 'a')
        self.assertEqual(EtcPath(self.tmpdir, REPO_DIR, fname),
                         EtcPath(self.tmpdir, ROOT_DIR, fname))

    def test_previous_tag(self):
        # Check the '<branch>-prev' git tag.
        self.simple_cherry_pick()
        self.run_cmd('sync')
        out = self.emt.repo.git_cmd('diff master-prev...master')
        self.assertIn('-line 3\n+package line 3', out)

    def test_sync_unresolved_conflict(self):
        # Sync after a git cherry-pick.
        self.test_update_conflict()
        with self.assertRaisesRegex(EmtError, 'repository is not clean'):
            self.run_cmd('sync')

    def test_sync_dry_run(self):
        # Sync after a git cherry-pick in dry-run mode.
        self.simple_cherry_pick()
        self.run_cmd('sync', '--dry-run')
        self.check_simple_cherry_pick('master-tmp', ETCMAINT_BRANCHES)

    def test_sync_timestamp(self):
        # Check that a package added after a cherry-pick and before a sync is
        # not ignored on the next update.
        self.simple_cherry_pick()

        time.sleep(1)
        files = {'b': 'b content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package_b', files)

        self.run_cmd('sync')

        self.run_cmd('update')
        self.check_results(['a'], ['a', 'b'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'b', 'b content')

    def test_sync_no_cherry_pick(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

        self.emt.repo.checkout('master-tmp', create=True)
        with self.assertRaisesRegex(EmtError,
                          'cannot find a cherry-pick in master-tmp branch'):
            self.run_cmd('sync')

class DiffTestCase(CommandsTestCase):
    def test_diff(self):
        files = {f: 'content of %s' % f for f in ('a', 'b', 'c')}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', {'a': 'package content'})
        self.run_cmd('create')
        self.check_results(['a'], ['a'], ['etc', 'master', 'timestamps'])
        self.check_content('master', 'a', 'content of a')
        self.check_content('etc', 'a', 'package content')

        self.run_cmd('diff')
        self.check_output(is_in='\n'.join(os.path.join(ROOT_SUBDIR, x)
                               for x in ['b', 'c']))

    def test_diff_exclude_prefixes(self):
        files = {f: 'content of %s' % f for f in
                 ('%s_file' % n for n in ('a', 'b', 'c'))}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', {'a_file': 'package content'})
        self.run_cmd('create')
        self.check_results(['a_file'], ['a_file'], ['etc', 'master', 'timestamps'])
        self.check_content('master', 'a_file', 'content of a_file')
        self.check_content('etc', 'a_file', 'package content')

        self.run_cmd('diff', '--exclude-prefixes', 'foo, b_, bar')
        self.check_output(is_in=os.path.join(ROOT_SUBDIR, 'c_file'),
                               is_notin=os.path.join(ROOT_SUBDIR, 'b_file'))

    def test_diff_use_etc_tmp_no_tmp(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')

        self.run_cmd('diff', '--use-etc-tmp')
        self.assertIn('The etc-tmp branch does not exist',
                      self.stdout.getvalue())

    def test_diff_use_etc_tmp(self):
        # File cherry-picked by git.
        content = ['line %d' % n for n in range(5)]
        a_content = '\n'.join(content)
        self.cmd.add_etc_files({'a': a_content})
        self.cmd.add_package('package_a', {'a': a_content})
        self.run_cmd('create')
        self.check_results([], ['a'])

        user_content = content[:]; user_content[0] = 'user line 0'
        self.cmd.add_etc_files({'a': '\n'.join(user_content)})
        package_content = content[:]; package_content[3] = 'package line 3'
        self.cmd.add_package('package_a', {'a': '\n'.join(package_content)})
        self.cmd.add_etc_files({'b': 'b content'})
        self.cmd.add_package('package_b', {'b': 'b content'})
        self.run_cmd('update')
        self.check_results([], ['a'], ETCMAINT_BRANCHES)
        self.run_cmd('diff')
        self.check_output(is_in=os.path.join(ROOT_SUBDIR, 'b'))
        self.run_cmd('diff', '--use-etc-tmp')
        self.check_output(equal='\n')
