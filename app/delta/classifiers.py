"""
Product type classifiers for the delta engine.

Determines whether an Eversports product name is a trial, card, membership,
voucher, drop-in, or merchandise item.

Classification order (first match wins):
  1. Per-location keyword map  (``locations.product_keyword_map``)
  2. Built-in keyword rules    (below)
  3. Fallback → ``"unknown"``

The per-location map is a JSONB dict on ``Location``:
  {
    "trial":      ["schnupperkurs", "probestunde"],
    "membership": ["monatsabo-premium"],
    "card":       ["duo-karte"]
  }
Any product name fragment listed under a type key overrides the built-in rules
for that product.  Keys are matched case-insensitively, as substrings.

This module is dependency-free (pure Python).  All functions are synchronous.

References:
  - requirements_v2/00_master_overview.md §GHL Tags
  - requirements_v2/03_ghl_pipelines.md §Pipeline entry points
"""

from __future__ import annotations

import re

# ── Type literal ───────────────────────────────────────────────────────────────
# Matches the ``active_package_type`` GHL custom field values from the spec.
ProductType = str  # "trial" | "card" | "membership" | "voucher" | "drop_in" | "unknown"

# ── Built-in keyword rules (ordered: first match wins within each group) ───────
# Each tuple is (product_type, list_of_substrings_to_match).
# Substrings are matched case-insensitively against the full product name.
_BUILTIN_RULES: list[tuple[str, list[str]]] = [
    ("trial", [
        "trial", "probe", "schnupper", "intro",
        "probierstunde", "probestunde", "schnupperkurs",
        "first class", "erstklasse",
    ]),
    ("membership", [
        "membership", "mitgliedschaft",
        "abo ", "monat", "jahres",   # space after "abo" avoids "karabo" etc.
        "unlimited", "flatrate", "flat rate",
        "monatsabo", "jahresabo",
    ]),
    ("card", [
        "karte", "card",
        "10er", "5er", "3er", "20er", "8er",
        "punch", "block",
        # "voucher" and "gutschein" are intentionally listed here, not in the
        # "voucher" bucket below.  In Eversports, gift vouchers are sold as
        # card-like products (they grant class credits).  This means the built-in
        # "voucher" ProductType is unreachable without a per-location keyword_map
        # override — which is by design for studios that sell standalone gift
        # vouchers they want to track separately.
        "voucher", "gutschein",
    ]),
    ("voucher", [
        # Only reachable via per-location keyword_map override (see above).
        "geschenkgutschein", "gift",
    ]),
    ("drop_in", [
        "drop-in", "drop in", "einzelstunde",
        "single class", "single session",
        "pay as you go", "payg",
    ]),
]


def classify_product(
    name: str,
    keyword_map: dict[str, list[str]] | None = None,
) -> ProductType:
    """
    Classify an Eversports product name into a ``ProductType``.

    Args:
        name: Product name string (e.g. ``"10er Karte-Gruppe"``).
        keyword_map: Per-location override map from ``Location.product_keyword_map``.
            Keys are product type strings; values are lists of substrings (case-insensitive).

    Returns:
        Product type string: ``"trial"``, ``"card"``, ``"membership"``,
        ``"voucher"``, ``"drop_in"``, or ``"unknown"``.
    """
    if not name:
        return "unknown"

    name_lower = name.lower()

    # 1. Per-location overrides take priority
    if keyword_map:
        for ptype, keywords in keyword_map.items():
            for kw in keywords:
                if kw.lower() in name_lower:
                    return ptype

    # 2. Built-in rules
    for ptype, keywords in _BUILTIN_RULES:
        for kw in keywords:
            if kw in name_lower:
                return ptype

    return "unknown"


def classify_products(
    product_names: list[str],
    keyword_map: dict[str, list[str]] | None = None,
) -> list[ProductType]:
    """Classify a list of product names. Returns a parallel list of types."""
    return [classify_product(name, keyword_map) for name in product_names]


def active_package_type_from_products(
    products: list[dict],
    keyword_map: dict[str, list[str]] | None = None,
) -> ProductType:
    """
    Determine the ``active_package_type`` from a contact's ``products_purchased``
    list (as stored in Postgres ``contacts.products_purchased``).

    Hierarchy (most specific wins):
      membership > card > trial > voucher > drop_in > unknown

    Args:
        products: List of product dicts (each has at least a ``"name"`` key).
        keyword_map: Per-location override map.

    Returns:
        The highest-priority product type found, or ``"unknown"`` if the list
        is empty or all products classify as unknown.
    """
    _PRIORITY = {
        "membership": 5,
        "card": 4,
        "trial": 3,
        "voucher": 2,
        "drop_in": 1,
        "unknown": 0,
    }
    best: ProductType = "unknown"
    best_priority = 0

    for product in products:
        name = product.get("name", "") if isinstance(product, dict) else str(product)
        ptype = classify_product(name, keyword_map)
        priority = _PRIORITY.get(ptype, 0)
        if priority > best_priority:
            best = ptype
            best_priority = priority

    return best


def is_trial(name: str, keyword_map: dict | None = None) -> bool:
    return classify_product(name, keyword_map) == "trial"


def is_card(name: str, keyword_map: dict | None = None) -> bool:
    return classify_product(name, keyword_map) == "card"


def is_membership(name: str, keyword_map: dict | None = None) -> bool:
    return classify_product(name, keyword_map) == "membership"


def is_voucher(name: str, keyword_map: dict | None = None) -> bool:
    return classify_product(name, keyword_map) == "voucher"


def is_drop_in(name: str, keyword_map: dict | None = None) -> bool:
    return classify_product(name, keyword_map) == "drop_in"
