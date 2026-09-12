"""What an agent should read before it builds anything.

SKILL.md is written for a model, not for a human browsing the repository: short
imperative rules, one worked example, and the two or three mistakes that cost a
round trip. It ships inside the package so `uvx applace` carries it.

The document alone is not enough, because half of what an agent needs is about
*this* machine: which stacks are installed, whether code goes to GitHub, what
the dependency policy allows. :func:`describe` answers the document and the
machine in one call, which is what `get_skill` returns.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from . import github, policy, vercel
from .paths import ApplacePaths
from .stacks import StackError, registry

DOCUMENT = "SKILL.md"


def text() -> str:
    """SKILL.md as shipped."""
    return resources.files("applace").joinpath(DOCUMENT).read_text(encoding="utf-8")


def describe(paths: ApplacePaths) -> dict[str, Any]:
    """The skill, plus what this machine is set up to do."""
    out: dict[str, Any] = {"skill": text(), "home": str(paths.home)}

    try:
        available = registry(paths.stacks)
        out["stacks"] = [stack.summary() for stack in available.values()]
    except StackError as exc:  # pragma: no cover - a broken install
        out["stacks"] = []
        out["warning"] = str(exc)

    link = github.load(paths)
    out["github"] = (
        {
            "org": link.org,
            "host": link.host,
            "visibility": link.visibility,
            "note": (
                f"every app created here becomes a {link.visibility} repository "
                f"in {link.org}, and every green write is pushed to it"
            ),
        }
        if link is not None
        else None
    )

    production = policy.DEFAULT_RULE
    try:
        rules = policy.load(paths)
        out["policy"] = {"summary": rules.summary(), **rules.as_dict()}
        production = rules.production_deploys
    except policy.PolicyError as exc:
        # Say it here rather than let the agent discover it as a red gate.
        out["policy"] = {"error": str(exc)}

    # Where an app can be shipped from this machine. `local` is always there;
    # saying so is how an agent knows deploying is available before it asks a
    # human for an account it does not need.
    hosted = vercel.load(paths)
    out["deploy"] = {
        "targets": ["local", "vercel"] if hosted is not None else ["local"],
        "vercel": {"team": hosted.team} if hosted is not None else None,
        "production": production,
        "note": (
            "a production deploy needs a human's confirmation, whatever the "
            "policy says"
        ),
    }
    return out


__all__ = ["describe", "text"]
