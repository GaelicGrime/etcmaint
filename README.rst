An Arch Linux tool based on git for the maintenance of /etc files.

* The ``master`` branch of the git repository tracks the /etc files that are
  customized by the user.
* /etc files installed or upgraded by ``pacman`` are tracked in the ``etc``
  branch.
* After a pacman upgrade, changes introduced to the files in the ``etc`` branch
  that are also files customized by the user in the /etc directory, are
  cherry-picked from the ``etc`` branch into the ``master`` branch with the
  etcmaint ``update`` subcommand:

  + Conflicts arising during the cherry-pick must be resolved and commited by
    the user.
  + All the changes made by the ``update`` subcommand are done in the
    ``master-tmp`` and ``etc-tmp`` temporary branches.
  + The etcmaint ``sync`` subcommand is used next to retrofit those changes to
    the /etc directory and to merge the temporary branches into their main
    counterparts thus finalizing the ``update`` subcommand (i.e.  the
    ``update`` subcommand is not effective until the ``sync`` subcommand has
    completed).

* The /etc files created by the user and manually added to the ``master``
  branch are managed by etcmaint and the ensuing changes to those files are
  automatically tracked by the etcmaint ``update`` subcommand.

The project is hosted on `GitLab`_.

The documentation is on the project `GitLab Pages`_.

.. _`GitLab`: https://gitlab.com/xdegaye/etcmaint
.. _`GitLab Pages`: https://xdegaye.gitlab.io/etcmaint/
