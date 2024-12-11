"""Tests ResponseChecker class."""

##==============================================================#
## SECTION: Imports                                             #
##==============================================================#

from testlib import *

from _Review_Vocab import ResponseChecker

##==============================================================#
## SECTION: Class Definitions                                   #
##==============================================================#

class TestCase(unittest.TestCase):

    def test_that_is_valid_ignores_accent_chars(self):
        result = ResponseChecker.is_valid("hello", ["helló"])
        self.assertTrue(result)

    def test_that_is_valid_ignores_punctuation(self):
        result = ResponseChecker.is_valid("hello", ["Hello."])
        self.assertTrue(result)

    def test_that_is_valid_ignores_casing(self):
        result = ResponseChecker.is_valid("hello", ["HeLlO"])
        self.assertTrue(result)

    def test_that_is_valid_allows_lower_score(self):
        should_fail = ResponseChecker.is_valid("helo world", ["hello world"])
        should_pass = ResponseChecker.is_valid("helo world", ["hello world"], 95)
        self.assertFalse(should_fail)
        self.assertTrue(should_pass)

##==============================================================#
## SECTION: Main Body                                           #
##==============================================================#

if __name__ == '__main__':
    unittest.main()
