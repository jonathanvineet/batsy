"""Knowing your words: commands (sudo, Tailscale, git...) and your music (artists, Tamil film songs...).

Parakeet is a general English model, so it writes "pseudo tail scale up" and "sarasara sara karthi". Retraining it
isn't possible on this laptop, and a huge hotword list makes it invent those words out of noise. Instead, once the
first transcript shows what you're talking about:
  1. topic(text): "music" for "play ...", "command" for "run / type / sudo / tailscale ..." (else nothing changes)
  2. hotwords(topic, text): the few dozen entries of that topic's vocabulary that SOUND closest to what was heard,
     for a second, biased decode of the same audio (stt.Transcriber)
  3. repair(topic, text): spans that sound like a vocabulary entry are rewritten to it ("pseudo" -> "sudo",
     "tail scale" -> "tailscale", "jon jon" -> "jonjon"). Music is matched against the catalogue by
     spotify.candidates(), which uses sound_key() too.
sound_key() compares how words sound, not how they're spelt, with Tamil transliteration in mind (aa/a, th/t, zh/l,
doubled letters, k/c/q...).
"""
import json
import re
import shutil
import subprocess
import time

from batsy.config import DATA_DIR

CATALOG = DATA_DIR / "music_catalog.json"  # built by spotify.build_catalog()

# --- what you're talking about ------------------------------------------------------------------------------------
MUSIC = re.compile(r"^(?:(?:hey|ok|okay|please|can you|could you|now|batsy)\s+)*(?:play|put on|queue|listen to)\b|"
                   r"\b(song|songs|spotify|album|playlist)\b")
COMMAND = re.compile(r"\b(terminal|console|shell|command|sudo|pseudo|tailscale|tail scale|tale scale|tails scale|"
                     r"tails hill|tail skill|"
                     r"git|get (?:pull|push|status|clone|commit)|dnf|systemctl|system control|journalctl|ssh|"
                     r"run|type in|execute)\b")


def topic(text: str) -> str | None:
    t = text.lower()
    if MUSIC.search(t):
        return "music"
    if COMMAND.search(t):
        return "command"
    return None


# --- how things sound ---------------------------------------------------------------------------------------------
_RULES = [("ph", "f"), ("gh", "g"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("c(?=[eiy])", "s"), ("c", "k"),
          ("zh", "l"), ("z", "s"), ("w", "v"), ("th", "t"), ("dh", "d"), ("bh", "b"), ("kh", "k"), ("sh", "s"),
          ("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"), ("ou", "u"), ("ai", "e"), ("ay", "e"),
          ("y", "i"), (r"([a-z])\1+", r"\1")]


def sound_key(text: str) -> str:
    """'Saara Kaathu' and 'sara kathu' -> the same key; spaces kept so word counts still matter."""
    s = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    for pattern, repl in _RULES:
        s = re.sub(pattern, repl, s)
    return " ".join(s.split())


# --- vocabularies ---------------------------------------------------------------------------------------------------
TAILSCALE = ["up", "down", "set", "status", "ip", "ping", "ssh", "funnel", "serve", "file cp", "file get", "exit-node",
             "netcheck", "whois", "login", "logout", "switch", "update", "web", "cert", "dns", "drive", "lock"]
SHELL = ["sudo", "dnf", "git", "tailscale", "systemctl", "journalctl", "ssh", "scp", "rsync", "docker", "podman",
         "flatpak", "npm", "npx", "pip", "python3", "htop", "top", "nano", "vim", "nvim", "code", "ls", "cat", "grep",
         "curl", "wget", "chmod", "chown", "kill", "ps", "df", "du", "free", "ping", "nmcli", "bluetoothctl", "claude",
         "batsy", "arduino-cli", "kubectl", "ollama", "neofetch", "fastfetch", "make", "cargo", "go", "node"]
# Spoken forms recognisers produce for words they don't know -> the real word (only used for commands).
SAID = {"tails hill": "tailscale", "tail hill": "tailscale", "a c d c": "AC/DC", "pseudo": "sudo", "sue do": "sudo", "sudo": "sudo", "tail scale": "tailscale", "tale scale": "tailscale",
        "tails scale": "tailscale", "tail skill": "tailscale", "tailskill": "tailscale", "d n f": "dnf", "the nf": "dnf",
        "system control": "systemctl", "system ctl": "systemctl", "system c t l": "systemctl",
        "journal control": "journalctl", "journal ctl": "journalctl", "s s h": "ssh", "n p m": "npm",
        "exit node": "exit-node", "v s code": "code", "ar do we know": "arduino", "h top": "htop",
        "cube control": "kubectl", "cube ctl": "kubectl", "oh llama": "ollama", "o llama": "ollama"}
GIT_VERBS = r"(pull|push|status|clone|commit|log|diff|checkout|add|branch|stash|fetch|merge|rebase)"

_cache: dict[str, tuple[float, list[str]]] = {}


def _tailscale_machines() -> list[str]:
    if not shutil.which("tailscale"):
        return []
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3).stdout
        peers = json.loads(out).get("Peer", {}).values()
        return sorted({re.sub(r"[^\w-]", "", p["HostName"]).lower() for p in peers if p.get("HostName")} - {"localhost", ""})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def commands() -> list[str]:
    hit = _cache.get("command")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    words = [w for w in SHELL if shutil.which(w.split()[0]) or w in ("cd", "batsy")] + TAILSCALE + _tailscale_machines()
    _cache["command"] = (time.time(), words)
    return words


def catalog() -> list[tuple[str, str]]:
    """(title, artist) pairs plus ("", artist) for artists: your library and the Tamil/English catalogue."""
    try:
        data = json.loads(CATALOG.read_text())
    except (OSError, ValueError):
        return []
    return [(t[0], t[1]) for t in data.get("tracks", [])] + [("", a) for a in data.get("artists", [])]


# --- 2. hotwords for the second decode -------------------------------------------------------------------------
def hotwords(kind: str, text: str, limit: int = 40) -> str:
    """Newline-separated hotwords with a score, as sherpa-onnx's create_stream(hotwords=...) wants them."""
    from rapidfuzz import fuzz, process

    if kind == "command":
        words = commands()
    else:
        heard = re.sub(r"^.*?\b(?:play|put on|queue|listen to)\s+", "", text.lower())
        items = catalog()
        if not items or not heard:
            return ""
        names = [f"{t} {a}".strip() if t else a for t, a in items]
        keys = [sound_key(n) for n in names]
        best = process.extract(sound_key(heard), keys, scorer=fuzz.partial_ratio, limit=limit)
        words = []
        for _, _, i in best:
            t, a = items[i]
            words += [w for w in (t, a) if w]
    clean = []
    for w in words:
        w = " ".join(re.sub(r"[^\w' ]", " ", w).split())
        if w and len(w) <= 40 and w not in clean:
            clean.append(w)
    return "\n".join(f"{w} :1.3" for w in clean[:limit])  # gentle: a strong bias invents words ("LaTeX")


def music_fit(text: str) -> float:
    """0..100: how well the "play ..." part sounds like a song or artist in the catalogue."""
    from rapidfuzz import fuzz, process

    heard = re.sub(r"^.*?\b(?:play|put on|queue|listen to)\s+", "", text.lower())
    heard = re.sub(r"\b(on|in|from) (my )?(spotify|ipad|laptop|playlist).*$", "", heard).strip(" .,!?")
    items = catalog()
    if not heard or not items:
        return 0.0
    bare = lambda t: re.sub(r"\(.*?\)|\[.*?\]| [-\u2013\u2014] .*$", "", t)  # noqa: E731  no '(From "Master")'
    keys = [sound_key(bare(t) or a) for t, a in items]
    best = process.extractOne(sound_key(heard), keys, scorer=fuzz.ratio)
    return best[1] if best else 0.0


# --- 3. repairing what was heard ------------------------------------------------------------------------------------
def repair(kind: str, text: str) -> str:
    if kind != "command":
        return text  # music: matched against the catalogue when searching (spotify.candidates), not rewritten
    out = text
    for said, real in sorted(SAID.items(), key=lambda kv: -len(kv[0])):  # longest first: "tail scale" before "scale"
        out = re.sub(rf"\b{re.escape(said)}\b", real, out, flags=re.I)
    out = re.sub(rf"\bget (?={GIT_VERBS}\b)", "git ", out, flags=re.I)  # "get pull" -> "git pull"
    # anything else that sounds like a command word or one of your Tailscale machines ("jon jon" -> jonjon)
    from rapidfuzz import fuzz

    known = {w.lower() for w in commands()}
    vocab = {sound_key(w).replace(" ", ""): w for w in commands() if len(w) >= 4}
    words = out.split()
    i = 0
    while i < len(words):
        fixed = False
        for n in (2, 1):
            parts = [re.sub(r"[^\w-]", "", w).lower() for w in words[i:i + n]]
            if len(parts) < n or any(p in known for p in parts):  # already a real command word: leave it
                continue
            span = " ".join(words[i:i + n])
            key = sound_key(span).replace(" ", "")
            if len(key) < 4:
                continue
            for k, real in vocab.items():
                if abs(len(key) - len(k)) <= 2 and fuzz.ratio(key, k) >= 88:
                    words[i:i + n] = [real]
                    fixed = True
                    break
            if fixed:
                break
        i += 1
    return " ".join(words)
