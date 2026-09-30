/**
 * Google sign-in for Save to Drive, entirely in the browser (Google Identity Services token model).
 * The access token lives only in this module's memory for its one-hour lifetime; nothing is stored.
 */

const DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
const GSI_SRC = 'https://accounts.google.com/gsi/client'

type TokenResponse = { access_token?: string; expires_in?: number; error?: string; error_description?: string }
type TokenClient = { requestAccessToken: (overrides?: { prompt?: string }) => void }
type GoogleOAuth2 = {
  initTokenClient: (config: {
    client_id: string
    scope: string
    callback: (response: TokenResponse) => void
    error_callback?: (error: { type: string }) => void
  }) => TokenClient
  hasGrantedAllScopes: (response: TokenResponse, scope: string) => boolean
}
declare global {
  interface Window { google?: { accounts: { oauth2: GoogleOAuth2 } } }
}

let clientId: string | null = null
let cached: { token: string; expiresAt: number } | null = null
let pending: { resolve: (token: string) => void; reject: (error: Error) => void } | null = null
let tokenClient: TokenClient | null = null

function settle(outcome: { token: string } | { error: string }) {
  const waiting = pending
  pending = null
  if (!waiting) return
  if ('token' in outcome) waiting.resolve(outcome.token)
  else waiting.reject(new Error(outcome.error))
}

/** Load Google's script and the client ID ahead of time, so the click can open the popup immediately. */
export async function prepareGoogleSignIn(): Promise<void> {
  if (!document.querySelector(`script[src="${GSI_SRC}"]`)) {
    const script = document.createElement('script')
    script.src = GSI_SRC
    script.async = true
    document.head.appendChild(script)
  }
  const response = await fetch('/api/drive/config')
  const config = await response.json().catch(() => null)
  clientId = config?.client_id ?? null
}

function client(): TokenClient {
  if (tokenClient) return tokenClient
  const oauth2 = window.google?.accounts.oauth2
  if (!clientId) throw new Error('Google sign-in is not configured: set GOOGLE_CLIENT_ID for the backend (see DEVELOPMENT.md).')
  if (!oauth2) throw new Error('Google sign-in has not loaded yet. Check your connection and try again.')
  tokenClient = oauth2.initTokenClient({
    client_id: clientId,
    scope: DRIVE_SCOPE,
    callback: response => {
      if (response.error || !response.access_token) {
        settle({ error: response.error_description || 'Google sign-in did not complete.' })
      } else if (!oauth2.hasGrantedAllScopes(response, DRIVE_SCOPE)) {
        settle({ error: 'Allow access to Google Drive to save images there.' })
      } else {
        cached = { token: response.access_token, expiresAt: Date.now() + (response.expires_in ?? 3600) * 1000 }
        settle({ token: response.access_token })
      }
    },
    error_callback: error => settle({
      error: error.type === 'popup_closed' ? 'Google sign-in was closed before it finished.'
        : error.type === 'popup_failed_to_open' ? 'Allow pop-ups for this site to sign in with Google.'
          : 'Google sign-in failed.',
    }),
  })
  return tokenClient
}

/**
 * An access token for the user's Drive. Call directly from a click handler: the popup opens
 * synchronously so the browser does not block it. Reuses the token until a minute before it expires.
 */
export function getDriveToken(): Promise<string> {
  if (cached && cached.expiresAt - 60_000 > Date.now()) return Promise.resolve(cached.token)
  let tokens: TokenClient
  try {
    tokens = client()
  } catch (cause) {
    return Promise.reject(cause)
  }
  settle({ error: 'A newer sign-in replaced this one.' })
  return new Promise((resolve, reject) => {
    pending = { resolve, reject }
    // An empty prompt skips the consent screen when this user already granted access.
    tokens.requestAccessToken({ prompt: '' })
  })
}

/** Forget the token, e.g. after the backend reports it expired or was revoked. */
export function clearDriveToken() {
  cached = null
}
