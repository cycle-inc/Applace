import { Badge, Card, Empty, Page } from './ui'

/**
 * {{app_name}}
 *
 * Start here. The page below uses the Acme design system and nothing else --
 * copy its shape rather than writing Tailwind by hand, and reach for
 * `acme<T>('/path')` from `./lib/acme` when this page needs real data.
 */

type Step = { label: string; done: boolean }

const steps: Step[] = [
  { label: 'The Acme design system is in src/ui', done: true },
  { label: 'The Acme API client is in src/lib/acme.ts', done: true },
  { label: 'Replace this page with the real one', done: false },
]

export default function App() {
  return (
    <Page title="{{app_name}}" subtitle="{{app_description}}">
      <Card title="Getting started">
        <ul className="space-y-2">
          {steps.map((step) => (
            <li key={step.label} className="flex items-center justify-between">
              <span className="text-slate-700">{step.label}</span>
              <Badge tone={step.done ? 'positive' : 'neutral'}>
                {step.done ? 'ready' : 'to do'}
              </Badge>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Data">
        <Empty>Nothing is loaded yet. This app talks to Acme APIs that already exist.</Empty>
      </Card>
    </Page>
  )
}
