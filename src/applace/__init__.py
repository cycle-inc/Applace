"""Applace: build, preview and ship web apps from any LLM agent.

Four ways in, one set of behaviours: the `applace` CLI for a person, the MCP
server for an agent, `SKILL.md` for the model reading it, and this package for
the developer embedding it in a backend (D17).

    from applace import Applace

    ap = Applace()
    made = ap.create("Team Dashboard")
    ap.write(made["app"], {"src/App.tsx": code})

A backend serving more than one person gives each of them their own home under
a root, and gets the machine's arbitration -- ports, quotas, collection -- with
it (D20):

    from applace import Machine

    ap = Machine("/srv/applace").user("alice@example.com")
"""

from .api import Applace
from .machine import Limits, Machine

__all__ = ["Applace", "Machine", "Limits"]
