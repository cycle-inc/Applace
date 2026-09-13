"""Stacks: the registry, the manifest it refuses, and what rendering produces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applace.paths import ApplacePaths
from applace.stacks import (
    BUILTIN,
    DEFAULT_STACK,
    StackError,
    load,
    registry,
    resolve,
)


def test_the_builtin_stack_is_always_available(paths: ApplacePaths) -> None:
    available = registry(paths.stacks)
    assert DEFAULT_STACK in available
    stack = available[DEFAULT_STACK]
    assert stack.source == BUILTIN
    assert stack.entry == "src/App.tsx"
    assert stack.env_prefix == "VITE_"
    assert "vercel" in stack.deploy


def test_an_installed_stack_shows_up_next_to_the_builtin_ones(
    paths: ApplacePaths, fake_stack: Path
) -> None:
    available = registry(paths.stacks)
    assert {"fake", DEFAULT_STACK} <= set(available)


def test_an_installed_stack_wins_a_name_collision(paths: ApplacePaths) -> None:
    """A company that ships its own `vite-react-ts` has decided what it means."""
    root = paths.stacks / DEFAULT_STACK
    (root / "template").mkdir(parents=True)
    (root / "stack.yaml").write_text(
        f"name: {DEFAULT_STACK}\ntitle: Ours\n"
        "commands:\n  install: 'true'\n  dev: 'true'\n  build: 'true'\n",
        encoding="utf-8",
    )
    assert registry(paths.stacks)[DEFAULT_STACK].title == "Ours"


def test_resolve_defaults_and_reports_what_exists(paths: ApplacePaths) -> None:
    assert resolve(paths.stacks, None).name == DEFAULT_STACK
    with pytest.raises(StackError) as exc:
        resolve(paths.stacks, "nope")
    assert DEFAULT_STACK in str(exc.value)


def test_a_stack_says_whether_its_pages_can_be_opened_at_all(tmp_path: Path) -> None:
    """A stack whose every route sits behind a login opts out, once (D25)."""
    (tmp_path / "stack.yaml").write_text(
        "name: intranet\ncommands:\n  install: 'true'\n  dev: 'true'\n  build: 'true'\n"
        "manifest: package.json\nvisit: false\n",
        encoding="utf-8",
    )
    stack = load(tmp_path)
    assert stack.visit is False
    assert stack.summary()["visit"] is False


def test_a_stack_that_says_nothing_about_it_gets_visited(paths: ApplacePaths) -> None:
    assert registry(paths.stacks)[DEFAULT_STACK].visit is True


def test_a_stack_missing_a_required_command_is_refused(tmp_path: Path) -> None:
    (tmp_path / "stack.yaml").write_text(
        "name: half\ncommands:\n  install: 'true'\n", encoding="utf-8"
    )
    with pytest.raises(StackError) as exc:
        load(tmp_path)
    assert "dev" in str(exc.value) and "build" in str(exc.value)


def test_a_stack_naming_a_command_applace_never_runs_is_refused(tmp_path: Path) -> None:
    """Silently ignoring `deploy: rm -rf /` would be the wrong kind of tolerant."""
    (tmp_path / "stack.yaml").write_text(
        "name: odd\ncommands:\n"
        "  install: 'true'\n  dev: 'true'\n  build: 'true'\n  publish: 'true'\n",
        encoding="utf-8",
    )
    with pytest.raises(StackError) as exc:
        load(tmp_path)
    assert "publish" in str(exc.value)


def test_a_stack_with_no_manifest_at_all_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StackError):
        load(tmp_path)


def test_rendering_substitutes_and_renames_the_gitignore(
    paths: ApplacePaths, fake_stack: Path, tmp_path: Path
) -> None:
    target = tmp_path / "rendered"
    written = registry(paths.stacks)["fake"].render(
        target,
        context={
            "app_slug": "my-app",
            "app_name": "My App",
            "app_description": "Does a thing.",
        },
    )

    assert (target / "manifest.json").read_text() == '{"name": "my-app"}\n'
    assert (target / "src" / "main.txt").read_text() == "My App: Does a thing.\n"
    assert (target / ".gitignore").read_text() == "out/\n"
    assert not (target / "_gitignore").exists()
    assert ".gitignore" in written


def test_the_builtin_template_has_no_placeholders_left_after_rendering(
    paths: ApplacePaths, tmp_path: Path
) -> None:
    """A missed `{{app_name}}` is a syntax error in whatever file holds it."""
    target = tmp_path / "rendered"
    registry(paths.stacks)[DEFAULT_STACK].render(
        target,
        context={"app_slug": "x", "app_name": "X", "app_description": "Y"},
    )
    for path in target.rglob("*"):
        if path.is_file():
            assert "{{app_" not in path.read_text(encoding="utf-8"), path


def test_the_builtin_stack_reports_untidiness_without_failing_on_it(
    paths: ApplacePaths, tmp_path: Path
) -> None:
    """M6's measurement, pinned.

    A leftover `import { useEffect }` made a mid-sized model's build red and it
    never recovered, while the page it had written rendered perfectly. The gate
    refuses code that does not work, not code that is untidy -- so unused
    locals are a lint warning here, and the rest of TypeScript stays strict.
    """
    target = tmp_path / "rendered"
    registry(paths.stacks)[DEFAULT_STACK].render(
        target,
        context={"app_slug": "x", "app_name": "X", "app_description": "Y"},
    )
    tsconfig = json.loads((target / "tsconfig.json").read_text())["compilerOptions"]
    assert tsconfig["strict"] is True
    assert "noUnusedLocals" not in tsconfig
    assert "noUnusedParameters" not in tsconfig

    eslint = (target / "eslint.config.js").read_text()
    assert "'@typescript-eslint/no-unused-vars': ['warn'" in eslint
