# {{app_name}}

{{app_description}}

An Acme internal web app: Vite, React, TypeScript and Tailwind, with the Acme
design system in `src/ui` and the Acme API client in `src/lib/acme.ts`.

```bash
npm install
npm run dev        # http://localhost:5173
npm run typecheck
npm run lint
npm run build      # -> dist/
```

It needs two values, which are not committed anywhere:

| Variable | What it is |
|---|---|
| `VITE_ACME_API_URL` | Base URL of the Acme gateway |
| `VITE_ACME_TENANT` | The tenant this instance belongs to |

Created by [Applace](https://github.com/cycle-inc/Applace) from the `acme-web`
stack, and an ordinary repository regardless: nothing here depends on Applace
being installed.
