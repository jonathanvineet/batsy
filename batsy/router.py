"""What is this about? Batsy decides the topic of a sentence first, so a handler for something else can't grab it.

Batsy's commands are many small handlers tried in order; each one matches its own words. That breaks when words
overlap ("next" is music and research, "lay it" is printing, "notes" is the brain and research), and whichever
handler comes first wins. The router looks at the whole sentence once, before any handler:

  cues     strong words for each topic ("slice", "nozzle", "playlist", "papers")
  meaning  the sentence's embedding against example sentences for each topic (nomic-embed-text on the HP)
  context  what you were just talking about: "lay it flat" right after a slice is still printing

When it's sure, only that topic's handlers (plus the general ones) may take the sentence. When it isn't, nothing
changes: the handlers run in their usual order. It learns as you use it: a sentence a handler took (and you didn't
correct) becomes an example for that topic, and "no, I meant the printer" moves the last one where it belongs.

Adding a topic for something new: one `TOPICS` entry with its cue words and a handful of example sentences, and tag
its handlers with `app._may("<topic>")`.
"""
import json
import logging
import re
import threading
import time
from dataclasses import dataclass

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.router")
LEARNED = DATA_DIR / "router_learned.jsonl"
FOLLOW_UP = re.compile(r"\b(?:it|this|that|them|those|these|again|also|too|instead|the same|one more|another)\b|"
                       r"^(?:and|but|now|then|also|no|not|make|do|lay|put|turn|try)\b")
CORRECTION = re.compile(r"^(?:no|nope|not that|wrong|that's wrong)?[ ,]*(?:i (?:meant|mean|was (?:talking|asking) about)|"
                        r"i'?m (?:talking|asking) about|that(?:'s| is| was) (?:about|for)|it'?s about|this is about) "
                        r"(?:the |my )?(?P<what>.+)$")


@dataclass
class Topic:
    cues: str           # regex of words that (almost) only mean this topic
    examples: list      # typical sentences, for meaning


TOPICS: dict[str, Topic] = {
    "print": Topic(r"\b(?:re ?slice|slic(?:e|ed|ing|er)|3d print\w*|printer|print(?:ing)? (?:it|this|that|the)|the print|my print|nozzle|filament|"
                   r"layer (?:height|shift\w*)|layers? (?:shift\w*|moved)|shifted|layer lines?|infill|supports?|tree supports?|brim|skirt|raft|bed (?:temp\w*|level\w*)|"
                   r"stl|3mf|g ?code|preheat|extruder|orca\w*|orient\w*|(?:lay|lie|put) (?:it|this|that) (?:down|flat)|"
                   r"lying down|scale (?:it|this|that)|resize|calibration|temperature tower|first layer|warp\w*|stringing|"
                   r"overhangs?|fuzzy skin|ironing|print (?:time|speed|quality))\b",
                   ["slice it with 0.16 layer height", "how is the print going", "the print shifted a layer",
                    "lay the mask flat and slice it", "scale it down to 50 mm", "preheat the printer",
                    "make it stronger with more walls", "how long will it take to print", "print it in PETG",
                    "the supports are too hard to remove", "find me a phone stand to print"]),
    "research": Topic(r"\b(?:research\w*|papers?|journals?|literature|citations?|sources?|findings|hypothes\w+|study|studies|"
                      r"report on|write ?up|deep dive|go deeper|think hard|open questions|arxiv|semantic scholar|"
                      r"what do we know)\b",
                      ["start a new research on how bats navigate", "find papers on echolocation",
                       "what do we know about the detector", "prepare a report on the gold project",
                       "what are the open questions in my research", "go deeper on the DP5 protocol",
                       "save the second paper", "summarise my research"]),
    "brain": Topic(r"\b(?:note (?:that|down|this)|jot|my brain|obsidian|vault|journal|daily note|what did i do)\b",
                   ["note that the meeting moved to Friday", "what did I do yesterday", "open my brain",
                    "add this to my journal"]),
    "music": Topic(r"\b(?:play(?:ing)?|songs?|tracks?|playlists?|spotify|album|artist|music|skip|next song|previous song|"
                   r"shuffle|discover weekly|volume|pause|resume)\b",
                   ["play some lo-fi", "next song", "play songs by Lionel Richie", "make my discovery playlist",
                    "pause the music", "what's playing", "skip this one", "turn the volume up"]),
    "mail": Topic(r"\b(?:e ?mails?|inbox|gmail|mail from|unread|reply to)\b",
                  ["check my email", "any important emails today", "read the email from Karan",
                   "what's due from my email", "prepare the report for that email"]),
    "home": Topic(r"\b(?:lights?|lamp|fan|night mode|bedroom|esp ?32|switch (?:on|off))\b",
                  ["turn on the light", "night mode", "switch off the lamp"]),
    "pc": Topic(r"\b(?:the pc|my pc|desktop pc|batman drive|nightwing|superman|on the pc)\b",
                ["open YouTube on the PC", "shut down the PC", "what's on the batman drive"]),
    "git": Topic(r"\b(?:git|commit|push|pull request|repo(?:sitory)?|branch|merge)\b",
                 ["commit and push the batsy folder", "what changed in the repo", "pull the latest"]),
    "phone": Topic(r"\b(?:my phone|iphone|android|widget app|pair (?:my )?phone|phone app)\b",
                   ["install the widget app on my phone", "pair my phone", "is my phone connected"]),
    "shopping": Topic(r"\b(?:price|prices|cost|costs|cheapest|buy|amazon|flipkart|robu|croma|in stock|order)\b",
                      ["how much does an ESP32 cost", "find the cheapest PLA filament", "compare prices for a soldering iron"]),
    "info": Topic(r"\b(?:weather|rain|forecast|sunrise|sunset|what time|what day|date today|convert|exchange rate|"
                  r"euros?|dollars?|rupees?|inr|usd|calculate|square root|plus|minus|times|divided)\b",
                  ["what's the weather tomorrow", "35K euros in INR", "what time is it", "what's 15 percent of 240",
                   "will it rain today"]),
    "todo": Topic(r"\b(?:remind(?:er)?s?|to ?dos?|tasks?|alarm|timer|tick (?:it )?off|done with|schedule|calendar)\b",
                  ["remind me to call mom at 6", "add buy milk to my to do", "I finished the video",
                   "set a timer for 10 minutes", "what's on my list today"]),
    "system": Topic(r"\b(?:restart yourself|shut ?down|reboot|update the system|terminal|battery|cpu|laptop temp\w*|"
                    r"running hot|(?:how )?hot is the laptop|the laptop|fans?|temperature of|wifi|bluetooth|screen ?saver|cockpit)\b",
                    ["restart yourself", "how hot is the laptop", "open a terminal", "update the system"]),
}


@dataclass
class Decision:
    topic: str | None    # None: not sure, let every handler try as before
    score: float = 0.0
    why: str = ""
    lean: str | None = None  # the likeliest topic even when not sure ("upright with the head down": print)


class Router:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.last: tuple[str, str, float] | None = None   # (text, topic it went to, when)
        self.pending: tuple[str, str, float] | None = None  # handled, kept unless you correct it next turn
        self.vectors: dict[str, list] = {}                 # topic -> example vectors
        self.learned = self._load()
        self._cues = {k: re.compile(t.cues) for k, t in TOPICS.items()}
        threading.Thread(target=self._warm, daemon=True, name="router-warm").start()

    # --- deciding ---------------------------------------------------------------------------------------------------
    def route(self, n: str) -> Decision:
        """The topic of `n` (normalised text), or Decision(None) when it isn't clear."""
        cue = {k: len(rx.findall(n)) for k, rx in self._cues.items()}
        cue["research"] += self._research_names(n)  # "this gold thing": the name of one of your researches
        sem = self._meaning(n)
        recent = self.last if self.last and time.time() - self.last[2] < 180 else None
        follow = bool(FOLLOW_UP.search(n))
        scores = {}
        for k in TOPICS:
            s = min(1.0, 0.55 * cue[k]) * 0.6
            if sem:
                s += 0.5 * max(0.0, min(1.0, (sem.get(k, 0) - 0.45) / 0.3))
            if recent and recent[1] == k:  # what we were just on: a strong pull for "lay it flat", a nudge otherwise
                s += 0.3 if follow else 0.05  # only a follow-up ("lay it flat", "do it again") carries over
            scores[k] = s
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        (top, s1), (_, s2) = ranked[0], ranked[1]
        sure = s1 >= 0.42 and s1 - s2 >= 0.14
        why = ", ".join(f"{k} {v:.2f}" for k, v in ranked[:3])
        log.info("topic %r -> %s (%s)", n[:60], top if sure else "unsure", why)
        return Decision(top if sure else None, s1, why, top if s1 >= 0.3 else None)

    def _research_names(self, n: str) -> int:
        """Words that name one of your researches ("gold", "dp5", "echolocation") count as research words."""
        if time.time() - getattr(self, "_names_t", 0) > 300:
            try:
                from batsy import research

                skip = {"project", "research", "ma'am", "maam", "sir", "the", "and", "for", "with", "how", "what", "on"}
                words = set()
                for r in research.all_research():
                    words |= {w for w in re.findall(r"[a-z0-9']{3,}", f"{r['name']} {r.get('question') or ''}".lower())
                              if w not in skip and not w.endswith("'s")}
                self._names = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(words))) + r")\b") if words else None
            except Exception:
                self._names = None
            self._names_t = time.time()
        return len(self._names.findall(n)) if getattr(self, "_names", None) else 0

    def _meaning(self, n: str) -> dict[str, float] | None:
        if not self.vectors:
            return None
        v = _embed([n], self.cfg, timeout=1.2)
        if not v:
            return None
        import numpy as np

        q = np.asarray(v[0], dtype="float32")
        q /= np.linalg.norm(q) or 1
        out = {}
        for k, vs in self.vectors.items():
            m = np.asarray(vs, dtype="float32")
            sims = m @ q
            out[k] = float(np.sort(sims)[-2:].mean()) if len(sims) > 1 else float(sims[0])  # best two examples
        return out

    # --- learning ---------------------------------------------------------------------------------------------------
    def handled(self, text: str, topic: str) -> None:
        """A handler for `topic` took `text`. Kept as an example once the next turn doesn't correct it."""
        with self.lock:
            self._commit()
            self.pending = (text, topic, time.time())
            self.last = (text, topic, time.time())

    def correction(self, n: str) -> tuple[str, str] | None:
        """ "no, I meant the printer": (the sentence before, the topic it should have gone to), and remembered."""
        m = CORRECTION.match(n)
        if not m or not self.last or time.time() - self.last[2] > 300:
            return None
        what = m["what"]
        topic = next((k for k, rx in self._cues.items() if rx.search(what)), None) or \
            next((k for k in TOPICS if k in what), None)
        if not topic:
            sem = self._meaning(what)
            topic = max(sem, key=sem.get) if sem else None
        if not topic:
            return None
        with self.lock:
            text = self.last[0]
            self.pending = None  # the wrong guess isn't learned
            self._learn(text, topic)
            self.last = (text, topic, time.time())
        log.info("corrected: %r is about %s", text, topic)
        return text, topic

    def _commit(self) -> None:
        if self.pending and time.time() - self.pending[2] < 600:
            self._learn(*self.pending[:2])
        self.pending = None

    def _learn(self, text: str, topic: str) -> None:
        if topic not in TOPICS or len(text.split()) < 2:
            return
        mine = [x for x in self.learned if x["topic"] == topic]
        if any(x["text"].lower() == text.lower() for x in mine):
            return
        row = {"text": text[:200], "topic": topic, "t": int(time.time())}
        self.learned.append(row)
        # at most 60 learned per topic (oldest go)
        drop = [x for x in mine][:-59] if len(mine) >= 60 else []
        self.learned = [x for x in self.learned if x not in drop]
        try:
            LEARNED.write_text("".join(json.dumps(x) + "\n" for x in self.learned))
        except OSError:
            pass
        v = _embed([text], self.cfg, timeout=5)
        if v and topic in self.vectors:
            import numpy as np

            a = np.asarray(v[0], dtype="float32")
            self.vectors[topic].append((a / (np.linalg.norm(a) or 1)).tolist())

    def _load(self) -> list[dict]:
        try:
            return [json.loads(x) for x in LEARNED.read_text().splitlines() if x.strip()]
        except (OSError, ValueError):
            return []

    def _warm(self) -> None:
        """Embed every topic's examples (and the learned ones) once; retried until the HP answers."""
        import numpy as np

        for wait in (0, 10, 30, 60, 120, 300):
            time.sleep(wait)
            texts, owners = [], []
            for k, t in TOPICS.items():
                for e in t.examples + [x["text"] for x in self.learned if x["topic"] == k]:
                    texts.append(e)
                    owners.append(k)
            v = _embed(texts, self.cfg, timeout=60)
            if not v:
                continue
            vecs: dict[str, list] = {k: [] for k in TOPICS}
            for k, e in zip(owners, v):
                a = np.asarray(e, dtype="float32")
                vecs[k].append((a / (np.linalg.norm(a) or 1)).tolist())
            self.vectors = vecs
            log.info("topic router ready: %d examples, %d learned", len(texts), len(self.learned))
            return


def _embed(texts: list[str], cfg, timeout: float) -> list | None:
    import urllib.request

    from batsy import llm_hosts

    try:
        body = json.dumps({"model": cfg.get("research.embed_model") or "nomic-embed-text", "keep_alive": -1,
                           "input": ["classification: " + t[:500] for t in texts]}).encode()
        req = urllib.request.Request(llm_hosts.best(cfg) + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("embeddings")
    except Exception as e:
        log.debug("router embed: %s", e)
        return None
