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
- **The agent has eyes, and the gate uses them.** A build going green and a
  page rendering are two different facts. Every write ends with the built app
  opened in a real browser: a page that throws or paints nothing is a red write
  that commits nothing, reported with the browser's own message and the line of
  your source that produced it.
- **A stack is company property.** The design system, the auth wiring and the
  internal API client live in a versioned stack, and every app starts from one.
  That is the difference between *a* Lovable and *our* Lovable.

### Status

**M1 to M16 are shipped** — every milestone in the spec: the store (apps as git
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
embeds), the package (`from applace import Applace`, the same behaviours as
a Python class, with the MCP server rewritten as a skin over it), the
machine (one home per person under a root, ports nobody can collide on, quotas
that refuse instead of queueing, and a collection that stops processes without
deleting anybody's code), and the gateway (an app reads the company's own APIs
through a proxy that holds the credential, in preview from a process Applace
runs and in production from a serverless function committed to your repository,
so nothing a browser downloads ever holds a token), the sensor (the gate
ends in a browser: every declared route is opened, and a white screen under a
green build is a red write), the take-over (`applace take` makes a front end
that already exists into an app without writing a single byte into it), and the
register (the developer who runs the machine sees every home's apps, every
deploy with the human who confirmed it and what it all cost, through journals
opened read-only and without opening a single app's files).
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

### Take over a front end you already have

Your company's apps were not all created by Applace, and the way in cannot be
"rewrite it here". `take` points Applace at a repository that exists:

```bash
uv run applace take ~/work/customer-board            # a checkout on this machine
uv run applace take https://github.com/acme/board    # or a URL to clone
uv run applace take ~/work/customer-board --dry-run  # say what it would do
```

The stack is recognised from the manifest — each stack declares in its
`stack.yaml` what an app of its kind depends on — and a tree nothing recognises
is **refused, naming what was looked for**, rather than built with a guess you
would then have to debug. Name one yourself with `--stack` when you know better.

Nothing is written into the repository: no config key, no marker file, no
import. The history, the working tree and the remote are exactly as they were,
a local checkout stays where it is, and the app is a row in Applace's database —
delete it and the repository will not know anything happened. From that moment
it has a journal, a gate, previews and deploys like any other app.

The first gate runs once as a **report**: it type-checks, lints, builds and
opens the app, tells you what it found, and commits nothing. A codebase that is
already red is taken over all the same — refusing red would refuse every real
codebase in the building.

Not to be confused with `applace github adopt`, which points an app Applace
already has at an existing GitHub repository.

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

A stack can also say what an app of its kind looks like, which is how `applace
take` recognises the front ends you already have — and how a company says "the
repositories using our design system are mine":

```yaml
recognise:
  dependencies: [vite, '@acme/ui']
```

The most specific claim wins, so a company stack beats the generic one it is
built on. A stack that says nothing is never recognised.

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

### Calling your company's APIs

A token in front-end code is a published token. So an app never holds one: the
agent declares the API, and the app fetches a relative path.

```
use_api { app, name: "crm", base_url: "https://crm.internal/api/v2",
          token_env: "CRM_TOKEN", paths: ["customers/**"], methods: ["GET"] }
```

```ts
const customers = await viaGateway<Customer[]>('crm', 'customers')
```

In preview, Applace runs a gateway beside the dev server: it adds the credential
server-side, forwards only the paths and methods that were declared, and refuses
the rest with a reason. On deploy, the same declaration is compiled into an
ordinary serverless function committed to **your** repository — no import from
Applace, nothing to keep if you drop the harness. A `fetch` at a host nobody
declared is a red gate naming the file and the line, and `policy.yaml` can
narrow which hosts this machine will proxy to at all:

```yaml
apis:
  hosts: ["*.internal", "api.acme.com"]
```

```bash
uv run applace api ls team-dashboard    # what it may call, and whether the token is set
uv run applace api gateway              # the process holding the credentials
```

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
apis:
  hosts: ["*.internal"]                 # which upstreams the gateway will proxy to
gate:
  visit: false                          # off only if your apps cannot be opened anonymously
```

```bash
uv run applace policy       # what is in force
uv run applace skill        # what an agent is told, and what this machine is
```

### What the page must show

The last stage of the gate is a browser. With nothing declared it opens `/`; an
agent says what else is the app, and what each page has to show.

```
add_route { app, path: "/customers", selector: "[data-testid='customers'] li",
            text: "Acme" }
```

From then on every write serves the real build, opens that route, and goes red
if the page throws, renders nothing, or stops showing what was declared —
nothing is committed, and the error names a line of `src/App.tsx` rather than of
a minified chunk. The declarations live in the journal: no test file and no test
framework enters your repository.

```bash
uv run applace route ls team-dashboard          # what gets opened, and what it must show
uv run applace route add team-dashboard /orders --shows "table tbody tr"
```

An app whose every page is behind a login cannot be opened anonymously. Turn the
check off for a stack (`visit: false` in `stack.yaml`) or for the machine (`gate:
visit: false` in `policy.yaml`) — and it is then reported as skipped, with the
reason, everywhere it would otherwise look green.

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
`get_app`, `read_files`, `write_files`, `set_env`, `use_api`, `apis`,
`drop_api`, `add_route`, `routes`, `drop_route`, `start_preview`,
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
catch anything. The agent's sixteen methods mirror the tools one for one —
`skill`, `stacks`, `create`, `apps`, `app`, `read`, `write`, `declare_env`,
`declare_api`, `apis`, `forget_api`, `preview`, `stop_preview`, `shot`,
`deploy`, `rollback` — and the MCP server
calls exactly those, so the two doors cannot answer the same question
differently.

The class also has what an agent must never have (D19): `set_env` with a
*value*, `connect_github`, `connect_vercel`, `push`, `adopt`, `take`,
`install_stack`, `remove`, and the panel's `card`/`cards` without the HTTP.
`take` is on that list deliberately: pointing Applace at a path on somebody's
disk is a decision about which codebase this is, not a step in building it.
None of those is a
tool, and none ever will be — a secret value and a connection are the
deployment's to decide, not the model's.

### For more than one person

A backend serving a company serves thousands of people. Each of them gets a
whole Applace home under one root (D20) — their own database, their own
repositories, their own secrets at `0700` — and the root arbitrates what there
is only one of: this machine's ports and this machine's disk (D21).

```python
from applace import Machine

machine = Machine("/srv/applace")

def handle(request):                       # in your own web handler
    ap = machine.user(request.user.id)     # made on first use, cheap after
    return ap.create(request.json["name"])
```

Applace authenticates nobody: you say who the user is, having already asked. The
id is opaque — an email, a UUID, a display name in any script — and it is never
parsed as a path.

Limits are per home and refuse rather than queue (D22), which is also where the
cap that stops a model looping at your expense belongs:

```yaml
# /srv/applace/machine.yaml -- absent means the defaults, which are generous
limits:
  apps: 20
  previews: 2
  disk_mb: 4000
  writes_per_hour: 240
  deploys_per_hour: 30
  shots_per_hour: 300
```

A refusal is an ordinary answer — `{"ok": false, "code": "quota", …}` or
`{"ok": false, "code": "rate-limit", "retry_after": 812, …}` — so a chat window
can say "not right now, and here is why" instead of hanging.

Whoever is on call gets the same view from the terminal, and a `gc` that is safe
on a cron: it stops idle dev servers, frees their ports and removes scratch
checkouts, and never deletes an app, a repository, a secret or a home (D23).

```bash
uv run applace machine users --root /srv/applace
uv run applace machine ports --root /srv/applace
uv run applace machine gc    --root /srv/applace --preview-hours 2
```

### Seeing what is on the machine

Everything above is one person's view. The developer who runs the machine has
the other one (D27): what exists here, whose it is, what was allowed to happen,
and what it cost — across every home at once.

```bash
uv run applace ls --json                                   # one home, in detail
uv run applace machine apps  --root /srv/applace           # every home's apps
uv run applace machine audit --root /srv/applace --since 2026-09-01
uv run applace machine spend --root /srv/applace --hours 24
uv run applace machine panel --root /srv/applace           # and in a browser
```

```
alice@example.com   sales-board   vite-react-ts  green  9f3c1a8d2b04  http://127.0.0.1:5287/
bob@example.com     ops-notes     vite-react-ts  red    4b1e07c5aa19
carol@example.com   legacy-board  vite-react-ts  green  7d2f9c4e1103
                    taken from /srv/teams/legacy-board
```

Every journal is opened **read-only**, and none of this opens an app's files —
no `git`, no `stat`, no walk of a `node_modules`. It is safe to run while people
are working and it stays fast on a machine holding hundreds of apps. `ls` in a
home and `machine apps` share one definition of `green`, `red` and `new`, so the
two views cannot disagree about the same app.

`audit` is the one an auditor asks for: apps created or taken over, every deploy
with the human confirmation that permitted it (D6), every upstream an app was
allowed to call, and every write a policy refused. No secret's value has ever
been in a journal, so none can be in the export. `spend` reports two numbers
side by side rather than adding them — the ledger's rolling window, which is
what the limits refuse against and is forgotten after a week, and the journal's
total, which goes back to the day each app was created.

`machine panel` is the M10 chat card mounted one level up: an index of homes,
and every home's apps in a browser. It binds loopback because it authenticates
nobody. In production your own backend mounts it inside the authentication it
already has, and says who may see whose work:

```python
from applace import Machine, panel

machine = Machine("/srv/applace")

def visible(request, user):                 # your session, your rules
    return request.user.is_staff or request.user.id == user

routes = panel.machine_routes(machine, visible=visible)   # mount in your app
```

A home the callable refuses answers **404, not 403**: a 403 confirms the person
exists, and on a machine whose directory names come from email addresses that is
a staff directory for anyone who can guess. The same three answers are on the
class — `machine.apps()`, `machine.audit()`, `machine.spend()` — and every CLI
command above takes `--json` and `--user`.

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
