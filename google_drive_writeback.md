# Google Drive write-back

Status: implemented. This is the last step of the current MVP (`AGENTS.md`): once a
product has an approved image, someone clicks **Save to Drive**, signs in with Google, and
every approved image of that product not yet in Drive is written to *their* Google Drive. Product-page publishing stays out of scope.

## Decisions

| Topic | Decision | Notes |
| --- | --- | --- |
| Whose Drive | The user who clicks | The app owns no Google account or stored credentials; the user chooses by signing in. |
| Auth | Google Identity Services token model, in the browser | A one-hour access token is sent with each save request, used for that batch, and discarded. No sessions, cookies, refresh tokens or server-side token storage. |
| Scope | `https://www.googleapis.com/auth/drive.file` | Least privilege: the app sees only files it created for that user. |
| Location | Top level of the user's My Drive | No folders. |
| Filename | `<SKU>_styled_v<N>.<ext>` | N is the image's own candidate version (e.g. `HG-002_styled_v3.png`), so two different images never share a name and a save never overwrites another approved image. SKUs are uppercase (the importer rejects lowercase) and filename-safe; extension from the stored image. No `_01`/`_02`/`_03` numbering is reserved. Each delivery stores its filename, so files saved under the old `<SKU>_styled_01.<ext>` name keep it and are not renamed. |
| Existing file | Overwrite in place | Crash recovery for the same image only: a file with this image's name exists when a save uploaded it but did not record it. Same file ID and link; Drive keeps the old content as a short-lived revision. Two or more same-named files are an error and nothing is written. |
| Approved images | Every approved image is saved | One to three approvals per product and brief version, and approvals survive a brief change, so a product can have several. **Save to Drive** is offered from the first approval and saves each approved image not yet in Drive, so one approved after an earlier save is written by the next click. |
| Trigger | Explicit click only | Per product and a batch button. Approval never triggers a write. |
| HTTP client | Standard library, like `backend/luma.py` | No Google client library in the backend. |

## Flow

```
Browser ── Google sign-in popup ──▶ accounts.google.com  (returns 1-hour access token)
   │
   └─POST /api/deliveries {skus, access_token}──▶ Backend ──Drive REST (HTTPS)──▶ user's My Drive
   ▲                                                  │
   │ poll GET /api/catalog                            ▼
   └──────────────────────────────────────────── SQLite (drive_deliveries)
```

1. On load, the page loads Google's script (`accounts.google.com/gsi/client`) and fetches
   the public client ID from `GET /api/drive/config`, so a click can open the popup
   immediately (browsers block popups opened after an await).
2. The click asks for a token. A cached token is reused until a minute before it expires;
   otherwise the popup opens with `prompt: ''`, which skips the consent screen for a user
   who already granted access. If the user unticks Drive access, the page reports it.
3. `POST /api/deliveries` rejects a missing token (401), returns early if nothing is
   eligible (no Google call), then makes one cheap Drive call to check the token. An
   expired or revoked token returns 401 and the page forgets it, so the next click signs in
   again. Otherwise it inserts `pending` rows, claims each SKU in process, and returns 202.
4. Worker per SKU, holding the token only in memory, for each of its pending images:
   1. Look for the exact filename at the top of My Drive (`'root' in parents`).
   2. One match: overwrite its content. None: create the file. More than one: error.
   3. In its own short transaction, set `delivered` with the Drive file ID, link and time.
      Never hold `database()` open across a Google call.
5. The table shows **Saved to Drive** with a link; the page polls while work is pending.

## Data model: `drive_deliveries`

| Column | Notes |
| --- | --- |
| `image_id` PK, FK to `generated_images.id` | One delivery per approved image. |
| `product_sku` FK to `products.sku` | |
| `state` | `pending` or `delivered`, CHECK constrained, forward-only trigger. |
| `filename` | The name written, e.g. `VASE-042_styled_v3.jpg`. Set when queued and never renamed. |
| `drive_file_id`, `drive_url`, `delivered_at` | Required when `delivered`. |
| `error` | Last failure; cleared on retry. A failed attempt stays `pending` so it is visible and retryable. |
| `created_at`, `updated_at` | |

- No token, account or folder is stored. Records do not say whose Drive a file went to.
- Delivery never changes review state, so a Drive failure cannot undo Ellie's decision.

Derived in `product_view` over every approved image: `delivery_status` (`pending` if any
save is pending, else `delivered` if any is delivered, else `null`), `delivery_error`,
`drive_url` (the newest delivered image), `delivering` (a save in flight) and `can_deliver`
(some approved image is not in Drive and nothing is in flight).

## Code

- `backend/drive.py`: `DriveClient(access_token)` with `check_access`, `find_root_files`,
  `upload_file` (multipart POST to the upload endpoint, no parent) and `overwrite_file`
  (multipart PATCH). A Drive 401 becomes a "sign in again" error; error text never
  contains the token.
- `backend/services/delivery.py`: `start_delivery(skus, access_token)`, worker `_deliver`,
  `delivery_filename`, `recover_unfinished` on startup.
- `backend/api/routes/deliveries.py`: `GET /api/drive/config`, `POST /api/deliveries`.
- `frontend/src/googleAuth.ts`: script loading, token client, in-memory token cache.
- `frontend/src/App.tsx`, `NextStep.tsx`, `CatalogView.tsx`, `catalogApi.ts`: **Save to Drive**,
  **Save all to Drive**, stage pill, Drive link, retry. The image view shows each approved
  image's Drive filename.

## Setup

See `DEVELOPMENT.md`: a **Web application** OAuth client with Authorized JavaScript
origins for localhost and the deployed URL, a consent screen published to "In
production", and `GOOGLE_CLIENT_ID` in the backend's environment.

## Failure handling (visible and retryable)

- No token or rejected token: 401 before anything is queued; the next click signs in again.
- Popup blocked or closed, or Drive access unticked: message on the page, nothing sent.
- Quota, rate limit, network error, 5xx, missing local image: error on that product's row;
  retry by clicking again. One product failing does not stop the others.
- Server restart mid-save: the token is gone; `recover_unfinished` marks the row, and
  **Retry** signs in again.

## Known limits

- Once anyone saves a product, it shows as saved for everyone, and the link opens only for
  that person. Per-person copies would need delivery records keyed by Google account.
- Files sit loose at the top of My Drive; delivering into an existing shared folder would
  need a folder picker or the broader `drive` scope. Google Picker is now used only to
  import a catalog CSV or Sheet; see `google_drive_import_plan.md`.
- Simple multipart upload suits files up to about 5 MB.
