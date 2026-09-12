"""Applace: build, preview and ship web apps from any LLM agent.

Four ways in, one set of behaviours: the `applace` CLI for a person, the MCP
server for an agent, `SKILL.md` for the model reading it, and this package for
the developer embedding it in a backend (D17).

    from applace import Applace

    ap = Applace()
    made = ap.create("Team Dashboard")
    ap.write(made["app"], {"src/App.tsx": code})
"""

from .api import Applace

__all__ = ["Applace"]
