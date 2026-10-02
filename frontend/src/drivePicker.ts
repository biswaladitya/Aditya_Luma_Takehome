/**
 * Import a catalog from Google Drive, entirely in the browser: Google Picker lets the user choose one CSV
 * or Google Sheet, and the file is downloaded with the same drive.file token Save to Drive uses. The bytes
 * then go through the normal CSV preview, so the backend validates them exactly like a local upload.
 */

import { loadDriveConfig, loadedDriveConfig, type DriveConfig } from './googleAuth'

const GAPI_SRC = 'https://apis.google.com/js/api.js'
const CSV = 'text/csv'
const SHEET = 'application/vnd.google-apps.spreadsheet'
const DRIVE_FILES = 'https://www.googleapis.com/drive/v3/files/'

export type PickedFile = { id: string; name: string; mimeType: string }

// The small part of Google's Picker API used here (loaded at runtime from apis.google.com).
type PickerDoc = { id: string; name: string; mimeType: string }
type PickerResponse = { action: string; docs?: PickerDoc[] }
type DocsView = {
  setMimeTypes: (mimeTypes: string) => DocsView
  setIncludeFolders: (include: boolean) => DocsView
  setSelectFolderEnabled: (enabled: boolean) => DocsView
}
type Picker = { setVisible: (visible: boolean) => void; dispose: () => void }
type PickerBuilder = {
  addView: (view: DocsView) => PickerBuilder
  setOAuthToken: (token: string) => PickerBuilder
  setDeveloperKey: (key: string) => PickerBuilder
  setAppId: (appId: string) => PickerBuilder
  setTitle: (title: string) => PickerBuilder
  setCallback: (callback: (response: PickerResponse) => void) => PickerBuilder
  build: () => Picker
}
export type GooglePicker = {
  DocsView: new (viewId?: string) => DocsView
  PickerBuilder: new () => PickerBuilder
  ViewId: { DOCS: string }
  Action: { PICKED: string; CANCEL: string }
}
type Gapi = { load: (library: string, options: { callback: () => void; onerror: () => void }) => void }
declare global {
  interface Window { gapi?: Gapi }
}

/** The download was refused because the token expired or was revoked: clear it and sign in again. */
export class DriveSignInError extends Error {
  constructor() {
    super('Your Google sign-in has expired. Click Fetch from Google Drive again to sign in.')
  }
}

let pickerReady: Promise<void> | null = null

function loadScript(): Promise<void> {
  if (window.gapi) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = GAPI_SRC
    script.async = true
    script.onload = () => resolve()
    script.onerror = () => {
      script.remove()
      reject(new Error('Google Drive’s file picker could not load. Check your connection and try again.'))
    }
    document.head.appendChild(script)
  })
}

/** Load Google's Picker ahead of time, so a click opens it without waiting. A failed load is retried on the next call. */
export function preparePicker(): Promise<void> {
  pickerReady ??= loadScript()
    .then(() => new Promise<void>((resolve, reject) => window.gapi!.load('picker', {
      callback: resolve,
      onerror: () => reject(new Error('Google Drive’s file picker could not load. Check your connection and try again.')),
    })))
    .catch(cause => {
      pickerReady = null
      throw cause
    })
  return pickerReady
}

function missingSetting(config: DriveConfig): string | null {
  if (!config.api_key) return 'Fetching from Google Drive is not configured: set GOOGLE_CLOUD_API_KEY for the backend (see DEVELOPMENT.md).'
  if (!config.app_id) return 'Fetching from Google Drive is not configured: GOOGLE_CLIENT_ID must be a Web application client ID that starts with the project number (see DEVELOPMENT.md).'
  return null
}

/**
 * Throw if the loaded config cannot open Picker. Synchronous, so a click can check it before the sign-in
 * popup opens; if the config has not loaded yet, `pickCatalogFile` checks it instead.
 */
export function checkPickerConfigured() {
  const config = loadedDriveConfig()
  const missing = config && missingSetting(config)
  if (missing) throw new Error(missing)
}

/** Open Google Picker for one CSV or Google Sheet. Resolves `null` when the user cancels. */
export async function pickCatalogFile(token: string): Promise<PickedFile | null> {
  const [config] = await Promise.all([loadDriveConfig(), preparePicker()])
  const missing = missingSetting(config)
  if (missing) throw new Error(missing)
  const picker = window.google?.picker
  if (!picker) throw new Error('Google Drive’s file picker could not load. Check your connection and try again.')
  return new Promise(resolve => {
    // Folders are shown for browsing but cannot be picked; multi-select is off, so one file comes back.
    const view = new picker.DocsView(picker.ViewId.DOCS)
      .setMimeTypes(`${CSV},${SHEET}`)
      .setIncludeFolders(true)
      .setSelectFolderEnabled(false)
    const dialog = new picker.PickerBuilder()
      .addView(view)
      .setOAuthToken(token)
      .setDeveloperKey(config.api_key!)
      // The app ID makes picking grant drive.file access to the chosen file; without it the download is a 404.
      .setAppId(config.app_id!)
      .setTitle('Choose a catalog CSV or Google Sheet')
      .setCallback(response => {
        if (response.action === picker.Action.PICKED) {
          const doc = response.docs?.[0]
          dialog.dispose()
          resolve(doc ? { id: doc.id, name: doc.name, mimeType: doc.mimeType } : null)
        } else if (response.action === picker.Action.CANCEL) {
          dialog.dispose()
          resolve(null)
        }
      })
      .build()
    dialog.setVisible(true)
  })
}

/** Download the picked file as CSV (a Sheet exports its first tab), named `.csv` so the preview accepts it. */
export async function downloadCatalogFile(token: string, file: PickedFile): Promise<File> {
  const id = encodeURIComponent(file.id)
  const url = file.mimeType === SHEET ? `${DRIVE_FILES}${id}/export?mimeType=${encodeURIComponent(CSV)}` : `${DRIVE_FILES}${id}?alt=media`
  let response: Response
  let blob: Blob
  try {
    response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    if (response.ok) blob = await response.blob()
  } catch {
    throw new Error('Could not reach Google Drive to download this file. Check your connection and try again.')
  }
  if (response.status === 401) throw new DriveSignInError()
  if (response.status === 403 || response.status === 404) {
    throw new Error('Google Drive did not allow access to this file. Pick it again, or ask its owner to share it with you.')
  }
  if (!response.ok) throw new Error(`Google Drive returned ${response.status} while downloading this file. Try again.`)
  const name = /\.csv$/i.test(file.name) ? file.name : `${file.name}.csv`
  return new File([blob!], name, { type: CSV })
}
