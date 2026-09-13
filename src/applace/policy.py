"""What an agent is allowed to pull in, and what it may expose (D9, D6).

Two rules live here, and they are the two places where a company's opinion has
to be able to override an agent's.

**Dependencies are policy (D9).** Every dependency a write adds is reported by
the gate and checked here. The default allows anything from the public registry
and only reports it, because a harness that refuses by default is a harness
nobody switches on; a company tightens it by writing ``~/.applace/policy.yaml``.

**Exposure is gated, writing is not (D6).** Making code public is the act a
human has to be present for, so the policy can require confirmation (the
default) or refuse it outright.

**Egress is policy too (D24).** An app reaches the company's APIs through the
gateway, and which hosts the gateway will proxy for is the same kind of question
as which dependencies may be installed -- so it is answered in the same file, the
same way, with the same permissive default. A company that has an internal
domain writes it down and the machine stops proxying anywhere else.

The file is optional and a broken one is loud: a policy that silently reads as
"allow everything" because of a typo is worse than no policy at all.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .paths import ApplacePaths

# What an exposure rule can say. `confirm` is the D6 default: a human answers,
# once, at the moment of exposure.
RULES = ("allow", "confirm", "deny")
DEFAULT_RULE = "confirm"

DEFAULT_REGISTRY = "https://registry.npmjs.org"

# A version spec that is not a version: a git URL, a tarball, a local path. Npm
# accepts all of these, and each one is a way to bring in code the registry
# never saw.
_REMOTE_SPEC = re.compile(
    r"^(?:https?:|git\+|git:|ssh:|file:|link:|github:|bitbucket:|gitlab:|[\w.-]+/[\w.-]+$)",
    re.IGNORECASE,
)

_NPMRC_REGISTRY = re.compile(r"^\s*(?:[^\s=]*:)?registry\s*=\s*(\S+)", re.MULTILINE)


class PolicyError(Exception):
    """The policy is the answer: the file is wrong, or it refuses what was asked.

    Both are a human's business and neither is worth retrying, which is why they
    are one code to a caller (D22): a policy refusal is final until someone edits
    the file, and an agent that reads `policy` should stop rather than rephrase.
    """


@dataclass(frozen=True)
class Refusal:
    """One dependency the policy will not have, and why."""

    name: str
    version: str
    reason: str
    where: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "reason": self.reason,
            "where": self.where,
        }


@dataclass(frozen=True)
class Policy:
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    registry: str | None = None
    public_repositories: str = DEFAULT_RULE
    production_deploys: str = DEFAULT_RULE
    # The hosts the gateway may proxy to (D24). Empty means any, and reported --
    # the same default as dependencies, for the same reason.
    hosts: tuple[str, ...] = ()
    source: Path | None = field(default=None, compare=False)

    @property
    def restricted(self) -> bool:
        """Does this policy refuse any dependency?"""
        return bool(self.allow or self.deny or self.registry)

    @property
    def narrowed(self) -> bool:
        """Does this policy refuse anything at all, dependencies or egress?"""
        return self.restricted or bool(self.hosts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source) if self.source else None,
            "dependencies": {
                "allow": list(self.allow),
                "deny": list(self.deny),
                "registry": self.registry,
                "restricted": self.restricted,
            },
            "exposure": {
                "public_repositories": self.public_repositories,
                "production_deploys": self.production_deploys,
            },
            "apis": {"hosts": list(self.hosts), "restricted": bool(self.hosts)},
        }

    def summary(self) -> str:
        """One line an agent can be told without reading the file."""
        if not self.narrowed:
            return "any dependency from the public registry is allowed, and reported"
        parts: list[str] = []
        if self.allow:
            parts.append(f"only {', '.join(self.allow)} may be added")
        if self.deny:
            parts.append(f"{', '.join(self.deny)} may not be added")
        if self.registry:
            parts.append(f"dependencies must come from {self.registry}")
        if self.hosts:
            parts.append(f"APIs may only call {', '.join(self.hosts)}")
        return "; ".join(parts)

    # -- the checks --------------------------------------------------------

    def check(self, new_deps: list[dict[str, str]]) -> list[Refusal]:
        """Which of these additions the policy refuses (D9)."""
        refused: list[Refusal] = []
        for dependency in new_deps:
            name = str(dependency.get("name", ""))
            version = str(dependency.get("version", ""))
            reason = self._refuse(name, version)
            if reason:
                refused.append(Refusal(name=name, version=version, reason=reason))
        return refused

    def _refuse(self, name: str, version: str) -> str:
        if self.deny and _matches(name, self.deny):
            return f"{name} is on this machine's denylist"
        if self.allow and not _matches(name, self.allow):
            return (
                f"{name} is not on this machine's allowlist "
                f"({', '.join(self.allow)})"
            )
        if self.registry and _REMOTE_SPEC.match(version) and not self._from_registry(version):
            return (
                f"{name} would come from {version!r} rather than from "
                f"{self.registry}, which is the only registry this machine allows"
            )
        return ""

    def _from_registry(self, spec: str) -> bool:
        allowed = urlparse(self.registry or "").netloc
        return bool(allowed) and urlparse(spec).netloc == allowed

    def check_registry_config(self, root: Path) -> Refusal | None:
        """Refuse an app that points npm at a registry the policy did not allow.

        Pinning the registry in the policy and letting a write drop an `.npmrc`
        beside the manifest would be a rule with a hole in it.
        """
        if not self.registry:
            return None
        npmrc = root / ".npmrc"
        try:
            content = npmrc.read_text(encoding="utf-8")
        except OSError:
            return None
        for found in _NPMRC_REGISTRY.findall(content):
            if not self._from_registry(found):
                return Refusal(
                    name=".npmrc",
                    version=found,
                    reason=(
                        f".npmrc points npm at {found}, but this machine only "
                        f"allows {self.registry}"
                    ),
                    where=".npmrc",
                )
        return None

    def refuse_api(self, name: str, host: str) -> str:
        """Why this machine will not proxy to that host, or "" if it will.

        Checked when the declaration is made *and* when the call is forwarded: a
        policy tightened after an app was built must take effect on the next
        request, not on the next rewrite of the app.
        """
        if not self.hosts:
            return ""
        if _matches(host, self.hosts):
            return ""
        return (
            f"{name} would call {host}, and this machine only proxies to "
            f"{', '.join(self.hosts)}"
        )

    def rule_for(self, act: str) -> str:
        """The exposure rule for ``public_repositories`` or ``production_deploys``."""
        return str(getattr(self, act, DEFAULT_RULE))


DEFAULT = Policy()


def load(paths: ApplacePaths) -> Policy:
    """Read ``~/.applace/policy.yaml``, or the default when there is none."""
    path = paths.policy
    if not path.exists():
        return DEFAULT
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"{path} could not be read: {exc}") from exc
    if raw is None:
        return Policy(source=path)
    if not isinstance(raw, dict):
        raise PolicyError(f"{path} must be a mapping, not {type(raw).__name__}")

    dependencies = _section(path, raw, "dependencies")
    exposure = _section(path, raw, "exposure")
    api_section = _section(path, raw, "apis")
    return Policy(
        allow=_names(path, dependencies.get("allow")),
        deny=_names(path, dependencies.get("deny")),
        registry=_registry(path, dependencies.get("registry")),
        public_repositories=_rule(path, exposure, "public_repositories"),
        production_deploys=_rule(path, exposure, "production_deploys"),
        hosts=_names(path, api_section.get("hosts")),
        source=path,
    )


def _section(path: Path, raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise PolicyError(f"{path}: `{key}` must be a mapping")
    return value


def _names(path: Path, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{path}: dependency lists must be lists of names")
    return tuple(str(v) for v in value)


def _registry(path: Path, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not urlparse(value).netloc:
        raise PolicyError(f"{path}: `registry` must be a URL, got {value!r}")
    return value


def _rule(path: Path, exposure: dict[str, Any], key: str) -> str:
    value = exposure.get(key, DEFAULT_RULE)
    if value not in RULES:
        raise PolicyError(
            f"{path}: `{key}` must be one of {', '.join(RULES)}, got {value!r}"
        )
    return str(value)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


__all__ = ["DEFAULT", "Policy", "PolicyError", "Refusal", "load"]
