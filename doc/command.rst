etcmaint
========

Synopsis
--------

**etcmaint** [*--version*] {*help,create,update,sync,diff*} [*options*]

Description
-----------

:program:`etcmaint` uses Git to merge the /etc configuration files installed by
`pacman`_ that have been customized.

Run :program:`etcmaint help <command>` to get help on a subcommand.

Subcommands
-----------

create
^^^^^^

Description
"""""""""""

:program:`etcmaint create` creates the Git repository and populates the etc
and master branches.

The Git repository is located at $XDG_DATA_HOME/etcmaint if the XDG_DATA_HOME
environment variable is set and at $HOME/.local/share/etcmaint otherwise.

The 'diff' subcommand may be used now to list the files added to /etc by the
user. If any of those files is added (and commited) to the 'master' branch,
the 'update' subcommand will track future changes made to those files in /etc
and include these changes to the 'master' branch.

Options
"""""""

.. program:: etcmaint create

.. option:: --cache-dir CACHE_DIR

   Set pacman cache directory (override the /etc/pacman.conf setting of the
   CacheDir option)

.. option:: --aur-dir AUR_DIR

   Set the path of the root of the directory tree where to look for built AUR
   packages

.. option:: --exclude-pkgs PFXS

   A comma separated list of prefix of package names to be ignored
   (default: "")

.. option:: --exclude-files FILES

   A comma separated list of /etc path names to be ignored (default: "passwd,
   group, mtab, udev/hwdb.bin")

.. option:: --root-dir ROOT_DIR

   Set the root directory of the etc files, mostly used for testing
   (default: "/")

update
^^^^^^

Description
"""""""""""

:program:`etcmaint update` updates the repository with packages and user
changes.

The changes are made in temporary branches named 'master-tmp' and 'etc-tmp'.
When those changes do not incur a cherry-pick, the 'master-tmp' (resp.  'etc-
tmp') branch is merged as a fast-forward into its main branch and the
temporary branches deleted. The operation is then complete and the changes can
be examined with the Git diff command run on the differences between the Git
tag set at the previous 'update' command, named '<branch name>-prev', and the
branch itself. For example, to list the names of the files that have been
changed in the master branch::

  $ git diff --name-only master-prev...master

Otherwise the fast-forwarding is postponed until the 'sync' command is run and
until then it is still possible to start over with a new 'update' command, the
previous temporary branches being discarded in that case. To examine the
changes that will be merged into each branch by the 'sync' command, use the
Git diff command run on the differences between the branch itself and the
corresponding temporary branch. For example, to list all the changes that will
be made by the 'sync' command to the master branch::

  $ git diff master...master-tmp

Options
"""""""

.. program:: etcmaint update

The ``update`` subcommand options are the same as the ``create`` subcommand
options plus the ``dry-run`` option.

.. option:: --dry-run, -n

   Perform a trial run with no changes made (default: False)

sync
^^^^

Description
"""""""""""

:program:`etcmaint sync` synchronizes /etc with changes made by the previous
update command.

To print the changes that are going to be made to /etc by the 'sync' command,
first print the list of files that will be copied::

  $ etcmaint sync --dry-run

Then for each file in the list, run the following git command where 'rpath' is
the relative path name as output by the previous command and that starts with
'etc/'::

  $ git diff master...master-tmp -- rpath

This command must be run as root when using the --root-dir default value.

Options
"""""""

.. program:: etcmaint sync

.. option:: --dry-run, -n

   Perform a trial run with no changes made (default: False)

.. option:: --exclude-files FILES

   A comma separated list of /etc path names to be ignored (default: "passwd,
   group, mtab, udev/hwdb.bin")

.. option:: --root-dir ROOT_DIR

   Set the root directory of the etc files, mostly used for testing
   (default: "/")

diff
^^^^

Description
"""""""""""

:program:`etcmaint diff` prints the list of the /etc files not tracked in the
etc branch.

These are the /etc files not extracted from an Arch Linux package. Among them
and of interest are the files created by a user that one may want to manually
add and commit to the 'master' branch of the etcmaint repository so that their
changes start being tracked by etcmaint (for example the netctl configuration
files).

pacnew, pacsave and pacorig files are excluded from this list.

Options
"""""""

.. program:: etcmaint diff

.. option:: --exclude-prefixes PFXS

   A comma separated list of prefixes of /etc path names to be ignored
   (default: "ca-certificates, ssl/certs")

.. option:: --use-etc-tmp

   Use the etc-tmp branch instead (default: False)

.. option:: --root-dir ROOT_DIR

   Set the root directory of the etc files, mostly used for testing
   (default: "/")

.. _`pacman`: https://www.archlinux.org/pacman/pacman.8.html

Environment Variables
---------------------

The :program:`etcmaint` program refers to the following environment variables:

.. describe:: XDG_DATA_HOME

   A path to the parent of the ``etcmaint`` directory that holds the etcmaint
   repository.

.. vim:sts=2:sw=2:tw=78
