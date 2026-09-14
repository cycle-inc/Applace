---
name: applace
description: Build, preview and ship a web app with Applace. Read this before calling any other Applace tool.
---

# Building an app with Applace

Applace owns the repository, the build and the hosting. You write source files
and read what the build and the browser say about them. You never run `npm`,
never run `git`, and never edit files outside the app.

## The loop

1. `get_skill` — this document. Once per session.
2. `list_stacks` — what an app can be made of. Prefer a company stack over the
   generic one: it already holds the design system and the API client.
3. `create_app { name }` — takes a minute or two. It comes back with `app` (the
   slug you use everywhere afterwards) and `entry` (the file to start from).
4. `read_files { app, paths }` — read `entry` and anything you are about to
   change. Read before you write, always.
5. `write_files { app, files, message }` — **this is a compiler**. It
   type-checks, lints, builds, and then opens the built pages in a real
   browser. Green means it is committed and pushed. Red means nothing was
   committed, your files are still on disk, and `errors` holds the real tool's
   message with a file and a line. Fix and call again.
6. `add_route { app, path, selector, text }` — say which pages are the app and
   what each one must show, so step 5 checks it on every write (below).
7. `screenshot_app { app }` — look at the page yourself when the report is not
   enough.
8. Tell the human the preview URL and the GitHub URL — or just tell them to run
   `applace open <app>`, which opens whichever of them the app has.
9. `deploy_app { app }` when they want an address rather than a dev server.

## Rules that will save you a round trip

- **Write whole files.** `files` is `{path: content}` and the content replaces
  the file. There is no patch, no diff and no partial write.
- **One `write_files` per coherent change**, not per file. Send every file the
  change touches in one call: they are type-checked together.
- **Fix red before doing anything else.** A red app is dirty until it is green.
  Do not start a new feature on top of a failing build.
- **A company stack's own components are the point of it.** If the app has
  `src/ui/`, an API client, or anything else the stack shipped, read it and use
  it. Writing a second button from scratch is not neutral: it is the thing the
  stack exists to prevent. If `get_app` says `stack_drifted`, the app is older
  than the installed stack — that is normal, its files are the ones in the
  repository, and copying in the newer template is not your call to make.
- **Prefer what the stack already has.** Every dependency you add costs an
  install, is reported, and may be refused by this machine's policy. React,
  the router and the styling are already there. Check `package.json` before
  reaching for a library.
- **Some apps were here before Applace was.** If `get_app` says `taken_from`,
  this repository was written by a team and Applace was pointed at it later. The
  stack above describes how it is *built*, not how it is laid out: there may be
  no `src/App.tsx`, the conventions are the codebase's own, and its authors are
  still working in it. Read more of it than you would of an app you created, and
  match what is there rather than what this document shows.
- **A human may be in the repository too.** `get_app` reports `human`: commits
  somebody made by hand, and files they are editing right now. `write_files`
  replaces whole files, so it refuses to write one with uncommitted changes
  that Applace did not make — that is `stage: "handover"`, and it is not a bug
  to fix. Tell the human which file you need and let them commit or discard.
- **Never write a secret into source.** A browser bundle is public. Use
  `set_env` for configuration, and `use_api` for anything that needs a token
  (both below).
- **Do not touch** `node_modules/`, `dist/`, `.git/`, `package-lock.json`, or
  anything outside the app.

## The generic stack (`vite-react-ts`)

Vite + React 19 + TypeScript + Tailwind CSS 4. A single-page browser
application with no server of its own.

```
src/App.tsx       the entry point: start here
src/main.tsx      mounts App; you rarely touch it
src/lib/api.ts    the one place the app talks to an HTTP API
src/index.css     Tailwind; utility classes in the JSX, not a stylesheet
index.html        the document title lives here
package.json      dependencies
```

TypeScript is strict: type your props and your API responses, and `any` fails
the lint stage. An import you left behind is only a warning — the gate refuses
code that does not work, not code that is untidy.

The hooks are where this stack is strict. **Data you already have is a `const`,
not state.** `useState` is for something that changes while the page is open,
`useEffect` is for reaching outside React (a fetch, a timer, a subscription). A
page of fixed content needs neither, and calling a setter synchronously inside
an effect is a lint error, not a style opinion.

```tsx
type Service = { name: string; up: boolean }

const services: Service[] = [
  { name: 'Checkout', up: true },
  { name: 'Billing', up: false },
]

export default function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold">Status</h1>
      <ul className="mt-4 space-y-2">
        {services.map((service) => (
          <li key={service.name} className="flex items-center gap-2 rounded bg-white p-3">
            <span
              className={`h-3 w-3 rounded-full ${service.up ? 'bg-green-500' : 'bg-red-500'}`}
            />
            {service.name}
          </li>
        ))}
      </ul>
    </main>
  )
}
```

Calling an API that already exists is where the hooks belong. Note that the
effect hands the promise a setter instead of setting state in the effect body
itself, which is the form the linter accepts:

```tsx
import { useEffect, useState } from 'react'
import { api } from './lib/api'

type Team = { id: string; name: string }

export default function App() {
  const [teams, setTeams] = useState<Team[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api<Team[]>('/teams').then(setTeams).catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <p className="p-8 text-red-600">{error}</p>
  return (
    <main className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold">Teams</h1>
      <ul className="mt-4 space-y-2">
        {teams.map((team) => (
          <li key={team.id} className="rounded bg-white p-3 shadow-sm">{team.name}</li>
        ))}
      </ul>
    </main>
  )
}
```

Note what that example does that a broken page does not: it starts from an
empty array rather than `undefined`, and it renders the error instead of
throwing. `undefined.map` is the single most common way to ship a blank page.

## Secrets and configuration

You declare **names**; a human supplies **values**. You will never see a value,
and there is no tool that returns one.

```
set_env { app, name: "VITE_API_BASE_URL", description: "Base URL of the teams API" }
```

The response tells you the exact command for the human to run
(`applace env set <app> <NAME>`). Say it to them and carry on; the value
reaches the dev server and the build by itself. On this stack a variable must
start with `VITE_` to reach the browser — `set_env` warns you when it does not.
Read one with `import.meta.env.VITE_API_BASE_URL`.

## Calling an API that needs a credential

A token in front-end code is a published token: anyone who opens the page can
read it. So an app never holds one. Declare the API instead, and Applace proxies
the call and adds the credential server-side.

```
use_api {
  app,
  name: "crm",
  base_url: "https://crm.internal/api/v2",
  token_env: "CRM_TOKEN",
  paths: ["customers/**"],
  methods: ["GET"],
}
```

Then the app's own code fetches a **relative** path, and nothing else changes:

```ts
import { viaGateway } from './lib/api'

const customers = await viaGateway<Customer[]>('crm', 'customers?limit=20')
```

Four things follow from that, and they are all enforced rather than advised:

- `paths` and `methods` are an allowlist. `["GET"]` on `["customers/**"]` means
  a `DELETE`, or a call to `admin`, is refused by the proxy with a 403 your page
  can see. **Declare the narrowest thing that works** — you can always widen it.
- `token_env` is a **name**. The response carries `needs`: the command for the
  human to run. Relay it. Until they do, the call comes back 503 saying the
  credential has no value, which is a state to report, not to work around.
- **A `fetch` to an absolute URL you did not declare is a red gate.** The write
  is refused with `undeclared_calls` naming the file and the line. Declare it or
  remove it; there is no third option, and hard-coding a token to get past it is
  the one thing this whole mechanism exists to prevent.
- It works the same in production. The declaration compiles to an ordinary
  serverless function committed into the app's repository — a file a human can
  read, importing nothing from Applace.

`apis { app }` lists what the app may call. Use plain `api()` for a public API
that needs no credential; the gateway is for the ones that do.

### When the API answers differently for each person

`token_env` is one credential for everybody who opens the app. That is right for
an API where the app is the customer, and wrong for one where the *person* is —
an accountant must not see another cabinet's clients because they opened the
same page. For those, declare the exchange your company hosts instead of a
token:

```
use_api {
  app,
  name: "billing",
  base_url: "https://billing.internal/v1",
  on_behalf_of: { url: "https://auth.internal/applace/token", secret_env: "EXCHANGE_SECRET" },
  paths: ["invoices/**"],
  methods: ["GET"],
}
```

The app's code does not change: the same relative `fetch`, the same allowlist.
What changes is who the call is made as — Applace hands the caller's identity to
that endpoint and sends upstream the short-lived token it gets back.

- **The two are mutually exclusive.** Declaring both is refused. Ask the human
  which one their API expects if it is not obvious; "it returns different rows
  for different people" means `on_behalf_of`.
- **Never send an identity yourself.** Do not put a user id in the path, the
  query or the body to say who is asking, and do not build an `Authorization`
  header for a gateway call — the gateway sets that header itself, so yours is
  discarded, and an app that can name the subject is an app that can name
  somebody else. The identity comes from the page, not from your code.
- **A call with nobody behind it is a 401**, never a fallback to some other
  credential. Previewing works because the home knows whose it is; if the 401
  says it does not, that is a sentence for the human, not something to route
  around.

## The pages the gate opens

Every `write_files` ends in a real browser. The built app is served and each
route is opened: a page that throws, or that paints nothing, is a **red write**
that commits nothing, and the report carries the browser's own message with the
line of your source that produced it. A green build and a working page are two
different facts, and this is where the second one is checked.

With nothing declared, the gate opens `/`. Tell it what else is the app:

```
add_route { app, path: "/customers", selector: "[data-testid='customers'] li",
            text: "Acme", description: "The list everyone opens first" }
```

- `path` is a route on the app's own origin, nothing else: `/`, `/customers`,
  `/orders/recent`. A URL is refused (`code: "invalid-route"`).
- `selector` is a CSS selector that must match something on that page, and
  `text` a string the page must contain. Both are optional and both are checked
  on every write from then on — a change that quietly empties the list is red.
- Declare the page, not the implementation. A selector on a `data-testid` you
  put in the JSX survives restyling; one on `div > div > span` does not.
- `routes { app }` lists them, `drop_route { app, path }` removes one. There is
  no test file and no test framework: these are declarations, not code.

If the report says the visit was `skipped`, read the reason. It means this
machine has the check turned off — for a stack whose pages sit behind a login,
or by policy — and **a skipped check is not a passed one**. `get_app` says the
same thing under `visit`. Then look yourself: `screenshot_app` returns a picture
**and** a report — `blank`, the console `errors`, and `failed_requests`, where a
404 is usually an API path that does not exist or an asset never added.

## GitHub

If the machine has an organisation connected, the app is a repository there
from birth and every green write is pushed. You do not choose this and cannot
turn it off. In a `write_files` result, `github.pushed` says whether the commit
landed. `github.diverged: true` means a human pushed to the repository and
Applace refused to overwrite them — **say this to the human**, verbatim, and
stop. Do not try to work around it.

Some machines review. `get_skill` says so, and then `github.reviewed` is true
in every write result: your commits go to a branch of this app's own and
`github.pull_request_url` is a pull request waiting for a person. Give them
that URL. Merging it is their decision and not part of your task — keep
working on the same branch, the pull request updates itself.

## Shipping it

A preview URL is a dev server on someone's laptop. `deploy_app` is how an app
becomes something with an address.

```
deploy_app { app, target: "local" }                       # the default
deploy_app { app, target: "vercel" }                      # a preview URL
deploy_app { app, target: "vercel", environment: "production", confirm: true }
```

Three things to know before you call it.

**It deploys a commit, never your working tree.** If the last write went red,
what is live is the last green commit, and the deploy will tell you so in
`warnings`. Get to green first.

**Production is not yours to decide.** `environment: "production"` requires
`confirm: true`, and `confirm` means *a human said yes*, in this conversation,
about this deploy. You cannot supply it on your own reasoning, and a machine's
policy may refuse production outright (`code: "policy"`). Ask, relay, wait.

**Start with `target: "local"`.** It needs no account and runs the real
production build, which is stricter than the dev server — a page that works in
the preview and fails here is a page that would have failed on Vercel too.

When something goes wrong after a deploy, `rollback_app { app }` puts the
previous commit back. Reach for it before you start debugging: the broken commit
is still in git, and a human staring at a broken page is not.

## What this harness is not

It builds front ends that talk to APIs that already exist. There is no backend,
no database and no server runtime. If the human needs one, say so plainly
rather than inventing a mock and calling it done.
