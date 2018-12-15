Developer's guide
=================

Debugging a test case
---------------------

Set ``debug`` to ``True`` in etcmaint/tests/test_commands.py and run only the
test to debug::

  python -m unittest -k test_to_debug

This enables two features:

* The name of the temporary directory used for the location of the etcmaint
  repository is printed and the directory is not removed at the end of the
  test so that its content may be examined.

* ``print()`` statements may be inserted in the test or in ``etcmaint.py``
  itself and their output is printed.

.. vim:sts=2:sw=2:tw=78
