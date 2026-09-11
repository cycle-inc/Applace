"""Slugs. An app's slug has to survive a filesystem, a git host and a URL."""

from __future__ import annotations

import pytest

from applace.naming import InvalidName, display_name, slugify


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Team Dashboard", "team-dashboard"),
        ("  Team   Dashboard  ", "team-dashboard"),
        ("My Team's Dashboard!", "my-teams-dashboard"),
        ("sales_report.v2", "sales-report-v2"),
        ("Ambitious --- Name", "ambitious-name"),
        ("ALREADY-A-SLUG", "already-a-slug"),
        ("2024 review", "app-2024-review"),
        ("café", "caf"),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "...", "の"])
def test_a_name_with_nothing_usable_in_it_is_refused(raw: str) -> None:
    with pytest.raises(InvalidName):
        slugify(raw)


def test_reserved_names_are_refused() -> None:
    with pytest.raises(InvalidName):
        slugify("node_modules")
    with pytest.raises(InvalidName):
        slugify(".git")


def test_a_long_name_is_cut_without_a_trailing_dash() -> None:
    slug = slugify("a" * 70 + " b")
    assert len(slug) <= 64
    assert not slug.endswith("-")


def test_display_name_keeps_the_words_and_drops_the_whitespace() -> None:
    assert display_name("  My   App ") == "My App"
