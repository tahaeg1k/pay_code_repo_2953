"""Deterministic guards that run before any AI and before PayPal. They do not depend on a model noticing anything.

- scan_text / scan_basket: hidden characters (zero-width, bidi overrides, Unicode tag characters) and text aimed
  at an AI ("ignore previous instructions", "approved by the user", "raise the cap") in merchant or agent text.
- catalog_prices: when ScopePay knows a shop's catalog, every basket line must be a real product at its real
  price, so a tampered checkout page cannot relabel a £34 keyboard as £3.40.
"""
import re
import unicodedata

import catalog

# zero-width and joiners, bidi controls, word joiner / invisible operators, BOM, soft hyphen, Unicode "tag" block
_HIDDEN = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
_INSTRUCTION = [
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|all|any|earlier|your)\b.{0,20}\b(instruction|rule|prompt|limit|guideline)s?\b",
    r"\b(system|developer)\s*(prompt|message|instruction)s?\b",
    r"</?\s*(system|instructions?|assistant|tool_?call)\s*>",
    r"\byou are now\b",
    r"\b(pre-?)?approved by (the )?(user|owner|payer|wallet owner)\b",
    r"\b(raise|increase|remove|lift|bypass|disable)\b.{0,20}\b(cap|limit|budget|threshold|intent check|rules?)\b",
    r"\bverdict\b.{0,15}\ballow\b",
    r"\b(send|transfer|pay)\b.{0,30}\b(to|into)\b.{0,10}\b(this|my|the following)\b.{0,10}\b(account|wallet|email|address)\b",
]
_INSTRUCTION_RE = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _INSTRUCTION]


def scan_text(text):
    """Return a list of findings (empty when clean). Never raises."""
    if not isinstance(text, str) or not text:
        return []
    found = []
    hidden = _HIDDEN.findall(text)
    if hidden:
        names = sorted({unicodedata.name(ch, f"U+{ord(ch):04X}") for ch in hidden})
        found.append(f"hidden characters ({', '.join(names[:3])}{'…' if len(names) > 3 else ''})")
    visible = _HIDDEN.sub("", unicodedata.normalize("NFKC", text))
    for rx in _INSTRUCTION_RE:
        mt = rx.search(visible)
        if mt:
            found.append(f"text aimed at an AI: “{mt.group(0)[:60]}”")
            break
    return found


def scan_basket(basket):
    """Findings across every line name. Returns [(line_name, finding)]."""
    out = []
    for i in basket or []:
        for f in scan_text(i.get("name", "")):
            out.append((_HIDDEN.sub("", i.get("name", ""))[:60], f))
    return out


def catalog_prices(merchant_id, basket):
    """(ok, detail). Shops without a catalog in ScopePay are not checked (ok=True)."""
    products = catalog.products(merchant_id)
    if not products:
        return True, "no catalog on file for this shop"
    by_name = {p["name"].casefold(): p for p in products}
    for i in basket or []:
        p = by_name.get(str(i.get("name", "")).strip().casefold())
        if not p:
            return False, f"“{str(i.get('name', ''))[:60]}” is not sold at this shop"
        if abs(float(i.get("price", -1)) - p["price"]) > 0.005:
            return False, f"{p['name']} is £{p['price']:.2f}, basket says £{float(i.get('price', 0)):.2f}"
        qty = i.get("qty")
        if not isinstance(qty, int) or not 1 <= qty <= 50:
            return False, f"bad quantity for {p['name']}: {qty!r}"
    return True, f"{len(basket or [])} line(s) match the shop's prices"
