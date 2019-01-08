Git conflict resolution
=======================

This section demonstrates with an example how to resolve a Git conflict with
Vim.

Initial setup
-------------

* Customize the bash prompt by sourcing the ``git-prompt.sh`` script in
  ~/.bashrc.  The script is located in /usr/share/git/completion/ as well as
  the corresponding scripts for the other types of shell. The prompt will
  show:

    + The current branch name.
    + Whether there is any modified file (marked with ``*``) or any staged
      file (marked with ``+``).
    + When a conflict resolution session is being run.

* Configure Git with gvim as the text editor for use by Git commands::

    $ git config --global core.editor 'gvim -f'

* Configure Git with gvim as the merge tool::

    $ git config --global --add merge.tool vim-mergetool
    $ git config --global --add mergetool.keepBackup  false
    $ git config --global --add mergetool.vim-mergetool.trustExitCode true
    $ git config --global --add mergetool.vim-mergetool.cmd \
        'gvim -f -d -M -c "set modifiable write noreadonly" $MERGED $LOCAL $REMOTE $BASE'

.. _conflict-resolution:

Conflict resolution
-------------------

.. note::

   etcmaint uses the term *merge* in its documentation while it is a
   *cherry-pick* that the implementation is actually doing.

In this example the /etc/shorewall/shorewall.conf file from the shorewall
package has been modified by the user (``STARTUP_ENABLED`` has been set to
``Yes``) and a new version of the package adds a new comment line. The diff
of both files is shown below::

  $ diff $XDG_DATA_HOME/cachedir/etc/shorewall/shorewall.conf $XDG_DATA_HOME/root/etc/shorewall/shorewall.conf
  11,12c11,12
  < # Package shorewall-5.2.2.1-1
  < STARTUP_ENABLED=No
  ---
  >
  > STARTUP_ENABLED=Yes

After the new version of the shorewall package has been upgraded with pacman,
the ``etcmaint update`` command detects the conflict as shown in the top half
of the bash session captured in the following image.

.. image:: _static/conflict_1.png

This bash session shows also the six commands used next to resolve the
conflict. The next paragraphs detail these commands.

Run mergetool
^^^^^^^^^^^^^

The first command changes the current working directory to the etcmaint
repository. The prompt shows then that we are on the ``master-tmp`` branch and
``*+|CHERRY-PICKING`` tells us we are about to resolve conflicts and that
there are modified and staged files.

To know which files are in conflicts we run the second command ``git status
-s``, that confirms that etc/shorewall/shorewall.conf is ``unmerged``. See the
**Short Format** section of the `git-status man page`_ for the description of
the output of this command.

The next command ``git mergetool`` starts an instance of gvim for each file
that is in conflict (here we only have one).

.. image:: _static/conflict_2.png

The ``git mergetool`` command starts gvim instance(s) with four buffers. The
leftmost one is the file to be merged in the ``master-tmp`` branch, i.e. the
result of the merge. This is the only buffer that is not readonly.

Git has inserted the ``<<< === >>>`` conflict markers at each location in the
file where there is a conflict.

The other three buffers are from left to right:

* The ``LOCAL`` buffer, the previous content of the file on the current
  branch.
* The ``REMOTE`` buffer, the content of the file on the ``etc-tmp`` branch.
* The ``BASE`` buffer, the content of the file on the common ancestor of both
  branches which is only really useful for Git merges and not for
  cherry-picks.

At that time we may decide to abandon the conflict resolution of this file by
entering the ``:cquit`` Vim command to tell Git that the merge has failed. We
could decide then:

* either to abort the cherry-pick with ``git cherry-pick --abort`` and start
  over from scratch a new etcmaint session with the ``update`` etcmaint
  command,
* or to make another attempt at resolving the conflict by running again the
  mergetool git subcommand.

Resolve the conflict
^^^^^^^^^^^^^^^^^^^^

In the following image we are about to resolve the conflict after having
removed the conflicts markers and having kept the lines from both branches in
the leftmost buffer and by saving the result with the ``:wqa`` Vim command.

.. image:: _static/conflict_3.png

Commit the cherry-pick
^^^^^^^^^^^^^^^^^^^^^^

The next ``git status -s`` command confirms that the changes in this file are
ready to be commited and this is done with the ``git cherry-pick --continue``
command.  This last command spawns the editor to allow us, if necessary, to
edit the commit message which is the original message of the commit in the
``etc-tmp`` branch we are cherry-picking from.

Note that it is safe to run cherry-pick with ``--continue`` even when missing
the fact that there are still conflicts pending, the command fails with an
explicit error message in that case.

.. image:: _static/conflict_4.png

The ``update`` etcmaint command is now fully completed as confirmed by the
output of the last command ``git status -s``. And the etcmaint session can be
finalized by running the etcmaint ``sync`` command to copy the merged files to
/etc.

.. _`git-status man page`: https://git-scm.com/docs/git-status

.. vim:sts=2:sw=2:tw=78
