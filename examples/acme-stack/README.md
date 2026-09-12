# acme-web — an example company stack

This is what a company's own stack looks like: a git repository holding
`stack.yaml` and a `template/` tree, carrying the three things that make an
agent's output *ours* rather than generic.

- **A design system.** `template/src/ui/index.tsx` — `Page`, `Card`, `Badge`,
  `Button`, `Field`, `Empty`, `Failed`. The first page an app is born with uses
  them, so a model has an example of the house components instead of a blank
  file and a free hand with Tailwind.
- **An internal API client.** `template/src/lib/acme.ts` — the gateway's tenant
  header on every call, the company's error envelope turned into an `AcmeError`
  with the `code` support asks for, and the base URL read from an environment
  variable an agent declares but never sees (D8).
- **The house rules.** The lint config, the tsconfig and the scripts the gate
  runs. Whatever this repository enforces, every app built from it enforces.

## Try it

The stack is installed from git, so make this directory a repository first —
in real life it is a repository in your organisation and you skip this step.

```bash
cd examples/acme-stack && git init -q && git add -A && git commit -qm "The Acme stack"
applace stacks add "file://$PWD"
applace stacks
applace new "Billing Portal" --stack acme-web
```

Every app created from it records the commit it was born at. When the stack
moves, `applace stacks update acme-web` moves what *new* apps start from, and
`applace stacks drift` lists the apps that are older than it. Existing apps are
never rewritten: their files are committed in their own repositories, which is
where an agent's and a human's work both live.
