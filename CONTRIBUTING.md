# Contributing

Applace is built milestone by milestone from [SPEC.md](SPEC.md). Read that first:
it holds the locked decisions and the roadmap, and it is the reason the codebase
looks the way it does.

## The rules that matter

- **Do not implement ahead of the current milestone.** The spec says which one is
  in progress. A pull request that adds deployment while M2 is unfinished will be
  asked to wait, however good it is.
- **Do not revisit a locked decision in code.** If D4 is wrong, change D4 in the
  spec first, with the reasoning, and say what the old decision got wrong. Every
  superseded decision stays in the document — the audit trail is the point.
- **Comments say why, not what.** The code already says what. A comment earns its
  place by recording a decision, a trade-off, or the obvious approach that turned
  out to be wrong.
- **Every milestone ends green**: `pytest`, `pyright`, and its acceptance script.

## Setup

```bash
uv sync
uv run pytest -m 'not needs_npm'   # fast, no network
uv run pytest                      # everything, including a real npm install
uv run pyright
./scripts/m1_acceptance.sh         # the current milestone, end to end
```

Tests marked `needs_npm` install packages from the public registry and take tens
of seconds. They run in their own CI job for that reason.

## Tests

Test names are sentences about behaviour, not about functions:
`test_a_failed_install_keeps_the_app_and_says_so`. A test whose name does not say
what a user would notice is usually testing an implementation detail.

Never let a test touch a real `~/.applace` — the `paths` fixture points
`APPLACE_HOME` at a temporary directory, and it also pins git's configuration so
a contributor's own git identity cannot change the result.
