##==============================================================#
## SECTION: Imports                                             #
##==============================================================#

from __future__ import annotations
from collections import namedtuple
from dataclasses import asdict, dataclass, field
from functools import wraps
from math import ceil, isclose
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
from auxly.filesys import File, walkfiles
from auxly.shell import has, silent
from auxly.stringy import randomize
from dacite import from_dict
from gtts import gTTS as tts
from playsound import playsound
from thefuzz import fuzz
from tinydb import TinyDB, Query
from unidecode import unidecode
from wakepy import keep
import qprompt as q
import yaml

##==============================================================#
## SECTION: Global Definitions                                  #
##==============================================================#

#: Allow exceptions to be thrown, otherwise failures are silent.
DEBUG_MODE = False

#: Default talk cache option unless one is explicitly provided.
CACHE_TALK = True

##==============================================================#
## SECTION: Decorators                                          #
##==============================================================#

def config_for(forcls):
    def wrapper(cls):
        cls.__forcls__ = forcls
        return cls
    return wrapper

##==============================================================#
## SECTION: Class Definitions                                   #
##==============================================================#

class ModeBase(metaclass=abc.ABCMeta):
    def __init__(self, config: CommonModeConfig, provider: ProviderBase, trackers: list[TrackerBase]):
        self._config = config
        self._provider = provider
        self._trackers = trackers
        self._items: list[VocabItem] = []
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
        menu.add("i", "Mode info", self._show_info)
        menu.add("q", "Quit mode", trigger_quit)
        while not quit:
            menu.show(header=self.__class__.__name__, default="r")

    def _show_info(self):
        q.info(self.__class__.__doc__ or "NA")

    def review(self):
        with keep.presenting():
            self._items = self._provider.get_items(self.config.reviewnum, self.config.shuffle)
            self._review_init()
            repeat_review = True
            while repeat_review:
                self._review_start()
                for num, item in enumerate(self._items, 1):
                    self._curr_num = num
                    self._reset_banner()
                    tracker_info = self._review_item(item)
                    if tracker_info is not None:
                        for tracker in self._trackers:
                            tracker.track(tracker_info)
                repeat_review = self._review_end()
                q.clear()

    def _reset_banner(self):
        q.clear()
        flush_input()
        q.echo("%s of %s" % (self._curr_num, self._num_items))

    @property
    def _num_items(self) -> int:
        return len(self._items)

    def _review_init(self):
        pass

    def _review_start(self):
        pass

    def _review_end(self):
        pass

    @abc.abstractmethod
    def _review_item(self, item):
        pass

class PracticeMode(ModeBase):
    """Vocab practice where user enters full response."""
    def __init__(self, config, provider, trackers):
        super().__init__(config, provider, trackers)
        self._repeat = None

    def _review_start(self):
        self._missed = set()
        if self._repeat != None:
            self._items = self._repeat
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
        if self.config.lang1to2:
            question = item.lang1.choice
            extra = item.lang1.extra
            answers = item.lang2.equivs
            return question, extra, answers
        else:
            question = item.lang2.choice
            extra = item.lang2.extra
            answers = item.lang1.equivs
            if self.config.talk_lang2_question:
                Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False, wait=False)
            return question, extra, answers

    def _review_item(self, item):
        question, extra, answers = self._get_question_extra_answers(item)
        q.alert(f"{question} {extra}")
        correct = False
        tracker_info = TrackerInfo(item)
        tracker_info.points = 8 if self.config.lang1to2 else 4
        while not correct:
            response = q.ask_str("")
            tracker_info.set_response_once(response)
            score = ResponseChecker.get_score(response, answers)
            tracker_info.set_score_once(score)
            correct = score >= self.config.min_score
            if correct:
                q.hrule()
                q.echo("Correct!" if score == 100 else "Almost correct!")
                q.echo(format_equivs(answers))
                if self.config.talk_lang2_correct or not self.config.lang1to2:
                    Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False, wait=True)
                elif score == 100:
                    time.sleep(self.config.correct_delay)
                else:
                    q.pause()
            else:
                q.echo("Incorrect!")
                q.echo(">>>   " + format_equivs(answers))
                self._missed.add(item)
                if self.config.missed_file:
                    File(self.config.missed_file).appendline(item.line)
        return tracker_info

class TranslateMode(ModeBase):
    """The user must listen to a lang2 translation, enter it correctly, then enter the lang1 translation."""
    SCORE_WEIGHT_LISTEN    = (1/3)
    SCORE_WEIGHT_TRANSLATE = (2/3)

    def _review_item(self, item):
        score = self._do_listen(item)
        response = None
        if not self.config.skip_translate:
            q.hrule()
            response, translate_score = self._do_translate(item)
            score = ceil(score * TranslateMode.SCORE_WEIGHT_LISTEN) + ceil(translate_score * TranslateMode.SCORE_WEIGHT_TRANSLATE)
        else:
            Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False, wait=True)
            q.pause()
        score = ScoreFormatter.sanitize_score(score)
        tracker_info = TrackerInfo(item, response, score)
        tracker_info.points = 8 if not self.config.skip_translate else 3
        return tracker_info

    def _do_listen(self, item):
        Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False, wait=False)
        q.echo(f"(Type the {item.lang2.name.full} you hear.)")
        attempts = 0
        correct = False
        first_score = None
        while not correct:
            response = q.ask_str("")
            if response:
                score = ResponseChecker.get_score(response, [item.lang2.choice])
                if first_score is None:
                    first_score = score
                correct = score >= self.config.listen_min_score
                if not correct:
                    attempts += 1
                    if attempts >= self.config.listen_attempts_before_reveal:
                        q.alert(item.lang2.choice)
                else:
                    q.echo("Correct!" if score == 100 else "Almost correct!")
                    q.echo(item.lang2.choice)
            if not correct:
                Audio.talk(item.lang2.choice, item.lang2.name.short, slow=True, wait=False)
        return first_score

    def _do_translate(self, item):
        q.echo(f"(Type the {item.lang1.name.full} translation.)")
        Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False, wait=False)
        attempts = 0
        correct = False
        first_response = None
        first_score = None
        score = 0
        while not correct:
            response = q.ask_str("")
            if first_response is None:
                first_response = response
            score = ResponseChecker.get_score(response, item.lang1.equivs)
            if first_score is None:
                first_score = score
            correct = score >= self.config.translate_min_score
            if not correct:
                attempts += 1
                if attempts >= self.config.translate_attempts_before_reveal:
                    q.alert(random.choice(item.lang1.equivs))
        q.echo("Correct!" if score == 100 else "Almost correct!")
        q.echo(format_equivs(item.lang1.equivs))
        Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False, wait=True)
        return first_response, first_score

class ListenMode(ModeBase):
    """Audio flashcards for review or testing."""
    def _review_item(self, item):
        if self.config.output_file:
            File(self.config.output_file).appendline(item.line)
        cmds = self.config.cmds.split()
        while cmds:
            cmds = self._run_cmds(cmds, item)

    @staticmethod
    def _get_speed(cmd) -> float:
        speed = 1.0
        if "=" in cmd:
            _,speedstr = cmd.split("=")
            speed = float(speedstr)
        return speed

    def _run_cmds(self, cmds, item):
        lang1_shown = False
        lang2_shown = False
        for idx,cmd in enumerate(cmds):
            if cmd.startswith("talk1") or cmd == "show1":
                should_talk = cmd.startswith("talk1")
                if not lang1_shown:
                    q.alert(format_choice_extra(item, 1))
                    lang1_shown = True
                if should_talk:
                    speed = ListenMode._get_speed(cmd)
                    Audio.talk(item.lang1.choice, item.lang1.name.short, wait=True, speed=speed)
            elif cmd.startswith("fast2") or cmd.startswith("slow2"):
                slow = cmd.startswith("slow")
                if not lang2_shown:
                    q.alert(format_choice_extra(item, 2))
                    lang2_shown = True
                speed = ListenMode._get_speed(cmd)
                Audio.talk(item.lang2.choice, item.lang2.name.short, slow=slow, wait=True, speed=speed)
            elif cmd.startswith("delay="):
                delay = float(cmd.split("=")[1])
                time.sleep(delay)
            elif cmd == "pause":
                q.pause()
            elif cmd.startswith("askrepeat"):
                num = int(cmd.split("=")[1]) if "=" in cmd else -1
                num = -1 if num <= 0 else num
                if q.ask_yesno(f"Repeat? ({'all' if num == -1 else num})", default=False):
                    if num == -1:
                        self._reset_banner()
                    if self.config.repeat_file:
                        File(self.config.repeat_file).appendline(item.line)
                    return cmds if num == -1 else cmds[idx-num:]
            elif cmd == "beep":
                Audio.beep()
            time.sleep(self.config.delay_between_cmds)
        return []

    def _do_lang1(self, item):
        translation = random.choice(item.lang1.equivs)
        q.alert(translation)
        Audio.talk(translation, item.lang1.name.short, wait=True)

    def _do_lang2(self, item):
            translation = random.choice(item.lang2.equivs)
            q.alert(translation)
            for _ in range(self.config.lang2_repeat_slow):
                Audio.talk(translation, item.lang2.name.short, slow=True, wait=True)
            for _ in range(self.config.lang2_repeat_fast):
                Audio.talk(translation, item.lang2.name.short, slow=False, wait=True)

class LearnMode(ModeBase):
    """The user must type in the displayed lang2 translation, then enter it in again from memory."""
    def _review_item(self, item):
        correct = False
        while not correct:
            self._learn(item)
            correct = self._test(item)

    @staticmethod
    def format_instruction_msg(item, include_extra=False):
        msg = f"(Type the {item.lang2.name.full} translation."
        if include_extra:
            msg += " The text in parenthesis is extra info only."
        msg += ")"
        return msg

    def _learn(self, item):
        has_extra = bool(item.lang2.extra)
        msg = LearnMode.format_instruction_msg(item, has_extra)
        q.echo(msg)
        q.alert(format_choice_extra(item, 1))
        if self.config.talk_lang1:
            Audio.talk(item.lang1.choice, item.lang1.name.short)
        if self.config.pause_between_langs:
            q.pause()
        q.alert("> " + format_choice_extra(item, 2))
        if self.config.talk_lang2:
            Audio.talk(item.lang2.choice, item.lang2.name.short, slow=False)
            Audio.talk(item.lang2.choice, item.lang2.name.short, slow=True)
        correct = False
        while not correct:
            response = q.ask_str("")
            correct = ResponseChecker.is_valid(response, [item.lang2.choice])
        q.echo("Correct!")
        time.sleep(1)
        Audio.wait_talk()

    def _test(self, item) -> bool:
        q.clear()
        q.echo(LearnMode.format_instruction_msg(item))
        correct = False
        attempts = 0
        while not correct:
            Audio.talk(item.lang2.choice, item.lang2.name.short, slow=True, speed=0.9)
            response = q.ask_str("")
            correct = ResponseChecker.is_valid(response, [item.lang2.choice])
            if not correct:
                attempts += 1
                if attempts >= self.config.max_attempts:
                    return False
        q.echo("Correct!")
        q.alert(format_choice_extra(item, 2))
        q.alert(format_choice_extra(item, 1))
        Audio.talk(item.lang2.choice, item.lang2.name.short, slow=True, wait=True, speed=0.9)
        Audio.talk(item.lang1.choice, item.lang1.name.short, slow=False, wait=True)
        Audio.talk(item.lang2.choice, item.lang2.name.short, slow=True, wait=True)
        return True

class RapidMode(ModeBase):
    """Rapidly review vocab where user does not directly provide response but instead self-grades."""
    def __init__(self, config, provider, trackers):
        super().__init__(config, provider, trackers)
        self._repeat = []

    def _review_start(self):
        if self._repeat:
            self._items = self._repeat
            random.shuffle(self._items)
            self._repeat = []
        else:
            super()._review_start()

    def _review_end(self):
        q.clear()
        if self._repeat and q.ask_yesno(f"Repeat {len(self._repeat)} items?"):
            self.review()

    def _review_item(self, item):
        pause_after = True
        tracker_info = TrackerInfo(item)
        tracker_info.points = 2 if self.config.lang1to2 else 1
        self._show_first(item)
        q.pause()
        self._reset_banner()
        self._show_first(item, True)
        self._show_second(item)
        if self.config.ask_score:
            pause_after = False
            menu = q.Menu()
            menu.add("1", "none", lambda: 0)
            menu.add("2", "weak", lambda: 40)
            menu.add("3", "medium", lambda: 80)
            menu.add("4", "strong", lambda: 100)
            tracker_info.score = menu.show(msg="Vocab strength", returns="func")
            if tracker_info.score <= self.config.repeat_score_threshold:
                self._repeat.append(item)
        Audio.wait_talk()
        if self.config.output_file:
            File(self.config.output_file).appendline(item.line)
        if self.config.missed_file:
            pause_after = False
            if q.ask_yesno("Add to missed file?", default=False):
                File(self.config.missed_file).appendline(item.line)
        if pause_after:
            q.pause()
        if tracker_info.score != None:
            return tracker_info

    def _show_first(self, item, prevent_talk=False):
        def _show(choice, talk):
            q.alert(choice)
            if not prevent_talk:
                talk(item)
        if self.config.lang1to2:
            _show(item.lang1.choice, self._talk_lang1)
        else:
            _show(item.lang2.choice, self._talk_lang2)

    def _show_second(self, item):
        def _show(choice, equivs, talk):
            q.echo(">>> " + choice)
            if len(equivs) > 1:
                q.echo(f">>> ({format_equivs(equivs)})")
            talk(item)
        if self.config.lang1to2:
            _show(item.lang2.choice, item.lang2.equivs, self._talk_lang2)
        else:
            _show(item.lang1.choice, item.lang1.equivs, self._talk_lang1)

    def _talk_lang1(self, item):
        if self.config.talk_lang1:
            Audio.talk(item.lang1.choice, item.lang1.name.short)

    def _talk_lang2(self, item):
        if self.config.talk_lang2:
            Audio.talk(item.lang2.choice, item.lang2.name.short)

class ProviderBase(metaclass=abc.ABCMeta):
    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    def get_items(self, reviewnum: int, shuffle: bool) -> list[VocabItem]:
        pass

    def show_menu(self):
        q.echo("Menu not implemented")

@dataclass(frozen=True)
class LanguageName:
    short: str = "en"
    full: str = "English"

@dataclass
class CommonProviderConfig:
    lang1: LanguageName = field(default_factory=LanguageName)
    lang2: LanguageName = field(default_factory=LanguageName)

class SingleFileProvider(ProviderBase):
    def get_items(self, reviewnum: int, shuffle: bool) -> list[VocabItem]:
        pfile = File(self.config.filepath)
        if not pfile.exists():
            raise Exception(f"File could not be found: {self.config.filepath}")
        lines = FileParser.get_valid_lines(pfile.read())
        samplenum = reviewnum if (reviewnum <= len(lines)) else len(lines)
        review_lines = random.sample(lines, samplenum) if shuffle else lines[:reviewnum]
        return [VocabItem.parse(l, self.config.lang1, self.config.lang2) for l in review_lines]

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

@dataclass
@config_for(SingleFileProvider)
class SingleFileProviderConfig(CommonProviderConfig):
    filepath: str = ""

class MultiFileProvider(ProviderBase):
    def get_items(self, reviewnum: int, shuffle: bool) -> list[VocabItem]:
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
        return [VocabItem.parse(l, self.config.lang1, self.config.lang2) for l in review_lines]

@dataclass
@config_for(MultiFileProvider)
class MultiFileProviderConfig(CommonProviderConfig):
    filepaths: list[str] = field(default_factory=list[str])

@dataclass
class ProviderConfig:
    _default: str = "singlefile"
    _common: CommonProviderConfig = field(default_factory=CommonProviderConfig)
    singlefile: SingleFileProviderConfig = field(default_factory=SingleFileProviderConfig)
    multifile: MultiFileProviderConfig = field(default_factory=MultiFileProviderConfig)

class TrackerBase:
    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    def track(self, info: TrackerInfo):
        pass

class TinydbTracker(TrackerBase):
    def track(self, info: TrackerInfo):
        db = TinyDB(self.config.path)
        words = set()
        score = info.score or 0
        points = info.points or 0
        if DEBUG_MODE:
            q.echo(f"{points=} {score=}")
            q.pause()
        for equiv in info.item.lang2.equivs:
            toks = WordTokenizer.tokenize(equiv)
            words.update(toks)
        for word in words:
            if not word:
                continue
            query = Query()
            results = db.search(query.word == word)
            if results:
                entry = results[0]
                entry['last_review'] = int(time.time())
                entry['last_score'] = score
                new_ave_score = ceil(accum_ave(entry['ave_score'], entry['num_reviews'], score))
                entry['ave_score'] = ScoreFormatter.sanitize_score(new_ave_score)
                new_ema5_score = score
                old_ema5_score = entry.get('ema5_score', None)
                if old_ema5_score != None:
                    new_ema5_score = ceil(accum_ema(old_ema5_score, score))
                entry['ema5_score'] = ScoreFormatter.sanitize_score(new_ema5_score)
                entry['points'] += points
                entry['num_reviews'] += 1
                db.update(entry, query.word == word)
            else:
                entry = {}
                entry['word'] = word
                entry['last_review'] = int(time.time())
                entry['last_score'] = score
                entry['ave_score'] = score
                entry['ema5_score'] = score
                entry['points'] = points
                entry['num_reviews'] = 1
                db.insert(entry)

class TxtfileTracker(TrackerBase):
    def track(self, info: TrackerInfo):
        File(self.config.path).appendline(f"{info.score};{info.response};{info.item.line}")

@dataclass
@config_for(TinydbTracker)
class TinydbTrackerConfig:
    path: str = "tracker.json"

@dataclass
@config_for(TxtfileTracker)
class TxtfileTrackerConfig:
    path: str = "tracker.txt"

@dataclass
class TrackersConfig:
    tinydb: Optional[TinydbTrackerConfig] = None
    txtfile: Optional[TxtfileTrackerConfig] = None

    def get_trackers(self) -> list[TrackerBase]:
        trackers = []
        for cfg in [self.tinydb, self.txtfile]:
            if cfg:
                trackers.append(get_config_for_cls(cfg)(cfg))
        return trackers

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
@config_for(PracticeMode)
class PracticeModeConfig(CommonModeConfig):
    lang1to2: bool = True
    min_score: int = 90
    talk_lang2_question: bool = True
    talk_lang2_correct: bool = True
    missed_file: str = ""
    correct_delay: float = 1.5

@dataclass
@config_for(TranslateMode)
class TranslateModeConfig(CommonModeConfig):
    listen_min_score: int = 90
    listen_attempts_before_reveal: int = 2
    translate_attempts_before_reveal: int = 2
    translate_min_score: int = 90
    skip_translate: bool = False

@dataclass
@config_for(ListenMode)
class ListenModeConfig(CommonModeConfig):
    cmds: str = "talk1 slow2 fast2 beep"
    delay_between_cmds: float = 0.1
    output_file: str = ""
    repeat_file: str = ""

@dataclass
@config_for(LearnMode)
class LearnModeConfig(CommonModeConfig):
    talk_lang1: bool = True
    talk_lang2: bool = True
    pause_between_langs: bool = False
    max_attempts: int = 2

@dataclass
@config_for(RapidMode)
class RapidModeConfig(CommonModeConfig):
    lang1to2: bool = True
    talk_lang1: bool = True
    talk_lang2: bool = True
    ask_score: bool = True
    repeat_score_threshold: int = 40
    output_file: str = ""
    missed_file: str = ""

@dataclass
class ModesConfig:
    _default: str = "listen"
    _common: CommonModeConfig = field(default_factory=CommonModeConfig)
    listen: ListenModeConfig = field(default_factory=ListenModeConfig)
    learn: LearnModeConfig = field(default_factory=LearnModeConfig)
    translate: TranslateModeConfig = field(default_factory=TranslateModeConfig)
    practice: PracticeModeConfig = field(default_factory=PracticeModeConfig)
    rapid: RapidModeConfig = field(default_factory=RapidModeConfig)


@dataclass
class UtilConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    modes: ModesConfig = field(default_factory=ModesConfig)
    trackers: TrackersConfig = field(default_factory=TrackersConfig)
    _cfgdict: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_path(path) -> UtilConfig:
        cfile = File(path)
        if not cfile.isfile():
            raise Exception(f"Provided config file could not be found: {path}")
        cfgdict = yaml.load(cfile.read(), Loader=yaml.FullLoader)
        provider = cfgdict['provider'].get('_default')
        if not provider:
            keys = [k for k in cfgdict['provider'].keys() if not k.startswith('_')]
            if len(keys) > 1:
                raise Exception(f"Provided config file contains multiple providers!")
            provider = keys[0]
            cfgdict['provider']['_default'] = provider
        config = from_dict(data_class=UtilConfig, data=cfgdict)
        config._cfgdict = cfgdict
        return config

    def get_modecls(self, modename: str):
        modecfg = getattr(self.modes, modename)
        return get_config_for_cls(modecfg)

    def get_modecfg(self, modename: str, overrides=None) -> Optional[CommonModeConfig]:
        try:
            modecfg = getattr(self.modes, modename)
        except AttributeError:
            return None
        cfgdict = asdict(self.modes._common)
        cfgdict.update(self._cfgdict.get('modes', {}).get(modename, {}))
        if overrides:
            cfgdict.update(overrides)
        return from_dict(data_class=type(modecfg), data=cfgdict)

    def get_default_mode(self) -> str:
        return self.modes._default

    def _get_default_provider(self) -> str:
        return self.provider._default

    def get_provider(self) -> ProviderBase:
        providername = self._get_default_provider()
        if not self._has_provider(providername):
            raise Exception(f"No provider config found for default: {providername}")
        providercfg = self._get_providercfg(providername)
        if not providercfg:
            raise Exception(f"No provider config found for default: {providername}")
        return get_config_for_cls(providercfg)(providercfg)

    def _has_provider(self, providername: str) -> bool:
        return bool(self._cfgdict.get('provider', {}).get(providername, {}))

    def _get_providercfg(self, providername: str, overrides=None) -> Optional[CommonProviderConfig]:
        try:
            provider = getattr(self.provider, providername)
        except AttributeError:
            return None
        cfgdict = asdict(self.provider._common)
        cfgdict.update(self._cfgdict.get('provider', {}).get(providername, {}))
        if overrides:
            cfgdict.update(overrides)
        return from_dict(data_class=type(provider), data=cfgdict)

    def get_trackers(self) -> list[TrackerBase]:
        return self.trackers.get_trackers()

@dataclass(frozen=True)
class VocabLang:
    text: str
    name: LanguageName
    choice: str
    equivs: list[str]
    extra: str

    def __hash__(self):
        return hash(self.line) + hash(self.lang1) + hash(self.lang2)

    @staticmethod
    def parse(text: str, name: LanguageName):
        equivs = LangParser.get_equivs(text)
        return VocabLang(
            text,
            name,
            random.choice(equivs),
            equivs,
            LangParser.get_extra(text)
        )

@dataclass(frozen=True)
class VocabItem:
    line: str
    lang1: VocabLang
    lang2: VocabLang

    def __hash__(self):
        return hash(self.line) + hash(self.lang1.name) + hash(self.lang2.name)

    @staticmethod
    def parse(line: str, name1: LanguageName, name2: LanguageName):
        text1, text2 = line.split(";")
        lang1 = VocabLang.parse(text1, name1)
        lang2 = VocabLang.parse(text2, name2)
        return VocabItem(
            line,
            lang1,
            lang2,
        )

@dataclass
class TrackerInfo:
    item: VocabItem
    response: Optional[str] = None
    score: Optional[int] = None
    points: Optional[int] = None

    def set_response_once(self, value: str):
        if self.response is None:
            self.response= value

    def set_score_once(self, value: int):
        if self.score is None:
            self.score = value

class Static:
    def __new__(cls):
        raise TypeError("Static classes cannot be instantiated")

class FileParser(Static):
    @staticmethod
    def get_valid_lines(content):
        lines = []
        for line in content.splitlines():
            if FileParser.is_line_valid(line):
                lines.append(line.strip())
        return lines

    @staticmethod
    def is_line_valid(line):
        if not line:
            return False
        if line.count(";") != 1:
            return False
        if FileParser.is_comment_line(line):
            return False
        return True

    @staticmethod
    def is_comment_line(line):
        stripped = line.strip()
        if stripped.startswith("//"):
            return True
        if stripped.startswith("#"):
            return True
        return False

    def strip_comment(line):
        stripped = line.strip()
        index = stripped.find("//")
        if index >= 0:
            return stripped[:index].strip()
        index = stripped.find("#")
        if index >= 0:
            return stripped[:index].strip()
        return stripped

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
            equiv = " ".join(combo).strip()
            equiv = re.sub(r' +', ' ', equiv)
            results.append(equiv)
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
        return ScoreFormatter.sanitize_score(max(ratios))

    @staticmethod
    def _sanitize(txt: str) -> str:
        sanitized = unidecode(txt.lower().strip())
        sanitized = re.sub(r'[^ a-z0-9]', '', sanitized, re.UNICODE)
        return sanitized

class WordTokenizer(Static):
    @staticmethod
    def tokenize(txt: str, tolower=True):
        sanitized = re.sub(r'[^\w\s]', ' ', txt).strip()
        if tolower:
            sanitized = sanitized.lower()
        toks = re.split(r"[\s'’]", sanitized)
        return [tok for tok in toks if tok]

class ScoreFormatter(Static):
    @staticmethod
    def sanitize_score(score: int | float) -> int:
        if score > 100:
            return 100
        elif 0 > score:
            return 0
        return int(score)

class Audio(Static):
    _TALK_LOCK = Lock()

    @staticmethod
    def beep():
        try:
            import winsound
            beep_ms = 700
            winsound.Beep(300, beep_ms)
        except:
            pass

    @staticmethod
    def wait_talk():
        while Audio._TALK_LOCK.locked():
            time.sleep(0.1)

    @staticmethod
    def _sanitize_for_talk(text: str) -> str:
        chars_to_strip = ["'’‘"]
        sanitized_text = text.replace('"', '').replace('“', '').replace('”', '')
        for char in chars_to_strip:
            sanitized_text = sanitized_text.strip(char)
        return sanitized_text

    @staticmethod
    def _adjust_speed(talkfile, speed):
        temptalk = File(f"{talkfile}.tmp")
        talkfile.move(temptalk)
        try:
            if has("ffmpeg"):
                cmd = f'ffmpeg -i "{temptalk}" -vn -b:a 192k -filter:a "atempo={speed}" "{talkfile}"'
                silent(cmd)
        except:
            q.warn(f"Could not adjust speed of file: {talkfile}")
        temptalk.delete()

    @staticmethod
    def talk(text, lang, slow=False, wait=False, speed=1, cache: Optional[bool] = None):
        if cache is None:
            cache = CACHE_TALK
        def _talk(talkfile):
            """Pronounces the given text in the given language."""
            with Audio._TALK_LOCK:
                playsound(talkfile, block=True)
                if not cache:
                    talkfile.delete()
        sanitized_text = Audio._sanitize_for_talk(text)
        sanitized_hash = str(hash(f"{lang}-{slow}-{speed}-{sanitized_text}")).replace("-", "d")
        talkfile = File(tempfile.gettempdir(), "ReviewVocab", "talk", lang, f"__temp-talk-{sanitized_hash}.mp3",)
        if not talkfile.exists():
            talkfile.make()
            tts(text=sanitized_text, lang=lang, slow=slow).save(talkfile)
            if not isclose(speed, 1):
                Audio._adjust_speed(talkfile, speed)
        try:
            t = Thread(target=_talk, args=(talkfile,))
            t.start()
            if wait:
                t.join()
        except KeyboardInterrupt:
            raise
        except Exception:
            q.warn("Could not talk at this time.")

class MainMenu(Static):
    @staticmethod
    def show(cfgpath):
        config = UtilConfig.from_path(cfgpath)
        provider = config.get_provider()
        mode_chars = {
            'learn': "l",
            'listen': "i",
            'translate': "t",
            'practice': "p",
            'rapid': "r",
        }
        dftmode = mode_chars[config.get_default_mode()]
        quit = False
        def trigger_quit():
            nonlocal quit
            quit = True
        def on_err():
            q.error("Mode exited early!")
        def build_mode(name):
            return lambda: trycatch(config.get_modecls(name)(config.get_modecfg(name), provider, config.get_trackers()).show_menu, oncatch=on_err, rethrow=DEBUG_MODE)()
        menu = q.Menu()
        for name,char in mode_chars.items():
            menu.add(char, f"{name.title()} mode", build_mode(name))
        menu.add("m", "Provider menu", provider.show_menu)
        menu.add("q", "Quit", trigger_quit)
        while not quit:
            menu.show(default=dftmode)

##==============================================================#
## SECTION: Function Definitions                                #
##==============================================================#

def format_equivs(equivs: list[str]) -> str:
    return " (OR) ".join(equivs)

def format_choice_extra(item, langnum=1):
    if langnum == 2:
        msg = item.lang2.choice
        if item.lang2.extra:
            msg += f" {item.lang2.extra}"
        return msg
    else:
        msg = item.lang1.choice
        if item.lang1.extra:
            msg += f" {item.lang1.extra}"
        return msg

def get_config_for_cls(cfg):
    return cfg.__forcls__

def flush_input():
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        import sys, termios
        termios.tcflush(sys.stdin, termios.TCIOFLUSH)

def accum_ave(prev_ave, prev_count, new_value):
    return ((prev_ave * prev_count) + new_value) / (prev_count + 1)

# Exponential Moving Average
def accum_ema(prev_ema, new_value, approx_window=5):
    alpha = (2 / (approx_window + 1))
    return (new_value * alpha) + (prev_ema * (1 - alpha))

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
