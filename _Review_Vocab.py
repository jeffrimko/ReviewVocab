##==============================================================#
## SECTION: Imports                                             #
##==============================================================#

import os
import os.path as op
import random; random.seed()
import string
import tempfile
import unicodedata
from collections import namedtuple
from threading import Thread, Lock

import qprompt as q
import related
from auxly import callstop, trycatch
from auxly.stringy import subat, randomize
from auxly.filesys import File, delete
from gtts import gTTS as tts
from playsound import playsound

##==============================================================#
## SECTION: Global Definitions                                  #
##==============================================================#

MISSED_VOCAB = "__temp-vocab_to_review.txt"

RANDOM_VOCAB = "__temp-random_vocab.txt"

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

@related.mutable
class TextInfo(object):
    text = related.StringField()
    lang = related.ChildField(LanguageInfo)

@related.mutable
class PracticeConfig(object):
    lang1 = related.ChildField(LanguageInfo)
    lang2 = related.ChildField(LanguageInfo)
    num = related.IntegerField(default=10)
    path = related.StringField(default="")
    redo = related.BooleanField(default=False)
    swap = related.BooleanField(default=False)

@related.mutable
class UtilConfig(object):
    lang1 = related.ChildField(LanguageInfo)
    lang2 = related.ChildField(LanguageInfo)
    redo = related.BooleanField(default=False)
    path = related.StringField(default=".")
    num = related.IntegerField(default=10)

class Practice(object):
    def __init__(self, config):
        self.config = config
        self.miss = set()
        self.okay = set()

    def start(self):
        self.miss = set()
        self.okay = set()
        path = get_file(self.config.path)
        lines = [line.strip() for line in File(path).readlines() if line][:self.config.num]
        random.shuffle(lines)
        for (num, line) in enumerate(lines):
            q.hrule()
            q.echo("%s of %s" % (num+1, len(lines)))
            self._ask(line)
        fmiss = File(op.join(self.config.path, MISSED_VOCAB))
        for miss in self.miss:
            fmiss.append(miss + "\n")

    def _ask(self, line):
        line = unicodedata.normalize('NFKD', line).encode('ascii','replace').decode("utf-8").strip()
        if not line: return
        qst = TextInfo(line.split(";")[0], self.config.lang1)
        ans = TextInfo(line.split(";")[1], self.config.lang2)
        if self.config.swap:
            ans,qst = qst,ans
        msg = qst.text
        if ans.lang.hint:
            msg += " (%s)" % hint(ans.text, ans.lang.hint)
        talk_qst = callstop(talk)
        while True:
            q.alert(msg)
            if qst.lang.talk:
                talk_qst(qst.text, qst.lang.name.short)
            rsp = q.ask_str("").lower().strip()
            ok = [x.lower().strip() for x in ans.text.split("(")[0].split("/")]
            if rsp in ok:
                q.echo("[CORRECT] " + ans.text)
                self.okay.add(line)
            else:
                q.error(ans.text)
                self.miss.add(line)
                if self.config.redo:
                    continue
            if ans.lang.talk:
                talk(ans.text, ans.lang.name.short, wait=True)
            return

class Util(object):
    def __init__(self, config):
        self.config = config

    def main_menu(self):
        def start(swap=False):
            p = PracticeConfig(swap=swap, **related.to_dict(self.config))
            trycatch(Practice(p).start)()
        menu = q.Menu()
        menu.add("1", f"{self.config.lang1.name.full} to {self.config.lang2.name.full}", start, [False])
        menu.add("2", f"{self.config.lang2.name.full} to {self.config.lang1.name.full}", start, [True])
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
                Setting("redo", q.ask_yesno, self.config),
                Setting("num", q.ask_int, self.config),
                Setting("hint", q.ask_int, self.config.lang2),
                Setting("talk", q.ask_yesno, self.config.lang2)]
        menu = q.Menu()
        for s in settings:
            menu.add(s.name[0], s.name.capitalize(), change, [s])
        menu.add("p", "Print", print, [self.config])
        menu.main(loop=True)

##==============================================================#
## SECTION: Function Definitions                                #
##==============================================================#

def get_file(path):
    menu = q.Menu()
    menu.add("f", "Select file")
    menu.add("r", "Random vocab")
    choice = menu.show()
    if "f" == choice:
        return ask_file(path)
    else:
        return make_random_file(path)

def ask_file(dpath, msg="File to review (blank to list)"):
    """Prompts user for a file to review. Returns the file name."""
    path = q.ask_str(msg, blk=True)
    if not path or not op.isfile(path):
        vfiles = [op.basename(f) for f in listdir(dpath) if f.endswith(".txt")]
        path = q.enum_menu(vfiles).show(returns="desc", limit=20)
        path = op.join(dpath, path)
    return path

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
        lines = [line.strip() for line in File(vfiles[filenum]).readlines() if line]
        linenum = random.randrange(len(lines))
        vocab = lines[linenum]
        if vocab not in vocabs:
            vocabs.append(vocab)
    rpath = op.join(path, RANDOM_VOCAB)
    File(rpath, del_at_exit=True).write("\n".join(vocabs))
    return rpath

def sort_all(path):
    okay = True
    for f in listdir(path):
        if f.endswith(".txt"):
            okay &= q.status("Sorting `%s`..." % op.basename(f), sort_file, [f])
    msg = "All files sorted successfully." if okay else "Issue sorting some files!"
    char = "-" if okay else "!"
    q.wrap(msg, char=char)

def sort_file(path=None):
    if not path:
        path = ask_file("File to sort")
    if not path.endswith(".txt"):
        q.error("Can only sort `.txt` files!")
        return
    with open(path) as fi:
        lines = fi.readlines()
    sorts = []
    okay = True
    for num,line in enumerate(lines):
        line = line.strip()
        try:
            if not line: continue
            en,it = line.split(";")
            enx = ""
            itx = ""
            if en.find("(") > -1:
                en,enx = en.split("(")
                en = en.strip()
                enx = " (" + enx
            if it.find("(") > -1:
                it,itx = it.split("(")
                it = it.strip()
                itx = " (" + itx
            en = "/".join(sorted(en.split("/")))
            it = "/".join(sorted(it.split("/")))
            sorts.append("%s%s;%s%s" % (en,enx,it,itx))
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
    if len(vocab) <= hintnum:
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
        if not i.endswith(".txt"): continue
        if i.startswith("__temp"): continue
        num = len(File(i).readlines())
        total += num
        q.echo("%u\t%s" % (num, op.basename(i)))
    q.wrap("Total = %u" % (total))

def listdir(path):
    for _,_,files in os.walk(path):
        for f in files:
            yield op.join(path, f)

def main():
    config = related.to_model(UtilConfig, related.from_yaml(File("config.yaml").read()))
    util = Util(config)
    trycatch(util.main_menu)()

##==============================================================#
## SECTION: Main Body                                           #
##==============================================================#

if __name__ == '__main__':
    main()
