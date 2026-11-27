"""What things cost.

A price (for a quote), part by part:
    material   grams x the rupees per gram of that material (the active spool's price / weight, else the price list)
    machine    print hours x machine_rate_per_hour      (the printer wearing out: nozzle, belts, bed, depreciation)
    power      print hours x printer_watts/1000 x kwh_price   (or electricity_rate_per_hour when no wattage is set)
  = subtotal, + maintenance / risk / labour / design percentages of it, (x qty), + packaging once per order,
  + profit margin, rounded up to `round_to`, never under `min_price`.

What a print really cost (a run): the grams it actually laid down (all of them when it finished, up to the line it
reached when it was stopped) x rupees per gram, + the hours it ran x (machine + power). A stopped or failed run's
cost is all loss: nothing sellable came out of it.

The rate names are PrintDesk Pro's own (filament_cost_per_gram ... profit_margin_pct), so its pricing page keeps
working; the rest are additions.
"""
import math

from batsy.shop import db

DEFAULT_RATES = {
    # PrintDesk Pro's
    "filament_cost_per_gram": 2.5,
    "machine_rate_per_hour": 30.0,
    "electricity_rate_per_hour": 8.0,
    "maintenance_pct": 5.0,
    "risk_pct": 5.0,
    "labour_pct": 10.0,
    "design_pct": 5.0,
    "packaging_flat": 50.0,
    "profit_margin_pct": 30.0,
    # added
    "printer_watts": 0.0,          # measured average draw; 0 = use electricity_rate_per_hour
    "kwh_price": 8.0,
    "materials": {"PLA": 2.5, "PETG": 3.0, "ABS": 3.0, "ASA": 3.5, "TPU": 4.5},  # rupees per gram
    "min_price": 100.0,
    "round_to": 10.0,
    "design_rate_per_hour": 300.0,  # modelling work you do for a customer
    "rush_pct": 25.0,               # due within 48 hours
    # Jonathan's own quick formula (27 Nov): base = grams x formula_per_gram + hours x formula_per_hour, + profit;
    # standard items at a fixed price. A suggestion: he sets the final price himself.
    "formula": 1.0,                 # 1 = quote with the formula below, 0 = the full PrintDesk breakdown above
    "formula_per_gram": 1.5,
    "formula_per_hour": 2.0,
    "formula_profit_pct": 50.0,
    "fixed_prices": {"keychain": 150.0},
}


def rates() -> dict:
    r = {**DEFAULT_RATES, **(db.setting("rates") or {})}
    r["materials"] = {**DEFAULT_RATES["materials"], **(r.get("materials") or {})}
    return r


def set_rates(changes: dict) -> dict:
    cur = {**(db.setting("rates") or {})}
    for k, v in changes.items():
        if k not in DEFAULT_RATES:
            continue
        if k == "materials":
            cur["materials"] = {str(m).upper(): float(p) for m, p in (v or {}).items()}
        elif k == "fixed_prices":
            cur["fixed_prices"] = {str(m).lower(): float(p) for m, p in (v or {}).items()}
        else:
            cur[k] = float(v)
    db.set_setting("rates", cur)
    return rates()


def per_gram(material: str, r: dict | None = None) -> float:
    """The active spool of that material decides (what you really paid), else the price list."""
    r = r or rates()
    mat = (material or "PLA").upper()
    sp = db.one("SELECT price, weight_g FROM spools WHERE active = 1 AND upper(material) = ? ORDER BY id DESC LIMIT 1",
                (mat,))
    if sp and sp["price"] and sp["weight_g"]:
        return float(sp["price"]) / float(sp["weight_g"])
    return float(r["materials"].get(mat, r["filament_cost_per_gram"]))


def power_per_hour(r: dict) -> float:
    if r.get("printer_watts"):
        return r["printer_watts"] / 1000 * r["kwh_price"]
    return float(r["electricity_rate_per_hour"])


def part_cost(grams: float, hours: float, material: str = "PLA", qty: int = 1, design_hours: float = 0,
              r: dict | None = None) -> dict:
    """The manufacturing cost of qty copies (before packaging and margin)."""
    r = r or rates()
    cpg = per_gram(material, r)
    one_material = grams * cpg
    one_machine = hours * r["machine_rate_per_hour"]
    one_power = hours * power_per_hour(r)
    sub = one_material + one_machine + one_power
    buffers = (r["maintenance_pct"] + r["risk_pct"] + r["labour_pct"] + r["design_pct"]) / 100
    each = sub * (1 + buffers)
    design = design_hours * r["design_rate_per_hour"]
    return {"per_gram": round(cpg, 3), "material": round(one_material * qty, 2), "machine": round(one_machine * qty, 2),
            "power": round(one_power * qty, 2), "buffers": round(sub * buffers * qty, 2), "design": round(design, 2),
            "each": round(each, 2), "manufacturing": round(each * qty + design, 2), "grams": round(grams * qty, 1),
            "hours": round(hours * qty, 2), "qty": qty}


def formula_price(parts: list[dict], discount: float = 0, r: dict | None = None) -> dict:
    """Jonathan's formula: each part (grams x 1.5 + hours x 2) x qty, + 50% profit, rounded up to the nearest 10;
    a standard item (a keychain) at its fixed price. Only a suggestion: the final price is his."""
    r = r or rates()
    fixed = {k.lower(): v for k, v in (r.get("fixed_prices") or {}).items()}
    lines, base_total, fixed_total = [], 0.0, 0.0
    for p in parts:
        g, h, qty = float(p.get("grams") or 0), float(p.get("hours") or 0), int(p.get("qty") or 1)
        name = str(p.get("name") or "").lower()
        std = next((v for k, v in fixed.items() if k in name), None)
        if std is not None:
            fixed_total += std * qty
            lines.append({"name": p.get("name"), "fixed": std, "qty": qty, "grams": round(g * qty, 1),
                          "hours": round(h * qty, 2), "each": std})
            continue
        base = g * r["formula_per_gram"] + h * r["formula_per_hour"]
        base_total += base * qty
        lines.append({"name": p.get("name"), "grams": round(g * qty, 1), "hours": round(h * qty, 2), "qty": qty,
                      "material": round(g * r["formula_per_gram"] * qty, 2), "machine": round(h * r["formula_per_hour"] * qty, 2),
                      "each": round(base, 2), "manufacturing": round(base * qty, 2)})
    price = base_total * (1 + r["formula_profit_pct"] / 100) + fixed_total
    price = max(price - (discount or 0), 0)
    step = r["round_to"] or 1
    price = math.ceil(price / step) * step if price else 0
    return {"lines": lines, "packaging": 0, "manufacturing": round(base_total, 2), "margin_pct": r["formula_profit_pct"],
            "rush": False, "discount": discount, "price": round(price, 2), "profit": round(price - base_total - fixed_total, 2),
            "formula": f"(₹{r['formula_per_gram']:g}/g + ₹{r['formula_per_hour']:g}/h) + {r['formula_profit_pct']:g}%",
            "suggested": True}


def order_price(parts: list[dict], rush: bool = False, discount: float = 0, r: dict | None = None) -> dict:
    """parts: [{grams, hours, material, qty, design_hours, name}] -> the full breakdown and the price to charge."""
    r = r or rates()
    if r.get("formula"):
        return formula_price(parts, discount, r)
    lines = [part_cost(p.get("grams") or 0, p.get("hours") or 0, p.get("material") or "PLA", int(p.get("qty") or 1),
                       float(p.get("design_hours") or 0), r) for p in parts]
    manufacturing = sum(x["manufacturing"] for x in lines) + (r["packaging_flat"] if parts else 0)
    price = manufacturing * (1 + r["profit_margin_pct"] / 100)
    if rush:
        price *= 1 + r["rush_pct"] / 100
    price = max(price - (discount or 0), 0)
    step = r["round_to"] or 1
    price = max(math.ceil(price / step) * step, r["min_price"] if parts else 0)
    return {"lines": lines, "packaging": r["packaging_flat"] if parts else 0, "manufacturing": round(manufacturing, 2),
            "margin_pct": r["profit_margin_pct"], "rush": rush, "discount": discount, "price": round(price, 2),
            "profit": round(price - manufacturing, 2)}


def run_cost(grams: float, hours: float, material: str = "PLA", r: dict | None = None) -> dict:
    """What a print actually used: filament laid down and hours the machine ran."""
    r = r or rates()
    if r.get("formula"):  # the same base rates as your quotes: a failed print loses what it would have been priced at
        cpg = float(r["formula_per_gram"])
        m, mach, pw = grams * cpg, hours * float(r["formula_per_hour"]), 0.0
        return {"grams": round(grams, 1), "hours": round(hours, 3), "per_gram": round(cpg, 3), "material": round(m, 2),
                "machine": round(mach, 2), "power": 0.0, "total": round(m + mach, 2)}
    cpg = per_gram(material, r)
    m, mach, pw = grams * cpg, hours * r["machine_rate_per_hour"], hours * power_per_hour(r)
    return {"grams": round(grams, 1), "hours": round(hours, 3), "per_gram": round(cpg, 3), "material": round(m, 2),
            "machine": round(mach, 2), "power": round(pw, 2), "total": round(m + mach + pw, 2)}
