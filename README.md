# Applace

**Build, preview and ship web apps from any LLM agent.**

Applace is the harness underneath a company's own Lovable. It gives an agent —
Claude Code, Cursor, an in-house chat — a way to create a web app, write code
into it that has been type-checked and built before it counts, look at the
result in a real browser, and land the whole thing in the company's GitHub.

It is the sibling of [Runlace](https://github.com/cycle-inc/Runlace). Runlace
compiles a *workflow* and replays it with no model in the loop. Applace compiles
an *app* and serves it.

### The three things that make it not a code generator

- **Every app is an ordinary repository, on real GitHub, from birth.** Not an
  export at the end. `cd ~/.applace/apps/my-app && npm run dev` works, and
  nothing in the app knows Applace exists.
- **The agent has eyes.** A build going green and a page rendering are two
  different facts. Applace can report the second one — screenshot, browser
  console, failed requests.
- **A stack is company property.** The design system, the auth wiring and the
  internal API client live in a versioned stack, and every app starts from one.
  That is the difference between *a* Lovable and *our* Lovable.

### Status

**M1 to M6 are shipped**: the store (apps as git repositories), the compiler
(`write_files` type-checks, lints and builds before anything counts, and commits
when it is green), the preview (a supervised dev server with a URL), the eyes (a
real browser, its console and its failed requests), GitHub (every new app is a
repository in your organisation, and every green snapshot is pushed there), and
the skill (the document an agent reads, the dependency policy, and the secret
path). Deployment is specified in [SPEC.md](SPEC.md) and not yet built. The
milestone list there is the roadmap, and it is followed in order.

### Try it

```bash
uv sync
uv run applace init
uv run applace new "Team Dashboard"
uv run applace dev team-dashboard      # a URL a human can watch
uv run applace check team-dashboard    # typecheck, lint, build, commit if green
uv run applace ls
```

`init` reports whether this machine has what a stack needs (`git`, `node`,
`npm`). `new` renders the stack, installs it, and makes the first commit. What
comes out is a plain Vite + React + TypeScript + Tailwind repository:

```bash
cd ~/.applace/apps/team-dashboard
npm run dev
```

For the eyes, the browser is an extra:

```bash
uv sync --extra eyes
uv run playwright install chromium
uv run applace shot team-dashboard --route /
```

### Connect it to your GitHub

```bash
uv run applace github connect --org acme     # --visibility, --prefix, --team
uv run applace github status
```

From then on every app Applace creates is a repository in `acme`, and every
green snapshot is pushed to it — the agent chooses nothing about this and is
told about it. A token comes from `APPLACE_GITHUB_TOKEN`, `GH_TOKEN`,
`GITHUB_TOKEN` or `gh auth token`, and never reaches the model.

```bash
uv run applace push team-dashboard                            # retry a push
uv run applace github adopt team-dashboard --repo acme/dash   # an existing repo
```

If someone else pushed first, the push is refused and says where the remote is:
Applace never force-pushes. Rebase the app, then `applace push` again.

### Secrets and configuration

An agent declares **names**; you supply **values**. There is no tool argument
for a value and no tool that returns one, so a secret never enters a model's
context.

```bash
uv run applace env set team-dashboard VITE_API_BASE_URL   # prompts, no echo
uv run applace env ls team-dashboard
```

Values live in `~/.applace/env/<app>.env`, `0600`, outside every repository, and
are injected into the dev server and the build — never written into the app.

### The policy

`~/.applace/policy.yaml` is what this machine allows. It is checked *before*
`npm install` runs, because installing a package is running it.

```yaml
dependencies:
  allow: [react, react-dom, recharts]   # an allowlist, if you want one
  deny: ["@acme/legacy-ui"]
  registry: https://npm.acme.internal
exposure:
  public_repositories: deny             # allow | confirm | deny
  production_deploys: confirm
```

```bash
uv run applace policy       # what is in force
uv run applace skill        # what an agent is told, and what this machine is
```

### As an MCP server

```bash
uv run applace serve            # stdio
uv run applace serve --http 8848
```

Tools available today: `get_skill`, `list_stacks`, `create_app`, `list_apps`,
`get_app`, `read_files`, `write_files`, `set_env`, `start_preview`,
`stop_preview`, `screenshot_app`. `get_skill` is the one to call first: it
returns the skill document plus what this particular machine does.

### Design

Decisions are locked before code and recorded in [SPEC.md](SPEC.md) — that an
app is an ordinary repository (D1), that versioning is git and the remote is
GitHub (D2), that `write_files` is a compiler (D3), that a red write is accepted
but not committed (D4), that the gate is on exposure rather than on writing
(D6), that secrets never enter the model's context (D8). Read that document
before changing behaviour; it is also the roadmap.

### License

MIT.
