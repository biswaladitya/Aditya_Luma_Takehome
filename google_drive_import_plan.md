# Google Drive CSV import: implementation plan

Status: implemented (2026-10-01), chunks 2–5; chunk 1 (Google Cloud setup) is manual, and the
chunk 6 manual browser checks still need a person signed in to Google. This adds a second way to start a CSV import: a **Fetch from
Google Drive** button next to **Choose CSV** on the Import CSV page. The user picks a file in
Google's own file browser (Google Picker). Its contents then go through the existing preview →
review → **Accept changes** flow unchanged. Uploading still spends no generation credits.

This is a one-time import of a chosen file, not the live two-way Sheets sync that `AGENTS.md`
assigns to intent consolidation.

## How it works

Google Picker is a JavaScript widget that runs **in the browser**. The backend cannot open it
or show it, and it never needs to talk to Google for the picking itself. The backend's part is
to hand the browser the public IDs Picker needs, which the page already fetches on load.

```
Page load ──GET /api/drive/config──▶ Backend        returns client_id, api_key, app_id
   │                                                (prefetched, so the click is instant)
   │
Click "Fetch from Google Drive"
   │ 1. getDriveToken()            ──▶ accounts.google.com   sign-in popup only if no cached token
   │ 2. Picker (token + api_key + app_id) ──▶ Google Picker  user browses folders, picks one file
   │                                                         picking grants drive.file access to that file
   │ 3. download the picked file   ──▶ www.googleapis.com/drive/v3
   │        CSV:   GET files/{id}?alt=media
   │        Sheet: GET files/{id}/export?mimeType=text/csv   (first tab only)
   │ 4. wrap bytes as File("<name>.csv")
   │
   └─POST /api/catalog/preview (multipart, unchanged)──▶ Backend: same parse, diff and pending preview
```

### Why the button does not call the backend first

- **Popup blocking.** Browsers allow a popup only while handling the click itself. Waiting on a
  backend request first can get Google's sign-in popup blocked. That is why
  `prepareGoogleSignIn()` already loads the config on page load, and Picker follows the same
  rule.
- **Picker is browser-only.** The backend cannot drive it. It can only provide the IDs.
- **The backend already validates.** The downloaded bytes go to the existing
  `POST /api/catalog/preview`, which applies the `.csv`, size, encoding and column checks.
  Validation stays in one place.

### Alternative considered: the backend downloads the file

The browser would send `{file_id, access_token}` to a new `POST /api/catalog/preview-from-drive`,
and `backend/drive.py` would download and parse the file, like Save to Drive does. That adds a
route, puts a token on the server, and adds Drive download code and tests, all to receive the
same bytes. It would only be worth it to record the Drive file ID on the import or to enforce a
size limit before downloading. **Recommendation: download in the browser.**

## Chunks

### Chunk 1: Google Cloud setup (manual, once)

1. In the project that owns `GOOGLE_CLIENT_ID`, enable the **Google Picker API**. The Drive API
   is already enabled for Save to Drive.
2. Restrict `GOOGLE_CLOUD_API_KEY` to the Google Picker API, and its HTTP referrers to
   `http://localhost:5173/*`, `http://localhost:8000/*` and the deployed URL. The key reaches the browser, so these
   restrictions are what protect it.
3. No new OAuth scope or consent-screen change. With `drive.file`, a file the user picks in
   Picker becomes accessible to the app; other files stay hidden.

### Chunk 2: backend config (small)

`backend/api/routes/deliveries.py`, `drive_config`:

- Return `{"client_id", "api_key", "app_id"}`.
- `api_key` = `setting("GOOGLE_CLOUD_API_KEY")`.
- `app_id` = the project number, the digits before the first `-` in the client ID
  (`<number>-….apps.googleusercontent.com`). `None` if the client ID is unset or not in that
  form. No new setting is needed.
- Update the docstring: these are public browser identifiers, not secrets.

Tests (`tests/test_delivery.py`): extend `test_config_exposes_only_the_public_client_id` to cover
all three values, the unset case and a malformed client ID (`app_id` is `None`).

### Chunk 3: Picker loader and download (`frontend/src/drivePicker.ts`, new)

- `preparePicker()`: load `https://apis.google.com/js/api.js`, then `gapi.load('picker')`. Call
  it on page load next to `prepareGoogleSignIn()`, so the click opens Picker without waiting.
- Keep `api_key` and `app_id` from the config. Have `prepareGoogleSignIn()` share the one
  `/api/drive/config` response, either by exposing it from `googleAuth.ts` or by moving the fetch
  into a small shared helper.
- `pickCatalogFile(token): Promise<PickedFile | null>`. Build the Picker with:
  - `setOAuthToken(token)`, `setDeveloperKey(api_key)`, `setAppId(app_id)`. Without the app ID,
    picking does not grant `drive.file` access and the download returns 404.
  - one `DocsView` filtered to `text/csv` and `application/vnd.google-apps.spreadsheet`, with
    folders shown and selectable for navigation (`setIncludeFolders(true)`), plus a single-file
    selection.
  - callback: `PICKED` resolves `{id, name, mimeType}`; `CANCEL` resolves `null` (a cancel is
    not an error).
- `downloadCatalogFile(token, file): Promise<File>`:
  - CSV: `GET https://www.googleapis.com/drive/v3/files/{id}?alt=media`.
  - Sheet: `GET https://www.googleapis.com/drive/v3/files/{id}/export?mimeType=text/csv`.
  - `Authorization: Bearer <token>`. These endpoints allow browser requests (CORS).
  - Return `new File([blob], name, {type: 'text/csv'})`, appending `.csv` when the name lacks
    it, since `parse_catalog` rejects other names. A Sheet called `Catalog` becomes
    `Catalog.csv`.
  - Errors: 401 throws a "sign in again" error so the caller clears the token. 403/404 throws
    "Google Drive did not allow access to this file". Network errors get their own message. The
    token never appears in an error message.
- Reuse `getDriveToken()` and `clearDriveToken()` from `googleAuth.ts`. The token is the same
  `drive.file` token Save to Drive uses, so a user who already signed in sees no popup.

### Chunk 4: button and flow (`frontend/src/ImportReview.tsx`)

- Add **Fetch from Google Drive** (secondary button) beside **Choose CSV** in the dropzone, and
  beside **Upload a different CSV** on the review screen, so a different Drive file can be
  picked from there too.
- Handler, synchronous up to the popup:
  1. Guard on `busyRef`.
  2. Call `getDriveToken()` inside the click.
  3. `pickCatalogFile(token)`; on `null`, stop quietly.
  4. Set `busy` to a new state `'fetching'` ("Downloading from Google Drive…").
  5. `downloadCatalogFile(...)`, then pass the file to the existing `upload(file)`, which shows
     "Comparing your catalog…" and calls `onPreview`.
- Failures (sign-in closed, popup blocked, Picker failed to load, Drive not configured, download
  failed) use the existing `errorBox`. A 401 calls `clearDriveToken()`.
- When `api_key` or `app_id` is missing, keep the button visible and explain on click, as
  `client()` in `googleAuth.ts` does for a missing client ID.

### Chunk 5: docs

- `ASSUMPTIONS.md`: a one-time import of a user-picked file, not live sync. A Sheet exports
  only its **first tab**. The same `drive.file` sign-in covers import and Save to Drive.
- `DEVELOPMENT.md`, Google section: enable the Picker API, create and restrict the API key, set
  `GOOGLE_CLOUD_API_KEY`; add it to the settings table.
- `.env.example`: a comment on `GOOGLE_CLOUD_API_KEY` explaining its use.
- `google_drive_writeback.md`: replace the Known limits note about Picker with a pointer to this
  doc. `AGENTS.md` CSV import row: mention the Drive source.
- `README`/`APPROACH.md`: one line if the video walkthrough shows it.

### Chunk 6: verification

- `uv run pytest` for the config tests.
- Manual, locally and on the deployed URL, as a listed test user:
  1. Pick a `.csv` from a subfolder and check the preview matches a local upload of the same
     file.
  2. Pick a Google Sheet and check the first tab is imported.
  3. Cancel Picker: no error, no request.
  4. Signed in already via Save to Drive: no popup.
  5. Revoke the app's access at myaccount.google.com, then pick again: the page asks to sign in
     again.
  6. Pick a file over 5 MB or with bad columns: the existing backend errors appear.
  7. Phone browser: Picker opens and a file can be picked.

## Known limits

- A Sheet export includes only the first tab. A multi-tab Sheet needs the catalog on its first
  tab.
- The import does not record which Drive file it came from. The preview shows only the
  filename.
- Large files are downloaded in full before the backend's 5 MB check runs.
