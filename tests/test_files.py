"""The path lint, and what counts as an app's source."""

from __future__ import annotations

from pathlib import Path

import pytest

from applace.files import (
    PathRefused,
    apply_writes,
    diff_dependencies,
    lint_paths,
    read_dependencies,
    read_files,
    resolve_in_app,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    (app / "src" / "App.tsx").write_text("export const x = 1\n", encoding="utf-8")
    (app / "package.json").write_text(
        '{"dependencies": {"react": "^19.0.0"}, "devDependencies": {"vite": "^8.0.0"}}',
        encoding="utf-8",
    )
    return app


def test_a_path_inside_the_app_resolves(root: Path) -> None:
    assert resolve_in_app(root, "src/App.tsx") == root / "src" / "App.tsx"


def test_a_file_that_does_not_exist_yet_is_allowed(root: Path) -> None:
    assert resolve_in_app(root, "src/pages/Home.tsx").name == "Home.tsx"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.txt",
        "src/../../outside.txt",
        ".git/config",
        ".git/hooks/pre-commit",
        "node_modules/react/index.js",
        "package-lock.json",
        "pnpm-lock.yaml",
        "",
    ],
)
def test_paths_that_are_not_the_apps_source_are_refused(root: Path, path: str) -> None:
    with pytest.raises(PathRefused):
        resolve_in_app(root, path)


def test_a_refusal_says_what_to_do_instead(root: Path) -> None:
    with pytest.raises(PathRefused, match="package.json"):
        resolve_in_app(root, "package-lock.json")


def test_a_symlink_out_of_the_app_is_refused(root: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (root / "escape").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PathRefused, match="symlink"):
        resolve_in_app(root, "escape/secrets.txt")


def test_one_bad_path_in_a_batch_refuses_the_batch(root: Path) -> None:
    with pytest.raises(PathRefused):
        lint_paths(root, ["src/App.tsx", ".git/config"])


def test_reading_returns_the_files_that_worked_and_names_the_one_that_did_not(
    root: Path,
) -> None:
    result = read_files(root, ["src/App.tsx", "src/Nope.tsx"])
    assert result[0]["content"] == "export const x = 1\n"
    assert "no such file" in result[1]["error"]
    assert "content" not in result[1]


def test_reading_a_directory_says_so_rather_than_crashing(root: Path) -> None:
    assert "directory" in read_files(root, ["src"])[0]["error"]


def test_writing_creates_missing_directories(root: Path) -> None:
    result = apply_writes(root, {"src/pages/Home.tsx": "export default 1\n"})
    assert result.written == ["src/pages/Home.tsx"]
    assert (root / "src" / "pages" / "Home.tsx").exists()


def test_windows_line_endings_do_not_reach_the_repository(root: Path) -> None:
    apply_writes(root, {"src/App.tsx": "a\r\nb\r\n"})
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == "a\nb\n"


def test_deleting_a_file_that_is_already_gone_is_not_an_error(root: Path) -> None:
    result = apply_writes(root, {}, ["src/Nope.tsx"])
    assert result.deleted == []


def test_deleting_a_directory_is_refused(root: Path) -> None:
    with pytest.raises(PathRefused, match="directory"):
        apply_writes(root, {}, ["src"])


def test_dependencies_are_read_from_every_section(root: Path) -> None:
    deps = read_dependencies(root, "package.json")
    assert deps[("dependencies", "react")] == "^19.0.0"
    assert deps[("devDependencies", "vite")] == "^8.0.0"


def test_a_broken_manifest_reads_as_empty_rather_than_raising(root: Path) -> None:
    (root / "package.json").write_text("{ this is not json", encoding="utf-8")
    assert read_dependencies(root, "package.json") == {}


def test_an_added_dependency_is_reported(root: Path) -> None:
    before = read_dependencies(root, "package.json")
    (root / "package.json").write_text(
        '{"dependencies": {"react": "^19.0.0", "zod": "^4.0.0"}}', encoding="utf-8"
    )
    after = read_dependencies(root, "package.json")
    added = diff_dependencies(before, after)
    assert {"name": "zod", "version": "^4.0.0", "section": "dependencies"} in added


def test_a_bumped_dependency_says_what_it_was(root: Path) -> None:
    before = {("dependencies", "react"): "^18.0.0"}
    after = {("dependencies", "react"): "^19.0.0"}
    assert diff_dependencies(before, after) == [
        {"name": "react", "version": "^19.0.0", "section": "dependencies", "was": "^18.0.0"}
    ]


def test_removing_a_dependency_is_nobodys_business_to_approve() -> None:
    before = {("dependencies", "left-pad"): "^1.0.0"}
    assert diff_dependencies(before, {}) == []
