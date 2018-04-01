"""Test the etcmaint commands."""

import unittest
from collections import namedtuple

TestDirs = namedtuple('TestDirs', ['root', 'cache', 'repo'])

class BaseTestCase(unittest.TestCase):
    def test_foo(self):
        pass
