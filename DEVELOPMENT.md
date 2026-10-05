# Running the catalog and import review

Upload a CSV to create a saved pending preview. Review new products and field
changes, then click **Accept changes** to persist the catalog. Existing images
remain associated with their original generation inputs. Uploading and accepting
never call Luma; only an explicit **Generate** (or **Regenerate**) click does.

## Development

From the repository root, start the Litestar API in one terminal:

```bash
./start-backend.sh
```

In a second terminal, start the React app:

```bash
./start-frontend.sh
```

The backend script runs `uv sync` and starts port 8000. The frontend script runs
`npm ci` and starts port 5173. Open
`http://localhost:5173`; Vite forwards `/api` requests to Litestar.

To restart both development servers after changing dependencies, run from the
repository root:

```bash
./restart.sh
```

The restart script stops only this project's servers on those ports, then runs
both from one terminal. Keep that terminal open; press Ctrl+C to stop them.
Backend output goes to `.run/backend.log`, and frontend output appears in the
terminal.

## Production build

The `Dockerfile` builds React and serves its files through Litestar on port 8000. The sample `data/catalog.csv` can be uploaded to check the preview.

## Local database and images

SQLite is included with Python; no database server or extra package is required.
The backend initializes `runtime/catalog.sqlite3` on first use. The database
contains `products`, `pending_imports`, `generated_images`, `image_reviews`, and `drive_deliveries` tables. It persists
across process restarts and is excluded from git. The older `runtime/shots.sqlite3`
file, if present, is a separate database and is not imported or changed.

A version 1 database is upgraded to version 2 automatically on first open (the one-approval
index is dropped; rows are kept). The upgrade is one-way: older code refuses a version 2
database, so copy `catalog.sqlite3` before deploying and restore that copy to roll back.
If a local backend refuses to start with "Unsupported catalog schema version", stop it,
delete `runtime/catalog.sqlite3` (and the images in `runtime/images/` if you want a clean
slate), restart, and re-import the CSV. Do not delete a production database for this.

Configuration is read from the backend process environment:

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | Repository `runtime/` directory | Storage root. Local images go under `images/`. |
| `DATABASE_PATH` | `DATA_DIR/catalog.sqlite3` | Optional override for the SQLite file. Relative paths are anchored to the repository. |

If only `DATABASE_PATH` is configured, its parent directory becomes the storage
root. If both variables are set, `DATA_DIR` determines image storage.

The container uses `DATA_DIR=/data`. Mount a persistent volume at `/data` so its
database and image files survive container replacement.

```bash
docker build -t luma-catalog .
docker run --rm -p 8000:8000 -v luma-catalog-data:/data luma-catalog
```

## Slack review

Create the Slack app from `slack-app-manifest.yaml`, install it, invite the bot to
the review channel, and add these to `.env.local` (gitignored):

```
SLACK_BOT_TOKEN=xoxb-...       # posts messages and uploads images
SLACK_APP_TOKEN=xapp-...       # Socket Mode; receives Approve clicks (connections:write)
SLACK_CHANNEL_ID=C...
SLACK_APPROVER_USER_ID=U...    # optional: the only user whose Approve counts; unset = anyone in the channel can approve
```

On startup the backend opens the Socket Mode connection when both tokens are set.
**Generate** posts each product's candidates to its Slack thread, one Approve button per
candidate, as soon as they finish; a candidate stays "Generating" until it is posted. If
posting fails, the product shows **Retry posting** with the error. Each approval is final
and a brief can take up to three; the third marks the remaining candidates "Not selected".
The web app shows each decision on its next refresh. When an import changes a product's brief, its waiting
candidates become Outdated: their Approve buttons are replaced in Slack and Approve is
refused. Review state lives in the `image_reviews` table (`awaiting_approval` →
`approved`; a row exists only for a posted candidate; at most three approved images per
product and brief version, counted in `reviews.approve`, with a forward-only trigger). Tests use a fake
Slack client.

## Google Drive delivery

Approved images are saved to Drive only when someone clicks **Save to Drive** (one
product) or **Save all to Drive**. Every approved image of the product not yet in Drive
is saved; an approval survives a later brief change, so there can be more than one. The click opens Google's sign-in popup; the
browser receives a one-hour access token and sends it with the request. Each image lands
at the top level of that user's My Drive as `<SKU>_styled_v<N>.<ext>`, N being the
image's own version. The backend uses the
token for that batch only and never stores or logs it. It calls the Drive v3 REST API with
the standard library and the `drive.file` scope, so it only sees files it created.

One-time setup (the app's identity only; no user credentials are configured):

1. In Google Cloud Console, create a project and enable the **Google Drive API**.
2. Configure the OAuth consent screen (External) with the `drive.file` scope, and publish
   it to "In production" so any Google account can sign in (in "Testing" only listed test
   users can). `drive.file` is a non-sensitive scope, so no Google review is needed.
3. Create an OAuth client ID of type **Web application**. Under **Authorized JavaScript
   origins** add `http://localhost:5173` (Vite dev server), `http://localhost:8000` (built
   frontend served by the backend) and the deployed `https://` origin. No redirect URI or
   client secret is needed.
4. Add the client ID to `.env.local` (or the deployment's environment). It is public; the
   frontend fetches it from `GET /api/drive/config`:
   ```
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   ```

5. For **Fetch from Google Drive** on the Import CSV page, enable the **Google Picker API**
   in the same project, create an API key under **Credentials**, restrict it to the Google
   Picker API and to the HTTP referrers `http://localhost:5173/*`, `http://localhost:8000/*`
   and the deployed URL, and add it to `.env.local`. The key reaches the browser, so those
   restrictions are what protect it:
   ```
   GOOGLE_CLOUD_API_KEY=...
   ```

| Setting | Purpose |
| --- | --- |
| `GOOGLE_CLIENT_ID` | Public ID of the Web application OAuth client that shows Google sign-in. Its leading project number is also Picker's app ID. |
| `GOOGLE_CLOUD_API_KEY` | Public browser API key for Google Picker (Fetch from Google Drive). Restrict it to the Picker API and the app's referrers. |

**Fetch from Google Drive** (Import CSV page) uses the same sign-in and `drive.file` token.
Google Picker opens in the browser; the user picks one CSV or Google Sheet, which the browser
downloads (a Sheet is exported as CSV, first tab only) and sends to the usual
`POST /api/catalog/preview`. The backend only provides the public IDs from
`GET /api/drive/config` (`client_id`, `api_key`, `app_id`). See `google_drive_import_plan.md`.

An existing file with the target name at the top of My Drive is overwritten in place (same
file ID and link); two or more files with that name are reported as an error and nothing
is written. The backend checks the token with one cheap Drive call before queuing, so an
expired or revoked token returns 401 and the next click signs in again. Failures
(missing token, rejected token, quota, network, missing local image) appear on the
product row and are retried by clicking again; one failing product does not stop the
others. A restart marks unfinished writes as failed so they can be retried. Delivery
state lives in `drive_deliveries` (`pending` → `delivered`, forward-only) and never
changes the review decision. Tests use a fake Drive client.

## Import behavior

- CSVs must contain all nine columns in `data/catalog.csv`; additional columns
  are ignored. Missing headers and malformed CSVs are rejected.
- Every product is saved on confirmation, including products with a blank Shot
  Idea. Missing, duplicate or lowercase SKUs block confirmation of the entire preview.
- The browser remembers the latest pending preview ID, allowing it to reopen
  after a refresh. Confirmed products are fetched from SQLite on page load.
- Confirming the same preview twice does not apply it again. If product versions
  changed after the preview was created, confirmation returns HTTP 409 and the
  user must upload again to review fresh differences.
- Only the brief (Photo, Shot Idea, Product Name, Color / Finish, Material) makes
  images outdated: changing it raises the product's brief version. Price, Category
  and Notes are info-only. The review groups rows by what accepting does (new, brief
  changed and the product's state, info only, needs correction). Changes never
  automatically trigger image generation.
- Accepting returns HTTP 409 while a product whose brief changes is generating,
  posting to Slack or saving to Drive; wait for it to finish and accept again.

Backend checks use isolated temporary databases:

```bash
uv run --with pytest python -m pytest -q
```

## File layout

```text
backend/
  app.py                 Litestar setup and built frontend
  db.py                  SQLite schema and transaction boundaries
  repositories/products.py  Product, preview, and image database operations
  api/routes/catalog.py  Preview, confirmation, and catalog endpoints
  services/catalog.py    CSV validation and normalization
  services/imports.py    Comparison, brief cases, and confirmation
  services/generation.py Luma generation, then posting the candidates to Slack
  services/review.py     Post candidates to Slack, mark outdated ones, record the approval
  services/delivery.py   Write approved images to Google Drive
  slack.py               Slack client and Socket Mode listener
  drive.py               Google Drive REST client (standard library)
  settings.py            Settings from the environment or .env.local
  repositories/reviews.py  image_reviews state
  repositories/deliveries.py  drive_deliveries state
  api/routes/deliveries.py  Save-to-Drive and Drive sign-in config endpoints
frontend/
  src/App.tsx            Views, polling, and the generate / post / save actions
  src/catalogApi.ts      API calls, response types, and productStage
  src/ImportReview.tsx   CSV upload (local or from Google Drive) and the grouped change review
  src/CatalogView.tsx    Catalog with stage tabs and a Next step column
  src/NextStep.tsx       Stage labels and each product's next action
  src/ImageLightbox.tsx  Image view: candidates, status, and next step
  src/googleAuth.ts      Google sign-in popup, in-memory Drive access token, shared Drive config
  src/drivePicker.ts     Google Picker and download for Fetch from Google Drive
  src/style.css          Page styles
data/catalog.csv         Sample customer export
```
