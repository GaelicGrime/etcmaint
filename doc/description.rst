Description
===========

etcmaint session
----------------

An etcmaint session is started with the ``update`` subcommand. This session
ends in the following cases:

* No /etc configuration file has been merged by Git. The session ends when the
  ``update`` subcommand terminates.

* At least one /etc configuration file has been merged by Git and there has
  been no merge conflict. The merged files are copied back to /etc with the
  ``sync`` subcommand and this ends the session.

* There is at least one merge conflict. The ``update`` subcommand terminates
  and leaves the Git repository in a pending merge conflict state. The user
  must then resolve the conflict (see an example of a conflict resolution in
  :ref:`conflict-resolution`) and must end the etcmaint session with the
  ``sync`` subcommand as above.

  When a merge conflict is aborted by the user, the partial work done by the
  current session **must** be discarded by starting a new session with a new
  ``update`` subcommand.

* A new ``update`` subcommand is run while the previous session has not been
  terminated. In that case the changes made in the previous session are
  discarded. The ``master-tmp`` branch and the other temporary branches are
  deleted.

.. note::

   The merge is actually a cherry-pick since only one of the commits made in
   the ``etc-tmp`` branch needs to be merged. Anyway, Git cherry-pick
   conflicts are handled in the same way and with the same tools as merge
   conflicts and it is simpler here to use consistently the term 'merge'.

Git branches and tags
---------------------

Branches
^^^^^^^^

* The ``master`` branch of the Git repository tracks the /etc files that are
  customized by the user.

* The ``etc`` branch tracks the /etc files installed or upgraded by `pacman`_.

* After a pacman upgrade, changes introduced to the files in the ``etc-tmp``
  branch that are also files customized by the user in the /etc directory, are
  merged from the ``etc-tmp`` branch into the ``master-tmp`` branch with the
  etcmaint ``update`` subcommand:

  + Conflicts arising during the merge are resolved by the user.
  + All the changes made by the ``update`` subcommand are done in the
    ``master-tmp`` and ``etc-tmp`` temporary branches.
  + The etcmaint ``sync`` subcommand is used next to retrofit those changes to
    the /etc directory and to merge the temporary branches into their main
    counterparts thus finalizing the ``update`` subcommand (i.e.  the
    ``update`` subcommand is not effective until the ``sync`` subcommand has
    completed). The temporary branches are then deleted.

* The /etc files created by the user and manually added to the ``master``
  branch are managed by etcmaint and the ensuing changes to those files are
  automatically tracked by the etcmaint ``update`` subcommand.

* The ``timestamps`` branch is used internally by etcmaint to track the
  modification time of the installed package files.

Tags
^^^^

A tag is set on the ``master`` and ``etc`` branches at the last commit of the
previous session. The name of the tag is composed of the name of the
corresponding branch suffixed with ``-prev``. For example, to see the last
changes made in ``master``::

  git diff master-prev...master

To list the names of the files that have been changed::

  git diff --name-only master-prev...master

Caveats:
--------

* etcmaint does not use the ``pacnew`` files and relies on the time stamps of
  the pacman files in ``cachedir`` (see the ``CacheDir`` global configuration
  in `pacman.conf`_) to detect when a package has been upgraded.  Therefore
  one should not run the etcmaint ``create`` or the ``update`` subcommand
  after ``pacman`` has been run with ``--downloadonly``.

  As a side note, it is safe to empty the pacman ``cachedir`` or any part of
  it, once the ``update`` subcommand has been run.

* etcmaint does not handle the files or symlinks created in the /etc directory
  by `pacman`_ post-install and post_upgrade steps.

.. _`pacman`: https://www.archlinux.org/pacman/pacman.8.html
.. _`pacman.conf`: https://www.archlinux.org/pacman/pacman.conf.5.html

.. vim:sts=2:sw=2:tw=78
