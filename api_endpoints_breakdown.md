# API endpoints

The backend has ten HTTP endpoints. They're defined in `backend/api/routes/` and registered in `backend/app.py`. Slack approval is the exception: it comes in over a Slack connection rather than through an endpoint.

## 1. CSV upload and change review (`backend/api/routes/catalog.py`)

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/catalog/preview` | Uploads a CSV file. It validates the rows and compares them to the saved catalog by SKU, then stores the result as a pending preview. **The catalog isn't changed yet.** |
| `GET` | `/api/catalog/imports/{preview_id}` | Returns a pending preview: new products, changed fields shown before and after, and any problems. For a preview that's already been applied, it returns the stored result. |
| `POST` | `/api/catalog/imports/{preview_id}/confirm` | The **Accept changes** click, which is how changed items get approved. It checks that the saved catalog hasn't changed since the preview, then saves everything in one step. If something did change, it returns a conflict and the upload needs reviewing again. It also returns 409, naming the SKUs, while a product whose brief changes has a generation, Slack post or Drive save in progress ("wait for it to finish, then accept again"). Approvals are never set aside. After it commits, the Approve buttons of that product's now-outdated Slack candidates are replaced with "Outdated: brief changed" in the background (best effort). |

## 2. Catalog and status (`catalog.py`)

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/catalog` | The main read, which the UI keeps polling. It returns every product with its images and each product's status for generation, review and delivery. |

| `GET` | `/api/catalog/generation-candidates` | Same response, limited to products that have never been generated or whose brief has changed since. |
| `GET` | `/api/catalog/images/{image_id}` | Serves a generated image file. |

Brief-version response fields (see `persistence.md`): products have `brief_version`, `brief_changed`, `generating`, `can_generate`, `images_used`, `can_request_more`, `approved_image_ids`, `post_error` (replaces `send_error`) and `delivering`; `review_status` covers the current brief only and is `awaiting_approval`, `approved` or null (`pending_send` is gone); `approved_image_id` is the newest approval. Images have `brief_version`, `outdated`, `posting` and `post_error`; an image's `review` no longer has `send_error` or `superseded_at`. Preview rows have `brief_case` (`new`, `info_only`, `unchanged`, `invalid`, or for a brief change `not_generated`, `with_ellie`, `approved_not_in_drive`, `in_drive`) in place of `replaces_approval`, and previews have `brief_case_counts`.

## 3. Image generation (`catalog.py`)

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/generations` | Takes a list of SKUs and starts four Luma candidates for each eligible product: Ready products, or (an optional regenerate) products whose brief changed after an approval or delivery. When a product's candidates finish, the job posts them to its Slack thread; each stays `processing` until posted. Skips products that are generating, posting or saving to Drive, or that already have three approved images for the current brief. Returns 202 while generation continues in the background. **This is the only endpoint that spends Luma credits; the only other spender is the More options button in Slack (section 4), which queues through the same code and counts against the same cap of 12 non-failed images per brief version.** |

## 4. Slack review (`reviews.py`, plus Socket Mode)

| Method or channel | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/reviews` | **Retry posting.** Generate posts candidates itself; this takes a list of SKUs and posts each product's current-brief candidates that never reached Slack (a failed or interrupted post) to its thread, each with an **Approve** button. Returns 202. |
| Slack Socket Mode (inbound) | not an HTTP route | Carries Ellie's **Approve** clicks. `handle_block_action` in `backend/services/review.py` checks the clicker is `SLACK_APPROVER_USER_ID` when that optional variable is set (unset, anyone in the channel can approve) and the image was made from the product's current brief, records the approval (up to three per product and brief; the third removes the remaining candidates' buttons), and updates the Slack messages with the count. It also carries **More options** clicks (one button per posted batch, value = SKU and brief version): under the same approver rule, `request_more` in `backend/services/generation.py` queues another batch for that brief and the button's message becomes "More options requested by @user"; a refusal (generating, saving to Drive, three already approved, brief changed, cap reached, no candidates, unknown or invalid product) is a private note and queues nothing. Because it's a socket connection, no public URL is needed. |

## 5. Google Drive (`deliveries.py`)

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/drive/config` | Returns public browser identifiers: the Google OAuth client ID for sign-in, and the API key and app ID (project number from the client ID) for Google Picker. `api_key`/`app_id` are `null` when unset. |
| `POST` | `/api/deliveries` | Takes a list of SKUs and the access token from the user's Google sign-in in the browser. Writes every approved image of each product not yet in Drive to the top level of that user's My Drive as `<SKU>_styled_v<N>.<ext>`, where N is the image's own version. An existing file with that name (the same image, left by a crash) is overwritten. Returns 202; a missing or expired token returns 401. The token is used for that batch only and never stored. |

## Startup jobs

When the server starts, two recovery jobs run: `recover_interrupted` for generation and Slack posting, and `recover_unfinished` for Drive. Each one marks work that a restart cut off with an error, so it shows up in the app and can be retried. A candidate cut off while posting becomes done with a `post_error`, so it offers Retry posting rather than Generate.
