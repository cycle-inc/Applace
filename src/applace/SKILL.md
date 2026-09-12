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
   type-checks, lints and builds. Green means it is committed and pushed. Red
   means nothing was committed, your files are still on disk, and `errors`
   holds the real tool's message with a file and a line. Fix and call again.
6. `screenshot_app { app }` — look at the page. A build going green and a page
   rendering are two different facts.
7. Tell the human the preview URL and the GitHub URL — or just tell them to run
   `applace open <app>`, which opens whichever of them the app has.
8. `deploy_app { app }` when they want an address rather than a dev server.

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
- **A human may be in the repository too.** `get_app` reports `human`: commits
  somebody made by hand, and files they are editing right now. `write_files`
  replaces whole files, so it refuses to write one with uncommitted changes
  that Applace did not make — that is `stage: "handover"`, and it is not a bug
  to fix. Tell the human which file you need and let them commit or discard.
- **Never write a secret into source.** A browser bundle is public. Use
  `set_env` (below).
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

## Looking at the page

`screenshot_app` returns a picture **and** a report. Read the report first:

- `blank: true` — the page painted no text. Something threw before it rendered.
- `errors` — the browser console. `TypeError: Cannot read properties of
  undefined` with a file and a line is a real bug, and the line is real.
- `failed_requests` — a 404 here is usually an API path that does not exist or
  an asset that was never added.

A green build with a blank page is a normal state, and it is the one thing only
the browser can tell you. Check it before you say an app is finished.

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
