# A company's chatbot, with Applace underneath it

One file, `chat.py`: a chat window that takes "make me a page showing which of
our three services are up", builds the app, and shows it next to the
conversation while it is being built.

It exists to be read. Everything that is specific to Applace is three things —
the tools come from the MCP server unedited, the system prompt is `get_skill`,
and the app is drawn by `<applace-app>` from `/panel/embed.js`. The rest is the
ordinary chat loop you already have.

## Run it

```bash
applace serve --http 8848 &                                  # the harness
uv run examples/chat/chat.py --model mistral-large-latest    # the chat
```

Then open <http://127.0.0.1:8900>. The panel on the other half of the window is
served by the harness itself, on the same port as the tools.

The API key is read from the environment (`MISTRAL_API_KEY`, `OPENAI_API_KEY`)
or from the file given by `--env-file`, which defaults to `~/.env`. It is never
printed and never reaches an app.

```bash
uv run examples/chat/chat.py --provider openai --model gpt-4.1-mini
uv run examples/chat/chat.py --provider custom \
    --base https://llm.acme.internal/v1 --key-name ACME_LLM_KEY --model acme-large
```

`--budget` (default $2) ends a turn the moment the usage the API reports crosses
it, and `ROUNDS` in the file caps the tool calls per message. A model with a
build tool retries; both of those are there so a demo cannot spend a fortune
while you are at lunch.

## What it is not

One conversation, in memory, no users, no database, no authentication — a
company's chatbot already has all of that, and none of it is what Applace adds.
[`docs/integrate.md`](../../docs/integrate.md) is the same story written as
instructions, including what to be careful about before letting a company use
it.
