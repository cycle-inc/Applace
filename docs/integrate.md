# Putting Applace inside your chatbot

You already have a chatbot. People ask it things, it answers, and now they want
it to *make* them something — a small internal page, a form, a dashboard over an
API that already exists. This is how Applace goes underneath that chatbot, and
how the app shows up in the conversation.

There are exactly three pieces, and the whole of this document is those three:

| The piece | What it is | Where it comes from |
| --- | --- | --- |
| **The tools** | create an app, write code, look at it, ship it | the MCP server, `/mcp` |
| **The manual** | what those tools are and what this machine allows | `get_skill`, as the system prompt |
| **The view** | the app, its build, its errors, its links, live | `/panel/embed.js`, one script tag |

`examples/chat/chat.py` is all three in one readable file: a chat window that
builds apps. Read it after this, or instead of it.

## 1. Start the harness

On a developer's laptop, or on one machine the chat backend can reach:

```bash
uvx --from git+https://github.com/cycle-inc/Applace applace init
applace github connect --org acme --review pr   # optional, and recommended
applace serve --http 8848
```

That one port now serves:

```
http://127.0.0.1:8848/mcp                        the tools, streamable HTTP
http://127.0.0.1:8848/panel                      a page listing every app
http://127.0.0.1:8848/panel/embed.js             the <applace-app> element
http://127.0.0.1:8848/panel/apps                 every card, as JSON
http://127.0.0.1:8848/panel/apps/<slug>          one card
http://127.0.0.1:8848/panel/apps/<slug>/events   that card, whenever it changes
http://127.0.0.1:8848/panel/apps/<slug>/shot.png the last screenshot taken
```

Everything under `/panel` is **read-only**: it reads the same journal the tools
write to. Nothing there creates an app, writes a file or deploys anything, so
the panel can be exposed to a chat UI without becoming a second way in with none
of the rules.

## 2. Give your agent the tools

**Claude Code**

```bash
claude mcp add --transport http applace http://127.0.0.1:8848/mcp
# or, with no server to run: claude mcp add applace -- uvx --from git+https://github.com/cycle-inc/Applace applace serve
```

**Cursor** — `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "applace": { "url": "http://127.0.0.1:8848/mcp" }
  }
}
```

**Your own backend** (anything that speaks the OpenAI chat-completions shape —
OpenAI, Mistral, Azure, Bedrock through a gateway, a local vLLM):

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async with streamable_http_client("http://127.0.0.1:8848/mcp") as (read, write, *_):
    async with ClientSession(read, write) as applace:
        await applace.initialize()

        offered = [                      # the tools, as your API expects them
            {"type": "function", "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }}
            for tool in (await applace.list_tools()).tools
        ]
        system = text_of(await applace.call_tool("get_skill", {}))
```

Two rules, and they are the difference between a demo and something that works
on the twentieth request:

- **Pass the tool schemas through unedited.** What an agent may do is the
  harness's decision, expressed in the policy on that machine. A chatbot that
  filters the list is a second policy that will disagree with the first.
- **Make `get_skill` the system prompt.** It returns the manual *and* the state
  of this machine: which stacks exist, whether apps are pushed to GitHub,
  whether production deploys need a human, where it can deploy. Hard-coding any
  of that into a prompt means maintaining it twice.

Then run the ordinary loop: model → tool calls → results → model. There is
nothing Applace-specific in it. The gate is what makes that loop safe — a write
that does not typecheck, lint and build comes back red with the errors and
nothing is committed, so the model's next turn is a fix rather than a fiction.

## 3. Show the app

The person who asked is not going to read a tool result. Put the app in the
conversation:

```html
<script src="http://127.0.0.1:8848/panel/embed.js"></script>

<applace-app slug="team-dashboard" base="http://127.0.0.1:8848" height="520">
</applace-app>
```

That is the whole integration. It is a custom element, not a React component: a
chat UI is somebody else's codebase with somebody else's bundler and somebody
else's React version, and one script tag is the only thing that survives all of
them. It has no dependencies, it renders in a shadow root so it cannot inherit
or leak styles, and it opens its own event stream.

| Attribute | Meaning |
| --- | --- |
| `slug` | the app, as every tool result names it (`"app": "team-dashboard"`) |
| `base` | where the harness is; omit it when the chat is served from the same origin |
| `height` | the preview's height in pixels (default 420) |
| `view` | `preview` (default, a live iframe), `shot` (the last screenshot), `none` |

Set `slug` when a tool result first names an app, and the element does the rest:
it draws the state, the sentence, the errors and the links, and keeps drawing
them as the build moves. It also emits `applace:card` with the raw card, so a
chat that wants to render its own thing can ignore the element's markup and keep
the plumbing.

A card looks like this:

```json
{
  "app": "team-dashboard",
  "name": "Team Dashboard",
  "state": "red",
  "headline": "The last write did not pass typecheck: 2 errors to fix.",
  "failed_stage": "typecheck",
  "errors": [{"file": "src/App.tsx", "line": 12, "message": "..."}],
  "urls": {
    "live": "https://team-dashboard.vercel.app",
    "preview": "http://127.0.0.1:5173",
    "repo": "https://github.com/acme/team-dashboard",
    "pull_request": "https://github.com/acme/team-dashboard/pull/1",
    "dir": "/Users/you/.applace/apps/team-dashboard"
  },
  "commit": "9f1c2ab", "branch": "applace/team-dashboard",
  "dirty": false, "unpushed": 0,
  "human": {"touched": true, "edits": ["src/App.tsx"]},
  "shot": "/panel/apps/team-dashboard/shot.png", "shot_at": 1757660000
}
```

`state` is one of `new`, `green`, `red`, `missing`. The stream at
`/panel/apps/<slug>/events` sends `event: card` with that object whenever
anything in it changes, `: still here` every fifteen quiet seconds so a proxy
does not close the connection, and `event: gone` if the app disappears.

The URLs matter more than they look. A pull request announced once, in a message
that scrolled away an hour ago, is a pull request nobody merged; the card still
has it tomorrow.

## 4. Say what is happening, in their words

A tool name is not an answer. Between the tool call and the chat bubble, put one
line of translation — `examples/chat/chat.py` has the whole mapping:

| Tool | What the chat says |
| --- | --- |
| `create_app` | Creating “Team Dashboard” |
| `write_files` | Writing 3 file(s), and building |
| `screenshot_app` | Looking at the page |
| `deploy_app` | Putting it online |
| `set_env` | Asking for the key VITE_API_BASE_URL |
| `rollback_app` | Putting the previous version back |

And tell the model, in your preamble, that the person can see the app: otherwise
it pastes files, URLs and error lists into the chat, all of which are already on
screen and none of which are readable there.

## 5. Before you let a company use it

- **Loops cost money.** A model with a build tool will retry. Cap the tool
  rounds per message and stop the turn when a dollar budget is reached; the
  example does both in twenty lines, and prints what it spent.
- **The panel is read-only, not private.** It inherits the trust of the machine
  it runs on: anyone who can reach the port can see the cards and the previews.
  Keep it on loopback, or behind the authentication your chat already has. Who
  may see which app is a question Applace does not answer yet, and pretending
  otherwise here would be worse than saying it.
- **Secrets stay out of the conversation.** An agent declares the *name* of a
  variable with `set_env`; a person supplies the value with `applace env set`.
  There is no tool argument for a value and no tool that returns one. Do not add
  one in your chatbot.
- **Let the humans merge.** With `--review pr`, everything an agent writes lands
  on a branch with one open pull request, and the card carries its URL. Nothing
  reaches `main` because a conversation went well.
- **One harness, one machine.** Applace is local-first; a shared instance for a
  whole company is not what this is yet. Per-team laptops or per-team boxes.

## 6. Run the example

```bash
applace serve --http 8848 &
uv run examples/chat/chat.py --model mistral-large-latest --budget 1.00
# then open http://127.0.0.1:8900
```

Ask it for "a page showing which of our three services are up". You will watch
it choose a stack, create the repository, write the code, fail a typecheck if it
is unlucky, fix it, and put the page in the panel beside the conversation — and
the whole of what you would have to write yourself is the file you just ran.
