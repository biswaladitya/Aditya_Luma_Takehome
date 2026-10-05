# Agent guidance for the Luma FDE take-home

Read `README.md` for the customer problem and assignment requirements. Read `LUMA_FDE_IMPLEMENTATION_HANDOFF.md` for the proposed solution. The handoff's opening **Current MVP decisions (2026-09-29)** note takes precedence over older sections of that same document where they conflict. Do not infer requirements or implementation decisions beyond these sources and explicit user instructions; surface unresolved choices instead.

## Customer problem

A six-person home-goods brand has a catalog of roughly 300 products and struggles to produce styled photos from white-background product shots. Shot ideas and decisions are scattered across the spreadsheet, Slack, and email; final files go to Google Drive. Ellie makes the final creative choice and needs to do so from her phone without installing anything new. Maya needs visibility into progress and generation cost, particularly for an upcoming 40-product drop. The customer's full definition of done is 2–3 approved images matching the shot idea, in Drive and on the product page.

## Two components

1. **Intent consolidation:** Gather disparate requests and product data into a populated CSV. Today this involves the sheet, Slack, email, and human memory. The handoff treats broader automation of this component as future work; do not assume it already exists.
2. **Production and approval:** Accept a populated CSV in a web app, parse and display its rows, let Ellie explicitly generate image candidates, send selected candidates to Slack for review, record her decision in the backend, reflect it in the web app, and let an operator explicitly write approved images to Google Drive. This is the handoff's current implementation focus.

## Current MVP flow from the handoff

1. Ellie uploads a CSV. The app validates it and shows which rows have usable source photos and `Shot Idea` values. Uploading alone does not spend generation credits.
2. Ellie selects rows and explicitly starts generation of **two candidates** per selected request.
3. Ellie explicitly sends ready candidates to a product-specific Slack thread. Team discussion is advisory. Only Ellie's **Approve** or **Reject** action changes the canonical decision.
4. Slack actions update backend state; the web app displays that same state, including generated candidates, decisions, and progress.
5. Once a request has at least two approved images, the operator clicks **Write approved images to Drive**. Approved assets use deterministic, product-linked filenames, and Drive delivery is recorded.

Keep generation and review actions explicit. Preserve prior generations and decisions. Verify Slack requests and, when `SLACK_APPROVER_USER_ID` is set, the authorized approver. Make import, generation, Slack, and Drive failures visible. Keep credentials out of the repository; `.env.local` must remain gitignored.

## Scope boundary

The current handoff ends at Drive delivery. Feedback-guided regeneration, automatic Slack posting, and product-page publishing are outside this version, even where older handoff sections describe them. Broad Slack/Gmail request mining and live two-way Sheets sync belong to the intent-consolidation component and are not part of the current production flow. The README's product-page requirement remains part of the customer's full outcome, but the current MVP does not claim to fulfill it.

When implementation is requested, preserve the README's assignment requirements: working deployed software, fresh CSV ingestion, `ASSUMPTIONS.md`, `APPROACH.md`, a deployed-workflow video linked in `video.md`, and AI session history. Document any assumptions or scope choices explicitly. This file is project context, not authorization to begin coding.

## Implementation status (2026-10-01)

The production-and-approval flow is implemented end to end through Drive delivery. Where it differs from the handoff, `ASSUMPTIONS.md` records the choice: **one to three** approved images per product and brief (one is enough to save to Drive; the review closes at three), **four** candidates per batch (not two), approval is final and survives a brief change, there is no Reject button, and Generate posts its candidates to Slack automatically. The product state machine is in `state_machine_plan.md`; details live in `persistence.md`, `google_drive_writeback.md` and `api_endpoints_breakdown.md`.

### Data models (SQLite, one schema in `backend/db.py`; version 1 upgrades to 2 on open, one-way)

| Table | Holds | Key rules |
| --- | --- | --- |
| `products` | Current catalog row per SKU (the nine CSV attributes), a `version` and a `brief_version` | SKU is the primary key (uppercase); products are never deleted by an import. `brief_version` goes up only when Photo, Shot Idea, Product Name, Color / Finish or Material changes. |
| `pending_imports` | An uploaded CSV's reviewed rows and the product versions it was compared against | `pending` → `applied` once; the catalog only changes on confirmation. |
| `generated_images` | One row per Luma candidate: `storage_key`, `generated_from` snapshot, `brief_version`, `status` (`queued`/`processing`/`done`/`failed`), `error`, `post_error` | Snapshot is immutable; outdated when its `brief_version` is below the product's; stays `processing` until posted to Slack; at most 12 non-failed images per brief version. |
| `image_reviews` | Slack review per posted image: thread/message IDs, `brief_version`, `state` (`awaiting_approval` → `approved`), approver and time | Exists only once posted; forward-only trigger; up to three approved images per product and brief version, enforced in `reviews.approve` (schema version 2 dropped the one-approval unique index). |
| `drive_deliveries` | Drive save per approved image: `state` (`pending` → `delivered`), `filename`, `drive_file_id`, `drive_url`, `error` | Forward-only; filename fixed when queued; stores no Google token or account. |

Generation, review and delivery status are derived per product in `product_view` (`backend/services/catalog.py`); nothing is stored on `products`.

### Key API endpoints

| Area | Endpoint | Purpose |
| --- | --- | --- |
| CSV import | `POST /api/catalog/preview` | Validate a CSV and diff it by SKU; saves only a pending preview. The CSV comes from a local file or from **Fetch from Google Drive** (Picker; a Sheet exports its first tab), downloaded in the browser. |
| | `POST /api/catalog/imports/{id}/confirm` | Apply the reviewed changes; 409 if the catalog changed since the preview, or while a brief-changed product is generating, posting or saving to Drive. |
| Catalog | `GET /api/catalog` | Products, images and all statuses; polled by the UI. |
| Generation | `POST /api/generations` | Queue four Luma candidates per selected SKU and post them to Slack when done. The only HTTP route that spends credits; the **More options** button in Slack is the only other thing that does. |
| Slack review | `POST /api/reviews` | Retry posting: post current-brief candidates that never reached Slack. |
| | Slack Socket Mode (not HTTP) | Receives Approve clicks (only current-brief candidates) and **More options** clicks, which queue another batch for the same brief under the same approver rule. `SLACK_APPROVER_USER_ID` is optional: unset (testing phase), anyone in the channel can approve; set, only that user can. |
| Drive | `GET /api/drive/config` | Public browser identifiers: OAuth client ID for sign-in, plus Picker's API key and app ID. |
| | `POST /api/deliveries` | Save every approved image not yet in Drive to the signed-in user's My Drive using the access token sent with the request. |

### Overall app flow

1. **Import:** upload a CSV, review the changes grouped by what accepting does (new, brief changed and the product's state, info only, needs correction), click **Accept changes**. A brief change makes the product's waiting candidates Outdated (their Slack Approve buttons are removed); an approval, in Drive or not, is kept, and regenerating is optional.
2. **Generate:** select products and explicitly start generation; four candidates per product run in the background, each recording the exact inputs sent to Luma, and are then posted to the product's Slack thread. A failed post shows **Retry posting**.
3. **Review:** team comments are advisory; Ellie's **Approve** is final and she can approve up to three images per brief; the third approval marks the remaining candidates "Not selected". If she wants more to choose from, **More options** in the thread generates another batch for the same brief (up to the 12-image cap); earlier candidates stay approvable.
4. **Deliver:** from the first approval, click **Save to Drive**; images approved later are saved by the next click. Google's sign-in popup (Google Identity Services, `drive.file` scope) gives the browser a one-hour token; the backend uses it for that batch only and writes each approved image not yet in Drive as `<SKU>_styled_v<N>.<ext>` to the top of that user's My Drive.
5. **Status:** the web app shows every step from backend state, including failures, which stay visible and retryable. Startup jobs mark work cut off by a restart.

Credentials: Luma and Slack settings live in the gitignored `.env.local`. Drive needs only the public `GOOGLE_CLIENT_ID` of a **Web application** OAuth client; the consent screen is in Testing (listed test users only) until the app is deployed with a home page and privacy policy.
