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
> **Status: M1 to M13 are shipped** — the whole harness a developer drives, the
> panel that puts it inside a chat window, which is where a company's users
> actually are, and the gateway that lets an app read the company's own APIs
> without a credential ever reaching the browser.

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
| **D11** | **Local-first, single-user.** No accounts, no multi-tenancy, no hosted control plane. The unit of trust is the machine, exactly as in Runlace. Extended, not repealed, by D20: a machine can hold many homes, and each one is still a whole single-user Applace. |
| **D12** | **Vercel deploys through GitHub, not through an upload.** The Vercel project is linked to the app's repository: a branch gives a preview URL, `main` gives production. `deploy_app` establishes the link, triggers the build and waits for its outcome; it never bypasses the repository. A direct-upload fallback exists only for apps with no GitHub connection. |
| **D13** | **The remote wins.** If the GitHub repository has moved ahead of the local tree — a human pushed, another machine pushed — the snapshot is refused and the divergence is reported. Applace never force-pushes and never rewrites history. |
| **D14** | **v1 apps are front ends.** A generated app is a browser application that talks to APIs that already exist; the stack supplies the client and the base URL. No database, no server runtime, no BaaS. A full-stack stack is a v2 question and is not to be anticipated in v1 code. |
| **D15** | **A handover is a branch and a pull request, not a different repository.** A machine can be connected in review mode: every app's commits go to `applace/<slug>` and one pull request stays open against the default branch, so nothing an agent wrote reaches `main` without a person merging it. The agent is given the pull request URL and told to hand it over; it never merges, and there is no Applace command that does. The mirror of it holds too: a person's uncommitted edit in the working tree refuses the agent's write, because `write_files` replaces whole files and theirs exists nowhere else. |
| **D16** | **The chat window is a first-class client, and it reads the same journal as the agent.** A company already has a chatbot; Applace is what goes under it. So the same server that answers the MCP tools also answers, on the same port, a read-only view of what is happening — one card per app, one event stream, the last screenshot — and ships an embeddable component that needs no build step in the host. A chat UI never re-implements the journal, never polls git, and never needs a second port, a second process or a second set of credentials. |
| **D17** | **The package is the fourth door.** Applace ships three ways in: a CLI for a human at a terminal, an MCP server for an agent, a `SKILL.md` for the model that reads it. The developer putting Applace under their company's chatbot has none of the three — they have a Python process. `from applace import Applace` is the same store, the same gate and the same journal, called directly, and the MCP server becomes one caller of it rather than the only door. Every method returns a plain dict with `ok`, `code`, `error` and `hint`, and none raises because a build went red: that is the convention everywhere else and it is right here too, whether the reader is a model or a web handler. |
| **D18** | **Every method is synchronous.** Applace's slow calls are subprocesses — npm, tsc, vite, a browser — not sockets. A coroutine whose body is `asyncio.to_thread` claims a concurrency it does not have and colours every caller for nothing; the MCP SDK already runs sync tools on a worker thread, which is why `create_app` and `write_files` are declared sync there. An async backend writes `await asyncio.to_thread(ap.write, ...)` — one line, at the only place that knows its own event loop. |
| **D19** | **The developer's half of the API is not a tool, and never becomes one.** Two things exist only because a person is answerable for them: supplying the **value** of a secret (D8), and connecting this machine to a GitHub organisation, a Vercel account or a company stack (D2b, D6, D7). They are methods on the class and they are not MCP tools — the same principle as Runlace's `approve`: an agent that could do them would be authorising itself. And no method returns a value; `env()` lists names and whether each one has been supplied. |
| **D20** | **A tenant is a home, not a column.** A chatbot serving a company serves thousands of people, and the way to keep their work apart is one Applace home per person under a root — not a `user_id` on every table. Isolation is then the filesystem's and the operating system's job rather than a `WHERE` clause everyone has to remember: a leak needs a bug in Applace *and* in POSIX. Each home is a complete single-user Applace (D11) with its own database, its own git repositories and its own `env/` at `0700`; a machine with one home is the degenerate case, and the code path is the same one. Applace does not authenticate anybody: the caller says who the user is, and is answerable for having asked. |
| **D21** | **What is shared is exactly what cannot be copied.** Two homes can have their own of everything except the things there is only one of: the machine's ports and the machine's disk. So a root holds one ledger — who holds which port, and what each home has spent — and every claim on a port goes through it in a single transaction before anything binds. Probing a port and then binding it is a race that shows up as a dev server that died for no reason; the ledger is what makes "no other app, in no other home" true. |
| **D22** | **A limit is an answer, not a queue and not a crash.** Quotas (apps, previews, disk) and rates (writes, deploys, screenshots per window) come back the way every other refusal does — `ok: false`, a `code` of `quota` or `rate-limit`, a sentence a UI can show, and what it would take to proceed. Nothing blocks waiting for a slot: a chatbot's user asked a question, and "not right now, here is why" is a better answer than a request that hangs. The cap belongs here rather than in each integrator's loop, because the machine is what actually runs out. |
| **D23** | **Nobody's code is garbage.** Collection stops processes, releases ports and deletes scratch — a dev server nobody has looked at for hours, a claim whose process is gone, a checkout a deploy left behind. It never deletes an app, a repository, a secret or a home. A person who comes back to an idle preview finds it stopped, which is a URL away from where they were; a person who comes back to a deleted repository has lost work, and no quota is worth that. |
| **D24** | **A credential never reaches the browser, so the call goes through a gateway the app does not own.** An app declares the APIs it consumes — a name, a base URL, the *name* of the variable holding the credential, the paths it may call — and its own code only ever fetches a relative path. In preview, Applace serves that gateway beside the dev server; on deploy, the declaration compiles to an ordinary serverless function committed into the app's repository, with the value set on the target the way D8 already sets it. The generated function imports nothing from Applace and a human can read it, which is what keeps D1 true. The declaration is the allowlist: a call to an undeclared host is a red gate, exactly as an undeclared dependency is under D9. D14 is untouched — the app still has no database and no runtime of its own; it gains one door onto APIs that already exist. |
| **D25** | **The gate ends in the browser.** Compiling is not working. After the production build, Applace serves the built bundle and visits every declared route in the headless browser it already has (D5); a route that throws, paints nothing or logs a console error is a red gate carrying the message the browser gave. This is the behavioural category: maintainability has linters and architecture has fitness functions, and this one has had nothing, which is why "green" has never been quite trustworthy. Red still means accepted and not committed (D4). Routes are declared, never crawled — a crawler makes the gate's cost a function of the app's size, and the gate runs on every write. |
| **D26** | **Adoption adds a row, not a file.** A company has front ends already, and the way in cannot be "rewrite it here". `take` points Applace at a repository that exists: the stack is recognised from its manifest, the app is recorded, and from that moment it has a journal, a gate, previews and deploys like any other. Nothing is written *into* the tree — no config key, no marker, no Applace import — because D1 says an app must not be able to tell that Applace exists, and an adopted app is the case that proves it. An adoption that cannot pass the path lint is refused rather than repaired. |
| **D27** | **The register is a read, and it belongs to the developer.** Whoever mounts Applace under a chatbot has to answer "what exists, who owns it, what is exposed, what did it cost" without opening an app or a shell. That is the developer's half (D19): methods and a panel, never an MCP tool, because an agent that could enumerate other people's homes is the one leak D20 exists to prevent. One home's listing has existed since M1; the root's is `Machine.apps|audit|spend`, reading every home's journal and no app's files. It is read-only (D16), it aggregates and never mutates, and it authenticates nobody — the backend that mounts it decides who may look. |

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
  shots/<slug>.png         # the last screenshot, for the chat window (D16)
```

And on a machine serving many people (D20), the same directory is one per
person, under a root that holds the only two things they share:

```
/srv/applace/
  machine.db               # SQLite: the port ledger and what each home spent
  machine.yaml             # quotas and rates (optional; defaults are generous)
  users/<id>/              # a whole Applace home, exactly as above
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
applace stacks [add <git-url> | update <name> | rm <name> | drift]
applace dev <app>                 # start the preview, print its URL
applace stop <app>                # stop it
applace check <app>               # the D3 pipeline, by hand
applace shot <app> [--route R]    # what the browser sees, and what it said (D5)
applace open <app>                # the app in $EDITOR, the repo in a browser
applace env set <app> <NAME>      # prompts for the value, never echoes it (D8)
applace github connect --org O    # the D2b answer
applace vercel connect [--team T] # the D12 answer
applace deploy <app> [-t TARGET] [--production] [--commit SHA]
applace rollback <app> [SHA]      # the previous commit by default
applace deployments <app>
applace logs <app> [--of dev|deploy]
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
            url, detail_json, confirmed, started_at, finished_at)
env_vars(app_id, name, description, created_at)             -- names only (D8)
```

The tables arrive by append-only migration, never by editing the statement above:
`gates` in v2, `previews` in v3, `snapshots.pushed_at` in v4, `env_vars` in v5,
`deployments` in v6 and `apps.pull_request_url` in v7.

`gates` and `deployments` are simultaneously the audit log, the debug trace and the
answer to "why is this app in the state it is in". Never skip journaling to save
time.

## Out of scope for v1 (explicit, so the coding agent does not drift)

Multi-tenancy and user accounts; a web UI of Applace's own that can *do* anything
(an MCP chat host is the UI, as with Runlace — the M10 panel reads the journal and
writes nothing, D16); mobile and React Native; a database or server runtime for
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

**M4 — The eyes.** *Shipped.* `screenshot_app`, browser console capture,
failed-request capture, route and viewport selection, the `shot` CLI verb, and
`applace init` reporting the browser as optional.
*Acceptance:* `scripts/m4_acceptance.sh` — an app that builds green but throws at
runtime is diagnosed from the console output alone, with no human looking at the
screen.

What M4 learned. **The console is the diagnosis and the picture is the
evidence**: the acceptance run came back with `TypeError: Cannot read properties
of undefined (reading 'map')` at `App (…/src/App.tsx:10:66)` for a page whose
screenshot is a white rectangle, so an uncaught exception is recorded with its
name, its message and its first non-`node_modules` frame — `str(error)` alone is
the message without either. **"Did anything render" must be asked with
`innerText`, not `textContent`**: `textContent` counts the source of every inline
`<script>` as text on the page, which reports a blank page with a script in it as
rendered. And **an optional dependency must be optional all the way to the
report**: eyes live behind `applace[eyes]`, a missing browser is an instruction
rather than a traceback, `init` lists it apart from the requirements so it never
makes a machine "not ready", and playwright's own teardown chatter is filtered
out of stderr so a tool result never looks like a crash.

**M5 — GitHub.** *Shipped.* `applace github connect`, repository creation, remote
wiring, automatic push of green snapshots, adoption of an existing repository,
`applace push`, and the D13 divergence refusal.
*Acceptance:* `scripts/m5_acceptance.sh` — an app created in a chat exists on
GitHub a moment later, with its history; a push made behind Applace's back makes
the next snapshot refuse.

What M5 learned. **The remote wins is a question git can answer**: `merge-base
--is-ancestor <remote-head> HEAD` says whether the other side's work is already
in our history, so a divergence is detected before anything is sent, the refusal
names the remote sha and the path to rebase, and Applace never force-pushes.
**A push failure is news, not a failed write**: the push runs after the journal,
returns a report rather than raising, and a green re-check with nothing to commit
still pushes — so a refusal caught by a colleague's commit catches up on its own
once a human reconciles. **The backlog cannot be settled by matching shas**: the
only way out of a divergence is a rebase, which gives our commits new shas that
were never recorded as snapshots, so a successful push marks everything
outstanding — the push having succeeded *is* the proof, and matching on sha left
apps "one commit behind" forever. And **a machine's git credentials will fight
you**: the token goes in through an inline credential helper with the machine's
own helper reset to empty first, because otherwise osxkeychain answers with
whichever account it remembers and the push is refused as the wrong user.

**M6 — The skill and the policy.** *Shipped.* SKILL.md and `get_skill`,
`policy.yaml` (D9 and the D6 exposure rules), `set_env` and the secret path (D8),
`applace skill`, `applace policy`, `applace env set|ls|rm`, and a pass with a
small local model as Runlace did.
*Acceptance:* `scripts/m6_acceptance.sh` — a denied dependency refused before
`npm install` runs, a value the human supplies inlined into a real Vite build and
present in nothing the agent reads, and `qwen3:8b` driven through the tools by
`scripts/m6_local_model.py` building a correct page on its first attempt.

What M6 learned. **A small model measures the harness, not itself.** Given the
document and nothing else, qwen3:8b wrote a page that was right the first time
and then spent ten round trips failing to ship it, because the stack's
`noUnusedLocals` turned a leftover `import { useEffect }` and an unused
`setReleases` into a red build. It never recovered: it resent the same file
again and again while the page it had written would have rendered perfectly.
So the stack was changed rather than the model — unused locals and parameters
are a lint *warning* now, and everything that decides whether the app works
stays an error. **The gate must refuse code that does not work, not code that is
untidy**; tidiness costs an LLM round trips and buys a human nothing a
tree-shaker does not already do. **The document is a component, and it drifts**:
SKILL.md's only worked example used `useState` and `useEffect`, so the model
reached for hooks to hold three constants — the fix was a second example with
static data first, and a test that fails when a tool is renamed out of the
document. **A secret is kept out of a context by having no way in**: `set_env`
takes no value argument, no tool returns one, and the acceptance greps every
tool result, the journal database and every committed file for the value while
finding it inlined in `dist/` and served by the dev server. And **a policy must
refuse before the pipeline, not inside it**: `npm install` *is* the execution of
a dependency, so the check runs on the manifest diff in 0.02s, before anybody's
postinstall script gets a turn.

### v2 — host it and industrialise it

**M7 — Deployment.** *Shipped.* The adapter interface, the `local` target (build +
serve the `dist`), the **Vercel** target through GitHub (D12), preview versus
production, the D6 confirm gate, `rollback_app`, environment synchronisation, and
the CLI verbs `deploy`, `rollback`, `deployments`, `logs --of deploy` and
`vercel connect|status`.
*Acceptance:* `scripts/m7_acceptance.sh` — an app built by the real stack is served
on a URL that answers with its production bundle; a half-written file on disk does
not reach it; production is refused for an agent, asked of a human in the terminal,
and refused outright under a `deny` policy; a rollback puts the previous commit
back while the app's own tree stays on the regression; and the value the human
typed is in the bundle a browser downloads and in nothing the agent, the journal,
git or the deploy log holds.

What M7 learned. **"A deployment is a commit" is a claim about *where the build
runs*.** The first implementation built in the app directory whenever the target
sha was `HEAD` — which quietly shipped whatever the agent had half-written, and
would only have shown up later as a production page nobody could explain from the
sha. A dirty tree now builds the commit in a `git worktree`, with `node_modules`
lent by symlink when the two manifests are byte-identical, and the deploy says in
`warnings` that the uncommitted work was left behind. **Rollback is not a git
operation, it is a deploy of an older sha**: checking the past out under the agent's
feet would eat the work someone is in the middle of fixing, so the app's tree never
moves and the broken commit stays `HEAD`. **The gate has to sit below every entry
point, not at each one**: `deploy.gate` is the only place that decides, the policy
can refuse production but can never supply the confirmation, and both the CLI's
`typer.confirm` and the MCP tool's `confirm-required` refusal are ways of reaching
that one rule. **The journal opens before the adapter runs** and is closed on every
path including a crash, because an adapter that hangs has still changed the world.
And **a timing constant bound in a signature default cannot be tuned by a test**:
`wait(interval=POLL_INTERVAL)` read the module value at import, so the Vercel suite
politely slept 47 seconds per run until both timings were resolved in the body.

**M8 — Company stacks.** *Shipped.* `applace stacks add|update|rm|drift`, a stack
carrying a design system and an internal API client (`examples/acme-stack`),
commit pinning in `.applace-stack.yaml`, `stack_commit` on every app, and the
drift reported by `applace stacks drift` and by `get_app`.
*Acceptance:* `scripts/m8_acceptance.sh` — a company stack published as a git
repository is installed and pinned, an app created from it is born with the
company's components and passes the real gate without a model writing a line, the
stack gains a component, and the app that predates it keeps its own files while
being told, in `get_app`, exactly which commit it predates.

What M8 learned. **The directory is the name.** A stack names itself in
`stack.yaml`, but two forks of the same company stack both say `acme-web`, so the
installed directory wins and `--name` is real: `load()` takes a name override and
the registry passes the directory's. **Installing has to be validate-then-move.**
A `stack.yaml` with a typo in it is not one broken stack, it is `list_stacks`,
`applace stacks` and every `create_app` refusing to answer — so the clone goes to
a temporary directory, `load()` has to accept it, and only then does it replace
what was there, with the previous copy kept aside until the move has succeeded.
**What is installed is a snapshot, not a second checkout**: the clone's `.git` is
dropped and `.applace-stack.yaml` records the source, the ref and the sha, which
makes `update` a re-clone and removes the question of what to do with local edits
in a directory nobody thinks of as a repository. And **an update must not touch an
app.** Rewriting an app's files from a newer template would overwrite work that is
already committed in the app's own history (D1), and there is no merge in git for
"the template moved" — so Applace reports the drift in three places and applies it
in none. Installing a stack stays a human act with no MCP tool behind it, for the
same reason `github connect` has none: it decides what every future app is made
of.

**M9 — Handover.** *Shipped.* `github connect --review pr` (D15): commits go to
`applace/<slug>` and one pull request stays open against the default branch;
`handover.survey` telling an agent about a human's commits and refusing to write
over a human's uncommitted edits (`stage: "handover"`); `applace open`; and the
packaging that makes `uvx applace` a real install.
*Acceptance:* `scripts/m9_acceptance.sh` — against a GitHub on localhost whose
repositories are real bare repositories, a real app's green writes land on
`applace/review-me` behind one pull request while `main` does not move, a person
merges it and the catch-up push says there is nothing to propose, a person's
uncommitted edit refuses the agent's write with their bytes intact while a write
elsewhere still goes green and carries their wording, a person's commit is
reported as news and not as a refusal, `applace open --print` names the preview,
the repository and the directory, and the built wheel — run through `uvx` from a
directory with no checkout in it — initialises a home that already knows
`vite-react-ts` and prints SKILL.md.
*Blind:* `scripts/blind_handover.py` asks a hosted model the same question with
no hint of any of this. `mistral-large-latest` warned by `get_app` never
attempted the write and told the human to commit or discard (2 rounds, $0.023);
the same model surprised mid-task — the person starts typing after it has read
the file — was refused, stopped on the first refusal, and said what it had been
about to change (4 rounds, $0.051). Their bytes survived both.

What M9 learned. **The refusal has to be per path, not per repository.** A
harness that stopped because *any* file was dirty would be one nobody leaves
running: an agent editing `src/Chart.tsx` while a person edits `README.md` is not
a conflict, so `guard` intersects the write's own targets with what is
outstanding. **Telling a human's mess from the agent's own is the whole
problem**, and D4 is what makes it hard: a red gate leaves broken files on disk
on purpose, so those paths are dirty for a reason that has nothing to do with a
person — and suppressing *everything* after a red write would have let the agent
overwrite a real edit to a different file. The answer is the union of
`written_json` over the consecutive red gates since the last green one, which is
why `db.recent_gates` exists. **Order decides who gets blamed**: surveying before
the journal is written mislabels the agent's own red files as somebody's work, so
the tail survey runs after `_journal` and after the commit, while the refusal
branch surveys before it. **Review mode cannot open a pull request against a
branch that does not exist yet**, so the birth commit still goes to the default
branch and only the app's second commit onwards is proposed — and a pull request
that cannot be opened (a 422, a token without the scope) is reported, never
fatal: the commits are already pushed and a human can open it by hand.
**`applace open` is the sentence an agent cannot say.** The agent hands back URLs
in a chat window; the person wants the thing itself, and which of four addresses
an app currently has is a question the journal can answer and a human should not
have to.

**M10 — An app in a chat window.** *Shipped.* Every large company now has a chatbot,
and what its users ask for next is the thing Lovable does. M1–M9 built the engine
for a developer at a terminal; M10 is what a developer bolts under a chatbot they
already have, in an afternoon (D16).

Five deliverables, in order:

1. **The card.** `GET /panel/apps` and `GET /panel/apps/<slug>` on the same port
   as the MCP tools: what an app is, what state it is in (building, red, green,
   previewing, deployed), every address it has, its last gate with its errors,
   its outstanding handover. One request, everything a chat message needs to
   render.
2. **The stream.** `GET /panel/apps/<slug>/events`, server-sent events, so a chat
   window shows *typecheck → lint → build → commit → pushed → live* while it
   happens instead of waiting a minute in silence. The journal is the source of
   truth; the stream reports it and invents nothing.
3. **The last screenshot** at `GET /panel/apps/<slug>/shot.png`, because the
   proof that a page renders belongs next to the sentence that says it does.
4. **The embed.** `GET /panel/embed.js` defines `<applace-app slug="…">`: a
   framework-free custom element that draws the card, the live preview in an
   iframe and the links. No npm package, no build step, no React version to
   agree on — a company's chat UI includes one script tag.
5. **The reference chatbot**, `examples/chat/`: a small, readable chat that
   builds apps with these tools and embeds that component, plus
   `docs/integrate.md` with the configuration lines for Claude Code, Cursor and
   an OpenAI-compatible backend. It is the demo, and it is the documentation.

Out of scope on purpose, and named so it is not smuggled in: identity. The panel
is read-only and inherits the trust of the machine it runs on (D11). Who may see
an app, and an SSO in front of what is deployed, is the next milestone's subject,
not this one's.

*Acceptance:* `scripts/m10_acceptance.sh` — against a real `applace serve --http`
driven by a real MCP client over that same port: a card a chat can draw in one
request with no git vocabulary in it, a stream opened *before* anything happens
that reports the typecheck failure with its file and line and then the green
commit without being asked, the preview URL in the card answering 200, the
screenshot the agent took served as a PNG the card points at, a local deployment
appearing as `urls.live`, an `embed.js` with a custom element and no import in
it, a `/panel` page that uses the element it recommends, and
`examples/chat/chat.py` standing up against that harness — reading the skill,
serving a page that embeds the panel, and surviving a model that is not there.

*Blind:* the reference chatbot itself, with `mistral-large-latest` behind it and
a $0.50 cap, asked in one sentence for a page showing which of three internal
services are up. Two turns, $0.13 in total: it created the app, read the stack's
`App.tsx`, wrote a page that passed the gate first time, looked at it with the
eyes and described what it saw; then added the last-checked time and deployed it
locally. The chat said *Creating “Service Status Dashboard” · Reading 1 file ·
Writing 1 file, and building · Looking at the page · Putting it online* — and the
card beside it went from `new` to green to "Green, and what is live is this
commit" with the screenshot, the preview and the deployment all in `urls`. The
model was never told anything about Applace that `get_skill` did not tell it.

*Three vendors:* the same chatbot, the same harness, one app each, built and then
updated by a second request — Mistral (`mistral-medium-2604`, an incident
dashboard with filters, a chart, a detail pane and a CSV export, ~$0.15), OpenAI
(`gpt-6-astra` over `/responses`, an onboarding checklist, ~$0.95) and Google
(`gemini-3.8-flash`, an internal API catalogue, a few dollars against a guessed
price table). All three chose a stack, created the repository, passed the gate,
looked at the page and deployed locally with nothing said to them that
`get_skill` did not say. Two findings came out of it and are fixed in the
example: OpenAI's newest models accept function tools on `/responses` only, and
the *eyes have to reach the model* — `screenshot_app` returns a report and a PNG,
and the first version of the chat joined the text blocks and dropped the image.
Mistral's chart rendered as invisible bars and the model declared it fine,
because it had read a console log, not looked at a page. With the picture put
into the next message it opened with "aucune barre n'apparaît", found the CSS,
and fixed it. It then spent six more rounds fixing what it had already repaired,
announcing failure each time, until the provider refused a ninth image — so the
last capture is also kept deliberately short (`EYES`), and the round cap, not the
model's judgement, is what ends a visual loop.

What M10 learned. **A chat window is a different reader, not a smaller one.**
`get_app` is exhaustive because an agent is about to write code; a card is one
state, one sentence and the links, because it is about to be rendered next to a
person's question — the fold-down (`panel.card`) is the whole design, and the
sentence is generated where the facts are rather than in the integrator's
template. **A URL said once is a URL lost**: a pull request announced in a
message that scrolled away an hour ago is a pull request nobody merges, which is
why the PR URL is now a column (v7) rather than a line of output, and why the
screenshot is written to `~/.applace/shots/<slug>.png` instead of only returned.
**Polling the journal beat being notified by it.** A bus between the gate, the
push and the deploy and a chat window would be a second account of the same
facts, able to disagree with the first; half a second of latency is nothing next
to a build stage, and the stream that only forwards what it read cannot invent a
state the harness is not in. **The heartbeat is not a detail** — fifteen quiet
seconds and a reverse proxy closes the connection, taking the build with it.
**One port, or nobody integrates.** The panel is mounted on the MCP server's own
app (`custom_route`), so what a company opens in its firewall is one address, and
`applace serve` on stdio simply never fires those routes. **A custom element, not
a component**: a chat UI is somebody else's bundler and somebody else's React
version, and one script tag is the only integration that survives all of them.
And a test lesson with teeth: **Starlette's `TestClient` buffers a response
whole**, so an endless stream never returns its headers — the events generator is
therefore a function that can be read directly, and the HTTP end of it is what
the acceptance script exercises.

**M11 — The package.** *Shipped.* `from applace import Applace` (D17): one class
over the same store, the same gate and the same journal the MCP server uses, for
the developer who is embedding Applace in a backend rather than talking to it
from a terminal or an agent.

It has two halves, and the line between them is the point of the milestone.

1. **What an agent does**, method for tool, answer for answer: `skill`,
   `stacks`, `create`, `apps`, `app`, `read`, `write`, `declare_env`, `preview`,
   `stop_preview`, `shot`, `deploy`, `rollback`. The MCP server is rewritten as
   a skin over them — the tool docstrings stay, because the docstrings *are* the
   model's interface, but nothing behind them is implemented twice, so the two
   doors cannot drift.
2. **What only the deployment does** (D19): `set_env` with a *value*, `env`,
   `unset_env`, `connect_github`, `github`, `adopt`, `push`, `connect_vercel`,
   `vercel`, `install_stack`, `update_stack`, `installed_stacks`, `drift`,
   `policy`, `deployments`, `card`, `cards`, `remove`. None of these is an MCP
   tool and none ever will be. `card` and `cards` are the panel's own fold-down
   (D16) without the HTTP: a product that renders its own UI should not have to
   call its own harness over a socket.

Everything is synchronous (D18) and everything returns a dict.

*Acceptance:* `scripts/m11_acceptance.sh` — one Python process, one temporary
home: the class creates an app, a red write comes back with the failing stage and
its diagnostics and commits nothing, the green one commits, `card()` is byte-for-
byte what the panel serves over HTTP, a secret value set through `set_env`
reaches the app's env file at `0600` while no method anywhere returns it, and the
same question asked through the MCP server and through the class gives the same
answer.

What M11 learned. **The second door tells you where the first one leaked.**
Writing the class meant naming every behaviour once, and the public-repository
gate (D6) turned out to live inside a CLI command — a backend embedding Applace
would have re-implemented a policy decision, or forgotten it. It now lives in
`connect_github`, and `applace github connect` calls that: the terminal's own
part is asking the question out loud, not knowing the rule. **A value and a name
are different verbs.** The MCP tool `set_env` declares a name; the method
`set_env` supplies a value, and they share a name in two doors that must never
share a caller, so the agent-facing one is called `declare_env` here and the
docstrings say why on both sides. **The envelope is part of the answer.** The
first `card()` came back without the `ok` the panel's HTTP route adds, so the
same card had two shapes depending on the door — a difference nobody would have
noticed until a caller switched doors. The class carries it now, and the
acceptance compares the two byte for byte.

**M12 — Many people, one machine.** *Shipped.* D11 said single-user and M10
deferred identity to "the next milestone". This is it, and the answer is not a
`user_id` column: a root holds one whole Applace home per person (D20), and what
the homes share is only what there is one of — this machine's ports and this
machine's disk (D21).

    from applace import Machine

    ap = Machine("/srv/applace").user(request.user.id)
    ap.create("Team Dashboard")

Four deliverables:

1. **The root.** `Machine(root)` hands back a `Applace` per user id, made on
   first use, at `root/users/<name>-<digest>/`. The id is whatever the caller's
   own system calls a person and is never parsed: `../../etc` is a directory
   name, not a path, and two ids that flatten to the same readable name still
   get two homes because the digest is of the id.
2. **The ledger.** `root/machine.db`: who holds which port, and what each home
   has spent. A preview or a local deployment claims its port inside one
   `BEGIN IMMEDIATE` transaction, probe included, and says which pid took it.
   Nothing ever releases a port: a claim whose process is gone is reaped, which
   is the same lesson M3 learned about previews applied to the machine.
3. **The limits.** `Limits` and an optional `root/machine.yaml`: apps, previews
   and disk per home, and writes, deploys and screenshots per hour. They are
   checked in the class every other door goes through, so a quota refuses
   *before* `npm install` rather than after it, and a refusal is an answer with
   a `code` (D22) — which is also where the anti-loop cap now lives, instead of
   in each integrator's chat loop.
4. **The collection.** `Machine.gc()` and `applace machine gc --root`, safe on a
   cron: stop previews nobody has watched for hours, reap dead claims, remove
   scratch checkouts, forget stale spend. It deletes no app, no repository, no
   secret and no home (D23). `applace machine users|ports` is the same view for
   whoever is on call.

Out of scope, and named so it is not smuggled in: authentication. Applace does
not know who anybody is. The backend that calls `Machine.user(id)` has already
answered that question and is answerable for the answer.

*Acceptance:* `scripts/m12_acceptance.sh` — three people under one root create
the same app at the same instant with the real stack, each in their own home;
one of them sets a secret and it exists in no file of anybody else's; their
three dev servers run at once, answer, and hold three different ports in the
ledger; a quota refuses a fourth app with `code: "quota"` and installs nothing;
a rate limit refuses a second write with a `retry_after` a UI can show; a broken
`machine.yaml` is an error rather than a quieter limit; and a collection stops
all three previews and frees all three ports while every repository, every app
and every secret is still exactly where it was.

What M12 learned. **Opening the database is a write.** Three people arriving in
the same instant deadlocked the acceptance run, and the cause was one line's
order: `PRAGMA journal_mode = WAL` takes an exclusive lock, and a connection
that has not yet been told `busy_timeout` raises `database is locked` instead of
waiting for it. The pragma is set before the switch now — in the ledger and in
the per-home database, which had carried the same ordering since M1 and never
fired, because one caller never races itself. **Nothing releases a port.** A
`release()` beside `stop_preview` would have been dead code the day a process
was killed from outside, so a claim is a lease whose truth is the process, and
the only lifecycle is reaping — M3's lesson about previews, applied to the
machine. Which drags M3's other lesson along with it: **`os.kill(pid, 0)` says a
zombie is alive**, so a stopped preview kept holding its port until `alive()`
asked `ps` the same way the supervisor does. **The readable part of a home's
name is a courtesy; the digest is the identity.** `alice@example.com` and
`alice-example-com` flatten to the same directory name, and two people sharing
one home is the one failure this milestone cannot have. **A limit is counted at
the moment of asking**, from the apps table and the ledger rather than from a
balance somebody maintains: a balance is a second account of the same facts, and
the one that can be wrong. And **the collector cannot assume a database**: a
home registered by a person who never created anything still accumulates
scratch, and the first `gc` skipped those homes entirely because it looked for
`applace.db` before looking for work to do.

### v3 — the engine under the chatbot

M1 to M12 make an agent able to build, see, ship and share an app, and make a
machine able to hold many people doing it at once. What they do not yet make is
an app that touches the company's data, a gate anyone should trust without
looking, a way in for the front ends a company already has, or an answer to
"what is running in here". Those are the four.

**M13 — The gateway.** *Shipped.* The four deliverables of D24.

1. **The declaration.** `declare_api` — a name, a base URL, the *name* of the
   variable holding the credential, and the paths and methods the app may call.
   It is an agent-facing method for the same reason `declare_env` is: it names
   things and supplies nothing. It is stored in the journal and in no file of
   the app's tree (D1). `policy.yaml` gains the hosts a company permits at all,
   and a declaration outside them is refused the way a denied dependency is
   (D9).
2. **The gateway in preview.** The app fetches `/api/gateway/<name>/*` on the
   preview's own origin; the stack's dev server proxies that path to a process
   Applace runs, which adds the credential server-side and forwards only what
   was declared. Same origin, so there is no CORS question and no browser ever
   holds a token. The path is the production one on purpose: the same relative
   URL is served by the generated function once the app is deployed, so there is
   no development-only code in the app. One gateway per home, not per app, and
   it requires a key — loopback is not a boundary on a machine holding many
   homes (D20).
3. **The gateway in production.** The declaration compiles to one ordinary
   serverless function, committed with the green snapshot, and the credential is
   synchronised to the target the way M7 already synchronises environment
   variables. The function imports nothing from Applace and reads like code a
   company would have written, which is what keeps D1 true off the harness.
4. **The host lint.** The path lint's sibling: a written file that fetches an
   absolute URL whose host was never declared is a red gate naming the file, the
   line and the host. An agent cannot reach the internet by typing a URL.

Out of scope, named so it is not smuggled in: a database, a queue, server-side
rendering, and any upstream needing more than a header — an OAuth flow on behalf
of the end user is a later question, not a small one.

*Acceptance:* `scripts/m13_acceptance.sh` — an app built by the real stack reads
an internal API through the gateway with a token the model never saw; the token
is in no file of the repository, in no bundle a browser downloads, in no journal
row and in no deploy log; the deployed app makes the same call through its own
function; a write that fetches an undeclared host is red and is not committed;
and a declaration the policy forbids is refused with the policy's own code.

What M13 learned. **A guide and a control are not two strengths of the same
thing.** The host lint can be fooled by a computed URL and that is fine; it
exists to save an agent a round trip. The gateway cannot be fooled, because it
is the only thing holding the credential — which is why the lint's imperfection
is written into its docstring rather than filed as a bug. **`customers/**`
covers `customers`.** Translating the pattern to `customers/*` made the most
ordinary call there is — the list — a 403, and no human writing that pattern
means "the index is off limits but every item is fine". The rule had to be
fixed twice, in the Python allowlist and in the generated JavaScript, which is
the standing cost of a rule that runs in two languages. **A refusal from the
policy is not an invalid declaration.** Both were raised as `invalid-api` at
first, which tells an agent to retry what only a human can change; a policy
refusal is `policy` and it is final (D22). **The only test that found the real
bug was the one that ran the real proxy.** Vite forwards headers lowercased,
the gateway stripped its own key by an exact-case comparison, and so the key
that proves a caller is this home was travelling to the upstream — a unit test
with a hand-built request would have passed forever. **Two things that are
needed together do not have the same lifetime.** One gateway serves a home and
a preview serves an app, so the supervisor is not told the gateway exists and
the gateway is not told an app is previewing; the one place that knows both is
`start_preview`, and the gateway goes down when the last preview does, not when
any preview does. And **the policy is read per request, not per declaration**:
a company that tightens `policy.yaml` at noon has tightened it for the process
already running, which is the difference between a rule and a rule that was
true once.

**M14 — The sensor.** The behavioural half of the gate, per D25.

1. **The routes.** Declared by the agent, defaulting to `/`, stored beside the
   API declarations. Declared and never crawled: the gate runs on every write.
2. **The visit.** After the production build, the bundle is served and each
   route is opened in the browser M4 already drives — one browser per gate, not
   one per route. An uncaught exception, an empty body or a console error is a
   failure carrying the browser's own message, mapped to a file and a line
   through the sourcemap when there is one.
3. **The assertions.** Optional, per route, declarative: a selector that must be
   present, a string that must appear. No test framework enters the harness
   (D10) and none is written into the app (D1).
4. **The switch.** On by default, turned off per stack or per policy, because an
   app whose routes require a login cannot be visited anonymously. Off is stated
   in `get_app` rather than silently assumed, so nobody mistakes a skipped check
   for a passed one.

*Acceptance:* `scripts/m14_acceptance.sh` — an app that typechecks, lints and
builds green while painting a white screen is refused by the gate, with the
console error and the route named, and is not committed; the fix makes the same
write green and it commits; a declared selector that a later write removes turns
the gate red; a route behind a login with checks disabled is green and says in
`get_app` that it was not visited; and no browser is left running afterwards.

**M15 — The take-over.** Per D26. `take` is the front ends that already exist;
the older `adopt`, which points an Applace app at a GitHub repository that
already exists (D2), keeps its name and its meaning, and the documentation
stops letting the two be confused.

1. **`ap.take(source, name=...)` and `applace take <path-or-url>`.** Clone the
   repository or record the checkout, recognise the stack from the manifest and
   the lockfile, refuse rather than guess when nothing matches, and keep the
   git history exactly as it is.
2. **Nothing is written into the tree.** The stack, the routes and the API
   declarations live in the journal. An adopted app is the case that proves D1.
3. **The first gate is a report, not a verdict.** A codebase that is already red
   is adopted anyway and told so; refusing red would be refusing every real
   codebase in the building.
4. **`--dry-run`** says what it would recognise and changes nothing.

*Acceptance:* `scripts/m15_acceptance.sh` — a front end living outside Applace,
with commits of its own, is taken over; it appears in `apps()` with its stack
recognised; `git log` and `git status` are byte-identical to what they were
before, and no file was added; the gate runs on it, a preview serves it, and a
green write lands as a commit in its own history; a directory that is not a
recognisable front end is refused with what was looked for.

**M16 — The register.** Per D27 — the developer's view, one level above a home.

1. **Across the homes.** `Machine.apps()` — every app of every home with its
   owner, state, stack, last gate and where it is exposed; `Machine.audit()` —
   the journal rows that govern (what became public, what reached production,
   which human confirmed it, what a policy refused), filterable by date and by
   user; `Machine.spend()` — what M12's ledger already counts, per home and per
   app. Every home's database is opened read-only: a reporting tool that can
   write is a reporting tool that can corrupt.
2. **One home's listing, agreeing with it.** `ap.apps()` and `ap.cards()` gain
   the fields the register shows, so the two views never disagree about the
   state of the same app.
3. **The panel at the root.** The M10 routes mounted for a `Machine`: an index
   of homes and apps, each card reachable by `(user, slug)`, read-only (D16),
   authenticating nobody — the backend that mounts it passes a callable saying
   who may see what, and a home that callable refuses is a 404, not a 403.
4. **The CLI.** `applace machine apps|audit|spend`, each with `--user` to narrow
   and `--json` to feed a dashboard or hand a security team an export.

Out of scope: alerting, retention policy, and any metrics backend. The register
answers questions; it does not decide that somebody should be woken up.

*Acceptance:* `scripts/m16_acceptance.sh` — three homes holding apps in
different states; `machine apps` lists every one against the right owner while
opening no app's files and taking no write lock; `audit` contains the production
deploy and the human confirmation that permitted it and contains no secret's
value; `spend` agrees with the ledger to the unit; the root panel lists the
homes the callable allows and 404s the one it does not; and `--json` parses on
all three.
