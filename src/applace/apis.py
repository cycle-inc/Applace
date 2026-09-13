"""The APIs an app is allowed to call, and the credential it never holds (D24).

An internal app that cannot read the company's data is a mockup. Letting the
agent put a token in the front end would make it a leak. So the app declares
*which* API it needs -- a name, a base URL, the **name** of the variable holding
the credential -- and its own code only ever fetches a relative path under
:data:`GATEWAY_PATH`. Something else adds the credential, server-side:

* while previewing, the gateway process in :mod:`applace.gateway`, which the
  stack's dev server proxies to;
* once deployed, an ordinary serverless function generated from the same
  declaration and committed into the app's repository, which imports nothing
  from Applace and which a human can read, edit or delete (D1).

Two checks sit on top, and it matters which one is load-bearing. The **policy**
decides which hosts this machine will proxy at all (D9's reasoning, applied to
egress): that is a control, and it is enforced at the moment of the call. The
**host lint** below is a guide: it catches an agent typing an absolute URL into
a `fetch` and tells it to declare the API instead. It is best-effort by
construction -- a URL assembled at runtime is invisible to it -- and it does not
need to be more than that, because a direct call has no credential to send. The
lint saves a round trip; the gateway is what makes the rule true.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

from .db import Connection, declare_api, delete_api, list_apis
from .env import check_name

# The prefix an app fetches under. It lives below `/api/` because that is where
# Vercel looks for functions, so the generated one needs no routing file and no
# configuration key anywhere (D1).
GATEWAY_PATH = "/api/gateway"

# A name is part of a URL path and part of a directory name in the generated
# function, so it is the intersection of what both accept.
NAME = re.compile(r"^[a-z][a-z0-9-]*$")

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")

# Where the credential goes. A header, because a token in a query string ends up
# in every access log between here and the upstream.
DEFAULT_HEADER = "Authorization"
DEFAULT_SCHEME = "Bearer"

# Hops that belong to one connection and must not be forwarded to the next one.
HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "host",
        "content-length",
    }
)

# An absolute URL written as the first argument of a request call. Anything
# cleverer than this -- a base assembled from variables, a URL built in a
# template literal -- is out of reach of a regular expression, and is meant to
# be: see the module docstring on why the lint is a guide and not the control.
_CALL = re.compile(
    r"""\b(?:fetch|axios(?:\.\w+)?|ky(?:\.\w+)?|request|\.open)\s*\(\s*"""
    r"""["'`]\s*(https?://[^"'`\s)]+)""",
    re.IGNORECASE,
)

# URLs that are identifiers rather than destinations. An SVG carries the first
# one in every file it appears in, and refusing it would make the lint the most
# hated thing in the harness.
NOT_A_CALL = ("www.w3.org", "schema.org", "www.w3.org/2000/svg")

_SOURCE_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"})


class ApiError(Exception):
    """The declaration cannot be used, and is not stored half-checked."""


@dataclass(frozen=True)
class Api:
    """One upstream an app may reach, as declared and as stored."""

    name: str
    base_url: str
    token_env: str = ""
    header: str = DEFAULT_HEADER
    scheme: str = DEFAULT_SCHEME
    paths: tuple[str, ...] = ("**",)
    methods: tuple[str, ...] = ("GET",)
    description: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc

    @property
    def mount(self) -> str:
        """What the app's own code fetches under."""
        return f"{GATEWAY_PATH}/{self.name}"

    def as_dict(self) -> dict[str, Any]:
        """Safe to print, to log and to hand a model: names only, never a value."""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "token_env": self.token_env,
            "header": self.header,
            "scheme": self.scheme,
            "paths": list(self.paths),
            "methods": list(self.methods),
            "description": self.description,
            "fetch": f"{self.mount}/...",
        }

    def config(self) -> dict[str, Any]:
        """The same thing in the shape the gateway and the generated function read."""
        return {
            "base": self.base_url,
            "tokenEnv": self.token_env,
            "header": self.header,
            "scheme": self.scheme,
            "paths": list(self.paths),
            "methods": list(self.methods),
        }

    # -- the control -------------------------------------------------------

    def permits(self, method: str, sub_path: str) -> str:
        """Empty when this call is allowed, otherwise why it is not.

        The declaration is the allowlist. A method nobody declared is a refusal
        rather than a pass-through, because "read the customers" and "delete the
        customers" are the same URL to a proxy and very different to a company.
        """
        if method.upper() not in self.methods:
            return (
                f"{self.name} was declared for "
                f"{', '.join(self.methods)}, not {method.upper()}"
            )
        cleaned = normalise(sub_path)
        if cleaned is None:
            return f"{sub_path!r} climbs out of {self.name}"
        if not any(_matches(cleaned, pattern) for pattern in self.paths):
            return (
                f"{self.name} was declared for {', '.join(self.paths)}, "
                f"which does not cover {cleaned!r}"
            )
        return ""

    def upstream(self, sub_path: str, query: str = "") -> str:
        """The URL to actually call. Never built from anything but the declaration."""
        cleaned = normalise(sub_path)
        if cleaned is None:  # pragma: no cover - `permits` refuses first
            raise ApiError(f"{sub_path!r} climbs out of {self.name}")
        parts = urlparse(self.base_url)
        joined = "/".join(p for p in (parts.path.rstrip("/"), cleaned) if p)
        return urlunparse(
            (parts.scheme, parts.netloc, "/" + joined.lstrip("/"), "", query, "")
        )


def normalise(sub_path: str) -> str | None:
    """A relative path with no way out of it, or None if it tried.

    `..` is resolved here rather than trusted to the upstream, because the
    upstream is the thing being protected and a proxy that forwards `../..`
    is a proxy that turns one declared API into every route on that host.
    """
    out: list[str] = []
    for segment in sub_path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not out:
                return None
            out.pop()
            continue
        out.append(segment)
    return "/".join(out)


def _matches(path: str, pattern: str) -> bool:
    # `**` is the pattern people write for "anything below here"; fnmatch's `*`
    # already crosses `/`, so the two collapse -- but writing `**` and having it
    # silently mean something narrower would be the wrong surprise.
    #
    # `customers/**` also covers `customers` itself. Nobody who writes that
    # means "the list is off limits but every item is fine", and the alternative
    # is an agent reading a 403 for the most ordinary call there is.
    if pattern.endswith("/**") and path == pattern[:-3]:
        return True
    return fnmatch.fnmatch(path, pattern.replace("**", "*"))


# -- declaring -------------------------------------------------------------


def build(
    *,
    name: str,
    base_url: str,
    token_env: str | None = None,
    header: str | None = None,
    scheme: str | None = None,
    paths: list[str] | None = None,
    methods: list[str] | None = None,
    description: str | None = None,
) -> Api:
    """Validate a declaration into an :class:`Api`, or say exactly what is wrong."""
    if not NAME.match(name):
        raise ApiError(
            f"{name!r} is not a usable API name. Use lowercase letters, digits "
            f"and hyphens, starting with a letter: crm, billing, hr-directory."
        )
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError(
            f"{base_url!r} is not a usable base URL. Give the scheme and the "
            f"host: https://crm.internal/api/v2."
        )
    if parsed.query or parsed.fragment:
        raise ApiError(
            f"{base_url!r} carries a query string. A base URL is a scheme, a "
            f"host and a path; the rest is the call's business."
        )
    if token_env:
        check_name(token_env)

    wanted = [m.upper() for m in (methods or ["GET"])]
    unknown = sorted(set(wanted) - set(METHODS))
    if unknown:
        raise ApiError(
            f"unknown method(s) {', '.join(unknown)}; "
            f"declare some of {', '.join(METHODS)}"
        )
    for pattern in paths or []:
        if "://" in pattern or pattern.startswith("/"):
            raise ApiError(
                f"{pattern!r} is not a path pattern. Patterns are relative to "
                f"the base URL: customers/*, orders/**."
            )
    return Api(
        name=name,
        base_url=base_url.rstrip("/"),
        token_env=token_env or "",
        header=header or DEFAULT_HEADER,
        scheme=DEFAULT_SCHEME if scheme is None else scheme,
        paths=tuple(paths or ["**"]),
        methods=tuple(dict.fromkeys(wanted)),
        description=" ".join((description or "").split()),
    )


def declare(conn: Connection, *, app_id: str, api: Api) -> Api:
    """Store a declaration. Re-declaring the same name replaces it outright."""
    declare_api(conn, app_id=app_id, name=api.name, config=api.as_dict())
    conn.commit()
    return api


def forget(conn: Connection, *, app_id: str, name: str) -> bool:
    removed = delete_api(conn, app_id, name)
    conn.commit()
    return removed


def declarations(conn: Connection, app_id: str) -> list[Api]:
    """Every API this app declared, in the order a human would list them."""
    out: list[Api] = []
    for row in list_apis(conn, app_id):
        out.append(from_row(row))
    return out


def from_row(row: Any) -> Api:
    raw = json.loads(str(row["config_json"]))
    return Api(
        name=str(row["name"]),
        base_url=str(raw.get("base_url", "")),
        token_env=str(raw.get("token_env", "")),
        header=str(raw.get("header", DEFAULT_HEADER)),
        scheme=str(raw.get("scheme", DEFAULT_SCHEME)),
        paths=tuple(str(p) for p in raw.get("paths", ["**"])),
        methods=tuple(str(m) for m in raw.get("methods", ["GET"])),
        description=str(raw.get("description", "")),
    )


def find(apis: list[Api], name: str) -> Api | None:
    for api in apis:
        if api.name == name:
            return api
    return None


# -- the generated function ------------------------------------------------
#
# What ships with the app. It is generated rather than imported, and generated
# as plain JavaScript rather than as a dependency, because D1 says an app must
# not be able to tell that Applace exists: a company that deletes Applace
# tomorrow still has a repository that builds, deploys and proxies. The file is
# rewritten from the declarations on every green gate, which is why it says so
# at the top -- an edit here is lost, and an edit to the declaration is not.

FUNCTION_PATH = "api/gateway/[name]/[...path].js"

_FUNCTION = """\
// Generated by Applace from this app's declared APIs. Do not edit: this file is
// rewritten whenever the declarations change. It imports nothing and depends on
// nothing -- it is an ordinary serverless function, and deleting it removes the
// proxy and nothing else.
//
// It exists so the browser never holds a credential. The app fetches
// {mount}/<api>/<path>; this adds the token, server-side, from the
// environment variable named in the declaration.

const APIS = {config};

const HOP_BY_HOP = new Set({hops});

function clean(segments) {{
  const out = [];
  for (const segment of segments) {{
    if (!segment || segment === '.') continue;
    if (segment === '..') {{
      if (out.length === 0) return null;
      out.pop();
      continue;
    }}
    out.push(segment);
  }}
  return out.join('/');
}}

function allows(patterns, path) {{
  return patterns.some((pattern) => {{
    // `customers/**` covers `customers` itself, as it does in the declaration.
    if (pattern.endsWith('/**') && path === pattern.slice(0, -3)) return true;
    const expression = pattern
      .replace(/[.+^${{}}()|[\\]\\\\]/g, '\\\\$&')
      .replace(/\\*\\*/g, '*')
      .replace(/\\*/g, '.*');
    return new RegExp(`^${{expression}}$`).test(path);
  }});
}}

export default async function handler(request, response) {{
  const {{ name, path }} = request.query;
  const api = APIS[name];
  if (!api) {{
    response.status(404).json({{ error: `no API called ${{name}}` }});
    return;
  }}
  if (!api.methods.includes(request.method)) {{
    response.status(405).json({{ error: `${{name}} does not allow ${{request.method}}` }});
    return;
  }}
  const rest = clean(Array.isArray(path) ? path : [path].filter(Boolean));
  if (rest === null || !allows(api.paths, rest)) {{
    response.status(403).json({{ error: `${{name}} was not declared for ${{rest}}` }});
    return;
  }}

  const token = api.tokenEnv ? process.env[api.tokenEnv] : '';
  if (api.tokenEnv && !token) {{
    response.status(503).json({{ error: `${{api.tokenEnv}} is not set on this deployment` }});
    return;
  }}

  const query = request.url.includes('?') ? request.url.slice(request.url.indexOf('?')) : '';
  const headers = {{}};
  for (const [key, value] of Object.entries(request.headers)) {{
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers[key] = value;
  }}
  if (token) headers[api.header] = api.scheme ? `${{api.scheme}} ${{token}}` : token;

  const body =
    request.method === 'GET' || request.method === 'HEAD'
      ? undefined
      : typeof request.body === 'string'
        ? request.body
        : JSON.stringify(request.body ?? {{}});

  let upstream;
  try {{
    upstream = await fetch(`${{api.base}}/${{rest}}${{query}}`, {{
      method: request.method,
      headers,
      body,
    }});
  }} catch (error) {{
    response.status(502).json({{ error: `${{name}} did not answer: ${{error}}` }});
    return;
  }}

  const text = await upstream.text();
  const type = upstream.headers.get('content-type');
  if (type) response.setHeader('content-type', type);
  response.status(upstream.status).send(text);
}}
"""


def function_source(declared: list[Api]) -> str:
    """The serverless function for these declarations, as a whole file.

    Whole file, never a patch: the only way this stays readable is if it is
    generated from one place and nothing ever merges into it.
    """
    config = {api.name: api.config() for api in sorted(declared, key=lambda a: a.name)}
    return _FUNCTION.format(
        mount=GATEWAY_PATH,
        config=json.dumps(config, indent=2, sort_keys=True),
        hops=json.dumps(sorted(HOP_BY_HOP)),
    )


# -- the guide -------------------------------------------------------------


@dataclass(frozen=True)
class Reach:
    """One place a written file tries to call the internet directly."""

    file: str
    line: int
    url: str
    host: str
    hint: str = field(default="")

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "url": self.url, "host": self.host}


def host_lint(written: dict[str, str], declared: list[Api]) -> list[Reach]:
    """Absolute URLs an agent wrote into a request call, minus the declared ones.

    Best-effort and stated as such (see the module docstring). It exists so an
    agent that reaches for `fetch("https://...")` is told about `declare_api`
    on the same write, instead of discovering at runtime that its call had no
    credential and reading a 401 it cannot explain.
    """
    allowed = {api.host for api in declared}
    found: list[Reach] = []
    for path, content in sorted(written.items()):
        if not any(path.endswith(suffix) for suffix in _SOURCE_SUFFIXES):
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            for url in _CALL.findall(line):
                host = urlparse(url).netloc
                if not host or host in allowed or _harmless(host):
                    continue
                found.append(
                    Reach(
                        file=path,
                        line=number,
                        url=url,
                        host=host,
                        hint=(
                            f"declare {host} with declare_api, then fetch "
                            f"{GATEWAY_PATH}/<name>/... instead"
                        ),
                    )
                )
    return found


def _harmless(host: str) -> bool:
    if host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"):
        return True
    return any(host == known or host.endswith("." + known) for known in NOT_A_CALL)


__all__ = [
    "Api",
    "ApiError",
    "GATEWAY_PATH",
    "HOP_BY_HOP",
    "METHODS",
    "Reach",
    "build",
    "declarations",
    "declare",
    "find",
    "from_row",
    "forget",
    "host_lint",
    "normalise",
]
