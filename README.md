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

**M1 — the store.** Creating apps from a stack, storing them as git
repositories, and exposing that to an MCP host. The compiler (`write_files`),
the preview, the screenshots, GitHub and deployment are specified in
[SPEC.md](SPEC.md) and not yet built. The milestone list there is the roadmap,
and it is followed in order.

### Try it

```bash
uv sync
uv run applace init
uv run applace new "Team Dashboard"
uv run applace ls
```

`init` reports whether this machine has what a stack needs (`git`, `node`,
`npm`). `new` renders the stack, installs it, and makes the first commit. What
comes out is a plain Vite + React + TypeScript + Tailwind repository:

```bash
cd ~/.applace/apps/team-dashboard
npm run dev
```

### As an MCP server

```bash
uv run applace serve            # stdio
uv run applace serve --http 8848
```

Tools available today: `list_stacks`, `create_app`, `list_apps`, `get_app`.

### Design

Decisions are locked before code and recorded in [SPEC.md](SPEC.md) — that an
app is an ordinary repository (D1), that versioning is git and the remote is
GitHub (D2), that `write_files` is a compiler (D3), that a red write is accepted
but not committed (D4), that the gate is on exposure rather than on writing
(D6), that secrets never enter the model's context (D8). Read that document
before changing behaviour; it is also the roadmap.

### License

MIT.
