Installation
============

Installation from PyPi
----------------------

.. note::

   To be completed.

Installation from source
------------------------

Install etcmaint
^^^^^^^^^^^^^^^^

Clone the repository::

  git clone https://gitlab.com/xdegaye/etcmaint

Install `flit`_ from PyPI::

  python -m pip install flit

Install etcmaint locally with flit by running the following command at the
root of the etcmaint source::

  flit install

This will install etcmaint at ~/.local/lib/python3.7/site-packages if the
current python version is 3.7.

When running the sync subcommand with sudo, the existing user environment must
be preserved so that python may find the location where etcmaint has been
installed. So one must run::

  sudo -E etcmaint sync

Run the test suite
^^^^^^^^^^^^^^^^^^

Run the full test suite in verbose mode::

  python -m unittest -v

Run a single test named ``test_example``::

  python -m unittest -k test_example

Build the documentation
^^^^^^^^^^^^^^^^^^^^^^^

Install the Arch linux ``python-sphinx`` package.

Build the html documentation at doc/_build/html and the man pages at
doc/_build/man::

  sphinx-build -b html doc doc/_build/html
  sphinx-build -b man doc doc/_build/man

.. _`flit`: https://pypi.org/project/flit/

.. vim:sts=2:sw=2:tw=78
