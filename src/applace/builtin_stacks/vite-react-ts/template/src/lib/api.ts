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
  return request<T>(`${BASE_URL}${path}`, init)
}

/**
 * Call an API that needs a credential, through Applace's gateway.
 *
 * `name` is what `use_api` declared. The URL stays relative on purpose: the
 * token is added on the way out, by a process in development and by a function
 * in production, and neither one is anything this bundle can read.
 */
export async function viaGateway<T>(
  name: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  return request<T>(`/api/gateway/${name}/${path.replace(/^\//, '')}`, init)
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
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
