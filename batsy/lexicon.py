"""The words you use every day, so speech recognition gets them right: developers, 3D designers and modellers,
and college students.

Parakeet (and Whisper) were trained on general speech. They know "python" but write "pet g" for PETG, "fusion
three sixty" for Fusion 360 and "cube control" for kubectl. Two things fix that, both local and instant:
- hotwords(): a focused list Parakeet is biased toward while decoding (and Whisper gets as its prompt), plus
  your own project names, so "line-follower" or "Gotham UI" come out right too.
- fix(text): repairs the usual mishearings afterwards, e.g. "open fusion three sixty" -> "open Fusion 360".
  Only distinctive multi-word or spelled-out forms are rewritten, never ordinary words, so normal talk is safe.

Turn domains on or off with [voice] lexicon = ["developer", "3d", "student"].
"""
import re

# domain -> {canonical: [how speech recognisers tend to write it]}. Canonical forms with no variants are still
# hotwords; a variant must never be an everyday phrase ("my sequel" is fine, "sequel" alone is not).
DOMAINS: dict[str, dict[str, list[str]]] = {
    "developer": {
        "GitHub": ["get hub", "git hub", "gethub", "guitar hub"],
        "GitLab": ["get lab", "git lab"],
        "git": [],
        "VS Code": ["v s code", "vs code", "vee s code", "visual studio code", "vscode", "bs code"],
        "Claude Code": ["clawed code", "claud code", "cloud code"],
        "Copilot": ["co pilot", "co-pilot"],
        "Cursor": [],
        "Python": [],
        "PyTorch": ["pie torch", "pi torch", "pytorch"],
        "NumPy": ["num pie", "numb pie", "numpy", "num py"],
        "pandas": [],
        "Matplotlib": ["mat plot lib", "matplot lib"],
        "scikit-learn": ["sci kit learn", "psych it learn", "sky kit learn", "scikit learn", "sk learn"],
        "TensorFlow": ["tensor flow"],
        "Jupyter notebook": ["jupiter notebook"],
        "JupyterLab": ["jupiter lab", "jupyter lab"],
        "Jupyter": [],
        "Colab": ["co lab", "google co lab"],
        "Hugging Face": ["hugging face"],
        "LangChain": ["lang chain"],
        "OpenAI": ["open a i", "open ai"],
        "ChatGPT": ["chat g p t", "chat gpt", "chad gpt"],
        "Ollama": ["o llama", "olama", "oh llama"],
        "C++": ["c plus plus", "see plus plus"],
        "C#": ["c sharp", "see sharp"],
        "TypeScript": ["type script"],
        "JavaScript": ["java script"],
        "Node.js": ["node js", "node jay s", "node j s"],
        "Next.js": ["next js", "next j s"],
        "React": ["react js"],
        "Vue": ["view js", "vue js"],
        "Svelte": ["svelt", "svel"],
        "Tailwind": ["tail wind"],
        "Vite": ["veet"],
        "webpack": ["web pack"],
        "npm": ["n p m", "in p m"],
        "pnpm": ["p n p m"],
        "pip": [],
        "conda": [],
        "Anaconda": ["an a conda"],
        "Django": ["jango", "d jango"],
        "Flask": [],
        "FastAPI": ["fast api", "fast a p i"],
        "PostgreSQL": ["post gress", "post gres", "postgres q l", "post gre sql"],
        "MySQL": ["my sequel", "my s q l"],
        "SQLite": ["sequel light", "sequel lite", "s q lite", "sql lite"],
        "MongoDB": ["mongo d b", "mongo db"],
        "Redis": [],
        "Firebase": ["fire base"],
        "Supabase": ["super base", "supa base"],
        "Vercel": ["ver cell", "versel"],
        "Netlify": ["net lify", "netlifi"],
        "Docker": [],
        "Kubernetes": ["cooper netties", "cuber netties", "kuber netties", "cube er netties"],
        "kubectl": ["cube control", "cube cuddle", "cube c t l", "kube control", "kube c t l"],
        "AWS": ["a w s"],
        "GCP": ["g c p"],
        "CI/CD": ["c i c d", "see i see d", "ci cd"],
        "SSH": ["s s h"],
        "API": ["a p i"],
        "JSON": ["j son"],
        "JSON file": ["jason file"],
        "JSON data": ["jason data"],
        "JSON format": ["jason format"],
        "JSON object": ["jason object"],
        "YAML": ["yam l", "yamel"],
        "CSV": ["c s v"],
        "regex": ["reg ex", "rej ex"],
        "localhost": ["local host"],
        "LeetCode": ["leet code", "lead code", "lee code"],
        "HackerRank": ["hacker rank"],
        "Stack Overflow": ["stack overflow"],
        "Linux": [],
        "Fedora": [],
        "zsh": ["z s h", "zee shell", "zed shell"],
        "tmux": ["t mux", "tea mux"],
        "Neovim": ["neo vim"],
        "Arduino": ["ar dweeno", "ardweeno", "ar duino"],
        "ESP32": ["e s p thirty two", "e s p 32", "esp thirty two", "esp 32", "s32", "es p32", "esp-32"],
        "Raspberry Pi": ["raspberry pie"],
        "PlatformIO": ["platform i o", "platform io"],
        "GPIO": ["g p i o"],
        "I2C": ["i squared c", "i two c", "i 2 c"],
        "SPI": ["s p i"],
        "UART": ["u art"],
        "PWM": ["p w m"],
    },
    "3d": {
        "Blender": [],
        "Fusion 360": ["fusion three sixty", "fusion 3 60", "fusion three 60", "fusion 3 sixty", "fusion 360"],
        "SolidWorks": ["solid works", "solid work"],
        "AutoCAD": ["auto cad", "auto c a d", "autocad"],
        "Autodesk": ["auto desk"],
        "Onshape": ["on shape"],
        "Tinkercad": ["tinker cad", "tinker c a d"],
        "FreeCAD": ["free cad", "free c a d"],
        "OpenSCAD": ["open scad", "open s cad", "open s c a d"],
        "KiCad": ["key cad", "kai cad", "ki cad"],
        "ZBrush": ["z brush", "zee brush", "zed brush"],
        "Maya": [],
        "3ds Max": ["three d s max", "3 d s max", "three ds max"],
        "Cinema 4D": ["cinema four d", "cinema 4 d"],
        "Substance Painter": ["sub stance painter"],
        "Unreal Engine": ["un real engine"],
        "Unity": [],
        "KeyShot": ["key shot"],
        "Rhino": [],
        "Grasshopper": [],
        "Meshmixer": ["mesh mixer"],
        "STL": ["s t l"],
        "OBJ": ["o b j"],
        "FBX": ["f b x"],
        "GLB": ["g l b"],
        "glTF": ["g l t f", "gl tf"],
        "3MF": ["three m f", "3 m f"],
        "STEP": [],
        "G-code": ["g code", "gee code", "g-code"],
        "PETG": ["pet g", "pet gee", "p e t g"],
        "PLA": ["p l a"],
        "ABS": ["a b s"],
        "TPU": ["t p u"],
        "infill": ["in fill"],
        "Cura": ["ultimaker cura"],
        "PrusaSlicer": ["prusa slicer", "pruza slicer"],
        "Bambu Studio": ["bamboo studio", "bambu studio"],
        "Bambu Lab": ["bamboo lab", "bambu lab", "bamboo labs"],
        "OrcaSlicer": ["orca slicer"],
        "Ender 3": ["ender three", "ender 3"],
        "Prusa": ["pruza"],
        "Thingiverse": ["thing averse", "thingy verse", "thing a verse"],
        "Printables": [],
        "MakerWorld": ["maker world"],
        "Sketchfab": ["sketch fab"],
        "GrabCAD": ["grab cad"],
        "UV unwrap": ["u v unwrap", "you v unwrap", "uv unwrap"],
        "retopology": ["re topology", "red apology", "re-topology", "retopo logy"],
        "n-gon": ["n gon", "en gon"],
        "subdivision": ["sub d", "subsurf", "sub surf"],
        "PBR": ["p b r"],
        "HDRI": ["h d r i"],
        "Eevee": ["e v e e"],
        "Cycles": [],
        "fillet": [],
        "chamfer": [],
        "extrude": [],
    },
    "student": {
        "Canvas": [],
        "Moodle": ["mood l", "moodl"],
        "Google Classroom": ["google class room"],
        "MATLAB": ["mat lab", "mat lap", "matt lab"],
        "LaTeX": ["lay tech", "la tech", "lay tek", "latech"],
        "Overleaf": ["over leaf"],
        "Notion": [],
        "Obsidian": [],
        "Anki": [],
        "Quizlet": ["quiz let"],
        "Desmos": ["dez moss", "des moss"],
        "Wolfram Alpha": ["wolfram alpha"],
        "Zotero": ["zo tero", "zotaro"],
        "Coursera": ["course era"],
        "NPTEL": ["n p t e l", "en p tel"],
        "GeeksforGeeks": ["geeks for geeks"],
        "PowerPoint": ["power point"],
        "GPA": ["g p a"],
        "CGPA": ["c g p a"],
        "SGPA": ["s g p a"],
        "DBMS": ["d b m s"],
        "DSA": ["d s a"],
        "OOP": ["o o p"],
        "viva": [],
        "end-sem": ["end sem", "end sam", "nsam", "ends em"],
        "mid-sem": ["mid sem"],
        "internals": [],
        "lab record": [],
        "assignment": [],
        "syllabus": [],
        "semester": [],
        "thermodynamics": ["thermo dynamics"],
    },
}

# Parakeet is decoded with hotwords: a long list starts "hearing" them in background noise, so only these
# genuinely unusual words are biased; everything else is handled by fix() afterwards.
HOTWORDS: dict[str, list[str]] = {
    "developer": ["GitHub", "VS Code", "Claude Code", "PyTorch", "NumPy", "Jupyter", "Ollama", "TypeScript",
                  "Node.js", "Next.js", "npm", "Django", "FastAPI", "PostgreSQL", "SQLite", "Supabase", "Vercel",
                  "Kubernetes", "kubectl", "LeetCode", "ESP32", "Arduino", "PlatformIO", "GPIO", "regex"],
    "3d": ["Blender", "Fusion 360", "SolidWorks", "AutoCAD", "Onshape", "Tinkercad", "FreeCAD", "OpenSCAD",
           "KiCad", "ZBrush", "STL", "G-code", "PETG", "PLA", "TPU", "infill", "Cura", "PrusaSlicer",
           "Bambu Studio", "OrcaSlicer", "Thingiverse", "Printables", "retopology", "fillet", "chamfer"],
    "student": ["Moodle", "Google Classroom", "MATLAB", "LaTeX", "Overleaf", "Quizlet", "Desmos", "Zotero",
                "NPTEL", "CGPA", "DBMS", "viva", "end-sem", "syllabus"],
}

_compiled: dict[tuple, list[tuple[re.Pattern, str]]] = {}


def _rules(domains: tuple[str, ...]) -> list[tuple[re.Pattern, str]]:
    if domains not in _compiled:
        rules = []
        for d in domains:
            for canon, variants in DOMAINS.get(d, {}).items():
                for v in variants:
                    words = [re.escape(w) for w in v.split()]
                    rules.append((re.compile(r"(?<![\w-])" + r"[\s-]*".join(words) + r"(?![\w-])", re.I), canon))
        rules.sort(key=lambda r: len(r[0].pattern), reverse=True)  # longest first: "jupiter notebook" before "jupiter"
        _compiled[domains] = rules
    return _compiled[domains]


def domains(cfg) -> tuple[str, ...]:
    try:
        return tuple(cfg.get("voice.lexicon") or ())
    except (KeyError, TypeError):
        return tuple(DOMAINS)


def fix(text: str, cfg=None, which: tuple[str, ...] | None = None) -> str:
    """Repair the usual mishearings of technical words. 'open fusion three sixty' -> 'open Fusion 360'."""
    if not text:
        return text
    out = text
    for rx, canon in _rules(which if which is not None else (domains(cfg) if cfg is not None else tuple(DOMAINS))):
        out = rx.sub(canon, out)
    return out


def hotwords(cfg, extra: list[str] | None = None) -> list[str]:
    """Words to bias recognition toward: the config's own list, the chosen domains, and your project names."""
    words = list(cfg.get("voice.hotwords") or [])
    for d in domains(cfg):
        words += HOTWORDS.get(d, [])
    words += extra if extra is not None else _project_words()
    seen, out = set(), []
    for w in words:
        w = " ".join(str(w).split())
        if w and w.lower() not in seen and len(w) <= 40:
            seen.add(w.lower())
            out.append(w)
    return out


def _project_words() -> list[str]:
    """Names of your projects (tracked ones, GitHub repos, project folders), spoken the way you'd say them."""
    try:
        from batsy import projects

        return projects.spoken_names(limit=40)
    except Exception:
        return []


def whisper_prompt(cfg, base: str) -> str:
    """Whisper reads its prompt as earlier speech: a few of the words you use, kept short (it has a token limit)."""
    words = [w for w in hotwords(cfg) if w.lower() not in base.lower()]
    return (base.rstrip(".") + ", " + ", ".join(words[:45]) + ".") if words else base
