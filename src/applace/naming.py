"""Turning what a human or an agent typed into a directory name.

An app's slug is its identity everywhere: the directory under ``apps/``, the
GitHub repository name, the Vercel project. It therefore has to survive all
three, which is a stricter rule than any of them applies alone.
"""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s_./\\]+")
_ILLEGAL = re.compile(r"[^a-z0-9-]+")
_RUNS = re.compile(r"-{2,}")

MAX_SLUG = 64

# Names a repository host or a filesystem would take badly, whatever we think
# of them.
RESERVED = frozenset({".", "..", ".git", "con", "prn", "aux", "nul", "node_modules"})


class InvalidName(ValueError):
    """The name has nothing usable in it, or names something reserved."""


def slugify(raw: str) -> str:
    """``My Team's Dashboard!`` -> ``my-teams-dashboard``.

    Raises :class:`InvalidName` rather than inventing a slug, because a caller
    that named an app in Japanese deserves to be told we cannot, not to be given
    ``app-1``.
    """
    stripped = raw.strip().lower()
    if stripped in RESERVED:
        raise InvalidName(f"{raw.strip()!r} is a reserved name. Pick another.")
    lowered = _SEPARATORS.sub("-", stripped)
    # Apostrophes vanish rather than becoming separators: "team's" is one word.
    lowered = lowered.replace("'", "").replace("’", "")
    slug = _RUNS.sub("-", _ILLEGAL.sub("-", lowered)).strip("-")
    if not slug:
        raise InvalidName(
            f"{raw!r} has no letters or digits that can be used in a name. "
            "Names must contain latin letters or digits."
        )
    if slug[0].isdigit():
        # npm tolerates it, git tolerates it, humans reading a URL do not.
        slug = f"app-{slug}"
    slug = slug[:MAX_SLUG].strip("-")
    if slug in RESERVED:
        raise InvalidName(f"{slug!r} is a reserved name. Pick another.")
    return slug


def display_name(raw: str) -> str:
    """The name as typed, cleaned of whitespace. This is what the app is titled."""
    return " ".join(raw.split())
