"""Dependencies are policy (D9), and the policy is the company's, not the agent's."""

from __future__ import annotations

from pathlib import Path

import pytest

from applace import policy
from applace.paths import ApplacePaths


def write_policy(paths: ApplacePaths, body: str) -> Path:
    paths.policy.write_text(body, encoding="utf-8")
    return paths.policy


def test_the_default_allows_anything_and_still_reports_it(paths: ApplacePaths) -> None:
    """A harness that refuses by default is a harness nobody switches on."""
    rules = policy.load(paths)
    assert rules.restricted is False
    assert rules.check([{"name": "zod", "version": "^3.0.0", "section": "dependencies"}]) == []
    assert "reported" in rules.summary()


def test_a_denylist_refuses_by_name_and_by_glob(paths: ApplacePaths) -> None:
    write_policy(paths, "dependencies:\n  deny: ['left-pad', '@acme/internal-*']\n")
    rules = policy.load(paths)

    refused = rules.check(
        [
            {"name": "left-pad", "version": "^1.0.0"},
            {"name": "@acme/internal-billing", "version": "^2.0.0"},
            {"name": "zod", "version": "^3.0.0"},
        ]
    )
    assert [r.name for r in refused] == ["left-pad", "@acme/internal-billing"]
    assert "denylist" in refused[0].reason


def test_an_allowlist_refuses_everything_else_and_says_what_is_allowed(
    paths: ApplacePaths,
) -> None:
    write_policy(paths, "dependencies:\n  allow: ['react', 'react-dom', '@acme/*']\n")
    rules = policy.load(paths)

    refused = rules.check(
        [{"name": "@acme/ui", "version": "^1.0.0"}, {"name": "moment", "version": "^2.0.0"}]
    )
    assert [r.name for r in refused] == ["moment"]
    assert "react, react-dom, @acme/*" in refused[0].reason


def test_a_pinned_registry_refuses_a_dependency_from_anywhere_else(
    paths: ApplacePaths,
) -> None:
    """The interesting case is not a version range; it is a git URL."""
    write_policy(paths, "dependencies:\n  registry: https://npm.acme.internal\n")
    rules = policy.load(paths)

    refused = rules.check(
        [
            {"name": "ok-lib", "version": "^1.2.3"},
            {"name": "sneaky", "version": "git+https://github.com/someone/sneaky.git"},
            {"name": "tarball", "version": "https://example.com/pkg.tgz"},
            {"name": "internal", "version": "https://npm.acme.internal/pkg/-/pkg-1.0.0.tgz"},
        ]
    )
    assert [r.name for r in refused] == ["sneaky", "tarball"]
    assert "npm.acme.internal" in refused[0].reason


def test_a_pinned_registry_also_reads_the_app_npmrc(
    paths: ApplacePaths, tmp_path: Path
) -> None:
    """Pinning the registry and letting a write drop an .npmrc is a rule with a hole."""
    write_policy(paths, "dependencies:\n  registry: https://npm.acme.internal\n")
    rules = policy.load(paths)
    root = tmp_path / "app"
    root.mkdir()

    assert rules.check_registry_config(root) is None
    (root / ".npmrc").write_text("registry=https://registry.npmjs.org\n", encoding="utf-8")
    refusal = rules.check_registry_config(root)
    assert refusal is not None
    assert refusal.where == ".npmrc"

    (root / ".npmrc").write_text("registry=https://npm.acme.internal\n", encoding="utf-8")
    assert rules.check_registry_config(root) is None


def test_exposure_rules_default_to_asking_a_human(paths: ApplacePaths) -> None:
    """D6: the gate is on exposure, and `confirm` is what that gate is."""
    assert policy.load(paths).public_repositories == "confirm"
    write_policy(paths, "exposure:\n  public_repositories: deny\n  production_deploys: deny\n")
    rules = policy.load(paths)
    assert rules.public_repositories == "deny"
    assert rules.rule_for("production_deploys") == "deny"


def test_a_policy_that_does_not_parse_is_loud(paths: ApplacePaths) -> None:
    """Silently allowing everything because of a typo is worse than no policy."""
    write_policy(paths, "dependencies: not-a-mapping\n")
    with pytest.raises(policy.PolicyError) as raised:
        policy.load(paths)
    assert "must be a mapping" in str(raised.value)

    write_policy(paths, "exposure:\n  public_repositories: maybe\n")
    with pytest.raises(policy.PolicyError) as raised:
        policy.load(paths)
    assert "must be one of" in str(raised.value)

    write_policy(paths, "dependencies:\n  registry: not-a-url\n")
    with pytest.raises(policy.PolicyError):
        policy.load(paths)


def test_the_browser_check_is_on_until_a_company_turns_it_off(
    paths: ApplacePaths,
) -> None:
    assert policy.DEFAULT.visit is True
    assert policy.DEFAULT.narrowed is False

    write_policy(paths, "gate:\n  visit: false\n")
    rules = policy.load(paths)
    assert rules.visit is False
    assert rules.narrowed is True
    assert "does not open the build in a browser" in rules.summary()
    assert rules.as_dict()["gate"] == {"visit": False}


def test_a_switch_that_is_not_true_or_false_is_loud(paths: ApplacePaths) -> None:
    write_policy(paths, "gate:\n  visit: sometimes\n")
    with pytest.raises(policy.PolicyError) as raised:
        policy.load(paths)
    assert "must be true or false" in str(raised.value)


def test_an_empty_policy_file_is_the_default_policy(paths: ApplacePaths) -> None:
    write_policy(paths, "# nothing yet\n")
    rules = policy.load(paths)
    assert rules.restricted is False
    assert rules.source == paths.policy
