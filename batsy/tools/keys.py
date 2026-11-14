"""Typing into the user's focused app, for requests like "write a reply to this email" (batsy/keyboard.py)."""
from batsy import keyboard


def register(reg):
    @reg.tool(
        "type_text",
        "Type text into whatever app currently has keyboard focus on the user's laptop, as if typed on the keyboard "
        "(ASCII only). Use when the user asks you to type or write something out for them. Optionally press keys "
        "afterwards, e.g. 'enter'. Batsy's own screen closes first so the text lands in the user's app.",
        {
            "text": {"type": "string", "description": "the exact text to type"},
            "then_press": {"type": "string", "description": "optional keys to press after, e.g. 'enter' or 'ctrl s'"},
        },
        ["text"],
    )
    def type_text(ctx, text, then_press=None):
        if not keyboard.ready():
            return "Typing isn't set up. Tell the user to say 'set up typing' once."
        if ctx.display:
            ctx.display("hide")
        import time

        time.sleep(0.9)
        if not keyboard.type_text(text):
            return "Typing failed."
        if then_press:
            codes = keyboard.parse_keys(then_press)
            if codes is None or not keyboard.press(codes):
                return f"Typed the text, but couldn't press {then_press!r}."
        return f"Typed {len(text)} characters."
