**etcmaint** uses Git to merge the /etc configuration files installed by
`pacman`_ that have been customized.

* The customized configuration files in /etc are detected by etcmaint and
  tracked in the ``master`` branch of the Git repository. See `Handling Config
  Files`_ for when such a detection occurs. After a `pacman`_ upgrade,
  etcmaint uses Git to merge the customized configuration files with the
  changes introduced by the upgrade.

* The configuration files in /etc that are not installed by `pacman`_, for
  example netctl profiles, can be manually committed to the ``master`` branch.
  Changes made to those files will then be also tracked by etcmaint.

* etcmaint uses a ``master-tmp`` temporary branch, that stems from ``master``,
  to commit all the changes made during a session. This temporary branch is
  merged back into ``master`` only when the session is finalized, that is when
  the customized configuration files that have been merged by Git are copied
  back to /etc.

* The changes in the current etcmaint session before finalization can be
  printed with::

    $ git diff master...master-tmp

Install
-------

Install etcmaint from `PyPi`_::

  $ python -m pip install etcmaint

Usage
-----

::

  $ etcmaint [--version] {help,create,update,sync,diff} [options]

Documentation at `GitLab Pages`_.

.. _pacman: https://www.archlinux.org/pacman/pacman.8.html
.. _`Handling Config Files`: https://www.archlinux.org/pacman/pacman.8.html#_handling_config_files_a_id_hcf_a
.. _PyPi: https://pypi.org/project/etcmaint/
.. _`GitLab Pages`: https://xdegaye.gitlab.io/etcmaint/


.. vim:sts=2:sw=2:tw=78
