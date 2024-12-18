##==============================================================#
## SECTION: Imports                                             #
##==============================================================#

from __future__ import annotations
from dataclasses import asdict, dataclass, field
from threading import Thread, Lock
from typing import Any, Generator, Optional
import abc
import itertools
import os.path as op
import random; random.seed()
import re
import sys
import tempfile
import time

from auxly import trycatch
from auxly.filesys import File, delete, walkfiles
from auxly.stringy import randomize
from dacite import from_dict
from gtts import gTTS as tts
from playsound import playsound
from thefuzz import fuzz
from unidecode import unidecode
import qprompt as q
import yaml

##==============================================================#
## SECTION: Global Definitions                                  #
##==============================================================#

DEBUG_MODE = False

##==============================================================#
## SECTION: Class Definitions                                   #
##==============================================================#

@dataclass(frozen=True)
class LanguageName:
    short: str = "en"
    full: str = "English"

@dataclass
class CommonProviderConfig:
    lang1: LanguageName = field(default_factory=LanguageName)
    lang2: LanguageName = field(default_factory=LanguageName)

@dataclass
class SingleFileProviderConfig(CommonProviderConfig):
    filepath: str = ""

@dataclass
class MultiFileProviderConfig(CommonProviderConfig):
    filepaths: list[str] = field(default_factory=list[str])

@dataclass
class ProvidersConfig:
    _default: str = "singlefile"
    _common: CommonProviderConfig = field(default_factory=CommonProviderConfig)
    singlefile: SingleFileProviderConfig = field(default_factory=SingleFileProviderConfig)
    multifile: MultiFileProviderConfig = field(default_factory=MultiFileProviderConfig)

@dataclass
class CommonModeConfig:
    reviewnum: int = 10
    shuffle: bool = False

    def show_editor(self):
        def set_field(f):
            v = getattr(self, f)
            if type(v) == bool:
                new_v = q.ask_yesno("Enter new value", default=v)
            else:
                new_v = type(v)(q.ask_str("Enter new value", default=str(v)))
            setattr(self, f, new_v)
        quit = False
        def trigger_quit():
            nonlocal quit
            quit = True
        while not quit:
            menu = q.Menu(header=f"{self.__class__.__name__} Editor")
            for i,f in enumerate(self.__dataclass_fields__, 1):
                v = getattr(self, f)
                menu.add(str(i), f"{f} [{v}]", set_field, [f])
            menu.add("q", "Quit editor", trigger_quit)
            menu.show(note=repr(self), default="q")

@dataclass
class PracticeModeConfig(CommonModeConfig):
    to_lang1: bool = False
    min_score: int = 90

@dataclass
class TranslateModeConfig(CommonModeConfig):
    listen_min_score: int = 90
    listen_attempts_before_reveal: int = 2
    translate_attempts_before_reveal: int = 2
    translate_min_score: int = 90

@dataclass
class ListenModeConfig(CommonModeConfig):
    cmds: str = "lang1 slow2 fast2 beep"
    delay_between_cmds: float = 0.1
    output_file: str = ""

@dataclass
class LearnModeConfig(CommonModeConfig):
    lang1_talk: bool = True
    lang2_talk: bool = True
    max_attempts: int = 2

@dataclass
class RapidModeConfig(CommonModeConfig):
    show_lang1_first: bool = True
    output_file: str = ""

@dataclass
class ModesConfig:
    _common: CommonModeConfig = field(default_factory=CommonModeConfig)
    listen: ListenModeConfig = field(default_factory=ListenModeConfig)
    learn: LearnModeConfig = field(default_factory=LearnModeConfig)
    translate: TranslateModeConfig = field(default_factory=TranslateModeConfig)
    practice: PracticeModeConfig = field(default_factory=PracticeModeConfig)
    rapid: RapidModeConfig = field(default_factory=RapidModeConfig)

@dataclass
class UtilConfig:
    modes: ModesConfig = field(default_factory=ModesConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    _cfgdict: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_path(path) -> UtilConfig:
        cfile = File(path)
        if not cfile.isfile():
            raise Exception(f"Provided config file could not be found: {path}")
        cfgdict = yaml.load(cfile.read(), Loader=yaml.FullLoader)
        config = from_dict(data_class=UtilConfig, data=cfgdict)
        config._cfgdict = cfgdict
        return config

    def for_mode(self, modename: str, overrides=None) -> Optional[CommonModeConfig]:
        try:
            mode = getattr(self.modes, modename)
        except AttributeError:
            return None
        cfgdict = asdict(self.modes._common)
        cfgdict.update(self._cfgdict.get('modes', {}).get(modename, {}))
        if overrides:
            cfgdict.update(overrides)
        return from_dict(data_class=type(mode), data=cfgdict)

    def get_default_provider(self) -> bool:
        return self.providers._default

    def has_provider(self, providername: str) -> bool:
        return bool(self._cfgdict.get('providers', {}).get(providername, {}))

    def for_provider(self, providername: str, overrides=None) -> Optional[CommonProviderConfig]:
        try:
            provider = getattr(self.providers, providername)
        except AttributeError:
            return None
        cfgdict = asdict(self.providers._common)
        cfgdict.update(self._cfgdict.get('providers', {}).get(providername, {}))
        if overrides:
            cfgdict.update(overrides)
        return from_dict(data_class=type(provider), data=cfgdict)

@dataclass(frozen=True)
class ReviewItem:
    line: str
    lang1: LanguageName
    lang1_equivs: list[str]
    lang1_extra: str
    lang2: LanguageName
    lang2_equivs: list[str]
    lang2_extra: str
    def __hash__(self):
        return hash(self.line) + hash(self.lang1) + hash(self.lang2)

    @staticmethod
    def parse(line: str, lang1: LanguageName, lang2: LanguageName):
        return ReviewItem(
            line,
            lang1,
            ReviewItem._get_equivs(line, 1),
            ReviewItem._get_extra(line, 1),
            lang2,
            ReviewItem._get_equivs(line, 2),
            ReviewItem._get_extra(line, 2),
        )

    @staticmethod
    def _get_lang_index(langnum: int) -> int:
        idx = langnum - 1
        return idx if (idx == 0 or idx == 1) else 0

    @staticmethod
    def _get_equivs(line, langnum) -> list[str]:
        idx = ReviewItem._get_lang_index(langnum)
        return LangParser.get_equivs(line.split(";")[idx])

    @staticmethod
    def _get_extra(line, langnum):
        idx = ReviewItem._get_lang_index(langnum)
        return LangParser.get_extra(line.split(";")[idx])

class Static:
    def __new__(cls):
        raise TypeError("Static classes cannot be instantiated")

class ProviderBase(metaclass=abc.ABCMeta):
    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    def get_items(self, reviewnum: int, shuffle: bool) -> list[ReviewItem]:
        pass

    def show_menu(self):
        q.echo("Menu not implemented")

class SingleFileProvider(ProviderBase):
    def get_items(self, reviewnum: int, shuffle: bool) -> list[ReviewItem]:
        pfile = File(self.config.filepath)
        if not pfile.exists():
            raise Exception(f"File could not be found: {self.config.filepath}")
        lines = FileParser.get_valid_lines(pfile.read())
        samplenum = reviewnum if (reviewnum <= len(lines)) else len(lines)
        review_lines = random.sample(lines, samplenum) if shuffle else lines[:reviewnum]
        return [ReviewItem.parse(l, self.config.lang1, self.config.lang2) for l in review_lines]

    def show_menu(self):
        quit = False
        def trigger_quit():
            nonlocal quit
            quit = True
        while not quit:
            note = f"Selected file = {self.config.filepath}"
            menu = q.Menu()
            menu.add("a", "List all files", self._show_all_files)
            menu.add("f", "Filter files by name", self._show_filtered_files)
            menu.add("q", "Quit selector", trigger_quit)
            menu.show(header=f"{self.__class__.__name__} Menu", note=note, default="q")

    def _show_all_files(self):
        dirpath = File(self.config.filepath).parent
        files = [f for f in self._iterfiles()]
        path = q.enum_menu(files).show(header="Select File", returns="desc", limit=20)
        self.config.filepath = File(op.join(dirpath, path))

    def _show_filtered_files(self):
        term = q.ask_str("Filter term")
        dirpath = File(self.config.filepath).parent
        files = [f for f in self._iterfiles() if (term in f)]
        path = q.enum_menu(files).show(header="Select File", returns="desc", limit=20)
        self.config.filepath = File(op.join(dirpath, path))

    def _iterfiles(self):
        currfile = File(self.config.filepath)
        for f in walkfiles(currfile.parent):
            if f.endswith(currfile.ext):
                yield f.name

class MultiFileProvider(ProviderBase):
    def get_items(self, reviewnum: int, shuffle: bool) -> list[ReviewItem]:
        lines = []
        if len(self.config.filepaths) == 0:
            raise Exception("No founds found")
        for filepath in self.config.filepaths:
            pfile = File(filepath)
            if not pfile.exists():
                raise Exception(f"File could not be found: {filepath}")
            lines += FileParser.get_valid_lines(pfile.read())
        samplenum = reviewnum if (reviewnum <= len(lines)) else len(lines)
        review_lines = random.sample(lines, samplenum) if shuffle else lines[:reviewnum]
        return [ReviewItem.parse(l, self.config.lang1, self.config.lang2) for l in review_lines]

class FileParser(Static):
    @staticmethod
    def get_valid_lines(content):
        lines = []
        for line in content.splitlines():
            if FileParser._is_line_valid(line):
                lines.append(line.strip())
        return lines

    @staticmethod
    def _is_line_valid(line):
        if not line:
            return False
        if line.count(";") != 1:
            return False
        if line.startswith("//"):
            return False
        if line.startswith("#"):
            return False
        return True

class LangParser(Static):
    @staticmethod
    def get_extra(text: str) -> str:
        extra = re.findall(r"\(.*?\)", text)
        return " ".join(extra)

    @staticmethod
    def get_equivs(text: str) -> list[str]:
        result = []
        newtext = LangParser._remove_parentheses(text)
        for equiv in LangParser._split_equivalents(newtext):
            result += LangParser._separate_equiv_words(equiv)
        return result

    @staticmethod
    def _remove_parentheses(text: str) -> str:
        return re.sub(r"\([^)]*\)", "", text).strip()

    @staticmethod
    def _split_equivalents(text: str) -> list[str]:
        return text.split("/")

    @staticmethod
    def _separate_equiv_words(text: str) -> list[str]:
        if "|" not in text:
            return [text]
        tokens = []
        for token in text.strip().split():
            if "|" in token:
                tokens.append(LangParser._split_equiv_tokens(token))
            else:
                tokens.append([token])
        results = []
        for combo in itertools.product(*tokens):
            results.append(" ".join(combo))
        return results

    @staticmethod
    def _split_equiv_tokens(token: str) -> list[str]:
        equivs = token.split("|")
        punct_to_add: list[str] = []
        for char in reversed(equivs[-1]):
            if char in ",.!?":
                punct_to_add.insert(0, char)
        if not punct_to_add:
            return equivs
        equivs_with_punct = []
        for equiv in equivs[:-1]:
            for char in punct_to_add:
                equiv += char
            equivs_with_punct.append(equiv)
        equivs_with_punct.append(equivs[-1])
        return equivs_with_punct

class ModeBase(metaclass=abc.ABCMeta):
    def __init__(self, config, provider):
        self._config = config
        self._provider = provider
        self._review: list[ReviewItem] = []
        self._curr_num = 0

    @property
    def config(self):
        return self._config

    def show_menu(self):
        quit = False
        def trigger_quit():
            nonlocal quit
            quit = True
        def on_err():
            q.error("Review exited early!")
        menu = q.Menu()
        menu.add("r", "Start review", trycatch(lambda: self.review(), oncatch=on_err, rethrow=DEBUG_MODE))
        menu.add("e", "Edit config", self.config.show_editor)
        menu.add("i", "Mode info", self.show_info)
        menu.add("q", "Quit mode", trigger_quit)
        while not quit:
            menu.show(header=self.__class__.__name__, default="r")

    def show_info(self):
        q.info(self.__class__.__doc__ or "NA")

    def review(self):
        self._review_start()
        for num, item in enumerate(self._iter_review(), 1):
            self._curr_num = num
            self.reset_banner()
            self._review_item(item)
        self._review_end()
        q.clear()

    def reset_banner(self):
        q.clear()
        flush_input()
        q.echo("%s of %s" % (self._curr_num, self.num_review))

    @property
    def num_review(self) -> int:
        return len(self._review)

    def _review_start(self):
        self._review = self._provider.get_items(self.config.reviewnum, self.config.shuffle)

    def _iter_review(self) -> Generator[ReviewItem, None, None]:
        for item in self._review:
            yield item

    def _review_end(self):
        pass

    @abc.abstractmethod
    def _review_item(self, item):
        pass

class PracticeMode(ModeBase):
    """Quick flashcard-like vocab testing."""
    def __init__(self, config, provider):
        super().__init__(config, provider)
        self._repeat = None

    def _review_start(self):
        self._missed = set()
        if self._repeat != None:
            self._review = self._repeat
        else:
            super()._review_start()

    def _review_end(self):
        q.clear()
        q.alert(f"Missed {len(self._missed)} items.")
        self._repeat = None
        if len(self._missed) == 0:
            q.pause()
        elif q.ask_yesno("Repeat missed items?"):
            self._repeat = self._missed
            self.review()

    def _get_question_extra_answers(self, item) -> tuple[str, str, list[str]]:
        if not self.config.to_lang1:
            question = random.choice(item.lang1_equivs)
            extra = item.lang1_extra
            answers = item.lang2_equivs
            return question, extra, answers
        else:
            question = random.choice(item.lang2_equivs)
            extra = item.lang2_extra
            answers = item.lang1_equivs
            return question, extra, answers

    def _review_item(self, item):
        question, extra, answers = self._get_question_extra_answers(item)
        q.alert(f"{question} {extra}")
        correct = False
        while not correct:
            response = q.ask_str("")
            score = ResponseChecker.get_score(response, answers)
            correct = score >= self.config.min_score
            if correct:
                q.echo("Correct!" if score == 100 else "Almost correct!")
                q.echo(" (OR) ".join(answers))
                if score == 100:
                    time.sleep(1)
                else:
                    q.pause()
            else:
                q.echo("Incorrect!")
                q.echo(" (OR) ".join(answers))
                self._missed.add(item)

class TranslateMode(ModeBase):
    """The user must listen to a lang2 translation, enter it correctly, then enter the lang1 translation."""
    def _review_item(self, item):
        lang2_choice = random.choice(item.lang2_equivs)
        self._do_listen(item, lang2_choice)
        self._do_translate(item, lang2_choice)

    def _do_listen(self, item, lang2_choice):
        Audio.talk(lang2_choice, item.lang2.short, slow=False, wait=False)
        q.echo(f"(Type the {item.lang2.full} you hear.)")
        attempts = 0
        correct = False
        score = 0
        while not correct:
            response = q.ask_str("")
            score = ResponseChecker.get_score(response, [lang2_choice])
            correct = score >= self.config.listen_min_score
            if not correct:
                Audio.talk(lang2_choice, item.lang2.short, slow=True, wait=False)
                attempts += 1
                if attempts >= self.config.listen_attempts_before_reveal:
                    q.alert(lang2_choice)
        q.echo("Correct!" if score == 100 else "Almost correct!")
        q.echo(lang2_choice)

    def _do_translate(self, item, lang2_choice):
        q.echo(f"(Type the {item.lang1.full} translation.)")
        Audio.talk(lang2_choice, item.lang2.short, slow=False, wait=False)
        attempts = 0
        correct = False
        while not correct:
            response = q.ask_str("")
            score = ResponseChecker.get_score(response, item.lang1_equivs)
            correct = score >= self.config.translate_min_score
            if not correct:
                attempts += 1
                if attempts >= self.config.translate_attempts_before_reveal:
                    q.alert(random.choice(item.lang1_equivs))
        q.echo("Correct!" if score == 100 else "Almost correct!")
        q.echo(" (OR) ".join(item.lang1_equivs))
        Audio.talk(lang2_choice, item.lang2.short, slow=False, wait=True)

class ListenMode(ModeBase):
    """Audio flashcards for review or testing."""
    def _review_item(self, item):
        if self.config.output_file:
            File(self.config.output_file).appendline(item.line)
        lang1_choice = random.choice(item.lang1_equivs)
        lang2_choice = random.choice(item.lang2_equivs)
        lang1_shown = False
        lang2_shown = False
        cmds = self.config.cmds.split()
        for cmd in cmds:
            if cmd == "lang1":
                if not lang1_shown:
                    q.alert(lang1_choice)
                    lang1_shown = True
                Audio.talk(lang1_choice, item.lang1.short, wait=True)
            elif cmd == "fast2":
                if not lang2_shown:
                    q.alert(lang2_choice)
                    lang2_shown = True
                Audio.talk(lang2_choice, item.lang2.short, slow=False, wait=True)
            elif cmd == "slow2":
                if not lang2_shown:
                    q.alert(lang2_choice)
                    lang2_shown = True
                Audio.talk(lang2_choice, item.lang2.short, slow=True, wait=True)
            elif cmd.startswith("delay="):
                delay = float(cmd.split("=")[1])
                time.sleep(delay)
            elif cmd == "pause":
                q.pause()
            elif cmd == "beep":
                Audio.beep()
            time.sleep(self.config.delay_between_cmds)

    def _do_lang1(self, item):
        translation = random.choice(item.lang1_equivs)
        q.alert(translation)
        Audio.talk(translation, item.lang1.short, wait=True)

    def _do_lang2(self, item):
            translation = random.choice(item.lang2_equivs)
            q.alert(translation)
            for _ in range(self.config.lang2_repeat_slow):
                Audio.talk(translation, item.lang2.short, slow=True, wait=True)
            for _ in range(self.config.lang2_repeat_fast):
                Audio.talk(translation, item.lang2.short, slow=False, wait=True)

class LearnMode(ModeBase):
    """The user must type in the displayed lang2 translation, then type it in again from memory."""
    def _review_item(self, item):
        lang1_choice = random.choice(item.lang1_equivs)
        lang2_choice = random.choice(item.lang2_equivs)
        correct = False
        while not correct:
            self._learn(item, lang1_choice, lang2_choice)
            correct = self._test(item, lang1_choice, lang2_choice)

    def _learn(self, item, lang1_choice, lang2_choice):
        q.echo(f"(Type the {item.lang2.full} translation.)")
        q.alert(lang1_choice)
        if self.config.lang1_talk:
            Audio.talk(lang1_choice, item.lang1.short)
        q.alert("> " + lang2_choice)
        if self.config.lang2_talk:
            Audio.talk(lang2_choice, item.lang2.short, slow=False)
            Audio.talk(lang2_choice, item.lang2.short, slow=True)
        correct = False
        while not correct:
            response = q.ask_str("")
            correct = ResponseChecker.is_valid(response, [lang2_choice])
        q.echo("Correct!")
        time.sleep(1)
        Audio.wait_talk()

    def _test(self, item, lang1_choice, lang2_choice) -> bool:
        q.clear()
        q.echo(f"(Type the {item.lang2.full} translation.)")
        correct = False
        attempts = 0
        while not correct:
            Audio.talk(lang2_choice, item.lang2.short, slow=True)
            response = q.ask_str("")
            correct = ResponseChecker.is_valid(response, [lang2_choice])
            if not correct:
                attempts += 1
                if attempts >= self.config.max_attempts:
                    return False
        q.echo("Correct!")
        q.alert(lang2_choice)
        q.alert(lang1_choice)
        Audio.talk(lang2_choice, item.lang2.short, slow=False, wait=True)
        return True

class RapidMode(ModeBase):
    """Rapidly review vocab."""
    def _review_item(self, item):
        lang1_choice = random.choice(item.lang1_equivs)
        lang2_choice = random.choice(item.lang2_equivs)
        first, second = (lang1_choice, lang2_choice) if self.config.show_lang1_first else (lang2_choice, lang1_choice)
        q.alert(first)
        q.pause()
        self.reset_banner()
        q.alert(first)
        q.echo(">>> " + second)
        if self.config.output_file:
            if q.ask_yesno("Add to output file?", default=False):
                File(self.config.output_file).appendline(item.line)
        else:
            q.pause()

class ResponseChecker(Static):
    @staticmethod
    def is_valid(response: str, valid_answers: list[str], min_valid_score=100) -> bool:
        score = ResponseChecker.get_score(response, valid_answers)
        return score >= min_valid_score

    @staticmethod
    def get_score(response: str, valid_answers: list[str]) -> int:
        """Returns correctness as a score, 100 is exactly correct."""
        sanitized_response = ResponseChecker._sanitize(response)
        sanitized_answers = [ResponseChecker._sanitize(a) for a in valid_answers]
        ratios = [fuzz.ratio(sanitized_response, a) for a in sanitized_answers]
        return max(ratios)

    @staticmethod
    def _sanitize(txt: str) -> str:
        sanitized = unidecode(txt.lower().strip())
        sanitized = re.sub(r'[^ a-z0-9]', '', sanitized, re.UNICODE)
        return sanitized

class Audio(Static):
    _TALK_LOCK = Lock()

    @staticmethod
    def beep():
        try:
            import winsound
            winsound.Beep(300, 850)
        except:
            pass

    @staticmethod
    def wait_talk():
        while Audio._TALK_LOCK.locked():
            time.sleep(0.1)

    @staticmethod
    def talk(text, lang, slow=False, wait=False):
        def _talk():
            """Pronounces the given text in the given language."""
            with Audio._TALK_LOCK:
                tmppath = op.join(tempfile.gettempdir(), f"__temp-talk-{randomize(6)}.mp3")
                tts(text=text, lang=lang, slow=slow).save(tmppath)
                playsound(tmppath)
                delete(tmppath)
        try:
            t = Thread(target=_talk)
            t.start()
            if wait:
                t.join()
        except KeyboardInterrupt:
            raise
        except Exception:
            q.warn("Could not talk at this time.")

class MainMenu(Static):
    @staticmethod
    def _get_provider(config):
        providername = config.get_default_provider()
        if not config.has_provider(providername):
            raise Exception(f"No provider config found for default: {providername}")
        mapping = {
            "singlefile": SingleFileProvider,
            "multifile": MultiFileProvider,
        }
        return mapping[providername](config.for_provider(providername))

    @staticmethod
    def show(cfgpath):
        config = UtilConfig.from_path(cfgpath)
        provider = MainMenu._get_provider(config)
        quit = False
        def trigger_quit():
            nonlocal quit
            quit = True
        def on_err():
            q.error("Mode exited early!")
        def build_mode(mode, name):
            return lambda: trycatch(mode(config.for_mode(name), provider).show_menu, oncatch=on_err, rethrow=DEBUG_MODE)()
        menu = q.Menu()
        menu.add("l", "Learn mode", build_mode(LearnMode, "learn"))
        menu.add("i", "Listen mode", build_mode(ListenMode, "listen"))
        menu.add("t", "Translate mode", build_mode(TranslateMode, "translate"))
        menu.add("p", "Practice mode", build_mode(PracticeMode, "practice"))
        menu.add("r", "Rapid mode", build_mode(RapidMode, "rapid"))
        menu.add("m", "Provider menu", provider.show_menu)
        menu.add("q", "Quit", trigger_quit)
        while not quit:
            menu.show()

##==============================================================#
## SECTION: Function Definitions                                #
##==============================================================#

def flush_input():
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        import sys, termios
        termios.tcflush(sys.stdin, termios.TCIOFLUSH)

def main():
    cfgpath = "config.yaml" if len(sys.argv) == 1 else sys.argv[1]
    try:
        MainMenu.show(cfgpath)
    except KeyboardInterrupt:
        pass

##==============================================================#
## SECTION: Main Body                                           #
##==============================================================#

if __name__ == '__main__':
    main()
