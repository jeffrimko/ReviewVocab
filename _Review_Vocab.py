##==============================================================#
## SECTION: Imports                                             #
##==============================================================#

from collections import namedtuple
from datetime import datetime
from difflib import SequenceMatcher
from threading import Thread, Lock
import os
import os.path as op
import math
import random; random.seed()
import statistics
import string
import sys
import tempfile
import time
import unicodedata

from auxly import callstop, trycatch
from auxly.filesys import File, Path, delete
from auxly.stringy import subat, randomize
from gtts import gTTS as tts
from playsound import playsound
from tinydb import TinyDB, Query
from unidecode import unidecode
import qprompt as q
import related

##==============================================================#
## SECTION: Global Definitions                                  #
##==============================================================#

DEBUG_MODE = False

MISSED_VOCAB = "__temp-missed_vocab.txt"
RANDOM_VOCAB = "__temp-random_vocab.txt"

PREVIOUS_PATH = None

TALK_LOCK = Lock()

Setting = namedtuple('Setting', 'name func conf')

##==============================================================#
## SECTION: Global Definitions                                  #
##==============================================================#

@related.immutable
class LanguageName(object):
    short = related.StringField(default="en")
    full = related.StringField(default="English")

@related.mutable
class LanguageInfo(object):
    name = related.ChildField(LanguageName)
    talk = related.BooleanField(default=False)
    hint = related.IntegerField(default=0)
    dynamic = related.BooleanField(default=False)

@related.mutable
class TextInfo(object):
    text = related.StringField()
    rand = related.StringField()
    lang = related.ChildField(LanguageInfo)

@related.mutable
class UtilConfig(object):
    lang1 = related.ChildField(LanguageInfo)
    lang2 = related.ChildField(LanguageInfo)
    redo = related.BooleanField(default=False)
    path = related.StringField(default=".")
    num = related.IntegerField(default=10)
    record = related.BooleanField(default=True)

@related.mutable
class PracticeConfig(UtilConfig):
    swap = related.BooleanField(default=False)

class Practice(object):
    def __init__(self, config):
        self.config = config
        self.miss = set()
        self.okay = set()
        dbpath = Path(self.config.path, "db.json")
        self.db = TinyDB(dbpath)

    def learn(self):
        path = get_file(self.config.path)
        lines = [line.strip() for line in File(path).read().splitlines() if line]
        random.shuffle(lines)
        lines = lines[:self.config.num]
        q.clear()
        for num,line in enumerate(lines, 1):
            q.echo("%s of %s" % (num, len(lines)))
            qst,ans = self._get_qst_ans(line)
            vld = Practice._get_valid(ans)
            q.alert(qst.rand)
            q.alert(ans.rand)
            talk(qst.rand, qst.lang.name.short)
            talk(ans.rand, ans.lang.name.short, slow=True)
            rsp = ""
            while rsp not in vld:
                rsp = q.ask_str("").lower().strip()
            wait()
            q.clear()
            flush_input()
            talk(ans.rand, ans.lang.name.short, slow=True)
            q.echo("%s of %s" % (num, len(lines)))
            ans.lang.hint = len(ans.rand) // 4
            rsp = ""
            while rsp not in vld:
                msg = Practice._get_msg_base(qst) + Practice._get_msg_hint(ans)
                q.alert(msg)
                rsp = q.ask_str("").lower().strip()
                ans.lang.hint += 1
            q.echo("[CORRECT] " + ans.text)
            talk(ans.rand, ans.lang.name.short, wait=True)
            q.clear()

    def start(self):
        path = get_file(self.config.path)
        lines = [line.strip() for line in File(path).read().splitlines() if line]
        random.shuffle(lines)
        lines = lines[:self.config.num]
        while True:
            q.clear()
            self.miss = set()
            self.okay = set()
            for num, line in enumerate(lines, 1):
                q.echo("%s of %s" % (num, len(lines)))
                self._ask(line)
                q.hrule()
            fmiss = File(self.config.path, MISSED_VOCAB)
            for miss in self.miss:
                fmiss.append(miss + "\n")
            q.echo("Results:")
            q.echo(f"Correct = {len(self.okay)}")
            q.echo(f"Missed = {len(self.miss)}")
            q.hrule()
            if not q.ask_yesno("Retry?", default=False):
                break
            if self.miss and q.ask_yesno("Missed only?", default=True):
                lines = list(self.miss)
            if q.ask_yesno("Shuffle?", default=True):
                random.shuffle(lines)
                lines = lines[:self.config.num]

    def _get_qst_ans(self, line):
        qst = TextInfo(line.split(";")[0], "", self.config.lang1)
        qst.rand = random.choice(parse_valid(qst.text))
        ans = TextInfo(line.split(";")[1], "", self.config.lang2)
        ans.rand = random.choice(parse_valid(ans.text))
        if self.config.swap:
            ans,qst = qst,ans
        return qst,ans

    @staticmethod
    def _get_msg_base(qst):
        return (qst.rand + " " + parse_extra(qst.text)).strip()

    @staticmethod
    def _get_msg_hint(ans):
        return " (%s)" % hint(ans.rand, ans.lang.hint)

    @staticmethod
    def _get_valid_orig(ans):
        return parse_valid(ans.text)

    @staticmethod
    def _get_valid(ans):
        ok_orig = Practice._get_valid_orig(ans)
        ok = [] + ok_orig
        ok_ascii = []
        for o in ok:
            ascii_only = unidecode(o)
            if ascii_only not in ok:
                ok_ascii.append(ascii_only)
        ok += ok_ascii
        return ok

    def _ask(self, line):
        if not line: return
        qst,ans = self._get_qst_ans(line)
        msg = Practice._get_msg_base(qst)
        if ans.lang.hint or ans.lang.dynamic:
            if ans.lang.dynamic:
                Record = Query()
                results = self.db.search(Record.ln == line)
                # Get most recent results.
                num_results = 3
                results = sorted(results, key=lambda r: r['dt'], reverse=True)[:num_results]
                oks = [r['ok'] for r in results]
                while len(oks) < num_results:
                    oks.append(0.7)
                ratio = 0
                if results:
                    ratio = statistics.mean(oks)
                ans.lang.hint = dynamic_hintnum(ans.rand, ratio)
            if ans.lang.hint:
                msg += Practice._get_msg_hint(ans)
        talk_qst = callstop(talk)
        tries = 0
        while True:
            q.alert(msg)
            if qst.lang.talk:
                talk_qst(qst.rand, qst.lang.name.short)
            t_start = time.time()
            rsp = q.ask_str("").lower().strip()
            sec = time.time() - t_start
            vld = Practice._get_valid(ans)
            record = self._prep_record(line, rsp, qst, ans, sec, tries)
            closest_orig, _ = guess_similarity(rsp, Practice._get_valid_orig(ans))
            if rsp in vld:
                record['ok'] = 1.0
                q.echo("[CORRECT] " + ans.text)
                if self.config.record:
                    self.db.insert(record)
            else:
                tries += 1
                _, record['ok'] = guess_similarity(rsp, vld)
                q.error(closest_orig)
                if self.config.record:
                    self.db.insert(record)
                if self.config.redo:
                    continue

            if tries > 0:
                self.miss.add(line)
            else:
                self.okay.add(line)
            if ans.lang.talk:
                say, _ = guess_similarity(rsp, Practice._get_valid_orig(ans))
                talk(say, ans.lang.name.short, wait=True)
            return

    @staticmethod
    def _prep_record(line, rsp, qst, ans, sec, tries):
        record = {}
        record['dt'] = datetime.utcnow().isoformat()
        record['ln'] = line
        record['rs'] = rsp
        record['qt'] = qst.text
        record['at'] = ans.text
        record['ql'] = qst.lang.name.short
        record['al'] = ans.lang.name.short
        record['ht'] = ans.lang.hint
        record['tr'] = tries
        record['sc'] = sec
        return record

class Util(object):
    """Provides a CLI utility for vocab practice."""
    def __init__(self, config):
        self.config = config

    def main_menu(self):
        def start(swap=False):
            p = PracticeConfig(swap=swap, **related.to_dict(self.config))
            trycatch(Practice(p).start, rethrow=DEBUG_MODE)()
        def learn():
            p = PracticeConfig(swap=False, **related.to_dict(self.config))
            trycatch(Practice(p).learn, rethrow=DEBUG_MODE)()
        menu = q.Menu()
        menu.add("1", f"{self.config.lang1.name.full} to {self.config.lang2.name.full}", start, [False])
        menu.add("2", f"{self.config.lang2.name.full} to {self.config.lang1.name.full}", start, [True])
        menu.add("l", "Learn vocab", learn)
        menu.add("d", "Delete missed vocab", delete, [op.join(self.config.path, MISSED_VOCAB)])
        menu.add("c", "Count vocab", count, [self.config.path])
        menu.add("a", "Sort all files", sort_all, [self.config.path])
        menu.add("s", "Settings", trycatch(self.settings_menu))
        menu.main(loop=True)

    def settings_menu(self):
        def change(s):
            default = getattr(s.conf, s.name)
            setattr(s.conf, s.name, s.func(f"{s.name.capitalize()}", default=default))
        settings = [
                Setting("record", q.ask_yesno, self.config),
                Setting("redo", q.ask_yesno, self.config),
                Setting("num", q.ask_int, self.config),
                Setting("talk", q.ask_yesno, self.config.lang2),
                Setting("hint", q.ask_int, self.config.lang2),
                Setting("dynamic", q.ask_yesno, self.config.lang2)]
        menu = q.Menu()
        for i,s in enumerate(settings, 1):
            menu.add(str(i), s.name.capitalize(), change, [s])
        menu.add("p", "Print", print, [self.config])
        menu.main(loop=True)

##==============================================================#
## SECTION: Function Definitions                                #
##==============================================================#

def parse_extra(text):
    specialchars = "()"
    for char in specialchars:
        text = text.replace(char, f" {char} ")
    toks = text.split()

    # Include text in parenthesis.
    included = []
    include = False
    for tok in toks:
        if tok == "(":
            include = True
            included.append(tok)
        elif tok == ")":
            include = False
            included.append(tok)
        else:
            if include:
                included.append(tok)
    return " ".join(included).replace("( ", "(").replace(" )", ")")

def parse_valid(text):
    """Parses the input formatted text into a list of valid strings."""
    specialchars = "/()"
    for char in specialchars:
        text = text.replace(char, f" {char} ").lower()

    # Exclude text in parenthesis.
    included = []
    include = True
    toks = text.split()
    for tok in toks:
        if tok == "(": include = False
        elif tok == ")": include = True
        else:
            if include:
                included.append(tok)

    # Parse final valid text.
    valid = []
    buff = [""]
    for inc in included:
        if inc == "/":
            for i,_ in enumerate(buff):
                if buff[i]:
                    valid.append(buff[i].strip())
            buff = [""]
        elif "|" in inc:
            toks = inc.split("|")
            new_buff = []
            for p in buff:
                for t in toks:
                    new_buff.append(p + t + " ")
            buff = new_buff
        else:
            for i,_ in enumerate(buff):
                buff[i] += inc + " "
    for i,_ in enumerate(buff):
        if buff[i]:
            valid.append(buff[i].strip())
    return valid

def guess_similarity(actual, expected):
    """Compares the given actual word input against a list of expected words
    and returns the best similarity match as a ratio where 1.0 is correct and
    0.0 is very incorrect."""
    max_vocab = expected[0]
    max_ratio = 0.0
    for exp in expected:
        ratio = SequenceMatcher(None, actual, exp).ratio()
        if ratio > max_ratio:
            max_vocab = exp
            max_ratio = ratio
    return max_vocab, max_ratio

def get_file(dpath):
    global PREVIOUS_PATH
    menu = q.Menu()
    menu.add("f", "Select file")
    menu.add("r", "Random vocab")
    if PREVIOUS_PATH:
        menu.add("p", f"Previous file ({PREVIOUS_PATH.name})")
    choice = menu.show()
    fpath = None
    if "f" == choice:
        fpath = ask_file(dpath)
    elif "p" == choice:
        fpath = PREVIOUS_PATH
    else:
        fpath = make_random_file(dpath)
    PREVIOUS_PATH = Path(fpath)
    return fpath

def ask_file(dpath, msg="File to review (blank to list)"):
    """Prompts user for a file to review. Returns the file name."""
    fname = q.ask_str(msg, blk=True)
    path = Path(dpath, fname)
    if not path or not path.isfile():
        vfiles = [op.basename(f) for f in listdir(dpath) if f.endswith(".txt")]
        path = q.enum_menu(vfiles).show(returns="desc", limit=20)
        path = op.join(dpath, path)
    return path

def wait():
    while TALK_LOCK.locked():
        time.sleep(0.1)

def talk(text, lang, slow=False, wait=False):
    def _talk():
        """Pronounces the given text in the given language."""
        with TALK_LOCK:
            tpath = op.join(tempfile.gettempdir(), f"__temp-talk-{randomize(6)}.mp3")
            tts(text=text, lang=lang, slow=slow).save(tpath)
            playsound(tpath)
            delete(tpath)
    try:
        t = Thread(target=_talk)
        t.start()
        if wait:
            t.join()
    except:
        q.warn("Could not talk at this time.")

def make_random_file(path, num=20):
    vocabs = []
    vfiles = [f for f in listdir(path) if (f.endswith(".txt") and not f.startswith("__"))]
    while len(vocabs) != num:
        random.shuffle(vfiles)
        filenum = random.randrange(len(vfiles))
        lines = [line.strip() for line in File(vfiles[filenum]).read().splitlines() if line]
        linenum = random.randrange(len(lines))
        vocab = lines[linenum]
        if vocab not in vocabs:
            vocabs.append(vocab)
    rpath = op.join(path, RANDOM_VOCAB)
    File(rpath, del_at_exit=True).write("\n".join(vocabs))
    return rpath

def sort_all(dirpath):
    """Alphabetically sort the contents of all found vocabulary txt files in
    the given directory path."""
    okay = True
    for f in listdir(dirpath):
        if f.endswith(".txt"):
            okay &= q.status("Sorting `%s`..." % op.basename(f), sort_file, [f])
    msg = "All files sorted successfully." if okay else "Issue sorting some files!"
    char = "-" if okay else "!"
    q.wrap(msg, char=char)

def sort_file(path=None):
    """Alphabetically sort the contents of the given vocabulary txt file."""
    if not path:
        path = ask_file("File to sort")
    if not path.endswith(".txt"):
        q.error("Can only sort `.txt` files!")
        return
    with open(path) as fi:
        lines = fi.read().splitlines()
    sorts = []
    okay = True
    for num,line in enumerate(lines):
        line = line.strip()
        try:
            if not line: continue
            l1,l2 = line.split(";")
            l1x = ""
            l2x = ""
            if l1.find("(") > -1:
                l1,l1x = l1.split("(")
                l1 = l1.strip()
                l1x = " (" + l1x
            if l2.find("(") > -1:
                l2,l2x = l2.split("(")
                l2 = l2.strip()
                l2x = " (" + l2x
            l1 = "/".join(sorted(l1.split("/")))
            l2 = "/".join(sorted(l2.split("/")))
            sorts.append("%s%s;%s%s" % (l1,l1x,l2,l2x))
        except:
            okay = False
            temp = unicodedata.normalize('NFKD', line).encode('ascii','ignore').strip()
            q.warn("Issue splitting `%s` line %u: %s" % (path, num, temp))
            sorts.append(line)
    with open(path, "w") as fo:
        for line in sorted(set(sorts)):
            fo.write(line + "\n")
    return okay

def hint(vocab, hintnum, skipchars=" /'"):
    """Returns the given string with all characters expect the given hint
    number replaced with an asterisk. The given skip characters will be
    excluded."""
    random.seed()
    idx = []
    onlychars = vocab.replace(" ", "").replace("'", "").strip()
    numchars = len(onlychars)
    while hintnum >= numchars:
        hintnum -= 1
    if hintnum < 1:
        hintnum = 1
    while len(idx) < hintnum:
        cidx = random.randrange(0, len(vocab))
        if vocab[cidx] not in skipchars and cidx not in idx:
            idx.append(cidx)
    hint = vocab
    for i,c in enumerate(vocab):
        if c in skipchars:
            continue
        if i not in idx:
            hint = subat(hint, i, "*")
    return hint

def count(path):
    total = 0
    for i in listdir(path):
        try:
            if not i.endswith(".txt"): continue
            if i.startswith("__temp"): continue
            num = len(File(i).readlines())
            total += num
            q.echo("%u\t%s" % (num, op.basename(i)))
        except:
            pass
    q.wrap("Total = %u" % (total))

def dynamic_hintnum(vocab, ratio):
    if ratio > 0.98:
        return 0
    if ratio < 0.8:
        ratio -= 0.2
    elif ratio < 0.9:
        ratio -= 0.1
    if ratio < 0.35:
        ratio = 0.35
    onlychars = vocab.replace(" ", "").replace("'", "").strip()
    numchars = len(onlychars)
    hintnum = numchars - math.floor(numchars * ratio)
    return hintnum

def listdir(path):
    for _,_,files in os.walk(path):
        for f in files:
            yield op.join(path, f)

def flush_input():
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        import sys, termios
        termios.tcflush(sys.stdin, termios.TCIOFLUSH)

def main():
    cpath = trycatch(lambda: sys.argv[1], oncatch=lambda: "config.yaml")()
    config = related.to_model(UtilConfig, related.from_yaml(File(cpath).read()))
    util = Util(config)
    trycatch(util.main_menu, rethrow=DEBUG_MODE)()

##==============================================================#
## SECTION: Main Body                                           #
##==============================================================#

if __name__ == '__main__':
    main()
