/**
 * The Acme design system, as far as an app is concerned.
 *
 * Everything an Acme page is made of is exported from here. Import from
 * `./ui`, not from the files behind it: the day this becomes a published
 * package, that import is the only line that has to change.
 *
 * Do not restyle these components from the outside. If a page needs something
 * these do not do, that is a change to the design system, not a `className`
 * override on the way past.
 */

import type { ReactNode } from 'react'

export type Tone = 'neutral' | 'positive' | 'warning' | 'critical'

const TONES: Record<Tone, string> = {
  neutral: 'bg-slate-100 text-slate-700',
  positive: 'bg-emerald-100 text-emerald-800',
  warning: 'bg-amber-100 text-amber-800',
  critical: 'bg-rose-100 text-rose-800',
}

export function Page({ title, subtitle, children }: {
  title: string
  subtitle?: string
  children: ReactNode
}) {
  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-baseline gap-3 px-6 py-4">
          <span className="text-sm font-bold tracking-widest text-sky-700">ACME</span>
          <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
          {subtitle ? <p className="text-sm text-slate-500">{subtitle}</p> : null}
        </div>
      </header>
      <main className="mx-auto max-w-5xl space-y-4 px-6 py-8">{children}</main>
    </div>
  )
}

export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      {title ? (
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          {title}
        </h2>
      ) : null}
      {children}
    </section>
  )
}

export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  )
}

export function Button({ onClick, children, kind = 'primary' }: {
  onClick?: () => void
  children: ReactNode
  kind?: 'primary' | 'quiet'
}) {
  const style =
    kind === 'primary'
      ? 'bg-sky-700 text-white hover:bg-sky-800'
      : 'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50'
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${style}`}
    >
      {children}
    </button>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-sm font-medium text-slate-700">{label}</span>
      {children}
    </label>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-slate-500">{children}</p>
}

export function Failed({ error }: { error: string }) {
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
      {error}
    </div>
  )
}
