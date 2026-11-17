"""Brain tools: Jonathan's Obsidian vault (~/Batsy/Brain). Search it, read a note, add to one."""
from batsy import vault, vault_capture, vault_index


def register(reg):
    @reg.tool(
        "notes_search",
        "Search the user's notes (their Obsidian 'brain': daily notes, projects, files, prints, people, memory) when "
        "they ask what they wrote, did, planned or decided, or about something they took notes on.",
        {"query": {"type": "string"}},
        ["query"],
    )
    def notes_search(ctx, query):
        vault_index.reindex()
        hits = vault_index.search(query, 5)
        return "\n".join(f"{h['title']} ({h['path']}): {h['snippet']}" for h in hits) or "Nothing in the notes."

    @reg.tool(
        "notes_read",
        "Read one note from the user's brain by its title (e.g. '2026-11-16', 'Robot', 'About Jonathan').",
        {"title": {"type": "string"}},
        ["title"],
    )
    def notes_read(ctx, title):
        hit = vault_index.note_text(title)
        return vault_index._plain(hit[1])[:4000] if hit else "No such note."

    @reg.tool(
        "notes_add",
        "Add to the user's brain. kind 'note' makes a new inbox note; kind 'project' adds a line to that project's "
        "log (give project).",
        {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["note", "project"]},
         "project": {"type": "string"}},
        ["text"],
    )
    def notes_add(ctx, text, kind="note", project=""):
        if kind == "project" and project:
            vault_capture.project_update(project.strip().capitalize(), text, "note")
            return f"Added to the {project} project note."
        vault_capture.note(text, "ai")
        return "Saved to the inbox."
