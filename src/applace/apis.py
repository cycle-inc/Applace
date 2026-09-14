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

A declaration names *one* of two credentials, never both (D28). `token_env` is
the app's own: one value, the same for everybody who opens the app. That answers
"may this app call billing" and cannot answer "may *this* accountant see *this*
client's invoices". `on_behalf_of` is the other: no value of the app's at all,
an **exchange** endpoint the company hosts, and a short-lived token minted for
whoever is using the app. Both go out through the same two places above, from
the same stored declaration -- which is why the exchange is an HTTP endpoint and
not a Python hook: the generated function has to do it too, and it imports
nothing.

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

# Where the *caller's* proof of identity arrives, on a delegated API (D28). The
# same default as the credential going out, because in production the runtime in
# front of the app has already put the session's bearer there and asking a
# company to move it would be asking them to change their door for us.
DEFAULT_ASSERTION_HEADER = "Authorization"

# A header name, as HTTP defines a token. Checked because it is read back out of
# the declaration to look a header up and to strip it before forwarding.
HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")

# How long before an exchanged token expires we stop reusing it, in seconds. A
# token handed to an upstream one second before it dies is a 401 nobody can
# reproduce.
CLOCK_SKEW = 5.0

# And the longest we will believe an `expires_in`. Not distrust of the company's
# endpoint: a cache that holds a person's token for a week because a field was
# mistyped is a cache that outlives their access, and D28 says nothing is kept.
MAX_TOKEN_TTL = 3600.0

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
class Exchange:
    """Where a person's token comes from, for an API called on their behalf (D28).

    Three names and no value: the URL Applace POSTs to, the **name** of the
    variable holding the secret that authenticates Applace to it (D8), and the
    request header the caller's assertion arrives in.
    """

    url: str
    secret_env: str
    assertion_header: str = DEFAULT_ASSERTION_HEADER

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "secret_env": self.secret_env,
            "assertion_header": self.assertion_header,
        }

    def config(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "secretEnv": self.secret_env,
            "assertionHeader": self.assertion_header,
        }


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
    on_behalf_of: Exchange | None = None

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc

    @property
    def delegated(self) -> bool:
        """Is this API called as the person using the app, rather than as the app?"""
        return self.on_behalf_of is not None

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
            "delegated": self.delegated,
            "on_behalf_of": self.on_behalf_of.as_dict() if self.on_behalf_of else None,
        }

    def config(self) -> dict[str, Any]:
        """The same thing in the shape the gateway and the generated function read."""
        out: dict[str, Any] = {
            "base": self.base_url,
            "tokenEnv": self.token_env,
            "header": self.header,
            "scheme": self.scheme,
            "paths": list(self.paths),
            "methods": list(self.methods),
        }
        if self.on_behalf_of is not None:
            out["onBehalfOf"] = self.on_behalf_of.config()
        return out

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


def build_exchange(raw: dict[str, Any]) -> Exchange:
    """Validate an ``on_behalf_of`` block, or say exactly what is wrong (D28)."""
    unknown = sorted(set(raw) - {"url", "secret_env", "assertion_header"})
    if unknown:
        raise ApiError(
            f"on_behalf_of does not take {', '.join(unknown)}. It takes url, "
            f"secret_env and optionally assertion_header."
        )
    url = str(raw.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError(
            f"{url!r} is not a usable exchange URL. Give the scheme, the host "
            f"and the path your company answers on: "
            f"https://auth.internal/applace/token."
        )
    if parsed.query or parsed.fragment:
        raise ApiError(
            f"{url!r} carries a query string. Applace POSTs to the exchange; "
            f"what it sends is in the body, not in the URL."
        )
    secret_env = str(raw.get("secret_env") or "")
    if not secret_env:
        raise ApiError(
            "on_behalf_of needs secret_env: the *name* of the variable holding "
            "the credential that authenticates Applace to the exchange (D8). A "
            "human supplies the value with `applace env set`."
        )
    check_name(secret_env)
    header = str(raw.get("assertion_header") or DEFAULT_ASSERTION_HEADER)
    if not HEADER_NAME.match(header):
        raise ApiError(f"{header!r} is not a usable header name.")
    return Exchange(url=url, secret_env=secret_env, assertion_header=header)


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
    on_behalf_of: dict[str, Any] | None = None,
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
    if token_env and on_behalf_of:
        # D28. An upstream reached with either credential depending on what the
        # request happened to carry is an upstream that will one day be reached
        # with the wrong one, and the wrong one here sees every partition.
        raise ApiError(
            f"{name} cannot have both token_env and on_behalf_of. It is called "
            f"either as the app, with one credential for everybody, or as the "
            f"person using it, with a token the exchange mints for them."
        )
    exchange = build_exchange(on_behalf_of) if on_behalf_of else None

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
        on_behalf_of=exchange,
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
    delegated = raw.get("on_behalf_of") or None
    return Api(
        name=str(row["name"]),
        base_url=str(raw.get("base_url", "")),
        token_env=str(raw.get("token_env", "")),
        header=str(raw.get("header", DEFAULT_HEADER)),
        scheme=str(raw.get("scheme", DEFAULT_SCHEME)),
        paths=tuple(str(p) for p in raw.get("paths", ["**"])),
        methods=tuple(str(m) for m in raw.get("methods", ["GET"])),
        description=str(raw.get("description", "")),
        # Read back, not re-validated: it was validated when it was declared,
        # and a declaration that stopped loading because a rule tightened would
        # take a working app off the air without anybody touching it.
        on_behalf_of=(
            Exchange(
                url=str(delegated.get("url", "")),
                secret_env=str(delegated.get("secret_env", "")),
                assertion_header=str(
                    delegated.get("assertion_header") or DEFAULT_ASSERTION_HEADER
                ),
            )
            if delegated
            else None
        ),
    )


def egress(api: Api) -> list[tuple[str, str]]:
    """Every host reaching this API talks to, as (what it is called, host).

    Two, for a delegated API: the upstream and the exchange. One list rather
    than three copies of `if api.on_behalf_of`, because the policy is checked
    when the declaration is made, when the gate runs and when the call is
    forwarded, and a host that only two of those three knew about would be a
    host a company thought it had blocked.
    """
    out = [(api.name, api.host)]
    if api.on_behalf_of is not None:
        out.append((f"{api.name}'s exchange", api.on_behalf_of.host))
    return out


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
//
// An API declared with `onBehalfOf` is called as the person using the app: the
// assertion your own door put on the request is exchanged, here, for a token
// that is theirs and short-lived. No assertion is a 401 and never this app's
// own credential -- a request with nobody behind it has nobody to answer as.

const APP = {app};

const APIS = {config};

const HOP_BY_HOP = new Set({hops});

// Exchanged tokens, for as long as the exchange said they were good for. Module
// scope, so a warm function reuses them and a cold one starts empty; nothing is
// written anywhere, which is the rule (D28).
const TOKENS = new Map();
const SKEW_MS = {skew} * 1000;
const MAX_TTL_MS = {max_ttl} * 1000;

async function exchanged(name, api, assertion) {{
  const exchange = api.onBehalfOf;
  if (!assertion) {{
    return {{
      status: 401,
      error:
        `${{name}} is called on behalf of whoever is using this app, and this ` +
        `request carried no ${{exchange.assertionHeader}} header. There is ` +
        `nobody to call it as.`,
    }};
  }}
  const secret = process.env[exchange.secretEnv];
  if (!secret) {{
    return {{ status: 503, error: `${{exchange.secretEnv}} is not set on this deployment` }};
  }}

  const key = `${{name}}\\u0000${{assertion}}`;
  const now = Date.now();
  const hit = TOKENS.get(key);
  if (hit && hit.until > now) return {{ token: hit.token }};

  let answer;
  try {{
    answer = await fetch(exchange.url, {{
      method: 'POST',
      headers: {{ 'content-type': 'application/json', authorization: `Bearer ${{secret}}` }},
      body: JSON.stringify({{
        app: APP,
        api: name,
        assertion,
        asserted_by: 'runtime',
      }}),
    }});
  }} catch {{
    return {{ status: 502, error: `the exchange for ${{name}} did not answer` }};
  }}

  // Declared without a value and assigned in both branches: this file is linted
  // by the same eslint as everything else in the repository, and a generated
  // file that fails that lint is a file nobody can ship.
  let payload;
  try {{
    payload = await answer.json();
  }} catch {{
    payload = {{}};
  }}
  if (!answer.ok) {{
    // The status passes through; the body does not. An error body is written by
    // somebody else and may repeat what was sent to it.
    const said = typeof payload.error === 'string' ? `: ${{payload.error.slice(0, 200)}}` : '';
    return {{
      status: answer.status >= 400 && answer.status < 500 ? answer.status : 502,
      error: `the exchange would not issue a token for ${{name}}${{said}}`,
    }};
  }}
  if (typeof payload.token !== 'string' || !payload.token) {{
    return {{ status: 502, error: `the exchange for ${{name}} answered without a token` }};
  }}

  const ttl = Number(payload.expires_in) * 1000;
  if (Number.isFinite(ttl) && ttl > SKEW_MS) {{
    for (const [old, entry] of TOKENS) if (entry.until <= now) TOKENS.delete(old);
    TOKENS.set(key, {{ token: payload.token, until: now + Math.min(ttl, MAX_TTL_MS) - SKEW_MS }});
  }}
  return {{ token: payload.token }};
}}

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

  // The one place a credential is chosen, and it is never both (D28).
  let token = '';
  const strip = new Set(HOP_BY_HOP);
  if (api.onBehalfOf) {{
    strip.add(api.onBehalfOf.assertionHeader.toLowerCase());
    const carried = request.headers[api.onBehalfOf.assertionHeader.toLowerCase()];
    const assertion = (Array.isArray(carried) ? carried[0] : carried) || '';
    const got = await exchanged(name, api, assertion);
    if (got.error) {{
      response.status(got.status).json({{ error: got.error }});
      return;
    }}
    token = got.token;
  }} else if (api.tokenEnv) {{
    token = process.env[api.tokenEnv] || '';
    if (!token) {{
      response.status(503).json({{ error: `${{api.tokenEnv}} is not set on this deployment` }});
      return;
    }}
  }}

  const query = request.url.includes('?') ? request.url.slice(request.url.indexOf('?')) : '';
  const headers = {{}};
  for (const [key, value] of Object.entries(request.headers)) {{
    if (!strip.has(key.toLowerCase())) headers[key] = value;
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


def function_source(declared: list[Api], app: str = "") -> str:
    """The serverless function for these declarations, as a whole file.

    Whole file, never a patch: the only way this stays readable is if it is
    generated from one place and nothing ever merges into it.

    ``app`` is baked in rather than read from the environment because the
    exchange is told which app is asking, and an app that could name itself
    could name another one.
    """
    config = {api.name: api.config() for api in sorted(declared, key=lambda a: a.name)}
    return _FUNCTION.format(
        mount=GATEWAY_PATH,
        app=json.dumps(app),
        config=json.dumps(config, indent=2, sort_keys=True),
        hops=json.dumps(sorted(HOP_BY_HOP)),
        skew=int(CLOCK_SKEW),
        max_ttl=int(MAX_TOKEN_TTL),
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
    "DEFAULT_ASSERTION_HEADER",
    "Exchange",
    "GATEWAY_PATH",
    "HOP_BY_HOP",
    "METHODS",
    "Reach",
    "build",
    "build_exchange",
    "declarations",
    "declare",
    "egress",
    "find",
    "from_row",
    "forget",
    "host_lint",
    "normalise",
]
