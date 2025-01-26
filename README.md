# ReviewVocab

## Introduction
This a Python CLI script for reviewing foreign language vocab.

## Status
This project was refactored in December 2024 and is not compatible with previous versions. Refer to the tag `refactor-202412` for the previous codebase.

## Requirements
Python 3.10+ is required. Additional third-party libraries are required and can be installed using the following command: `pip install -r requirements.txt`

## Usage
Run the `_Review_Vocab.py` to show the main menu. A config file can be provided as an argument otherwise `config.yaml` will be used by default.

The config file has configuration for "providers" and "modes". Providers are code objects that provide vocab review items for the modes. Modes are interactive ways to review vocab.

For file providers, a set of vocab files are required. Example English/Italian vocab files are provided in [ItalianVocab](https://github.com/jeffrimko/ItalianVocab).

Vocab files are formatted as follows:

  - Vocab files use the `.txt` extension.
  - Each line is a single vocab entry.
  - Vocab entry lines use the format: `<lang1>;<lang2>`
      * Example: `hello;ciao`
  - Equivalent individual words are separated with a `|`.
      * Example: `the cloud;la nuvola|nube`
  - Equivalent translations are separated with a `/`.
      * Example: `the car;l'auto/la macchina`
  - Append extra info in parenthesis.
      * Example: `hello (formal);buongiorno`
  - Literal translations are shown in parenthesis prefixed with `lit`:
      * Example: `good luck (lit: in the mouth of the wolf);in bocca al lupo`
