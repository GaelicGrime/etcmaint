"""etcmaint tests."""

import sys
import os
import io
import stat
import tempfile
import time
import shutil
import unittest
from argparse import ArgumentError
from contextlib import contextmanager, ExitStack
from textwrap import dedent
from collections import namedtuple
from unittest import TestCase, skipIf
from unittest.mock import patch

from etcmaint.etcmaint import (ETCMAINT_BRANCHES, change_cwd, etcmaint,
                               ROOT_SUBDIR, EtcPath, EmtError, EtcMaint,
                               tarfile_open)

EXTENSION = 'zst'
ROOT_DIR = 'root'
REPO_DIR = 'repo'
CACHE_DIR = 'cache'
AUR_DIR = 'aur'
ROOT_SUBDIR_LEN = len(ROOT_SUBDIR)
PACMAN_CONF = '/etc/pacman.conf'

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
        setattr(sys, 'stderr', _stderr)
        strio.close()

# Index of fields returned by os.stat_result().
ST_UID = ST_GID = None

@contextmanager
def os_stat_as_root():
    def stat(path, **kwds):
        # Members of the StructSequence returned by os.stat are readonly, so
        # we must build a new one.
        global ST_UID, ST_GID
        st = _stat(path, **kwds)
        if ST_UID is None:
            # Find the index of both fields when the StructSequence is used as
            # a sequence by parsing the string representation.
            st_str = str(st)
            for fieldname in ('st_uid', 'st_gid'):
                idx = st_str[:st_str.index(fieldname)].count('=')
                if fieldname == 'st_uid':
                    ST_UID = idx
                else:
                    ST_GID = idx
        st_list = list(st)
        st_list[ST_UID] = 0
        st_list[ST_GID] = 0
        return os.stat_result(st_list)

    _stat = getattr(os, 'stat')
    setattr(os, 'stat', stat)
    try:
        yield
    finally:
        setattr(os, 'stat', _stat)

def raise_context_of_exit(func, *args, **kwds):
    try:
        func(*args, **kwds)
    except SystemExit as e:
        e = e.__context__ if isinstance(e.__context__, Exception) else e
        raise e from None

_has_setpriv = None
def skip_unless_setpriv(test):
    """Skip decorator for tests that require setpriv"""
    global _has_setpriv
    if _has_setpriv is None:
        _has_setpriv = True if shutil.which('setpriv') else False
    msg = "Requires functional setpriv implementation"
    return test if _has_setpriv else unittest.skip(msg)(test)

SymLink = namedtuple('SymLink', ['linkto', 'abspath'])

class Command():
    """Helper to build an etcmaint command.

    'relative_time' is used to ensure that the modification and access times
    of packages increment with each addition of a package.
    """

    relative_time = 0

    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.cache_dir = os.path.join(self.tmpdir, CACHE_DIR)
        self.root_dir = os.path.join(self.tmpdir, ROOT_DIR)

    def add_files(self, files, or_modes={}, and_modes={}, dir_path=''):
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
                if os.path.lexists(path):
                    os.unlink(path)
                os.symlink(linkto, path)
            else:
                with open(path, 'w') as f:
                    f.write(val)

            if fname in or_modes:
                st_mode = os.stat(path).st_mode
                os.chmod(path, st_mode | or_modes[fname])

            if fname in and_modes:
                st_mode = os.stat(path).st_mode
                os.chmod(path, st_mode & and_modes[fname])

    def add_etc_files(self, files, or_modes={}, and_modes={}):
        self.add_files(files, or_modes=or_modes, and_modes=and_modes,
                       dir_path=self.root_dir)

    def add_package(self, name, files, or_modes={}, and_modes={},
                version='1.0', release='1', cache_dir=None, delta_mtime=None):
        """Add a package."""
        cache_dir = self.cache_dir if cache_dir is None else cache_dir
        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir)
        pkg_name = os.path.join(cache_dir, '%s-%s-%s-%s.pkg.tar.%s' %
                    (name, version, release, os.uname().machine, EXTENSION))
        with temp_cwd():
            self.add_files(files, or_modes=or_modes, and_modes=and_modes)
            with tarfile_open(pkg_name, EXTENSION, mode='w') as tar:
                tar.add(ROOT_SUBDIR)
        # Update the package modification and access times.
        if delta_mtime is None:
            delta_mtime = Command.relative_time
            Command.relative_time += 1
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
        self.mkdtemp()

    def mkdtemp(self):
        if not debug:
            self.tmpdir = self.stack.enter_context(temp_cwd())
        else:
            self.tmpdir = tempfile.mkdtemp()
            os.chdir(self.tmpdir)
            print('The temporary test directory %s must be removed manually' %
                  self.tmpdir, file =self._stderr)
        self.cmd = Command(self.tmpdir)

    def run_cmd(self, command, *args, with_rootdir=True, clear_stdout=True):
        try:
            self.emt = self.cmd.run(command, *args, with_rootdir=with_rootdir)
        finally:
            if debug:
                out = self.stdout.getvalue()
                if out:
                    print(out, file=self._stderr)
            if clear_stdout:
                self.clear_stdout()

    def clear_stdout(self):
        self.stdout.seek(0)
        self.stdout.truncate(0)

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

    def make_base_dirs(self):
        os.makedirs(os.path.join(self.tmpdir, ROOT_DIR, ROOT_SUBDIR))
        os.makedirs(os.path.join(self.tmpdir, CACHE_DIR))

    @skipIf(not os.path.exists(PACMAN_CONF), '%s does not exist' % PACMAN_CONF)
    def test_cl_pacman_conf(self):
        # Check that CacheDir may be parsed in /etc/pacman.conf.
        with io.StringIO() as results:
            emt = EtcMaint(results)
            emt.root_dir = '/'
            emt.cache_dir = None
            emt.init()
            self.assertEqual(os.path.isdir(emt.cache_dir), True)

    def test_cl_main_help(self):
        self.run_cmd('help', with_rootdir=False, clear_stdout=False)
        self.assertIn('An Arch Linux tool based on git for the maintenance'
                      ' of /etc files.', self.stdout.getvalue())

    def test_cl_main_help_debug(self):
        global debug
        try:
            debug = 1
            _stderr = self._stderr
            self._stderr = io.StringIO()
            self.mkdtemp()
            print('debug info')
            self.test_cl_main_help()
        finally:
            debug = 0
            out = self._stderr.getvalue()
            self._stderr.close()
            self._stderr = _stderr
            shutil.rmtree(self.tmpdir)
        self.assertIn('debug info', out)
        self.assertIn('temporary test directory %s must be removed' %
                      self.tmpdir, out)
        self.assertIn('An Arch Linux tool based on git for the maintenance'
                  ' of /etc files.', out)

    def test_cl_create_help(self):
        self.run_cmd('help', 'create', with_rootdir=False, clear_stdout=False)
        self.assertIn('Create the git repository', self.stdout.getvalue())

    def test_cl_not_a_dir(self):
        # Check that ROOT_DIR exists.
        with self.assertRaisesRegex(ArgumentError,
                                    '--root-dir.*not a directory'):
            raise_context_of_exit(self.run_cmd, 'diff')

    def test_cl_repository_dir(self):
        with patch('os.getlogin', return_value='root'):
            from etcmaint.etcmaint import repository_dir
            self.assertEqual(repository_dir(), '/root/.local/share/etcmaint')

    def test_cl_repository_dir_XDG_DATA_HOME(self):
        with patch.dict('os.environ', values={'XDG_DATA_HOME': '/tmp'}):
            from etcmaint.etcmaint import repository_dir
            self.assertEqual(repository_dir(), '/tmp/etcmaint')

    def test_cl_no_command(self):
        import etcmaint.etcmaint
        try:
            _argv = getattr(sys, 'argv')
            setattr(sys, 'argv', ['etcmaint'])
            with self.assertRaisesRegex(SystemExit, '2'):
                etcmaint.etcmaint.main()
        finally:
            setattr(sys, 'argv', _argv)
        self.assertIn('a command is required', self.stdout.getvalue())

    def test_cl_no_repo(self):
        # Check that the repository exists.
        self.make_base_dirs()
        with patch('etcmaint.etcmaint.repository_dir',
                           return_value=os.path.join(self.tmpdir, REPO_DIR)):
            with self.assertRaisesRegex(EmtError, 'no git repository'):
                self.run_cmd('diff')

    def test_cl_no_repo_using_main(self):
        # Check that the repository exists.
        self.make_base_dirs()
        with patch('etcmaint.etcmaint.repository_dir',
                           return_value=os.path.join(self.tmpdir, REPO_DIR)):
            import etcmaint.etcmaint
            try:
                _argv = getattr(sys, 'argv')
                setattr(sys, 'argv', ['etcmaint', 'diff'])
                with self.assertRaisesRegex(EmtError, 'no git repository'):
                    raise_context_of_exit(etcmaint.etcmaint.main)
            finally:
                setattr(sys, 'argv', _argv)

    def test_cl_invalid_command(self):
        with self.assertRaisesRegex(ArgumentError, 'invalid choice'):
            raise_context_of_exit(self.run_cmd, 'foo', with_rootdir=False)

class CommandsTestCase(BaseTestCase):
    """Test the etcmaint commands."""

    def setUp(self):
        super().setUp()
        pre_patch = patch('etcmaint.etcmaint.repository_dir',
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

    def check_is_symlink(self, branch, fname):
        rpath = os.path.join(ROOT_SUBDIR, fname)
        tree = self.emt.repo.git_cmd('ls-tree -r %s' % branch)
        for line in tree.splitlines():
            mode, type, object, file = line.split()
            if file == rpath:
                self.assertEqual(mode[:3], '120')
                break
        else:
            self.fail('%s not found by git ls-tree' % rpath)

    def check_status(self, expected):
        self.assertEqual(self.emt.repo.get_status(), expected)

    def check_curbranch(self, expected):
        self.assertEqual(self.emt.repo.curbranch, expected)

    def add_repo_file(self, branch, fname, content, commit_msg):
        self.emt.repo.checkout(branch)
        os.makedirs(os.path.join(self.tmpdir, REPO_DIR, ROOT_SUBDIR))
        self.emt.repo.add_files({os.path.join(ROOT_SUBDIR, fname): content},
                               commit_msg)

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

class CreateTestCase(CommandsTestCase):
    def test_create_plain(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'a', 'content')

    def test_create_aur_package(self):
        # The AUR package is located in a subdirectory of 'aur-dir'.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        files = {'b': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('aur package', files,
                     cache_dir=os.path.join(self.tmpdir, AUR_DIR, 'subdir'))
        self.run_cmd('create', '--aur-dir', AUR_DIR)
        self.check_results([], ['a', 'b'])

    def test_create_not_readable(self):
        files = {'a': 'a content', 'b': 'b content'}
        self.cmd.add_etc_files(files)
        path = os.path.join(self.cmd.root_dir, ROOT_SUBDIR, 'b')
        os.chmod(path, 0)
        self.cmd.add_package('package', files)
        self.run_cmd('create', clear_stdout=False)
        self.check_results([], ['a'])
        out = self.stdout.getvalue()
        self.assertIn('skip %s not readable' % path, out)

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
        def create_package(release, add_etc=False, delta_mtime=0):
            files = {'a': 'release %s' % release}
            if add_etc:
                self.cmd.add_etc_files(files)
            self.cmd.add_package('package', files, release=release,
                                 delta_mtime=delta_mtime)

        create_package('X')
        # Create the package in the past.
        create_package('Y', add_etc=True, delta_mtime=-3600)
        self.run_cmd('create')
        self.assertNotIn('package-1.0-Y',
                        ('-'.join(p.rsplit('-', maxsplit=3)[:3]) for
                        p in self.emt.new_packages))
        self.check_results(['a'], ['a'])
        # The oldest release file is in master: it is the content of the /etc
        # file which differs from the content of the newest release (and
        # pacman would have written a pacnew file).
        self.check_content('master', 'a', 'release Y')
        self.check_content('etc', 'a', 'release X')

        # Remove previous packages and check that an old package is not
        # updated.
        cachedir = os.path.join(self.tmpdir, CACHE_DIR)
        shutil.rmtree(cachedir)
        os.makedirs(cachedir)
        create_package('Z', delta_mtime=-3600)
        self.run_cmd('update')
        self.assertFalse(hasattr(self.emt, 'new_packages'))

    def test_create_exclude_packages(self):
        files = {'a': 'a content', 'b': 'b content', 'c': 'c content'}
        self.cmd.add_etc_files(files)
        pkg_a = self.cmd.add_package('a_package', {'a': 'a content'})
        pkg_b = self.cmd.add_package('b_package', {'b': 'b content'})
        pkg_c = self.cmd.add_package('c_package', {'c': 'c content'})
        self.run_cmd('create', '--exclude-pkgs', 'foo, b_, bar',
                     clear_stdout=False)
        self.check_results([], ['a', 'c'])
        out = self.stdout.getvalue()
        self.assertIn(os.path.basename(pkg_a), out)
        self.assertNotIn(os.path.basename(pkg_b), out)
        self.assertIn(os.path.basename(pkg_c), out)

    def test_create_exclude_files(self):
        files = {'a': 'a content', 'b': 'b content', 'bbb': 'bbb content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create', '--exclude-files', 'foo, b, bar')
        self.check_results([], ['a', 'bbb'])

    def test_create_repo_not_empty(self):
        repo_dir = os.path.join(self.tmpdir, REPO_DIR)
        os.makedirs(os.path.join(repo_dir, 'some_dir'))
        # Create the cache and root directories.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        with self.assertRaisesRegex(EmtError, '%s is not empty' % repo_dir):
            self.run_cmd('create')

class UpdateTestCase(CommandsTestCase):
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

    def test_update_file_mode(self):
        # Check that a file is updated in the etc branch when its mode has
        # been modified as user executable in a new package.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_content('etc', 'a', 'content')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

        or_modes = {'a': stat.S_IXUSR}
        self.cmd.add_etc_files(files, or_modes=or_modes)
        self.cmd.add_package('package', files, or_modes=or_modes)
        self.run_cmd('update', clear_stdout=False)
        self.check_output(is_in='Commits in the etc branch')
        self.check_output(is_in='Files added or updated from new package'
                                ' versions')
        self.check_output(is_in=os.path.join(ROOT_SUBDIR, 'a'))

    def test_readonly_file(self):
        # Issue #15. Check that a file is not updated as a user change when
        # it is readonly.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_content('etc', 'a', 'content')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

        readonly = {'a': ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)}
        self.cmd.add_etc_files(files, and_modes=readonly)
        self.cmd.add_package('package', files, and_modes=readonly)
        self.run_cmd('update')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

    def test_not_readable_file(self):
        # Issue #16. No crash after an /etc file has been made not readable.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_content('etc', 'a', 'content')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

        not_readable = {'a': ~(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)}
        self.cmd.add_etc_files(files, and_modes=not_readable)
        self.run_cmd('update')
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

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

    def test_update_master_removed(self):
        # An /etc file is created by the user and manually commited to master.
        # Check that it is removed from master after it has been removed from
        # /etc.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

        fname = 'deleted etc file'
        self.add_repo_file('master', fname, 'content', 'some commit msg')
        self.run_cmd('update')
        self.assertIn(os.path.join(ROOT_SUBDIR, fname),
                                   self.emt.master_commits.removed.rpaths)

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

        self.cmd.add_package('package', files, release='X')
        self.run_cmd('update')
        self.check_results([], ['a'])
        self.assertFalse(self.emt.etc_commits.added.rpaths)

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

        self.run_cmd('update', clear_stdout=False)
        self.check_results([], ['a', 'b'], ['etc', 'master', 'timestamps'])
        self.check_content('etc', 'b', 'b content')
        self.check_output(is_in=os.path.basename(pkg_b),
                           is_notin=os.path.basename(pkg_a))

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

    def test_update_user_symlink(self):
        # 'a' /etc file is changed to a symlink by the user.
        files = {'a': 'content', 'b': 'content'}
        self.cmd.add_etc_files(files)
        files['a'] = 'package content'
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results(['a'], ['a', 'b'])

        self.cmd.add_etc_files({'a': SymLink('b', True)})
        self.run_cmd('update')
        self.check_results(['a'], ['a', 'b'])
        self.check_content('etc', 'a', 'package content')
        self.check_content('master', 'a', self.cmd.etc_abspath('b'))

    def test_update_upgrade_symlink(self):
        # Issue #9.
        # 'a' /etc file is changed to a symlink by an upgrade.
        files = {'a': 'a content', 'b': 'b content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a', 'b'])

        files['a'] = SymLink('b', False)
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('update')
        self.check_results([], ['a', 'b'])
        self.check_content('etc', 'a', 'b')
        self.check_is_symlink('etc', 'a')

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

    def test_update_cherry_pick_no_master(self):
        # Check an upgrade with a cherry-pick when there is no corresponding
        # file in master. This happens after a user change in the /etc file
        # and the next upgrade causes a cherry-pick.
        content = ['line %d' % n for n in range(5)]
        files = {'a': '\n'.join(content)}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

        etc_content = content[:]; etc_content[0] = '/etc line 0'
        self.cmd.add_etc_files({'a': '\n'.join(etc_content)})

        pkg_content = content[:]; pkg_content[4] = 'package line 4'
        self.cmd.add_package('package', {'a': '\n'.join(pkg_content)})
        self.run_cmd('update')
        self.check_results([], ['a'], ETCMAINT_BRANCHES)
        self.check_content('master-tmp', 'a', dedent("""\
                                               /etc line 0
                                               line 1
                                               line 2
                                               line 3
                                               package line 4"""))

    def test_update_new_package(self):
        # Check that a package is updated with a new release.
        files = {'a': 'initial content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files, release='X')
        self.run_cmd('create')

        files['a'] = 'new content'
        self.cmd.add_etc_files(files)
        pkg_a = self.cmd.add_package('package', files, release='Y')
        self.run_cmd('update')
        self.assertNotIn('package-1.0-X',
                        ('-'.join(p.rsplit('-', maxsplit=3)[:3]) for
                        p in self.emt.new_packages))
        self.check_results([], ['a'])
        self.check_content('etc', 'a', 'new content')

    def test_update_removed_after_upgrade(self):
        # Issue #8
        # A file is upgraded by a new package version and deleted from /etc
        # before the update command.
        files = {'a': 'a initial content'}
        files['b'] = 'b initial content'
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files, release='X')
        self.run_cmd('create')

        files['a'] = 'a new content'
        files['b'] = 'b new content'
        self.cmd.add_package('package', files, release='Y')
        self.cmd.remove_etc_file('b')
        self.run_cmd('update')

    def test_update_tracked_changes(self):
        # Issue #10.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        files['a'] = 'package content'
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results(['a'], ['a'])

        files['a'] = 'changed content in tracked file'
        self.cmd.add_files(files, dir_path=self.emt.repodir)
        with self.assertRaisesRegex(EmtError, "Run 'git reset --hard'"):
            self.run_cmd('update')

    def test_update_untracked_changes(self):
        # Issue #10.
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

        files = {'b': 'content of untracked file'}
        self.cmd.add_files(files, dir_path=self.emt.repodir)
        with self.assertRaisesRegex(EmtError, "Run 'git clean -d -x -f'"):
            self.run_cmd('update')

    def test_update_as_root_owned_by_root(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

        files = {'a': 'new content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        with os_stat_as_root(), patch('os.geteuid', return_value=0):
            self.run_cmd('update')
        self.check_results([], ['a'])
        self.check_content('etc', 'a', 'new content')

    @skipIf(os.geteuid() == 0, "non-root user required")
    @skip_unless_setpriv
    def test_update_as_root_not_owned_by_root(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')
        self.check_results([], ['a'])

        files = {'a': 'new content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        with self.assertRaisesRegex(EmtError, 'cannot be executed as root'):
            with patch('os.geteuid', return_value=0):
                self.run_cmd('update')

    def test_update_not_etcmaint_repo(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        try:
            import etcmaint.etcmaint
            _fist_commit = etcmaint.etcmaint.FIRST_COMMIT_MSG
            etcmaint.etcmaint.FIRST_COMMIT_MSG = 'not an etcmaint repository'
            self.run_cmd('create')
        finally:
            etcmaint.etcmaint.FIRST_COMMIT_MSG = _fist_commit
        self.check_results([], ['a'], ['etc', 'master', 'timestamps'])

        with self.assertRaisesRegex(EmtError,
                                    'this is not an etcmaint repository'):
            self.run_cmd('update')

    def test_empty_cherry_pick(self):
        # Check that if a commit being cherry picked duplicates a 'user
        # changes' commit already in the current history of the master-tmp
        # branch, then the commit is empty.
        content = ['line %d' % n for n in range(5)]
        user_content = content[:]; user_content[0] = 'user line 0'
        self.cmd.add_etc_files({'a': '\n'.join(user_content)})
        self.cmd.add_package('package_a', {'a': '\n'.join(content)})
        self.run_cmd('create')
        self.check_results(['a'], ['a'])

        user_content[3] = 'package line 3'
        self.cmd.add_etc_files({'a': '\n'.join(user_content)})
        content[3] = 'package line 3'
        self.cmd.add_package('package_a', {'a': '\n'.join(content)})
        self.run_cmd('update', clear_stdout=False)
        self.check_results(['a'], ['a'])
        self.check_content('master-tmp', 'a', dedent("""\
                                               user line 0
                                               line 1
                                               line 2
                                               package line 3
                                               line 4"""))
        self.check_output(is_in='empty commit')
        self.assertEqual('',
                         self.emt.repo.git_cmd('diff-tree --no-commit-id'
                                               ' --name-only -r master-tmp'))

class SyncTestCase(CommandsTestCase):
    def test_plain_sync(self):
        # Sync after a git cherry-pick.
        self.simple_cherry_pick()
        self.run_cmd('sync')
        self.check_simple_cherry_pick('master',
                                      ['etc', 'master', 'timestamps'])
        rpath = os.path.join(ROOT_SUBDIR, 'a')
        self.assertEqual(EtcPath(REPO_DIR, rpath), EtcPath(ROOT_DIR, rpath))

    def test_previous_tag(self):
        # Check the '<branch>-prev' git tag.
        self.simple_cherry_pick()
        self.run_cmd('sync')
        out = self.emt.repo.git_cmd('diff master-prev...master')
        self.assertIn('-line 3\n+package line 3', out)

    def update_conflict(self):
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

    def test_sync_unresolved_conflict(self):
        # Sync after a git cherry-pick.
        self.update_conflict()
        with self.assertRaisesRegex(EmtError, 'repository is not clean'):
            self.run_cmd('sync')

    def test_sync_resolved_conflict(self):
        # Sync after a resolved conflict.
        self.update_conflict()
        with open(os.path.join(self.emt.repodir, ROOT_SUBDIR, 'a')) as f:
            content = f.read()
        self.assertIn(dedent("""\
            <<<<<<< HEAD
            new user content
            =======
            new package content
            >>>>>>>"""), content)
        # Resove the conflict and commit the change.
        self.emt.repo.add_files(
            {os.path.join(ROOT_SUBDIR, 'a'):
                'after conflict resolution'}, 'Resolve the conflict')
        self.run_cmd('sync')
        self.check_results(['a'], ['a'], ['etc', 'master', 'timestamps'])
        self.check_content('master', 'a', 'after conflict resolution')

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

        self.emt.repo.checkout('master')
        self.emt.repo.checkout('master-tmp', create=True)
        self.emt.repo.checkout('etc')
        self.emt.repo.checkout('etc-tmp', create=True)
        with self.assertRaisesRegex(EmtError,
                          'cannot find a cherry-pick in the etc-tmp branch'):
            self.run_cmd('sync')

    @skipIf(os.geteuid() == 0, "non-root user required")
    @skip_unless_setpriv
    def test_plain_sync_as_root_not_owned_by_root(self):
        # Check that the sync command succeeds when run as root and the
        # repository is owned by a non-root user.
        self.simple_cherry_pick()
        with patch('os.geteuid', return_value=0):
            self.run_cmd('sync')
        self.check_simple_cherry_pick('master',
                                      ['etc', 'master', 'timestamps'])
        rpath = os.path.join(ROOT_SUBDIR, 'a')
        self.assertEqual(EtcPath(REPO_DIR, rpath), EtcPath(ROOT_DIR, rpath))

    def test_plain_sync_fforward_failure(self):
        self.simple_cherry_pick()
        self.emt.repo.checkout('master')
        files = {'a': 'content'}
        self.emt.repo.add_files(files, 'commit message')
        with self.assertRaisesRegex(EmtError, 'cannot fast-forward'):
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

        self.run_cmd('diff', clear_stdout=False)
        self.check_output(is_in='\n'.join(os.path.join(ROOT_SUBDIR, x)
                               for x in ['b', 'c']),
                               is_notin=os.path.join(ROOT_SUBDIR, 'a'))

    def test_diff_exclude_suffixes(self):
        files = {f: 'content of %s' % f for f in ('a', 'b', 'c')}
        files['b.pacnew'] = 'content of b.pacnew'
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', {'a': 'package content'})
        self.run_cmd('create')
        self.check_results(['a'], ['a'], ['etc', 'master', 'timestamps'])

        self.run_cmd('diff', clear_stdout=False)
        self.check_output(is_in='\n'.join(os.path.join(ROOT_SUBDIR, x)
                               for x in ['b', 'c']),
                               is_notin=os.path.join(ROOT_SUBDIR, 'b.pacnew'))

    def test_diff_exclude_prefixes(self):
        files = {f: 'content of %s' % f for f in
                 ('%s_file' % n for n in ('a', 'b', 'c'))}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', {'a_file': 'package content'})
        self.run_cmd('create')
        self.check_results(['a_file'], ['a_file'], ['etc', 'master', 'timestamps'])
        self.check_content('master', 'a_file', 'content of a_file')
        self.check_content('etc', 'a_file', 'package content')

        self.run_cmd('diff', '--exclude-prefixes', 'foo, b_, bar',
                     clear_stdout=False)
        self.check_output(is_in=os.path.join(ROOT_SUBDIR, 'c_file'),
                               is_notin=os.path.join(ROOT_SUBDIR, 'b_file'))

    def test_diff_use_etc_tmp_no_tmp(self):
        files = {'a': 'content'}
        self.cmd.add_etc_files(files)
        self.cmd.add_package('package', files)
        self.run_cmd('create')

        self.run_cmd('diff', '--use-etc-tmp', clear_stdout=False)
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

        self.run_cmd('diff', clear_stdout=False)
        self.check_output(is_in=os.path.join(ROOT_SUBDIR, 'b'))
        self.clear_stdout()

        self.run_cmd('diff', '--use-etc-tmp', clear_stdout=False)
        self.check_output(equal='\n')
