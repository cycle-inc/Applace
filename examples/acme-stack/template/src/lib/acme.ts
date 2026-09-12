/**
 * The Acme API client. The only way an Acme app talks to the outside world.
 *
 * Two things it does that a hand-written `fetch` does not: it sends the
 * gateway's tenant header on every call, and it turns Acme's error envelope
 * into an `AcmeError` with the `code` support will ask for. Use it for internal
 * APIs; `fetch` directly is for third parties, and there are very few of those.
 *
 * `VITE_ACME_API_URL` and `VITE_ACME_TENANT` are supplied by whoever runs the
 * app -- an agent declares the names with `set_env` and never sees the values.
 */

const BASE_URL: string = import.meta.env.VITE_ACME_API_URL ?? ''
const TENANT: string = import.meta.env.VITE_ACME_TENANT ?? ''

export class AcmeError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'AcmeError'
  }
}

type Envelope = { error?: { code?: string; message?: string } }

export async function acme<T>(path: string, init?: RequestInit): Promise<T> {
  if (!BASE_URL) {
    throw new AcmeError(0, 'not-configured',
      'VITE_ACME_API_URL is not set. Ask for it with set_env.')
  }
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      'x-acme-tenant': TENANT,
      ...(init?.headers ?? {}),
    },
  })
  const text = await response.text()
  if (!response.ok) {
    let code = 'unknown'
    let message = text.slice(0, 200)
    try {
      const body = JSON.parse(text) as Envelope
      code = body.error?.code ?? code
      message = body.error?.message ?? message
    } catch {
      // Not Acme's envelope -- a gateway or a proxy answered. Keep the body.
    }
    throw new AcmeError(response.status, code, message)
  }
  return (text ? JSON.parse(text) : null) as T
}

export const get = <T,>(path: string) => acme<T>(path)

export const post = <T,>(path: string, body: unknown) =>
  acme<T>(path, { method: 'POST', body: JSON.stringify(body) })
