"""
Product type classifier for Eversports product names.

Classification hierarchy (highest priority first):
  1. trial      — matches trial/probe/etc. keywords
  2. membership — matches mitgliedschaft/abo/etc. keywords
  3. voucher    — matches gutschein/voucher/etc. keywords
  4. merch      — matches merch/shirt/mat/etc. keywords
  5. card       — explicit positive keywords (karte/card/pack/credits/punktekarte)
                  OR residual (not any of the above)

Note on 'trial card': a product named '3 Trial Cards-Introduction to Pilates Reformer'
contains both 'trial' and 'card' keywords. is_trial() wins per spec — is_card() checks
is_trial() first and returns False if it matches.

Per-location overrides: locations.product_keyword_map is not evaluated here.
The caller (bootstrap.py) should apply overrides after calling classify_product().

Reference: 07_foundation_layer.md § "Helper Functions" and
§ "Updated helper: explicit-positive is_card".
"""


def is_trial(name: str) -> bool:
    """
    Match trial/introductory session products.

    Keywords (case-insensitive): trial, probe, probestunde, schnupper, schnupperkurs,
    intro, introductory, einführung, einfuhrung, starter
    """
    s = str(name).lower()
    return any(
        k in s
        for k in (
            "trial",
            "probe",
            "probestunde",
            "schnupper",
            "intro",
            "introductory",
            "einführung",
            "einfuhrung",
            "starter",
        )
    )


def is_membership(name: str) -> bool:
    """
    Match membership/subscription products.

    Keywords (case-insensitive): mitgliedschaft, membership, abo, abonnement,
    flatrate, flat rate, subscription
    """
    s = str(name).lower()
    return any(
        k in s
        for k in (
            "mitgliedschaft",
            "membership",
            " abo",
            "abo-",
            "abonnement",
            "flatrate",
            "flat rate",
            "subscription",
        )
    )


def is_voucher(name: str) -> bool:
    """
    Match voucher/gift products.

    Keywords (case-insensitive): gutschein, voucher, geschenk, gift
    """
    s = str(name).lower()
    return any(k in s for k in ("gutschein", "voucher", "geschenk", "gift"))


def is_merch(name: str) -> bool:
    """
    Match merchandise products.

    Keywords (case-insensitive): shirt, mat, matte, towel, handtuch, merchandise, merch
    """
    s = str(name).lower()
    return any(
        k in s for k in ("shirt", "mat", "matte", "towel", "handtuch", "merchandise", "merch")
    )


def is_card(name: str) -> bool:
    """
    Match multi-session card / punch-card products.

    Explicit positive keywords: karte, card, pack, credits, punktekarte.
    Exception: if is_trial(name) is True, return False — 'trial cards' defer to trial.

    Falls back to residual classification (not trial, not membership, not voucher,
    not merch) to preserve backwards compatibility with the spec.

    Reference: 07_foundation_layer.md § "Updated helper: explicit-positive is_card".
    """
    s = str(name).lower()
    # Explicit positive keywords
    if any(k in s for k in ("karte", "card", "pack", "credits", "punktekarte")):
        # Trial card patterns defer to is_trial()
        if is_trial(name):
            return False
        return True
    # Residual fallback
    return (
        not is_trial(name)
        and not is_membership(name)
        and not is_voucher(name)
        and not is_merch(name)
    )


def classify_product(name: str) -> str:
    """
    Classify a product name into one of the canonical buckets.

    Returns: 'trial' | 'card' | 'membership' | 'voucher' | 'merch'

    Priority order: trial > membership > voucher > merch > card.
    'card' is last because it also serves as the residual bucket for unrecognised names.
    """
    if is_trial(name):
        return "trial"
    if is_membership(name):
        return "membership"
    if is_voucher(name):
        return "voucher"
    if is_merch(name):
        return "merch"
    return "card"
