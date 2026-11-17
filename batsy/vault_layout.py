"""The parts of the vault that make it a good Obsidian vault from day one: Home, the guide, templates, Bases
(live tables over note properties), Obsidian's settings (daily notes, attachments, graph colours, Batsy yellow),
and the Git ignore list. `install` only creates what's missing, so Jonathan's changes to any of these stay."""
import json
from pathlib import Path

HOME = """---
type: home
tags: [moc]
cssclasses: [batsy-home]
---
# 🦇 Batsy Brain

%% batsy:today %%
%% /batsy %%

## Projects
![[Projects.base]]

## Inbox: to sort
![[Inbox.base]]

## Recent
%% batsy:recent %%
%% /batsy %%

## Areas
[[3D Printing]] · [[Music]] · [[College]] · [[Home & Devices]] · [[About Jonathan]]

## Resources
![[Files.base]]
![[Prints.base]]

---
How this brain works: [[Batsy]]
"""

GUIDE = """---
type: guide
tags: [batsy]
---
# How the brain works

Batsy writes here as things happen, and reads it back when it answers you.

**Where things go**
| Folder | What |
|---|---|
| `00 Inbox` | quick notes: "Batsy, note that…", the ＋ in the phone app. Sort them into projects/areas later (or leave them) |
| `01 Daily` | one note per day: plan, timeline, files, prints, music, your notes, and a summary Batsy writes at night. Weekly reviews on Sunday nights |
| `02 Projects` | one per project you told Batsy about: repo, folder, updates |
| `03 Areas` | ongoing parts of life: 3D printing, music, college, home & devices |
| `04 Resources` | a card for every file you received, every print, every price comparison |
| `05 People` | who sent you what |
| `06 Memory` | [[About Jonathan]]: everything Batsy remembers. **Edit it**: changes go back into Batsy's memory |

**Batsy's blocks.** Text between `%% batsy:… %%` and `%% /batsy %%` is Batsy's: it rewrites it. (Obsidian hides
those markers when reading.) Everything else is yours and is never touched. In [[About Jonathan]] you *can* edit
inside the block: change a line to correct a memory, delete a line to make Batsy forget it, add a line to teach it.

**Say it**
- "Note that the bed needs levelling" · "add to my robot notes: the motor driver overheats"
- "What did I do yesterday?" · "what did I do this week?" · "what do my notes say about PLA?"
- "Open my brain" · "open today's note" · "open the robot project note" · "sync my brain"

**Sync.** Batsy commits the vault every few minutes (full history) and pushes it to your private GitHub repo
`brain`. On the iPhone/Mac: Obsidian + the Obsidian Git plugin, cloning that repo.
"""

TEMPLATES = {
    "Daily": "## Plan\n\n## Summary\n\n## Timeline\n\n## Notes\n- \n",
    "Project": "---\ntype: project\nstatus: active\ncreated: {{date}}\ntags: [project]\n---\n# {{title}}\n\n"
               "## Goal\n\n## Next steps\n- [ ] \n\n## Log\n",
    "Note": "---\ntype: note\ncreated: {{date}}\ntags: []\n---\n# {{title}}\n\n",
    "Person": "---\ntype: person\ntags: [person]\n---\n# {{title}}\n\n",
}

BASES = {
    "Projects": """filters:
  and:
    - file.inFolder("02 Projects")
views:
  - type: table
    name: Active
    filters:
      and:
        - 'status != "done"'
    order:
      - file.name
      - status
      - updated
      - repo
    sort:
      - property: updated
        direction: DESC
  - type: cards
    name: All
""",
    "Files": """filters:
  and:
    - file.inFolder("04 Resources/Files")
views:
  - type: table
    name: Received
    order:
      - file.name
      - sender
      - kind
      - received
    sort:
      - property: received
        direction: DESC
    limit: 15
""",
    "Prints": """filters:
  and:
    - file.inFolder("04 Resources/Prints")
views:
  - type: table
    name: Prints
    order:
      - file.name
      - result
      - grams
      - time
      - date
    sort:
      - property: date
        direction: DESC
    limit: 15
""",
    "Inbox": """filters:
  and:
    - file.inFolder("00 Inbox")
views:
  - type: table
    name: Inbox
    order:
      - file.name
      - created
    sort:
      - property: created
        direction: DESC
""",
}

AREAS = {
    "3D Printing": "Ender-5 Pro (2019), Marlin 1.1.6, Bowden; OrcaSlicer with your presets.",
    "Music": "What you listen to, and the taste profile Batsy's radio uses.",
    "College": "Courses, assignments and documents.",
    "Home & Devices": "Laptop (Fedora), the PC (bob), phones, the light, the Batman drive.",
}

CSS = """/* Batsy Brain: Batsy yellow, and Batsy's own blocks shown as quiet, slightly inset text */
body { --interactive-accent: #ffc20e; --text-accent: #ffc20e; --link-color: #ffc20e; --link-unresolved-color: #b38a10; }
.batsy-home h1 { color: #ffc20e; }
/* Batsy's block markers (%% batsy:… %%): tiny and faint while editing (hidden anyway when reading) */
.markdown-source-view.mod-cm6 .cm-comment { font-size: 0.7em; opacity: .3; }
"""

GRAPH = {"colorGroups": [
    {"query": 'path:"01 Daily"', "color": {"a": 1, "rgb": 0x7a7f8a}},
    {"query": 'path:"02 Projects"', "color": {"a": 1, "rgb": 0xffc20e}},
    {"query": 'path:"03 Areas"', "color": {"a": 1, "rgb": 0x3ccf6e}},
    {"query": 'path:"04 Resources"', "color": {"a": 1, "rgb": 0x7aa7ff}},
    {"query": 'path:"05 People"', "color": {"a": 1, "rgb": 0xff7a7a}},
    {"query": 'path:"00 Inbox"', "color": {"a": 1, "rgb": 0xc58cff}},
], "showTags": False, "showAttachments": False, "hideUnresolved": True}

CORE = ["file-explorer", "global-search", "switcher", "graph", "backlink", "outgoing-link", "tag-pane", "properties",
        "page-preview", "daily-notes", "templates", "note-composer", "command-palette", "bookmarks", "outline",
        "word-count", "file-recovery", "bases", "canvas", "editor-status", "sync"]


def _new(p: Path, text: str) -> None:
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def install(root: Path) -> None:
    from batsy import vault

    _new(root / "Home.md", HOME)
    _new(root / "Batsy" / "Batsy.md", GUIDE)
    for name, body in TEMPLATES.items():
        _new(root / "Batsy" / "Templates" / f"{name}.md", body)
    for name, body in BASES.items():
        _new(root / "Batsy" / "Bases" / f"{name}.base", body)
    for name, about in AREAS.items():
        _new(root / "03 Areas" / f"{name}.md", vault.frontmatter(type="area", tags=["area"]) +
             f"# {name}\n{about}\n\n## Notes\n- \n\n## Log\n{vault.block('log')}\n")
    _new(root / "06 Memory" / "About Jonathan.md", vault.frontmatter(type="memory", tags=["memory"]) +
         "# About Jonathan\nEverything Batsy remembers. Edit a line to correct it, delete it to make Batsy forget, "
         "add one to teach it.\n\n" + vault.block("facts") + "\n")
    ob = root / ".obsidian"
    files = {
        "app.json": {"attachmentFolderPath": "_attachments", "newFileLocation": "folder",
                     "newFileFolderPath": "00 Inbox", "alwaysUpdateLinks": True, "showUnsupportedFiles": False,
                     "userIgnoreFilters": ["Batsy/Templates/"]},
        "appearance.json": {"theme": "obsidian", "accentColor": "#ffc20e", "enabledCssSnippets": ["batsy"]},
        "daily-notes.json": {"folder": "01 Daily", "format": "YYYY/YYYY-MM-DD", "template": "Batsy/Templates/Daily"},
        "templates.json": {"folder": "Batsy/Templates"},
        "graph.json": GRAPH,
        "core-plugins.json": {p: True for p in CORE},
        "bookmarks.json": {"items": [{"type": "file", "path": "Home.md", "title": "Home"},
                                     {"type": "file", "path": "06 Memory/About Jonathan.md"},
                                     {"type": "file", "path": "Batsy/Batsy.md", "title": "How the brain works"}]},
    }
    for name, data in files.items():
        _new(ob / name, json.dumps(data, indent=2))
    _new(ob / "snippets" / "batsy.css", CSS)
    # Git: the notes and the shared settings; not each device's window layout or caches
    _new(root / ".gitignore", ".obsidian/workspace*.json\n.obsidian/cache\n.trash/\n.*.tmp\n.DS_Store\n")
