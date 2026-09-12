# A company's chatbot, with Applace underneath it

One file, `chat.py`: a chat window that takes "make me a page showing which of
our three services are up", builds the app, and shows it next to the
conversation while it is being built.

It exists to be read. Everything that is specific to Applace is four things —
the tools come from the MCP server unedited, the system prompt is `get_skill`,
the screenshot a tool returns goes back into the conversation *as an image*, and
the app is drawn by `<applace-app>` from `/panel/embed.js`. The rest is the
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
uv run examples/chat/chat.py --provider openai --model gpt-5.5
uv run examples/chat/chat.py --provider openai-responses --model gpt-6-astra
uv run examples/chat/chat.py --provider google --model gemini-3.8-flash
uv run examples/chat/chat.py --provider custom \
    --base https://llm.acme.internal/v1 --key-name ACME_LLM_KEY --model acme-large
```

Three vendors, one harness. Two of them need a note:

- **OpenAI's newest models take function tools on `/responses` only** —
  `/chat/completions` answers 400. `--api responses` (what the
  `openai-responses` provider sets) speaks that shape: `input` instead of
  `messages`, `function_call_output` instead of `role: "tool"`, `input_image`
  instead of `image_url`. Everything else in the file is shared.
- **Gemini speaks the OpenAI shape** at
  `https://generativelanguage.googleapis.com/v1beta/openai`, with
  `GEMINI_API_KEY`. No second client.

`--budget` (default $2) ends a turn the moment the usage the API reports crosses
it, and `ROUNDS` in the file caps the tool calls per message. A model with a
build tool retries; both of those are there so a demo cannot spend a fortune
while you are at lunch.

## The eyes have to reach the model

`screenshot_app` answers with a report *and* a PNG. Joining the text blocks and
dropping the image is the easy mistake: the model then reads "0 console errors"
and tells you the page is fine. `picture_of()` pulls the PNG out of the tool
result and `Talk.see()` puts it in the next message, because no API accepts an
image inside a tool result. With the image in context a model looks at its own
chart and says "aucune barre n'apparaît"; without it, it does not.

The counterpart is `EYES = 3`: providers cap the images in one request (Mistral
refuses the ninth, mid-turn, as a 400) and an old capture is not what the page
looks like now, so older ones are replaced by a line of text.

## What it is not

One conversation, in memory, no users, no database, no authentication — a
company's chatbot already has all of that, and none of it is what Applace adds.
[`docs/integrate.md`](../../docs/integrate.md) is the same story written as
instructions, including what to be careful about before letting a company use
it.
