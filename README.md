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

**M1 to M11 are shipped** — every milestone in the spec: the store (apps as git
repositories), the compiler
(`write_files` type-checks, lints and builds before anything counts, and commits
when it is green), the preview (a supervised dev server with a URL), the eyes (a
real browser, its console and its failed requests), GitHub (every new app is a
repository in your organisation, and every green snapshot is pushed there), the
skill (the document an agent reads, the dependency policy, and the secret path),
deployment (a commit served from this machine or shipped to Vercel through its
own repository, with production gated on a human), company stacks (installed
from git, pinned to a commit, with the drift reported rather than applied), the
handover (a pull request a person merges, a refusal to write over what they
are editing, and `applace open`), the chat window (a read-only panel on the
same port, a live card per app, and the one script tag a company's own chatbot
embeds), and the package (`from applace import Applace`, the same behaviours as
a Python class, with the MCP server rewritten as a skin over it).
[SPEC.md](SPEC.md) records why each of those is the way it is, and what each
milestone taught.

### Try it

Applace runs from the package, with no checkout of this repository:

```bash
uvx --from git+https://github.com/cycle-inc/Applace applace init
uvx --from git+https://github.com/cycle-inc/Applace applace new "Team Dashboard"
```

From a checkout, which is what the rest of this README shows:

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

### Hand it to a human

Connect the machine in review mode and nothing an agent wrote reaches the
default branch without somebody merging it:

```bash
uv run applace github connect --org acme --review pr
```

Every app then commits to `applace/<slug>` and keeps **one** pull request open
against `main`. The agent is given the URL and told to say it out loud; merging
is yours, and there is no Applace command that does it for you.

The same courtesy runs the other way. An app is a repository you are invited to
open, so Applace records every commit it makes itself and can therefore tell
your work from its own:

- **Your commits are news.** `get_app` reports them and the agent is told to
  read those files before it changes them. Nothing is blocked.
- **Your uncommitted edits are a refusal.** `write_files` replaces whole files,
  so a write onto a file you are in the middle of editing comes back as
  `stage: "handover"` with your bytes untouched, and the agent is told to ask
  you rather than to retry. A write to a *different* file still goes through.
  `applace check <app>` commits what you left behind, once it builds.

And when the agent is done, the human end of the handover is one command:

```bash
uv run applace open team-dashboard              # live, else preview, else repo, else dir
uv run applace open team-dashboard --what repo
uv run applace open team-dashboard --print      # just say the address
```

### Your own stack

A stack is what makes this *your* Lovable rather than a generic one: the design
system, the internal API client and the house lint rules an agent must not have
to invent. It is a git repository you own, installed here and pinned to the
commit it was cloned at.

```bash
uv run applace stacks add https://github.com/acme/web-stack.git   # --ref, --path, --name
uv run applace stacks                                             # what is installed, and from where
uv run applace new "Billing Portal" --stack acme-web
```

`examples/acme-stack/` in this repository is a working one — a design system in
`template/src/ui`, an authenticated API client in `template/src/lib/acme.ts` —
and its README shows how to install it from a local path.

Every app records the stack commit it was born from. When the stack moves,
`applace stacks update <name>` changes what **new** apps start from; existing
apps are never rewritten, because their files are committed in their own
repositories, which is where an agent's work and a human's both live.

```bash
uv run applace stacks update acme-web
uv run applace stacks drift        # which apps are older than the stack now
```

An agent is told the same thing: `get_app` reports `stack_drifted` and says what
it means, so it does not try to "upgrade" an app by hand.

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

### Ship it

A preview is a dev server on a laptop. A deployment is a **commit** built and
put somewhere it can be opened — never the working tree, so a half-written file
cannot reach anybody.

```bash
uv run applace deploy team-dashboard                      # local: no account needed
uv run applace deploy team-dashboard --production         # asks before it does it
uv run applace deployments team-dashboard                 # what went out, and when
uv run applace rollback team-dashboard                    # the previous commit, back
```

`--target local` runs the real production build and serves the result from this
machine on a port of its own. It is the honest rehearsal: a page that works here
has cleared everything except the provider.

For Vercel, connect once; deployments then go through the app's GitHub
repository, so what is live always names a sha (an app with no repository falls
back to uploading its build, and says so).

```bash
uv run applace vercel connect --team acme     # APPLACE_VERCEL_TOKEN or VERCEL_TOKEN
uv run applace vercel status
uv run applace deploy team-dashboard -t vercel
uv run applace deploy team-dashboard -t vercel --production
```

Environment values are pushed to the target's environment for you, by name —
the values you typed, never the names an agent invented for them.

**Production always needs a human.** `deploy_app` refuses it with
`code: "confirm-required"` unless `confirm: true`, the CLI asks before it builds
anything, and `exposure.production_deploys: deny` in the policy refuses it
outright. The policy can tighten that rule; nothing loosens it.

### As an MCP server

```bash
uv run applace serve            # stdio
uv run applace serve --http 8848
```

Over HTTP the same port also serves the **panel** — the chat window's half of
the harness (D16), read-only, reading the same journal the tools write to:

```
http://127.0.0.1:8848/mcp                        the tools
http://127.0.0.1:8848/panel                      a page listing every app
http://127.0.0.1:8848/panel/embed.js             the <applace-app> element
http://127.0.0.1:8848/panel/apps/<slug>          one card, as JSON
http://127.0.0.1:8848/panel/apps/<slug>/events   that card, whenever it changes
http://127.0.0.1:8848/panel/apps/<slug>/shot.png the last screenshot
```

Putting an app in your own chat is one script tag and one element:

```html
<script src="http://127.0.0.1:8848/panel/embed.js"></script>
<applace-app slug="team-dashboard" base="http://127.0.0.1:8848" height="520">
</applace-app>
```

It draws the state, the sentence, the errors and the links, embeds the live
preview, and keeps itself up to date while the build runs — no npm package, no
build step, no React version to agree on.

Tools available today: `get_skill`, `list_stacks`, `create_app`, `list_apps`,
`get_app`, `read_files`, `write_files`, `set_env`, `start_preview`,
`stop_preview`, `screenshot_app`, `deploy_app`, `rollback_app`. `get_skill` is
the one to call first: it returns the skill document plus what this particular
machine does — which stacks it has, whether it pushes to GitHub, what the policy
allows, and where it can deploy.

### From your own Python

The fourth door (D17). Same store, same gate, same journal — no server to run,
no socket, no MCP client:

```python
from applace import Applace

ap = Applace()                                   # or Applace("/srv/applace")
made = ap.create("Team Dashboard")               # installed, built, committed
app = made["app"]

written = ap.write(app, {"src/App.tsx": code}, message="First screen")
if not written["ok"]:
    print(written["stage"], written["errors"])   # a result, never an exception

ap.preview(app)["url"]                           # a URL to put in an iframe
ap.card(app)                                     # exactly what /panel/apps/<slug> serves
ap.deploy(app, target="local")["url"]
```

Every method is synchronous and returns a dict with `ok` in it (D18): a failure
is `{"ok": False, "code": ..., "error": ...}`, so a web handler never has to
catch anything. The agent's thirteen methods mirror the tools one for one —
`skill`, `stacks`, `create`, `apps`, `app`, `read`, `write`, `declare_env`,
`preview`, `stop_preview`, `shot`, `deploy`, `rollback` — and the MCP server
calls exactly those, so the two doors cannot answer the same question
differently.

The class also has what an agent must never have (D19): `set_env` with a
*value*, `connect_github`, `connect_vercel`, `push`, `adopt`, `install_stack`,
`remove`, and the panel's `card`/`cards` without the HTTP. None of those is a
tool, and none ever will be — a secret value and a connection are the
deployment's to decide, not the model's.

### Inside your own chatbot

Your company has a chatbot already. Applace is what goes underneath it so its
users can ask it for an app — the tools, `get_skill` as the system prompt, and
the panel for the app itself.

```bash
uv run applace serve --http 8848 &
uv run examples/chat/chat.py --model mistral-large-latest   # then open :8900
```

`examples/chat/chat.py` is one readable file: a chat that builds apps and shows
them being built. [`docs/integrate.md`](docs/integrate.md) is the same thing as
instructions — the configuration lines for Claude Code, Cursor and an
OpenAI-compatible backend, the card's shape, the element's attributes, and what
to be careful about (loops cost money; the panel is read-only but not private;
secrets stay out of the conversation; let the humans merge).

### Design

Decisions are locked before code and recorded in [SPEC.md](SPEC.md) — that an
app is an ordinary repository (D1), that versioning is git and the remote is
GitHub (D2), that `write_files` is a compiler (D3), that a red write is accepted
but not committed (D4), that the gate is on exposure rather than on writing
(D6), that secrets never enter the model's context (D8), that a handover is a
branch and a pull request rather than a different repository (D15), that the chat
window is a read-only client of the same journal on the same port (D16). Read that document
before changing behaviour; it is also the roadmap.

### License

MIT.
