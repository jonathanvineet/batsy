"""Talking to Batsy about a research: instant commands (no AI), a thinking partner on the free local AI, and Claude
only when asked ("think hard", "go deeper", "find other ways").

  start a new research on how bats navigate     switch to my drone research      list my research
  what's my bat research about                  note: echolocation works to ~20 m (while a research is on)
  add to my bat research: ...                   what do we know about ...         what are the open questions
  what should I look at next                    find papers on ...  ->  save the second one
  summarise my bat research                     think hard about ... / go deeper / find other ways
  stop the research (it's put away; anything you say goes back to normal)
"""
import datetime as dt
import json
import logging
import re
import subprocess
import threading
import time

from batsy import research

log = logging.getLogger("batsy.research")

DEEP = re.compile(r"\b(?:think (?:hard|deeply|deeper|harder|it through properly)|go deeper|dig deeper|deep dive|"
                  r"(?:find|look for|other|different) (?:me )?(?:other |different )?(?:ways|angles|approaches)|"
                  r"research (?:this|that|it) (?:properly|thoroughly|in depth)|challenge (?:this|that|me|it)|"
                  r"ask claude|properly research|find out(?: more)?(?: about)?|look (?:it|this|that) up|learn (?:more )?about|"
                  r"understand (?:it|this|that|the project)|ask (?:chatgpt|chat gpt|reddit|the internet)|search reddit)\b")
REPORTY = re.compile(r"\b(?:report|write ?-?up|document|doc|summary|paper|essay|pdf|brief|overview|presentation)\b")
ACKS = re.compile(r"(?:okay|ok|alright|all right|right|sure|cool|got it|thanks|thank you|hmm+|mm+|yeah|yes|no|nope|"
                  r"fine|great|nice|good)(?: batsy)?")
STARTY = re.compile(r"\b(?:start(?:ed)?|begin|new|create|open up|set up|look into|dig into|investigate|explore|research|"
                    r"study|learn about|find out about)\b")
ORDINAL = re.compile(r"\b(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|number (?:one|two|three|four|five|\d)|"
                     r"(?:the )?(?:one|two|three|four|five|\d)(?: one)?$|last one|top one)\b")
ORD = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2,
       "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4, "five": 4}

last_intent: dict = {"text": "", "action": "", "t": 0.0}  # so the local brain only talks research when it IS research
pending: dict = {}           # a question Batsy asked about a file: {"kind": "which"|"new", ...}
_found: dict = {"t": 0, "rid": None, "items": []}   # the last paper search, for "save the second one"
_turns: list = []            # this conversation: [(t, rid, you, batsy)]
_closer = None


# --- understanding what he means (the local AI, not a list of phrases) ----------------------------------------------
# Only worth asking the AI when it could be about research at all; everything else goes straight on to the rest of
# Batsy (lights, music, the printer...), so they stay instant.
MAYBE = re.compile(r"\b(?:research\w*|report|write ?-?up|stud(?:y|ies|ying)|papers?|articles?|literature|sources?|thesis|hypothes\w+|"
                   r"look into|dig into|investigat\w+|explor\w+|find out about|learn about|read up|deep dive|"
                   r"notes?|topic|summari[sz]e|open questions?|think (?:hard|deeper)|go deeper)\b")
INTENT = """You understand what Jonathan says to his research assistant (speech-to-text: guess sensibly at misheard \
words). The message gives his research, the one he's on, and what just happened, then what he said.
Reply with JSON only: {{"action": "...", "research": "", "topic": "", "text": "", "query": "", "n": 0}}
action is one of:
- "start": he wants a new research. topic = what it's about, or "" if he didn't say.
- "switch": work on one of his existing research. research = its name.
- "list": which research he has.   - "about": an overview of one. research = its name.
- "note": he wants something kept as a note. text = the note itself, in his words. research = which one, if he said.
- "find_papers": look for papers/articles/sources. query = what to search for.
- "save_paper": keep one of the papers just found. n = its number (1-5).
- "know": what do we already know / what do the notes say about something. query = it.
- "questions": the open questions.   - "next": what to do or read next.   - "summarise": a fresh summary.
- "deep": think hard / go deeper / find other ways / research it properly / challenge it.
- "report": write up the research as a report / document / write-up / summary to read or share. research = which.
- "rename": give a research a new name. research = which one (its current name), text = the new name.
- "stop": put the research away / done for now.
- "talk": he's thinking aloud or discussing the research he's on (an idea, a question, a doubt).
- "none": not about his research at all (music, lights, printer, time, weather, files, chat about something else).
Examples: "I want to start a research" -> start, topic "". "can we look into how bats fly" -> start (or switch if he \
has it). "keep the second one" / "save number 3" after papers were read out -> save_paper, n 2 / 3. "what did we find \
about cooling" -> know. "maybe water cooling would help" while on a research -> talk. "prepare a report on the gold project" -> report, \
research = the gold one. "play some music" -> none."""


def is_talk(text: str) -> bool:
    """Did the intent check just decide this sentence is him discussing the research (not a currency question)?"""
    return last_intent["text"] == text and last_intent["action"] == "talk" and time.time() - last_intent["t"] < 300


def intent(text: str) -> dict:
    from batsy import llm_hosts

    rs = research.all_research()
    act = research.active()
    cfg = research._cfg
    body = {"model": cfg.get("local.model"), "stream": False, "keep_alive": cfg.get("local.keep_alive"), "format": "json",
            "messages": [{"role": "system", "content": INTENT},
                         {"role": "user", "content": (
                             f"His research: {', '.join(repr(r['name']) for r in rs) or 'none yet'}. "
                             f"He's on: {repr(act['name']) if act else 'none'}. "
                             + (f"Just now: I read out {len(_found['items'])} papers, numbered. "
                                if _found["items"] and time.time() - _found["t"] < 900 else "")
                             + (f"Just now: I asked him {pending.get('kind')}. " if pending else "")
                             + f"\nHe said: {text}")}],
            "options": {"temperature": 0, "num_predict": 80, "num_ctx": 4096}}  # the same size as every other call:
    # a different num_ctx makes Ollama reload the model
    try:
        with llm_hosts.live(), llm_hosts.open_chat(cfg, body, timeout=60) as r:
            j = json.loads(json.loads(r.read())["message"]["content"])
        log.info("research intent %r -> %s", text[:60], j)
        return j if isinstance(j, dict) else {"action": "none"}
    except Exception as e:
        log.info("research intent: %s", e)
        return {"action": "none"}


def handle(app, n: str, raw: str, explicit: bool = True) -> str | None:
    """Anything about research, understood by the local AI and done. None: not about research (or just talk about
    the active research, which the local brain streams as a conversation)."""
    said = _answer_pending(n, raw)
    if said:
        return said
    active = research.active()
    # explicit (early in Batsy's chain): only sentences with research words. Otherwise (the end of the chain, after
    # every instant command had its go): anything, while a research is on.
    if ACKS.fullmatch(n.strip()):
        last_intent.update(text=raw or n, action="none", t=time.time())  # "okay", "right": not a thought to discuss
        return None
    picking = bool(_found["items"] and time.time() - _found["t"] < 900 and ORDINAL.search(n))
    if not (MAYBE.search(n) or picking):
        if explicit or not active:
            return None
        rel = research.relevance(active["id"], raw or n)
        if rel is not None and rel >= 0.62:  # by meaning, it's about the research: talk it through
            last_intent.update(text=raw or n, action="talk", t=time.time())
            return None
        if rel is not None and rel < 0.40:  # clearly something else ("remind me…", "what's on tonight")
            last_intent.update(text=raw or n, action="none", t=time.time())
            return None
        # in between (or the HP is off): let the local AI decide below
    j = intent(raw or n)
    a = str(j.get("action") or "none")
    # the local AI proposes; things that cost something need the words to back it up
    if a == "deep" and not DEEP.search(n):
        a = "talk"  # "I think water cooling would help" is an idea, not "spend Claude credits on it"
    if a == "save_paper" and not picking:
        a = "talk"  # "would the pump use more power" is not "save number 2"
    if a == "start" and not STARTY.search(n):
        a = "talk" if active else "none"  # a stray "Okay." once started an (empty) duplicate research
    last_intent.update(text=raw or n, action=a, t=time.time())
    if a in ("none", "talk") or (a not in ("start", "list", "switch", "about", "find_papers") and not active
                                 and not research.all_research()):
        return None  # not research, or talk: the local brain carries the conversation
    named = research.find(j.get("research") or "") if j.get("research") else None
    r = named or active
    if a == "start":
        topic = (j.get("topic") or "").strip()
        if not topic:
            pending.update(kind="topic", t=time.time())
            return "Sure. What's the research about?"
        same = research.find(topic)
        if same and _close(topic, same["name"]):  # already have it: open that one instead of a second copy
            research.set_active(same["id"])
            return f"You already have {same['name']}, so I've opened it. " + _brief(same)
        r = research.create(topic)
        return f"Started your research on {r['name']}. Talk to me about it, or send me papers and notes."
    if a == "list":
        rs = research.all_research()
        if not rs:
            return "You don't have any research yet. Tell me what you want to look into and I'll start one."
        return "Your research: " + "; ".join(f"{x['name']} ({x['notes']} notes, {x['sources']} sources)" for x in rs[:8]) + "."
    if a in ("switch", "about"):
        if not named:
            return _unknown(j.get("research") or "that")
        research.set_active(named["id"])
        return (f"Okay, {named['name']}. " + _brief(named)) if a == "switch" else _brief(named, full=True)
    if a == "find_papers":
        return _papers(app, (j.get("query") or raw).strip(), r)
    if a == "save_paper":
        return _save_paper(int(j.get("n") or 1) - 1)
    if not r:
        return "Which research? Say the topic, or tell me to start a new one."
    if a == "note":
        research.add_note(r["id"], (j.get("text") or raw).strip())
        return f"Noted in {r['name']}."
    if a == "know":
        return _what_we_know(r, (j.get("query") or raw).strip())
    if a == "questions":
        return _from_block(r, "open-questions", "No open questions written down yet. Talk it through with me and "
                                                "I'll keep track of them.", "Open questions")
    if a == "next":
        return _from_block(r, "next-steps", "Nothing planned yet. Ask me to think hard about it and I'll suggest "
                                            "next steps.", "Next")
    if a == "summarise":
        threading.Thread(target=refresh_summary, args=(app, r["id"]), daemon=True).start()
        return f"Writing a fresh summary of {r['name']}. I'll tell you when it's in your brain."
    if a == "deep":
        research.set_active(r["id"])
        said: list[str] = []
        deep(app, r, raw or n, said.append)
        return " ".join(said) or "Okay."
    if a == "report" and not REPORTY.search(n):
        a = "talk"
    if a == "report":
        research.set_active(r["id"])

        def run():
            try:
                out = research.write_report(r["id"], raw or n)
                app.show_visual("card", f"Report ready | {r['name']}\n{out.name}\nIn your brain: 08 Research → Reports")
                subprocess.Popen(["xdg-open", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                app.announce(f"Your report on {r['name']} is ready. I've opened it, and it's in your brain under "
                             "Reports, with a PDF.")
            except Exception as e:
                log.warning("report: %s", e)
                app.announce(f"I couldn't finish the report on {r['name']}.")
        if research.needs_context(r["id"]):  # too little to write from: find out first, then write
            said: list[str] = []
            deep(app, r, f"Find out what this research is really about and the background needed to write a good "
                         f"report on it ({raw or n})", said.append, then=run)
            return ("There isn't enough in it yet to write a good report, so I'm finding out first: Claude is looking "
                    "for papers, application notes and discussions, then I'll read them all and write the report. "
                    "You can watch it in the cockpit.")
        threading.Thread(target=run, daemon=True, name="research-report").start()
        n_src = len(research.items(r["id"], "source"))
        return (f"Writing a report on {r['name']} from its {n_src} source{'s' if n_src != 1 else ''} and your notes. "
                "It takes a few minutes; you can watch it in the cockpit.")
    if a == "rename":
        m = NAME_IT.search(raw or n)
        new = _clean_name(j.get("text") or (m.group("name") if m else ""))
        if not new:
            return "What should I call it?"
        r2 = research.rename(r["id"], new)
        return f"Renamed it to {r2['name']}."
    if a == "stop":
        research.set_active(None)
        close_session(force=True)
        return f"Okay, I've put {r['name']} away."
    return None


def _papers(app, q: str, r: dict | None) -> str:
    from batsy import research_sources

    found = research_sources.papers(q)
    if not found:
        return "I couldn't reach the paper search just now. Try again in a minute."
    _found.update(t=time.time(), rid=r["id"] if r else None, items=found)
    app.show_visual("card", f"Papers on {q} | " + "\n".join(
        f"{i + 1}. {p['title']} ({p.get('authors', '')} {p.get('year', '')}, cited {p.get('citations', 0)})"
        for i, p in enumerate(found)))
    return f"I found {len(found)}. " + research_sources.spoken_list(found) + \
        (" Tell me which ones to keep." if r else " Start or open a research to keep them.")


def _save_paper(k: int) -> str:
    if not _found["items"] or time.time() - _found["t"] > 1800:
        return "I haven't searched for papers lately. Tell me what to look for."
    if not 0 <= k < len(_found["items"]):
        return f"There were only {len(_found['items'])}."
    r = research.get(_found["rid"]) if _found["rid"] else research.active()
    if not r:
        return "Which research? Open one first, then tell me again."
    p = _found["items"][k]
    research.add_source(r["id"], {**p, "kind": "paper", "what": f"a {p.get('year') or ''} paper, {p.get('venue') or ''}"
                                  .replace("  ", " ").strip(" ,")}, p.get("abstract", ""))
    threading.Thread(target=_read_saved, args=(r, p), daemon=True).start()  # Batsy reads it properly, if it can get it
    return f"Saved {p['title']} to {r['name']}."


def _read_saved(r: dict, p: dict) -> None:
    if not p.get("pdf"):
        return
    try:
        research.fetch(r, p["pdf"], p["title"], "a paper you saved")
        research.read_fetched(r["id"])
    except Exception as e:
        log.info("reading the saved paper: %s", e)


def _answer_pending(n: str, raw: str = "") -> str | None:
    """The answer to "What's the research about?" / "Is this for Bat or Drone research?" / "Start a research on X
    with it?"."""
    from batsy import safety

    if not pending or time.time() - pending.get("t", 0) > 600:
        return None
    if pending["kind"] == "topic":
        pending.clear()
        topic = re.sub(r"(?i)^(?:it'?s |it is |about |on |the |a )+", "", (raw or n).strip(" .?!"))
        if len(topic.split()) < 1 or safety.answer(n) is False:
            return "Okay, never mind."
        r = research.create(topic)
        return f"Started your research on {r['name']}. Talk to me about it, or send me papers and notes."
    if pending["kind"] == "new":
        named = NAME_IT.search(raw or n)
        yes = True if (named or YESISH.match(n)) else safety.answer(n)
        if yes is None:
            return None
        meta, topic = pending["meta"], pending["topic"]
        pending.clear()
        if not yes:
            return "Okay, I'll leave it in your inbox."
        name = _clean_name(named.group("name")) if named else topic
        r = research.create(name, topic if named else "")
        _read_in_full(r, meta)
        return f"Started {r['name']}. I'm reading the whole document now and I'll tell you when I've understood it."
    if pending["kind"] == "which":
        options = [research.get(i) for i in pending["options"]]
        pick = None
        if re.match(r"(?:the )?(?:first|1st|former)\b", n):
            pick = options[0]
        elif re.match(r"(?:the )?(?:second|2nd|latter|other)\b", n) and len(options) > 1:
            pick = options[1]
        else:
            pick = research.find(n) if len(n.split()) <= 6 else None
        if not pick:
            if safety.answer(n) is False:
                pending.clear()
                return "Okay, I'll leave it in your inbox."
            return None
        meta = pending["meta"]
        pending.clear()
        _read_in_full(pick, meta)
        return f"Filed under {pick['name']}. I'm reading it in full now."
    return None


YESISH = re.compile(r"(?:yeah|yes|yep|yup|sure|ok|okay|go ahead|do it|start(?:ed)?(?: it)?|please|why not|of course)\b")
NAME_IT = re.compile(r"(?i)\b(?:name|call|title)(?: the| this| that)?(?: research| it)?(?: as| to)?\s+(?P<name>[^.?!]{3,80})")


def _clean_name(name: str) -> str:
    name = re.sub(r"(?i)\b(?:uh+|um+|er+)\b[ ,]*", "", name)
    return re.sub(r"\s+", " ", name).strip(" ,.'\"")


def _read_in_full(r: dict, meta: dict) -> None:
    """A file joining a research: read the WHOLE document (not just its first page), understand it against the
    research question, keep its full text, then say what it's about."""
    from pathlib import Path

    def run():
        app = research._app
        try:
            what = research.read_document(r, Path(meta["file"]), title=meta.get("title", ""), why=meta.get("what", ""))
            if app:
                app.announce(f"I've read {Path(meta['file']).name} in full for {r['name']}: {what}. "
                             "Ask me anything about it.")
        except Exception as e:
            log.warning("reading %s in full: %s", meta.get("file"), e)
            research.add_source(r["id"], meta, meta.get("text", ""))  # at least what was understood at first
    threading.Thread(target=run, daemon=True, name="research-read").start()


# --- the thinking partner (local AI) ---------------------------------------------------------------------------------
PARTNER = """You are Batsy, {user}'s research partner, talking out loud. {user} is {about}. Each message starts \
with the research he's on, what it has so far, and his relevant notes and sources in [brackets]. Help him THINK, \
don't lecture: in 2-4 short spoken sentences, do one or two of these, whichever \
helps most right now: ask what exactly he means; point out an assumption; give the strongest counter-argument; offer \
another angle or an analogy from a different field; say what evidence would change his mind; suggest a concrete next \
step or experiment. Use his notes and sources from the brackets when they're relevant and say which one, by the title given \
there. Only ever mention a note or source that is in the brackets; never invent sources, notes, authors, numbers or \
quotes. If you don't know, say so and suggest finding papers. No lists or markdown."""


def talk(app, text: str, emit, abort) -> bool:
    """Talk about the active research with the local AI, spoken as it streams. False if there's no active research."""
    from batsy import llm_hosts
    from batsy.local_brain import who
    from batsy.speech import SpeechChunker

    r = research.active()
    if not r:
        return False
    research.touch(r["id"])
    hits = research.retrieve(r["id"], text, k=3)  # the 3 that matter most, kept short: the HP reads every word
    items = "\n".join(f"- [{h['kind']}] {h['title']}: {(h['text'] or '')[:220]}" for h in hits) or "(nothing yet)"
    summary = (research.vault.get_block(research.hub(r), "summary") or "").strip() if research.hub(r).exists() else ""
    cfg = research._cfg
    recent = [t for t in _turns if t[1] == r["id"] and time.time() - t[0] < 1800][-4:]
    messages = [{"role": "system", "content": PARTNER.format(**who(cfg))}]  # the same every time: cached on the HP
    for _, _, you, said in recent:
        messages += [{"role": "user", "content": you}, {"role": "assistant", "content": said}]
    messages.append({"role": "user", "content": (
        f"[The research: \"{r['name']}\" (question: {r.get('question') or r['name']})\n"
        f"So far: {summary[:500] or '(no summary yet)'}\nRelevant notes and sources:\n{items[:900]}]\n\n{text}")})
    body = {"model": cfg.get("local.model"), "stream": True, "keep_alive": cfg.get("local.keep_alive"),
            "messages": messages, "options": {"num_ctx": 4096, "temperature": 0.7, "num_predict": 160}}
    out = []

    def say(s):
        out.append(s)
        emit(s)
    chunker = SpeechChunker(say)
    try:
        with llm_hosts.live(), llm_hosts.open_chat(cfg, body) as resp:
            for line in resp:
                if abort.is_set():
                    break
                part = json.loads(line) if line.strip() else {}
                chunker.feed(part.get("message", {}).get("content", ""))
        chunker.flush()
    except Exception as e:
        log.warning("research talk: %s", e)
        emit("My local brain didn't answer. Try again, or say think hard and I'll ask Claude.")
        return True
    _turns.append((time.time(), r["id"], text, " ".join(out)))
    _schedule_close()
    return True


def deep(app, r: dict, text: str, emit, then=None) -> None:
    """Claude (credits): reads the whole research, searches papers and the web, writes back into the research."""
    from batsy import claude_code

    if claude_code.over_budget(app.cfg) is not None:
        emit("Claude's budget is used up for now, so I can't go deeper. I'll keep thinking with you locally.")
        return
    emit(f"Okay, I'll think hard about it with Claude. I'll tell you when it's in your {r['name']} research.")
    folder = research.folder(r)
    recent = [f"You: {t[2]}\nBatsy: {t[3]}" for t in _turns if t[1] == r["id"]][-6:]
    brief = DEEP_BRIEF.format(name=r["name"], question=r.get("question") or r["name"], request=text, folder=folder,
                              recent="\n\n".join(recent) or "(no talk yet)")

    def run():
        try:
            out = claude_code.run(brief, f"research: {r['name'][:30]}", cwd=folder, timeout=900, cfg=app.cfg, args=[
                "--add-dir", str(folder), "--allowedTools", "Read,Glob,Grep,WebSearch,WebFetch,Bash(batsy research:*)"])
        except Exception as e:
            log.warning("research deep: %s", e)
            app.announce(f"Claude couldn't finish the deep dive on {r['name']}.")
            return
        out = (out or "").strip()
        if out:
            research.add_finding(r["id"], "next", f"Deep dive ({dt.date.today():%d %b}): {out[:400]}")
        app.announce((out[:400] + " I'm reading them now.") if out else "Claude found some documents. I'm reading them now.")
        read = research.read_fetched(r["id"])  # Batsy reads and understands each one itself (local AI)
        if read:
            app.announce(f"I've read {len(read)} document{'s' if len(read) != 1 else ''} for {r['name']}: "
                         + "; ".join(read[:5]) + ". They're in your brain with their key points.")
            refresh_summary(app, r["id"], quiet=True)  # what it all adds up to, now that there's more
        if then is not None:
            then()
    threading.Thread(target=run, daemon=True, name="research-deep").start()


DEEP_BRIEF = """You are the research scout behind Batsy, Jonathan's voice assistant. He's an engineering student. His \
research "{name}" (question: {question}) lives in this folder, written by Batsy: {folder} (the hub note, Notes/, \
Sources/, Conversations/). Skim the hub note first so you know what he already has.

He asked: "{request}"
The talk so far:
{recent}

Your job is to FIND the documents, not to read them for him: Batsy reads and understands every document itself, on \
his own computer. If it isn't clear what the research is really about (a project name and a datasheet, say), first \
work out the most likely subject and context from what's there (what is that device used for? in what field?), and \
find material that explains it. Search everywhere useful: papers (Google Scholar, Semantic Scholar, arXiv, \
OpenAlex), manufacturer application notes and manuals, university and lab pages, good technical articles, forums \
and Reddit threads where practitioners discuss it. (Batsy usually can't open Reddit itself, as Reddit blocks it: \
if `batsy research fetch` refuses a Reddit link, read the thread yourself and add its useful points with \
`batsy research add-finding "{name}" --kind finding "(Reddit, r/...) ..."`.) Use WebSearch, and WebFetch only to check a page is the right one and to find its PDF link. Pick the 3-6 best \
documents for his question: the background he needs, different angles, the strongest counter-view, and what \
practitioners actually say. Favour PDFs and primary sources, plus 1-2 good discussions. Hand each one to Batsy:
  batsy research fetch "{name}" "https://...pdf-or-page" --title "..." --why "one line: why this one"
Then add the 2-5 most useful open questions or other approaches you noticed while searching:
  batsy research add-finding "{name}" --kind question|idea|next "one clear sentence"
Never invent documents. Final reply: 1-3 spoken sentences: what you found for him and why (Batsy will read them)."""


# --- after a conversation: write it up -------------------------------------------------------------------------------
def _schedule_close() -> None:
    global _closer
    if _closer:
        _closer.cancel()
    _closer = threading.Timer(300, close_session)
    _closer.daemon = True
    _closer.start()


def close_session(force: bool = False) -> None:
    """5 quiet minutes after a research talk: a Conversation note, and new open questions / ideas on the hub."""
    global _turns
    turns, _turns = list(_turns), []
    if not turns:
        return
    by: dict[int, list] = {}
    for t in turns:
        by.setdefault(t[1], []).append(t)
    for rid, ts in by.items():
        if len(ts) < 2 and not force:
            continue
        try:
            _write_conversation(rid, ts)
        except Exception as e:
            log.warning("research conversation note: %s", e)


def _write_conversation(rid: int, ts: list) -> None:
    from batsy import vault

    r = research.get(rid)
    talk_text = "\n".join(f"Jonathan: {t[2]}\nBatsy: {t[3]}" for t in ts)
    raw = research._ask(
        "Here is a conversation about Jonathan's research \"" + r["name"] + "\". Answer in JSON only: "
        "{\"topic\": \"3-6 words\", \"summary\": \"2-4 sentences\", \"ideas\": [\"...\"], \"questions\": [\"open questions "
        "raised\"], \"decisions\": [\"...\"], \"next\": [\"next steps\"]} (empty lists when there are none).\n\n" + talk_text[:5000],
        400, json_mode=True)
    j = json.loads(raw)
    name = f"{dt.date.today():%Y-%m-%d} {j.get('topic') or 'Conversation'}"
    p = research.folder(r) / "Conversations" / f"{vault.safe(name)}.md"
    sec = lambda title, xs: f"## {title}\n" + "\n".join(f"- {x}" for x in xs) + "\n\n" if xs else ""  # noqa: E731
    body = (vault.frontmatter(type="research-conversation", research=f"[[{vault.safe(r['name'])}]]",
                              date=dt.date.today().isoformat())
            + f"# {j.get('topic') or 'Conversation'}\n\n{j.get('summary', '')}\n\n" + sec("Ideas", j.get("ideas") or [])
            + sec("Open questions", j.get("questions") or []) + sec("Decisions", j.get("decisions") or [])
            + sec("Next steps", j.get("next") or []) + "## The talk\n" + "\n".join(f"> **You:** {t[2]}\n>\n> **Batsy:** {t[3]}\n"
                                                                                  for t in ts))
    vault.submit(lambda: (vault.create(p, body), vault.add_to_block(research.hub(r), "conversations",
                                                                     f"- {vault.link(name, j.get('topic') or name)}",
                                                                     "Conversations", newest_first=True)))
    for kind, key in (("idea", "ideas"), ("question", "questions"), ("next", "next")):
        for x in (j.get(key) or [])[:4]:
            research.add_finding(rid, kind, str(x))


def refresh_summary(app, rid: int, quiet: bool = False) -> None:
    """A fresh living summary from everything in the research (local AI)."""
    from batsy import vault

    r = research.get(rid)
    its = research.items(rid, limit=40)
    material = "\n".join(f"- [{i['kind']}] {i['title']}: {(i['text'] or '')[:300]}" for i in its)
    if not material:
        if not quiet:
            app.announce(f"{r['name']} is empty so far: there's nothing to summarise yet.")
        return
    text = research._ask(f"Write the living summary of Jonathan's research \"{r['name']}\" (question: {r.get('question')}): "
                         "one paragraph of 3-6 sentences on what's known so far, then a line 'Biggest gap:' with the most "
                         f"important open question. Only use this material:\n{material[:6000]}", 350)
    vault.submit(vault.set_block, research.hub(r), "summary", text.strip(), "Summary")
    threading.Thread(target=research._embed_research, args=(rid,), daemon=True).start()
    if not quiet:
        app.announce(f"The {r['name']} summary is updated. " + text.strip().split("\n")[0][:300])


# --- small helpers ----------------------------------------------------------------------------------------------------
def _brief(r: dict, full: bool = False) -> str:
    from batsy import vault

    summary = (vault.get_block(research.hub(r), "summary") or "").strip() if research.hub(r).exists() else ""
    summary = "" if summary.startswith("_") else summary
    counts = f"{len(research.items(r['id'], 'note'))} notes and {len(research.items(r['id'], 'source'))} sources"
    if full and summary:
        return f"{r['name']}: {summary.split(chr(10))[0][:400]} ({counts})."
    return f"It has {counts}." + ("" if summary else " Say summarise it when you want an overview.")


def _what_we_know(r: dict, q: str) -> str:
    hits = research.retrieve(r["id"], q, k=4)
    if not hits:
        return f"Nothing in {r['name']} about {q} yet. Say find papers on {q}, or talk it through with me."
    parts = [f"From {('your note' if h['kind'] == 'note' else h['title'])}: {_first(h['text'])}" for h in hits[:3]]
    return " ".join(parts)


def _from_block(r: dict, block: str, empty: str, label: str) -> str:
    from batsy import vault

    body = (vault.get_block(research.hub(r), block) or "").strip() if research.hub(r).exists() else ""
    lines = [x.lstrip("- ").strip() for x in body.splitlines() if x.strip().startswith("-")]
    return f"{label}: " + "; ".join(lines[:4]) + "." if lines else empty


def _close(a: str, b: str) -> bool:
    from rapidfuzz import fuzz

    return fuzz.token_set_ratio(a.lower(), b.lower()) >= 80


def _first(text: str, n: int = 220) -> str:
    s = re.split(r"(?<=[.!?])\s", (text or "").strip())[0]
    return s[:n]


def _unknown(x: str) -> str:
    names = [r["name"] for r in research.all_research()]
    return (f"I don't have a research called {x}. You have: {', '.join(names)}." if names
            else f"There's no research yet. Say: start a new research on {x}.")


def _original(raw: str, said: str) -> str:
    """The note in his own spelling (raw text), not the lower-cased command form."""
    i = raw.lower().find(said[:12].lower())
    return raw[i:].strip() if i >= 0 else said


def raw_topic(raw: str) -> str:
    m = re.search(r"(?i)research(?: project)?(?: on| about| into| for| called| named)?\s+(.+)$", raw.strip(" .?!"))
    return m.group(1) if m else ""
