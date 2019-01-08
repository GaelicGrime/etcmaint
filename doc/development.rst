Development
===========

Install from source
-------------------

Clone the repository::

  $ git clone https://gitlab.com/xdegaye/etcmaint

Install `flit`_ from PyPI::

  $ python -m pip install flit

Install etcmaint locally with `flit`_ by running the following command at the
root of the etcmaint source and so that one can test changes without
reinstalling etcmaint::

  $ flit install --symlink

This will install etcmaint at ~/.local/lib/python3.7/site-packages if the
current python version is 3.7.

When running the sync subcommand with sudo, the existing user environment must
be preserved so that python may find the location where etcmaint has been
installed. So one must run::

  $ sudo -E etcmaint sync

Run the test suite
------------------

Run the full test suite in verbose mode::

  $ python -m unittest -v

Run a single test named ``test_example``::

  $ python -m unittest -k test_example

Debug a test case
-----------------

Set ``debug`` to ``True`` in etcmaint/tests/test_commands.py and run only the
test to debug::

  $ python -m unittest -k test_to_debug

This enables two features:

* The name of the temporary directory used for the location of the etcmaint
  repository is printed and the directory is not removed at the end of the
  test so that its content may be examined.

* ``print()`` statements may be inserted in the test or in ``etcmaint.py``
  itself and their output is printed.

Build the documentation
-----------------------

Install the Arch linux ``python-sphinx`` package.

Build the html documentation at doc/_build/html and the man pages at
doc/_build/man::

  $ sphinx-build -b html doc doc/_build/html
  $ sphinx-build -b man doc doc/_build/man

.. _`flit`: https://pypi.org/project/flit/

Trigger GitLab pages
--------------------

To upload the documentation with GitLab CI::

  $ curl -X POST -F token=<token value> -F ref=master \
        https://gitlab.com/api/v4/projects/9843683/trigger/pipeline

.. vim:sts=2:sw=2:tw=78
