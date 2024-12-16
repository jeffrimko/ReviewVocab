"""Tests LangParser class."""

##==============================================================#
## SECTION: Imports                                             #
##==============================================================#

from testlib import *

from _Review_Vocab import LangParser

##==============================================================#
## SECTION: Class Definitions                                   #
##==============================================================#

class TestCase(unittest.TestCase):

    def test_that_single_word_is_handled(self):
        result = LangParser.get_equivs("hello world")
        self.assertEqual(result, ["hello world"])

    def test_that_multiple_translation_equivs_are_handled(self):
        result = LangParser.get_equivs("Hello world./Hi there.")
        self.assertEqual(result, ["Hello world.", "Hi there."])

    def test_that_multiple_word_equivs_are_handled(self):
        result = LangParser.get_equivs("Hello|Hi there.")
        self.assertEqual(result, ["Hello there.", "Hi there."])

    def test_that_punctuation_is_preserved_when_multiple_word_equivs_at_end(self):
        result = LangParser.get_equivs("Hello world|everyone.")
        self.assertEqual(result, ["Hello world.", "Hello everyone."])

##==============================================================#
## SECTION: Main Body                                           #
##==============================================================#

if __name__ == '__main__':
    unittest.main()
