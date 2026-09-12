# Applace v1 — Technical Specification

> This document doubles as the future public `ARCHITECTURE.md`. It is written to be
> handed to a coding agent milestone by milestone — **do not implement ahead of the
> current milestone.**
>
> Applace is the sibling of [Runlace](https://github.com/cycle-inc/Runlace). Runlace
> compiles a *workflow* and replays it with no model in the loop. Applace compiles an
> *app* and serves it. The two share their shape on purpose: a CLI, an MCP server, a
> SKILL.md, a SQLite journal, decisions locked before code.
>
> **Status: M1, M2 and M3 are shipped.** M4 is next and is not yet built.

## What v1 is

A local-first harness that lets **any LLM agent** (Claude Code, Cursor, an in-house
chat) build, preview, look at, and ship **web applications** — the substrate a
company builds its own Lovable on, instead of buying one.

Three deliverables:

1. A **CLI** (`applace init`, `applace new`, `applace dev`, `applace serve`),
   installable with `uvx applace` / `pipx`.
2. An **MCP server** exposing app tools (`create_app`, `write_files`, `preview_app`,
   `screenshot_app`, `deploy_app`, …) to any MCP host.
3. A **SKILL.md** that teaches the agent how to build for this runtime.

The agent never touches the filesystem, the network, npm or a cloud provider
directly. It asks the harness, and the harness answers with something that has
already been type-checked, built, rendered and committed.

## What makes it not just a code generator

Three things, and they are the whole product:

- **Every app is a real repository, on real GitHub, from birth.** Not an export at
  the end. The company keeps its code where it keeps its code.
- **The agent has eyes.** A build that goes green and a page that renders are two
  different facts. Applace can report the second one.
- **A stack is company property.** The design system, the auth wiring and the
  internal API client live in a stack, versioned in git, and every app starts from
  one. That is the difference between "a Lovable" and "*our* Lovable".

## Locked decisions (do not revisit while implementing)

| # | Decision |
|---|---|
| **D1** | **The artefact is an ordinary repository.** An app lives at `~/.applace/apps/<slug>/` and is a plain git repo with a plain `package.json`. A human can `cd` into it, run `npm run dev`, open their IDE, and never learn that Applace exists. Applace adds no runtime, no import, no config key to the app itself. Zero lock-in is a feature, not a side effect. |
| **D2** | **Versioning is git, and the remote is GitHub.** No content-hash version table of our own: a commit is a version. When a GitHub connection is configured, `create_app` creates the repository in the configured organisation, sets the remote, and pushes; every green snapshot afterwards is pushed too. An app can also be adopted onto an existing repository. |
| **D2b** | **The GitHub connection is organisation-level configuration, not per app.** `applace github connect --org <org> --visibility private [--prefix <p>] [--team <t>]` is answered once; every app follows it. Changing it never rewrites the remotes of apps that already exist. |
| **D3** | **`write_files` is a compiler**, in this order: (1) **path lint** — every target must resolve inside the app, and `.git/`, `node_modules/` and the lockfile are not writable by an agent; (2) **install** if and only if the manifest changed; (3) **typecheck**; (4) **lint**; (5) **production build**. Each stage's real tool output is parsed into `{ file, line, column, message }` and returned verbatim in `message`. Robustness is enforced at write time, not discovered when a human opens the page. |
| **D4** | **A red write is accepted but not snapshotted.** The working tree takes the code — iterating is impossible otherwise — but only a green gate produces a commit. `get_app` always says `dirty: true` and carries the outstanding errors, so an agent can never mistake "I wrote it" for "it works". This is where Applace deliberately differs from Runlace, where a workflow that does not compile does not exist. |
| **D5** | **The agent has eyes.** `screenshot_app` renders a route in a headless browser and returns the image **plus the console messages and the failed network requests**. An app that builds green and paints a white screen is a defect Applace must be able to name without a human looking. |
| **D6** | **The gate is on exposure, not on writing.** Pushing to a private repository inside the organisation authorised at `connect` time asks nothing further — that authorisation was given once, durably, by a human. Making a repository public, and deploying to **production**, require `confirm=True` and are refused otherwise with the list of what is about to become visible. Local previews and preview deployments are never gated. |
| **D7** | **A stack is a first-class object**: a template tree, an install/dev/typecheck/lint/build command set, the deploy adapters it is compatible with, and the environment-variable prefix its bundler exposes. Built-in stacks ship inside the package; company stacks are added from a git URL and pinned to a commit. |
| **D8** | **Secrets never enter the model's context.** The agent declares and reads *names* of environment variables; values are supplied by a human through the CLI, stored outside the repository, injected into the dev server and synchronised to the deploy target. No tool ever returns a value, and `.env` is in the app's `.gitignore` from the first commit. |
| **D9** | **Dependencies are policy.** Every dependency an agent adds is reported by the gate as `new_deps` and checked against `policy.yaml` (allowlist, denylist, permitted registry). The default policy allows anything from the public registry and reports it; a company tightens it. |
| **D10** | **Stack: Python ≥ 3.11**, official `mcp` SDK, `pydantic` v2, `typer`, stdlib `sqlite3` and `subprocess`, `uv` for packaging. No agent framework, no ORM, no Node in the harness itself — Applace *drives* Node, it is not written in it, so `uvx applace` is the only install step. |
| **D11** | **Local-first, single-user.** No accounts, no multi-tenancy, no hosted control plane. The unit of trust is the machine, exactly as in Runlace. |
| **D12** | **Vercel deploys through GitHub, not through an upload.** The Vercel project is linked to the app's repository: a branch gives a preview URL, `main` gives production. `deploy_app` establishes the link, triggers the build and waits for its outcome; it never bypasses the repository. A direct-upload fallback exists only for apps with no GitHub connection. |
| **D13** | **The remote wins.** If the GitHub repository has moved ahead of the local tree — a human pushed, another machine pushed — the snapshot is refused and the divergence is reported. Applace never force-pushes and never rewrites history. |
| **D14** | **v1 apps are front ends.** A generated app is a browser application that talks to APIs that already exist; the stack supplies the client and the base URL. No database, no server runtime, no BaaS. A full-stack stack is a v2 question and is not to be anticipated in v1 code. |

## Directory layout (user machine)

```
~/.applace/
  applace.db               # SQLite: apps, snapshots, gates, previews, deployments
  config.json              # GitHub connection, deploy targets
  policy.yaml              # dependency + exposure policy (optional)
  stacks/<name>/           # stacks installed from a git URL (D7)
  apps/<slug>/             # the app itself: an ordinary git repository (D1)
  env/<slug>.env           # secret values, outside the repository (D8)
  logs/<slug>/             # dev server and build logs
```

## The stack contract

A stack is a directory containing `stack.yaml` and a `template/` tree:

```yaml
name: vite-react-ts
title: React + TypeScript + Tailwind
description: A single-page app that talks to existing HTTP APIs.
runtime: node
commands:
  install: npm install
  dev: npm run dev -- --port {port} --strict-port
  typecheck: npm run typecheck
  lint: npm run lint
  build: npm run build
dist: dist
env_prefix: VITE_        # what the bundler is allowed to expose to the browser (D8)
entry: src/App.tsx       # where the agent is told to start reading
deploy: [local, vercel]  # adapters this stack is compatible with (D7)
```

Files under `template/` are copied verbatim, except that `{{app_name}}`,
`{{app_slug}}` and `{{app_description}}` are substituted, and `_gitignore` is
renamed to `.gitignore` (a `.gitignore` inside a Python package is not shippable
as data otherwise).

Built-in stacks live in the package. `applace stacks add <git-url>` clones one into
`~/.applace/stacks/` and pins the commit it came from, which is recorded on every
app created from it.

## MCP tools

All tools return structured JSON, with descriptions written for an LLM to read.

| Tool | Input | Behaviour |
|---|---|---|
| `get_skill` | — | SKILL.md, the stack index, and the conventions of this runtime. First call an agent should make. |
| `list_stacks` | — | Available stacks with their title, description, entry point and deploy targets. |
| `create_app` | `{ name, stack, description? }` | Render the template, `git init`, install, first gate, first commit, create + push the GitHub repository if one is configured. |
| `list_apps` | — | slug, stack, current commit, dirty, preview URL if running, last deployment. |
| `get_app` | `{ app }` | The above plus the file tree, the outstanding gate errors, the declared environment variable names, and the GitHub URL. |
| `read_files` | `{ app, paths[] }` | Contents of named files. There is no "read the whole app" call. |
| `write_files` | `{ app, files: [{path, content}], delete?: [] }` | The D3 pipeline. Returns `{ ok, stage, errors[], new_deps[], commit? }`. |
| `preview_app` | `{ app }` | Start the dev server, allocate a port, return the URL. Idempotent. |
| `stop_preview` | `{ app }` | Stop it. |
| `screenshot_app` | `{ app, route?, viewport? }` | Render and return the image, the console messages and the failed requests (D5). |
| `get_logs` | `{ app, source?, lines? }` | Dev server, build, or deployment logs. |
| `set_env` | `{ app, name, description? }` | Declare a variable the app needs. The **value** is never an argument; the response tells the agent to ask the human to run the CLI (D8). |
| `deploy_app` | `{ app, target, environment, confirm? }` | Deploy a green commit through an adapter. Production requires `confirm` (D6). |
| `rollback_app` | `{ app, commit, confirm? }` | Redeploy an earlier green commit. |

## CLI

```
applace init                      # create the home, register the built-in stacks
applace serve [--http PORT]       # the MCP server, stdio by default
applace new <name> [--stack S]    # what create_app does, from a terminal
applace ls                        # the apps, their state, their URLs
applace stacks [add <git-url>]
applace dev <app>                 # preview in the foreground
applace open <app>                # the app in $EDITOR, the repo in a browser
applace env set <app> <NAME>      # prompts for the value, never echoes it (D8)
applace github connect --org O    # the D2b answer
applace deploy <app> [--prod]
applace logs <app>
```

## SQLite schema (v1)

```sql
apps(id, slug, name, description, stack, stack_commit, path,
     github_repo, github_url, default_branch, created_at)
snapshots(id, app_id, commit_sha, message, created_at)      -- one per green gate
gates(id, app_id, snapshot_id, stage, ok, duration_ms,      -- one per write_files
      errors_json, new_deps_json, created_at)
previews(id, app_id, port, pid, url, status, started_at, stopped_at)
deployments(id, app_id, commit_sha, target, environment, status,
            url, detail, confirmed, started_at, finished_at)
```

`gates` and `deployments` are simultaneously the audit log, the debug trace and the
answer to "why is this app in the state it is in". Never skip journaling to save
time.

## Out of scope for v1 (explicit, so the coding agent does not drift)

Multi-tenancy and user accounts; a web UI of Applace's own (an MCP chat host is the
UI, as with Runlace); mobile and React Native; a database or server runtime for
generated apps (D14); authentication *inside* generated apps beyond what a company
stack provides; billing; real-time collaboration; any deploy target other than the
two named in M7.

## Milestones (strictly in order; each ends with passing tests and an acceptance script)

### v1 — build it, see it, and have it land in GitHub

**M1 — The store.** *Shipped.* `~/.applace/`, SQLite, the stack registry, the
built-in `vite-react-ts` stack, `create_app`, `list_apps`, `get_app`,
`list_stacks`, git initialisation and the first commit, the CLI verbs `init`,
`new`, `ls`, `stacks`, `rm` and `serve` exposing those four read/create tools.
*Acceptance:* `scripts/m1_acceptance.sh` — create an app, then run it by hand
outside the harness: `npm run typecheck && npm run lint && npm run build` pass,
`npm run dev` serves it, and `git log` shows one commit holding the source and
the lockfile and not `node_modules`.

Three things M1 learned that later milestones inherit. **Vite must be told
`--host 127.0.0.1`**: left alone it binds `localhost`, which on a machine with
IPv6 is `::1`, and a supervisor that assumed loopback meant `127.0.0.1` would
report a healthy dev server as dead. **Stopping a dev server means killing a
process group**, not a pid: `npm run dev` is a shell that spawns node, and
killing the parent leaves the port held — M3's supervisor must start each
preview in its own group. And **a failed `install` does not destroy the app**:
the source is valid and committed, and deleting a developer's work because npm
could not reach the registry would be the wrong call.

**M2 — The compiler.** *Shipped.* `read_files`, `write_files`, the D3 pipeline,
real tool output parsed to `{file, line, column, message}`, green/dirty per D4,
`new_deps` reported, a commit on every green gate, and `applace check` so a human
runs the identical pipeline.
*Acceptance:* `scripts/m2_acceptance.sh` — write a type error, get the compiler's
own message and line; fix it, get a commit.

What M2 learned. **Parsers must be written against captured output, never
against documentation**: vite 8 is rolldown, whose errors are `[CODE] message`
over a box-drawn frame and nothing like the esbuild format the docs describe,
and it colours its output after being told not to. **The install stage has to be
conditional** — `npm install` on an unchanged manifest is tens of seconds
between the agent and every edit — so the manifest's dependency sections are
read before and after the write, which is also where `new_deps` comes from.
And **the path lint runs on the whole batch before anything is written**, so a
refusal leaves the app byte-for-byte as it was.

**M3 — The preview.** *Shipped.* The dev-server supervisor: port allocation,
start, stop, idempotence, log capture, and recovery of a preview whose supervisor
was restarted, plus the CLI verbs `dev` and `stop`.
*Acceptance:* `scripts/m3_acceptance.sh` — the URL returns the app's HTML; a new
server object over the same home finds the preview still running; stopping gives
the port back.

What M3 learned. **A database row about a process is a claim, not a fact**: the
process may be gone and its pid may have been reused, so a claim is checked
against the live process's own command line before it is believed, and reaped
when it does not hold up — that check is the whole of "recovery". **A zombie is
not a running server**: it still has a pid and `ps` still prints it, so the
liveness check reads the process state and treats `Z` as gone, and every kill
path collects the child afterwards. And **"is this port free" must be asked the
way node asks it**, with `SO_REUSEADDR`: without it every port Applace just
stopped a preview on reads as busy for the two minutes its last connection
spends in `TIME_WAIT`.

**M4 — The eyes.** `screenshot_app`, browser console capture, failed-request
capture, route and viewport selection.
*Acceptance:* an app that builds green but throws at runtime is diagnosed from the
console output alone, with no human looking at the screen.

**M5 — GitHub.** `applace github connect`, repository creation, remote wiring,
automatic push of green snapshots, adoption of an existing repository, and the D13
divergence refusal.
*Acceptance:* an app created in a chat exists on GitHub a moment later, with its
history; a push made behind Applace's back makes the next snapshot refuse.

**M6 — The skill and the policy.** SKILL.md, `get_skill`, `policy.yaml` (D9 and the
D6 exposure rules), `set_env` and the secret path (D8), and a pass with a small
local model as Runlace did.
*Acceptance:* a mid-sized local model builds a correct page on its first attempt.

### v2 — host it and industrialise it

**M7 — Deployment.** The adapter interface, the `local` target (build + serve the
`dist`), the **Vercel** target through GitHub (D12), preview versus production, the
D6 confirm gate, `rollback_app`, and environment synchronisation.

**M8 — Company stacks.** `applace stacks add <git-url>`, a stack carrying a design
system and an internal API client, commit pinning, and what happens to apps when
their stack moves.

**M9 — Handover.** Opening a pull request instead of pushing to `main`, detecting a
human's own edits in the working tree, `applace open`, `uvx applace` packaging and
the public README.
