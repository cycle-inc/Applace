/**
 * The one place this app talks to the outside world.
 *
 * An Applace app has no server of its own: it calls HTTP APIs that already
 * exist. The base URL comes from an environment variable rather than being
 * written into the code, so the same build can point at staging or production.
 * Declare variables with `set_env`; never paste a secret into a source file --
 * anything a browser bundle holds is public.
 */

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`${status} ${body.slice(0, 200)}`)
    this.name = 'ApiError'
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      'content-type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    throw new ApiError(response.status, await response.text())
  }
  return (await response.json()) as T
}
