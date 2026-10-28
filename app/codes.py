"""Three-letter series codes: validation and a suggestion derived from the series name."""
import re
import string

from .payouts import RuleError

SKIP = {"the", "a", "an", "of", "and"}


def normalise_code(text: str) -> str:
    code = text.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise RuleError("Series code must be exactly three letters (A-Z)")
    return code


def suggest_code(name: str, taken=()) -> str:
    """FNB for 'Friday Night Brawl', WWA for 'The Weekend Warriors'; never one already in `taken`."""
    taken = {t.upper() for t in taken}
    words = [w for w in re.findall(r"[A-Za-z]+", name) if w.lower() not in SKIP] or re.findall(r"[A-Za-z]+", name)
    letters = "".join(words).upper() or "SER"
    if len(words) >= 3:
        base = "".join(w[0] for w in words[:3])
    elif len(words) == 2:
        base = words[0][0] + words[1][:2]
    else:
        base = letters[:3]
    base = (base.upper() + "XXX")[:3]
    if base not in taken:
        return base
    for extra in letters + string.ascii_uppercase:  # vary the last letter until free
        candidate = base[:2] + extra
        if candidate not in taken:
            return candidate
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            for c in string.ascii_uppercase:
                if a + b + c not in taken:
                    return a + b + c
    raise RuleError("No series codes left")
