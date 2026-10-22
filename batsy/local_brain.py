"""Local brain: a small model running in Ollama on this laptop. Free, offline, no limits.

Tuned for speed on a CPU with 8 GB RAM: a ~3B model kept loaded, a tiny system prompt (prompt reading is
the slow part on CPU), few compact tools, short tool outputs, and replies streamed straight into speech.
"Ask Claude ..." hands a request to Claude Code on the user's Pro plan, only when they say so.
"""
import getpass
import json
import re
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from batsy.speech import SpeechChunker
from batsy.config import WORKSPACE
from batsy.memory_store import get_store
from batsy.tools import ToolRegistry, truncate

log = logging.getLogger("batsy.local")

SYSTEM = """You are Batsy, {user}'s voice assistant on their Fedora laptop. Talk like a friend in a real \
conversation: warm, quick, natural, usually one or two short sentences. No lists, markdown or emoji. Input is \
speech-to-text, so guess sensibly at misheard words.
Use tools to actually do things on the laptop, then confirm in a few words. Open apps with run_shell \
background=true (e.g. gtk-launch firefox, xdg-open URL). Music plays on the iPad: use spotify.
Whenever the user wants to see something, however they phrase it, write [[show: exact name of the thing]] for a \
picture, or [[pull: exact name]] to put a person, place or thing on screen with its facts. For a list or steps \
write [[card: Title | line one; line two]]. Markers are not spoken.
If a request needs real expertise, careful reasoning, code, or you are not sure of the facts, call ask_claude \
with the full request instead of answering yourself.
Earlier conversation and things you remember come before [Now ...] in the message."""

# Compact tool set for the small model: (description, properties, required). Executed by the normal registry.
LOCAL_TOOLS = {
    "run_shell": ("Run a bash command on the laptop. background=true for apps and long tasks.",
                  {"command": {"type": "string"}, "background": {"type": "boolean"}}, ["command"]),
    "system_status": ("CPU, memory, temperature, battery, disk, network, top processes.",
                      {"sections": {"type": "array", "items": {"type": "string",
                       "enum": ["cpu", "memory", "temps", "battery", "disks", "network", "processes"]}}}, []),
    "spotify": ("Music on the iPad. action: play (query), queue, pause, resume, next, previous, now_playing, like.",
                {"action": {"type": "string", "enum": ["play", "queue", "pause", "resume", "next", "previous",
                                                       "now_playing", "like"]},
                 "query": {"type": "string"}}, ["action"]),
    "set_timer": ("Timer or reminder; message is spoken when it fires.",
                  {"seconds": {"type": "number"}, "message": {"type": "string"}}, ["seconds", "message"]),
    "agenda": ("The user's plans. add: date YYYY-MM-DD + text. list: date (and end_date).",
               {"action": {"type": "string", "enum": ["add", "list"]}, "date": {"type": "string"},
                "end_date": {"type": "string"}, "text": {"type": "string"}}, ["action"]),
    "remember": ("Save a lasting fact about the user.", {"fact": {"type": "string"}}, ["fact"]),
    "camera": ("Open or close the live camera window.",
               {"action": {"type": "string", "enum": ["open", "close"]}}, ["action"]),
    "look": ("See the camera (what the user is holding/showing) or the screen. Claude looks and answers.",
             {"source": {"type": "string", "enum": ["webcam", "screen"]}, "question": {"type": "string"}},
             ["source", "question"]),
    "ask_claude": ("Hand a hard request to Claude (expert answers, code, careful reasoning). Claude replies to the user.",
                   {"request": {"type": "string"}}, ["request"]),
}

# Requests that a 3B model would fumble: route straight to Claude without asking the small model first.
COMPLEX = re.compile(
    r"\b(write|code|coding|program|script|sketch|debug|function|algorithm|compile error|error message|"
    r"explain (why|how|the difference)|step by step|compare|difference between|pros and cons|analy[sz]e|"
    r"summari[sz]e|essay|email|letter|report|cover letter|resume|plan (a|my|the)|itinerary|design|calculate|"
    r"solve|prove|derive|translate|strategy|optimi[sz]e|review my|in detail|detailed|research)\b")


# Questions about what the camera or screen shows: the small model can't see, so Claude looks at one frame.
VISION = re.compile(
    r"\b(what am i holding|what('s| is) (this|that)( thing| in my hand)?\??$|what do you see|can you see|look at (this|me|my)|"
    r"what('s| is) in front of|in my hand|on (my|the) screen|this (error|page|window|tab|message)|"
    r"what colou?r is (this|my)|read (this|that)|how do i look|do you see|which (one|component) is this|identify this)")
SCREEN_WORDS = re.compile(r"\b(screen|window|page|tab|error|message|browser|code)\b")


# While a model is in the viewer, these are about it (anything the local phrase parser didn't handle).
THREE_D_TALK = re.compile(r"\b(it|this|that|model|part|mesh|print|printable|hole|holes|wall|walls|fillet|chamfer|"
                          r"thread|screw|mount|base|edge|corner|face|support|overhang|infill|tolerance|fit|design|"
                          r"add|cut|extrude|combine|join|subtract|engrave|text|logo|what is|why|how)\b")
DESIGN = re.compile(r"\b(design|model|make|create|build|generate|draw)\b.*\b(3d|three d|stl|printable|"
                    r"to print|for printing|3d print|enclosure|bracket|mount|stand|holder|case|box|gear|knob)\b")


# While the live camera is on screen, questions like these are about what it shows.
CAMERA_TALK = re.compile(r"\b(this|that|these|those|it|its|here|they|them|what|which|who|how|why|is|are|does|do|can|"
                         r"should|could|would|identify|read|tell|explain|wrong|right|correct|value|colou?r|wired|"
                         r"connected|working|broken)\b")


# Short questions without these go to the fast path: no tools, a tiny prompt (the CPU reads ~45 tokens/s,
# so the full prompt with tools and memory costs ~30 s before the first word).
NEEDS_TOOLS = re.compile(
    r"\b(open|close|launch|start|play|pause|resume|skip|music|song|spotify|timer|remind|reminder|alarm|remember|"
    r"forget|cpu|ram|memory|battery|temperature|temp|disk|storage|wifi|network|internet|volume|brightness|screen|"
    r"camera|run|install|update|file|folder|download|agenda|schedule|calendar|plan|plans|today|tomorrow|tonight|"
    r"weather|news|latest|price|score|search|look up|google|who won|current|now|my|laptop|computer|show|pull up)\b")
QUICK_SYSTEM = ("You are Batsy, {user}'s friendly voice assistant. Answer in one or two short spoken sentences, "
                "no lists or markdown. Input is speech-to-text, so guess sensibly at misheard words. If the answer "
                "needs the laptop, the internet, recent events, or facts you're not sure of, reply with only: PASS")


def vision_source(text: str) -> str | None:
    t = text.lower().strip(" ?.!")
    if not VISION.search(t):
        from batsy.tools.screen import camera_pid

        return "webcam" if camera_pid() and CAMERA_TALK.search(t) else None
    return "screen" if SCREEN_WORDS.search(t) else "webcam"


def is_complex(text: str) -> bool:
    return bool(COMPLEX.search(text.lower())) or len(text.split()) > 22


class LocalBrain:
    def __init__(self, cfg, registry: ToolRegistry):
        self.cfg = cfg
        self.registry = registry
        self.memory = get_store()
        self.lock = threading.Lock()
        self.visual: Callable[[str, str], None] | None = None
        self.url = cfg.get("local.url").rstrip("/") + "/api/chat"
        self.model = cfg.get("local.model")
        self.system = SYSTEM.format(user=os.environ.get("BATSY_USER_NAME") or getpass.getuser())
        self.tools = [
            {"type": "function", "function": {"name": name, "description": desc,
                                              "parameters": {"type": "object", "properties": props, "required": req}}}
            for name, (desc, props, req) in LOCAL_TOOLS.items()
            if name in registry.tools or name in ("ask_claude", "look")  # these two are handled by the brain itself
        ]
        self._claude_installed = bool(shutil.which("claude")) and cfg.get("local.auto_claude")
        self.skills = None            # SkillBook, set by the app
        self.last_skill_run: str | None = None
        self.last_doc = None          # (path, time) of the file worked on last, for follow-ups
        threading.Thread(target=self._warm_up, daemon=True, name="warm-up").start()

    @property
    def claude_ok(self) -> bool:
        """Claude Code is installed, auto-routing is on, and Batsy's Claude budget isn't used up."""
        from batsy import claude_code

        return self._claude_installed and claude_code.over_budget(self.cfg) is None

    # --- Ollama -------------------------------------------------------------
    def _options(self) -> dict:
        return {"num_ctx": self.cfg.get("local.context"), "temperature": 0.6, "num_predict": 400}

    def _warm_up(self) -> None:
        """Load the model and pre-read the system prompt so the first real reply is quick."""
        body = {"model": self.model, "stream": False, "keep_alive": self.cfg.get("local.keep_alive"),
                "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": "hi"}],
                "tools": self.tools, "options": {**self._options(), "num_predict": 1}}
        try:
            self._post(body).read()
            log.info("local model %s ready", self.model)
        except Exception as e:
            log.warning("local model warm-up failed: %s", e)

    def _post(self, body: dict):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=300)

    def _stream(self, messages: list[dict], chunker: SpeechChunker, abort: threading.Event):
        body = {"model": self.model, "stream": True, "keep_alive": self.cfg.get("local.keep_alive"),
                "messages": messages, "tools": self.tools, "options": self._options()}
        content, calls, stats, fed = "", [], {}, 0
        with self._post(body) as resp:
            for line in resp:
                if abort.is_set():
                    return None
                if not line.strip():
                    continue
                part = json.loads(line)
                msg = part.get("message", {})
                if msg.get("content"):
                    content += msg["content"]
                    # Small models sometimes write a tool call as text: never speak from the first '{"' on.
                    end = content.find('{"')
                    end = len(content) - content.endswith("{") if end < 0 else end
                    if end > fed:
                        chunker.feed(content[fed:end])
                        fed = end
                calls += msg.get("tool_calls") or []
                if part.get("done"):
                    stats = part
        self._log_usage(stats)
        if not calls:
            calls = self._calls_in_text(content)
        return content, calls

    def status(self, text: str) -> None:
        """What Batsy is doing right now, for the overlay ("Asking Claude"...). Replaced by the voice app."""

    def _quick(self, text: str) -> str | None:
        """Fast answer with a tiny prompt and no tools. None = the small model passed (needs the full path)."""
        self.status("Thinking · local model")
        recent = self.memory.build_context(text, self.cfg, recent=1, related=0)[-400:]
        messages = [{"role": "system", "content": QUICK_SYSTEM.format(user=os.environ.get("BATSY_USER_NAME") or getpass.getuser())},
                    {"role": "user", "content": (f"{recent}\n\n" if recent else "") + text}]
        body = {"model": self.model, "stream": False, "keep_alive": self.cfg.get("local.keep_alive"),
                "messages": messages, "options": {**self._options(), "num_predict": 80}}
        try:
            with self._post(body) as resp:
                out = json.loads(resp.read())
        except (urllib.error.URLError, ValueError) as e:
            log.warning("quick answer failed: %s", e)
            return None
        self._log_usage(out)
        reply = out.get("message", {}).get("content", "").strip()
        return None if not reply or "PASS" in reply else reply

    @staticmethod
    def _calls_in_text(content: str) -> list[dict]:
        """{"name": "runshell", "parameters": {...}} written as text -> a real tool call."""
        start = content.find('{"')
        if start < 0:
            return []
        try:
            obj, _ = json.JSONDecoder().raw_decode(content[start:])
        except ValueError:
            return []
        if not isinstance(obj, dict) or "name" not in obj:
            return []
        want = re.sub(r"[^a-z]", "", str(obj["name"]).lower())
        name = next((n for n in [*LOCAL_TOOLS, "look", "ask_claude"] if n.replace("_", "") == want), None)
        if name is None:
            return []
        return [{"function": {"name": name, "arguments": obj.get("parameters") or obj.get("arguments") or {}}}]

    def _log_usage(self, s: dict) -> None:
        from batsy.usage import USAGE_FILE

        row = {"t": int(time.time()), "model": f"local:{self.model}", "in": s.get("prompt_eval_count", 0),
               "out": s.get("eval_count", 0), "cache_read": 0, "cache_write": 0}
        log.info("local usage %s (%.1fs)", row, s.get("total_duration", 0) / 1e9)
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")

    # --- the turn -----------------------------------------------------------
    def note_announcement(self, text: str) -> None:
        self.memory.add_exchange("alert", "", text, [])

    def ask(self, text: str, source: str, say, abort: threading.Event, on_tool=None) -> str:
        with self.lock:
            return self._ask(text, source, say, abort, on_tool)

    def _ask(self, text, source, say, abort, on_tool) -> str:
        m = self.cfg.get("local")
        context = self.memory.build_context(text, self.cfg, recent=m["recent_exchanges"], related=m["related_exchanges"])
        stamp = time.strftime("%a %d %b %Y %H:%M")
        prompt = (f"{context}\n\n" if context else "") + f"[Now {stamp}] {text}"
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": prompt}]
        spoken, actions = [], []

        def emit(sentence: str) -> None:
            spoken.append(sentence)
            say(sentence)

        def on_marker(kind: str, arg: str) -> None:
            actions.append(f"showed {kind}: {arg[:60]}")
            if self.visual and not abort.is_set():
                threading.Thread(target=self.visual, args=(kind, arg), daemon=True).start()

        from batsy import docs
        from batsy.learning import ACTION, norm

        from batsy import model3d

        explicit = ASK_CLAUDE.match(text.strip(" .!?"))
        if explicit and self._claude_installed:  # "ask Claude to ...": straight to Claude Code, nothing local
            request = re.sub(r"\banyway\b", "", explicit.group(1), flags=re.I).strip()
            emit("Asking Claude.")
            self._claude(request, context, emit, on_marker, on_tool, actions,
                         force=bool(re.search(r"\banyway\b", text, re.I)))  # "anyway" ignores Batsy's budget
            return self._finish(source, text, spoken, actions, abort)

        if self.claude_ok and re.search(r"\bscreen ?saver\b", text.lower()):
            emit("Let me redesign that.")
            from batsy import claude_code
            from batsy.saver import SAVER_BRIEF, load

            self.status("Redesigning the screensaver · Claude")
            answer = claude_code.run(SAVER_BRIEF.format(request=text, spec=json.dumps(load())), "redesigning screensaver",
                                     args=["--allowedTools", "Bash(batsy saver:*)"], timeout=300)
            chunker = SpeechChunker(emit, on_marker)
            chunker.feed(answer)
            chunker.flush()
            actions.append("redesigned the screensaver")
            return self._finish(source, text, spoken, actions, abort)

        from batsy.tools.screen import camera_pid

        if camera_pid() and self.claude_ok and vision_source(text):  # the live camera is on screen: it's about that
            self._look("webcam", text, context, emit, on_marker, on_tool, actions)
            return self._finish(source, text, spoken, actions, abort)

        if self.claude_ok and (model3d.VIEWER.active() and THREE_D_TALK.search(text.lower())
                               or DESIGN.search(text.lower())):
            self._claude_3d(text, context, emit, on_marker, on_tool, actions)
            return self._finish(source, text, spoken, actions, abort)

        doc = self._doc_target(text)
        if doc is not None:
            self._doc(text, doc, context, emit, on_marker, on_tool, actions)
            return self._finish(source, text, spoken, actions, abort)

        src = vision_source(text)
        if src and self.claude_ok:
            self._look(src, text, context, emit, on_marker, on_tool, actions)
            return self._finish(source, text, spoken, actions, abort)

        if self.claude_ok and self.skills and ACTION.match(norm(text)):
            # a new kind of action: Claude writes code for it once; next time it runs without any AI.
            # Read back what we heard first: a misheard word ("plot" for "Claude") must not run anything.
            if not self.registry.ctx.confirm(f"Just to check, you want me to {text.strip().rstrip('.?!')}?"):
                return self._finish(source, text, spoken, actions, abort)
            emit("One sec.")  # usually an existing skill said differently; only new ones are "learned"
            self.status("Checking my skills · Claude")
            out = self.skills.learn(text, self.registry.ctx.confirm)
            if out["kind"] == "answer":
                emit(out["text"])
            else:
                if out.get("spoken"):
                    emit(out["spoken"])
                if out.get("ok") and not out.get("alias"):
                    emit("I've learned that, so next time it'll be instant.")
                    self.last_skill_run = out["name"]
                    actions.append(f"learned skill {out['name']}")
            return self._finish(source, text, spoken, actions, abort)

        if self.claude_ok and is_complex(text):
            self._claude(text, context, emit, on_marker, on_tool, actions)
            return self._finish(source, text, spoken, actions, abort)

        if len(text.split()) <= 14 and not NEEDS_TOOLS.search(text.lower()):
            reply = self._quick(text)
            if reply:
                for sentence in re.split(r"(?<=[.!?])\s+", reply):
                    if sentence.strip():
                        emit(sentence.strip())
                return self._finish(source, text, spoken, actions, abort)

        for _ in range(m["max_steps"]):
            chunker = SpeechChunker(emit, on_marker)
            self.status("Thinking · local model")
            try:
                result = self._stream(messages, chunker, abort)
            except urllib.error.URLError as e:
                emit("My local brain isn't running. Start it with: systemctl start ollama.")
                log.error("ollama: %s", e)
                break
            if result is None:
                break
            chunker.flush()
            content, calls = result
            messages.append({"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})})
            if not calls:
                break
            handed_off = False
            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if name == "look" and self.claude_ok:
                    self._look(args.get("source", "webcam"), args.get("question") or text, context,
                               emit, on_marker, on_tool, actions)
                    handed_off = True
                    break
                if name == "ask_claude" and self.claude_ok:
                    self._claude(args.get("request") or text, context, emit, on_marker, on_tool, actions)
                    handed_off = True
                    break
                if on_tool:
                    on_tool(name)
                self.status(f"Running {name.replace('_', ' ')}")
                out, is_error = self.registry.execute(name, args) if name in LOCAL_TOOLS else (f"No tool {name}.", True)
                out = out if isinstance(out, str) else "done"
                actions.append(f"{name} {json.dumps(args)[:80]} -> {'ERROR ' if is_error else ''}{out.strip()[:60]}")
                # every character here must be re-read by a slow CPU: keep it short
                messages.append({"role": "tool", "tool_name": name, "content": truncate(out, m["tool_result_chars"])})
            if abort.is_set() or handed_off:
                break
        self._maybe_learn_from(text, actions)
        return self._finish(source, text, spoken, actions, abort)

    def _maybe_learn_from(self, text: str, actions: list[str]) -> None:
        """The local AI just did something with tools: in the background, have Claude turn it into a skill
        so the same request skips the AI next time."""
        from batsy.learning import norm

        did = [a for a in actions if a.split(" ", 1)[0] in ("run_shell", "system_status", "spotify", "camera")]
        if not (self.claude_ok and self.skills and did) or any("ERROR" in a for a in did):
            return
        if self.skills.match(norm(text)):
            return
        threading.Thread(target=self.skills.learn, args=(text, self.registry.ctx.confirm),
                         kwargs={"execute": False, "happened": "\n".join(did)}, daemon=True,
                         name="learn").start()

    def _finish(self, source, text, spoken, actions, abort) -> str:
        reply = " ".join(spoken)
        self.memory.add_exchange(source, text, reply + (" (interrupted)" if abort.is_set() else ""), actions)
        return reply

    def _doc_target(self, text: str):
        """The file this request is about ("this pdf", "my physics notes"), or a follow-up edit of the last one."""
        from pathlib import Path

        from batsy import docs

        parsed = docs.parse_request(text)
        if parsed:
            name, kind = parsed
            path = docs.resolve_file(name, kind)
            return path if path else Path("/nonexistent")  # tell the user we couldn't find it
        last = getattr(self, "last_doc", None)
        if last and time.time() - last[1] < 1200 and docs.EDIT_VERBS.search(text.lower()) and \
                re.search(r"\b(it|this|that|there|also|now|again|too|page|line|word|the)\b", text.lower()):
            return last[0]
        return None

    def _doc(self, text, path, context, emit, on_marker, on_tool, actions) -> None:
        """Read the file locally (and remember it), then let Claude Code do what was asked with Batsy's doc tools."""
        from batsy import docs

        if not path.exists():
            emit("I couldn't find that file. Tell me its name, or open it first and say this again.")
            return
        if not self.claude_ok:
            note = docs.learn(path)
            emit(f"I've read {path.name}, {note}. Claude Code isn't set up, so I can't mark it up yet.")
            return
        emit(f"Looking at {path.stem.replace('_', ' ')}.")
        if on_tool:
            on_tool("doc")
        try:
            note = docs.learn(path)
        except Exception as e:
            log.warning("could not read %s: %s", path, e)
            note = "unreadable locally"
        self.last_doc = (path, time.time())
        actions.append(f"worked on file {path}")
        self.status("Reading the document · Claude")
        answer = ask_claude_docs(text, path, docs.info(path), note, context)
        chunker = SpeechChunker(emit, on_marker)
        chunker.feed(answer)
        chunker.flush()
        self.memory.set_document_summary(str(path), answer[:600])

    def _claude_3d(self, text, context, emit, on_marker, on_tool, actions) -> None:
        """Claude Code works on 3D: it can see the viewer, drive it, and create or edit models in code."""
        emit("Let me work on that.")
        if on_tool:
            on_tool("3d")
        self.status("Working on the 3D model · Claude")
        answer = ask_claude_3d(text, context)
        actions.append("3D work by Claude")
        chunker = SpeechChunker(emit, on_marker)
        chunker.feed(answer)
        chunker.flush()

    def _look(self, source, question, context, emit, on_marker, on_tool, actions) -> None:
        """One frame from the camera window (or a screenshot) -> Claude Code looks at it and answers."""
        from batsy.tools.screen import capture

        img = WORKSPACE / ".vision" / f"{source}.jpg"
        self.status("Looking through the camera" if source == "webcam" else "Reading the screen")
        err = capture(source, img, self.cfg)
        if err:
            emit(f"I couldn't see the {source}: {err[:120]}")
            return
        actions.append(f"looked at {source}")
        self._claude(f"{question}\n\n(Read the image at {img}: it's the user's {'screen' if source == 'screen' else 'webcam, showing them and what they hold'} right now.)",
                     context, emit, on_marker, on_tool, actions)

    def _claude(self, request, context, emit, on_marker, on_tool, actions, force: bool = False) -> None:
        """Answer with Claude Code on the user's Pro plan; its reply goes straight to speech and screen."""
        if on_tool:
            on_tool("ask_claude")
        log.info("routing to Claude: %s", request[:120])
        self.status("Asking Claude" if "(Read the image" not in request else "Asking Claude about what I see")
        answer = ask_claude(request, context, force=force)
        chunker = SpeechChunker(emit, on_marker)
        chunker.feed(answer)
        chunker.flush()
        actions.append("answered by Claude")

    def reset(self) -> None:
        with self.lock:
            self.memory.new_session()


ASK_CLAUDE = re.compile(r"(?:hey |ok )?ask (?:claude|cloud|clawed|claud)(?: to| about| if| whether)?[,\s]+(.+)", re.I | re.S)
CLAUDE_BRIEF = """You are the expert brain behind Batsy, a voice assistant on the user's Fedora laptop. Your reply is \
spoken aloud. You can work anywhere on this laptop: read, edit and create files in any folder (use absolute paths; \
home is {home}, Batsy's own code is {code}) and run any command. Risky commands are confirmed with the user by voice \
automatically; for root use pkexec, never sudo. When asked to do something, do it fully, then say briefly what you did. Answer exactly what was asked and stop: usually one short sentence, two at most, unless they \
ask for detail, an explanation or steps. No extra facts, background, tips or follow-up offers they didn't ask \
for ("what is this?" -> "That's a red banana."). No markdown or lists in the spoken part. Be precise and correct.
Only when they ask to see something, or for steps/lists/code, add a screen marker (not spoken): [[show: exact \
name]] for a picture, [[pull: exact name]] for a person/place/thing with its facts, [[card: Title | line; line]] \
for steps, lists, numbers or code summaries. Put any code or files under {workspace} and just say where.
{context}
User: {request}"""


DOC_BRIEF = """You are the hands and eyes of Batsy, a voice assistant on the user's Fedora laptop. The user said: \
"{request}"

It's about this file: {path}
{info} ({note}). Never modify the original. Put anything you create in {out_dir}.

Read it yourself with Read (PDF pages and images both work). Batsy's document tools (run with Bash):
  batsy doc text FILE                  full text (OCR for images/scans)
  batsy doc find FILE "exact words"    exact positions of text, as [{{page, bbox:[x0,y0,x1,y1]}}]
  batsy doc render FILE --page N --grid  PNG of the page with a pixel grid, for placing shapes; Read it to look
  batsy doc annotate FILE 'JSON'       mark up a copy. JSON: {{"actions": [...]}}, each action has a type, one of \
highlight, underline, strike, circle, box, note, label, arrow or redact. Give exact "text" (found for you, best \
for words) or "bbox" in render pixels (for drawings and objects). Optional: "page", "color" (red, yellow, green, \
blue, orange, purple, pink, black), "occurrence"; label/note take "text"; arrow takes "from"/"to". Returns the \
output path and anything not found.
For anything else (crop, rotate, convert, extract tables, fill forms, merge...), write and run Python with \
{python} (PyMuPDF as pymupdf, Pillow, pytesseract available).

After marking something up, render the output and look at it. Fix anything misplaced before you finish.
Final reply: two or three spoken sentences about what you found or did, no markdown. Then [[open: OUTPUT_PATH]] \
for every file you made, and optionally [[card: Title | line; line]] for key facts.
{context}"""


def ask_claude_docs(request: str, path, info: dict, note: str, context: str = "", timeout: int = 600) -> str:
    """Claude Code reads the file and does the job, using `batsy doc ...` and Python, then checks its own work."""
    import sys

    from batsy import claude_code
    from batsy.docs import OUT_DIR

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prompt = DOC_BRIEF.format(request=request, path=path, info=json.dumps(info), note=note, out_dir=OUT_DIR,
                              python=sys.executable, context=context)
    return claude_code.run(prompt, f"file: {path.name}", cwd=OUT_DIR, timeout=timeout, args=[
        "--permission-mode", "acceptEdits", "--add-dir", str(path.parent), "--add-dir", str(OUT_DIR),
        "--allowedTools", f"Read,Write,Edit,Glob,Grep,Bash(batsy doc:*),Bash({sys.executable}:*)"])[:3000]


THREE_D_BRIEF = """You are the 3D engineer behind Batsy, a voice assistant on the user's Fedora laptop. The user said: \
"{request}"

Batsy's 3D viewer (a window on their screen) state: {state}
Tools (run with Bash):
  batsy 3d info                    loaded models: size, bounds, volume, mass, watertight, parts
  batsy 3d screenshot              PNG of the viewer: Read it to see exactly what the user sees
  batsy 3d cmd 'JSON'              drive the viewer. Commands: load {{path, keep}}, view {{view: top|bottom|front|\
back|left|right|iso}}, rotate {{axis, deg}}, move {{dx, dy, dz}}, scale {{factor | to_mm + axis}}, mirror {{axis}}, \
center, drop, style {{style: solid|wireframe|edges|xray|opaque}}, color {{color}}, dims {{on}}, section {{axis, at 0-1}}, \
explode, measure, spin {{on}}, zoom {{factor}}, export {{format, path}}, message {{text}} (shown in the viewer)
To create or change geometry, write and run Python with {python}. trimesh, manifold3d (booleans, extrusions, \
fillets via offsets), numpy and scipy are available; work in millimetres. Save results in {out_dir}, then load them \
into the viewer (keep=true to show next to the original). Never overwrite the user's original file.
For printability questions, check watertightness, wall thickness, overhangs and the size against a typical \
220 x 220 x 250 mm printer bed.
Final reply: two or three spoken sentences, no markdown. Add [[card: Title | line; line]] for key numbers.
{context}"""


def ask_claude_3d(request: str, context: str = "", timeout: int = 900) -> str:
    import sys

    from batsy import claude_code
    from batsy.model3d import VIEWER
    from batsy.viewer3d import OUT

    OUT.mkdir(parents=True, exist_ok=True)
    try:
        state = json.dumps(VIEWER.send({"cmd": "info"})) if VIEWER.is_open() else "closed (load a file to open it)"
    except OSError:
        state = "not responding"
    prompt = THREE_D_BRIEF.format(request=request, state=state[:3000], python=sys.executable, out_dir=OUT,
                                  context=context)
    return claude_code.run(prompt, "3D: " + request[:40], cwd=OUT, timeout=timeout, args=[
        "--permission-mode", "acceptEdits", "--add-dir", str(OUT), "--add-dir", os.path.expanduser("~"),
        "--allowedTools", f"Read,Write,Edit,Glob,Grep,Bash(batsy 3d:*),Bash({sys.executable}:*)"])[:3000]


def ask_claude(prompt: str, context: str = "", timeout: int = 900, force: bool = False) -> str:
    """One request to Claude Code (the official CLI, on the user's Pro plan). It may work anywhere on the laptop:
    the whole filesystem, any command (risky ones voice-confirmed by batsy.guard), and the web."""
    from pathlib import Path

    from batsy import claude_code

    home = Path.home()
    full = CLAUDE_BRIEF.format(workspace=WORKSPACE, context=context, request=prompt, home=home,
                               code=Path(__file__).resolve().parent.parent)
    return claude_code.run(full, "ask: " + prompt[:40], cwd=home, timeout=timeout, force=force, args=[
        "--permission-mode", "acceptEdits", "--add-dir", "/",
        "--allowedTools", "Read,Write,Edit,MultiEdit,Glob,Grep,Bash,WebSearch,WebFetch,TodoWrite"])[:1500]
