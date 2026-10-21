"""Spoken arithmetic answered instantly, without any AI: "what is five plus five", "15% of 80",
"square root of 144", "12 times 7 divided by 3". Returns None for anything that isn't plain math."""
import ast
import math
import operator
import re

ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}

# Longest phrases first so "divided by" wins over "by".
OPS = [("multiplied by", "*"), ("divided by", "/"), ("to the power of", "**"), ("raised to", "**"),
       ("plus", "+"), ("add", "+"), ("minus", "-"), ("subtract", "-"), ("times", "*"), ("into", "*"),
       ("over", "/"), ("x", "*"), ("squared", "**2"), ("cubed", "**3"), ("percent of", "/100*"),
       ("% of", "/100*"), ("mod", "%")]
LEAD = re.compile(r"^(?:(?:hey |ok |okay )?(?:what(?:'s| is)|whats|calculate|compute|how much is|tell me|can you tell me"
                  r"|do you know)\s+)+")

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod}


def _words_to_numbers(text: str) -> str:
    out, cur, total, in_num = [], 0, 0, False

    def flush():
        nonlocal cur, total, in_num
        if in_num:
            out.append(str(total + cur))
        cur, total, in_num = 0, 0, False

    for w in text.split():
        if w in ONES:
            cur += ONES[w]; in_num = True
        elif w in TENS:
            cur = cur * 100 + TENS[w] if in_num and 10 <= cur < 100 else cur + TENS[w]  # "nineteen ninety"
            in_num = True
        elif w in SCALES and in_num:
            if SCALES[w] == 100:
                cur *= 100
            else:
                total += cur * SCALES[w]; cur = 0
        elif w == "and" and in_num:
            continue
        else:
            flush()
            out.append(w)
    flush()
    return " ".join(out)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("too big")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand)
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "sqrt" and len(node.args) == 1:
        return math.sqrt(_eval(node.args[0]))
    raise ValueError("not arithmetic")


def _fmt(v: float) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    return f"{v:.4g}" if abs(v) < 1e6 else f"{v:,.0f}"


def answer(text: str) -> str | None:
    t = text.lower().strip(" ?.!")
    t = re.sub(r"[,?!]", " ", t)
    t = LEAD.sub("", t)
    t = re.sub(r"^(?:the )?square root of (.+)$", r"sqrt(\1)", t)
    t = _words_to_numbers(t)
    for phrase, sym in OPS:
        t = re.sub(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", f" {sym} ", t)
    t = re.sub(r"(?<=\d)\s*%(?!\s*of)", "/100", t)
    t = re.sub(r"\s+", " ", t).strip().replace("÷", "/").replace("×", "*")
    if not re.fullmatch(r"[\d\s.+\-*/%()sqrt]+", t) or not re.search(r"\d", t) or \
            not re.search(r"[+\-*/%]|sqrt", t):
        return None
    try:
        v = _eval(ast.parse(t, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    return f"That's {_fmt(v)}."
