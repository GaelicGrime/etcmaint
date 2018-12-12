======
Design
======

* etcmaint does not use the ``pacnew`` files and relies on the time stamps of
  the pacman files in ``cachedir`` (see the ``CacheDir`` global configuration
  in `pacman.conf`_) to detect when a package has been upgraded.  Therefore one
  should not run the etcmaint ``create`` or the ``update`` command after
  ``pacman`` has been run with ``--downloadonly``. As a side note, it is safe
  to empty the pacman ``cachedir`` or any part of it, once the etcmaint command
  has been run.
* etcmaint does not handle the files or symlinks created in the /etc directory
  by ``pacman`` post-install and post_upgrade steps.


.. _`pacman.conf`: https://www.archlinux.org/pacman/pacman.conf.5.html
