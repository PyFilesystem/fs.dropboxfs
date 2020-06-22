from __future__ import print_function

import os
import unittest

from dropbox import create_session
from fs.test import FSTestCases
import fs.subfs

from dropboxfs.dropboxfs import DropboxFS


def join(a, b):
    return a + b


TEST_PATH = 'dropboxfs'

class TestDropboxFS(FSTestCases, unittest.TestCase):
    def make_fs(self):
        # Return an instance of your FS object here
        self.access_token = "olshmk5XitgAAAAAAAAJnRpQ3mrGJq5kmsI6QvycvC2kT8p18SsajOlhWV511oAd"

        if "DEV" in os.environ:
            proxies = {
                "http": "http://127.0.0.1:1087",
                "https": "http://127.0.0.1:1087",
            }

            sess = create_session(8, proxies=proxies)
        else:
            sess = None
        dfs = DropboxFS(self.access_token, session=sess)

        if dfs.exists(TEST_PATH):
            dfs.removetree(TEST_PATH)
        dfs.makedir(TEST_PATH)

        fs2 = fs.subfs.SubFS(dfs, TEST_PATH)
        return fs2


    def test_case_sensitive(self):
        # dropbox  insesitive
        pass
