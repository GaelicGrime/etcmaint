**etcmaint [--version] {help,create,diff,sync,update} ...**

An Arch Linux tool based on git for the maintenance of /etc files.

* The /etc files installed or upgraded by ``pacman`` are managed in the
  ``etc`` branch of the git repository.
* The changes of user-customized files made after a pacman upgrade are merged
  (actually cherry-picked) from the ``etc`` branch into the ``master`` branch
  with the ``update`` command. Merge conflicts detected by git are resolved
  and commited by the user.
* After a merge, the ``sync`` command is used to retrofit the changes to /etc.
* The /etc files created by the user and manually added to the ``master``
  branch are managed by etcmaint. The ensuing changes are tracked by the
  ``update`` command.

Run ``etcmaint help <command>`` to get help on a command.

Notes:

* etcmaint does not use the ``pacnew`` files and relies on the time stamps of
  the pacman files in ``cachedir`` (see CacheDir in the pacman.conf
  documentation) to detect when a package has been upgraded.  Therefore one
  should not run the ``create`` or ``update`` commands after pacman has been
  run with ``--downloadonly``. However it is safe to empty the pacman
  ``cachedir`` once the ``create`` or ``update`` command has been run.
* etcmaint does not handle the files or symlinks created in /etc by pacman
  post-install and post_upgrade steps.
