**etcmaint [--version] {help,create,diff,sync,update} ...**

An Arch Linux tool based on git for the maintenance of /etc files.

* The ``master`` branch of the git repository tracks the /etc files that are
  customized by the user.
* /etc files installed or upgraded by ``pacman`` are tracked in the ``etc``
  branch.
* After a pacman upgrade, changes introduced to the files in the ``etc`` branch
  that are also files customized by the user in the /etc directory, are
  cherry-picked from the ``etc`` branch into the ``master`` branch with the
  etcmaint ``update`` command:

  + Conflicts arising during the cherry-pick must be resolved and commited by
    the user.
  + All the changes made by the ``update`` command are done in the
    ``master-tmp`` and ``etc-tmp`` temporary branches.
  + The etcmaint ``sync`` command is used next to retrofit those changes to the
    /etc directory and to merge the temporary branches into their main
    counterparts thus finalizing the ``update`` command (i.e.  the ``update``
    command is not effective until the ``sync`` command has completed).

* The /etc files created by the user and manually added to the ``master``
  branch are managed by etcmaint and the ensuing changes to those files are
  automatically tracked by the etcmaint ``update`` command.

Run ``etcmaint help <command>`` to get help on a command.

Notes:

* etcmaint does not use the ``pacnew`` files and relies on the time stamps of
  the pacman files in ``cachedir`` (see CacheDir in the pacman.conf
  documentation) to detect when a package has been upgraded.  Therefore one
  should not run the etcmaint ``create`` or the ``update`` command after
  ``pacman`` has been run with ``--downloadonly``. As a side note, it is safe
  to empty the pacman ``cachedir`` or part of it, once the etcmaint command
  has been run.
* etcmaint does not handle the files or symlinks created in the /etc directory
  by ``pacman`` post-install and post_upgrade steps.
