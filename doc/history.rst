Release history
===============

Version 0.7
-----------

* Support the z-standard compression scheme.

Version 0.6
-----------

* Fix a crash when an /etc file becomes unreadable by ``user`` after a pacman
  upgrade.

Version 0.5
-----------

* An /etc file whose mode has changed is considered as modified only after a
  change in the executable bit of the file mode.
* Handle /etc files in non searchable directories.

Version 0.4
-----------

* Fix a crash when a file mode is modified in an upgraded package.

Version 0.3
-----------

* First release on PyPi.
