# New feature additions

Planned additions after the first deployed version. Each section describes what the feature does, what has to change, and the choices still open. Section 1 is implemented; sections 2 and 3 are not.

## 1. "More options" in Slack

**Status: implemented** (Slack button only; no web-app button). Where the build differs from the plan below:

- **Batch size** is `IMAGES_PER_REQUEST` = 4 (changed from 2 with multi-approval, below), so the confirm dialog says 4 images, about $0.26.
- **Cap** is 12 (`MAX_IMAGES_PER_BRIEF`), and it moved to `backend/services/catalog.py` beside `IMAGES_PER_REQUEST` so `product_view` can apply it. `product_view` returns `images_used`, `cap_reached` and `can_request_more`.
- **Queueing** is `request_more(sku, brief_version)` in `generation.py`, sharing `_queue` with `start_generation`. It returns `queued` or a refusal: `unknown_sku`, `outdated`, `generating`, `delivering`, `approval_limit`, `invalid_inputs`, `no_candidates`, `cap_reached`. "Full set of approvals" is three per brief (see the multi-approval note below).
- **One button per batch:** the button is posted only when every candidate in a posting call reached Slack. A partly posted batch gets its button when Retry posting posts the rest. It is posted just before the batch's last candidate is marked done, so the product reads as generating until the button is in the thread. No button is posted when the cap leaves no room for another batch.
- **Whole batch fails, or a restart cuts it off:** the thread gets "Generation failed, try again." with a fresh button, as planned.
- **Buttons are not retired** on approval or a brief change (their messages are not stored); a click is then refused with a private note.
- **Failed attempts are bounded too (added after code review):** failed images don't count against the cap of 12, so More options also stops once a brief has 24 attempts including failures (`MAX_ATTEMPTS_PER_BRIEF`). Otherwise a failing Luma could be retried from Slack without limit.
- **Approval during a running batch (added after code review):** if Ellie's third approval lands while a More options batch is still generating, the new images are kept but not posted to Slack, and show as "Not selected" in the web app.
- **Known gaps:** the button message is not stored, so if posting it fails twice the thread has no button and no way to get one; and a later batch that needs Retry posting is not counted in the web app's "Not posted" tab.
- **Web app:** the row shows the newest batch plus every other current-brief candidate; "With Ellie" also shows Retry posting and the newest batch's failures when a later batch had them.

### What it does

When Ellie likes too few of a product's candidates, she taps **More options** in that product's Slack thread. The app generates another batch for the same product attributes and posts it to the same thread. The earlier candidates stay where they are and can still be approved.

This removes today's dead end: once a product has candidates for its current brief it cannot be generated again, so a product whose candidates are all unusable waits in "With Ellie" until someone edits the CSV.

### No new HTTP endpoint for Slack

The app receives Slack clicks over **Socket Mode**, not HTTP (`backend/slack.py`, `start_listener`). Slack never calls a URL on our backend, so a `/more-options` endpoint would have nothing calling it. The click arrives the same way an Approve click does: as a `block_actions` payload handed to `handle_block_action` in `backend/services/review.py`.

So the Slack side is a **new button action**, not a new route:

| Piece | Today | Change |
| --- | --- | --- |
| Button | `approve_image`, one per candidate, value = image ID | Add `more_options`, one per batch, value = SKU and brief version |
| Receiver | `handle_block_action` ignores every action except `approve_image` | Dispatch on `action_id`; route `more_options` to a new handler |
| What the payload identifies | An image | A **product**, not an image. "More options" is about the whole set, so the button carries the SKU. |

An HTTP route is only needed if the web app also gets a More options button (see "Web app" below), and that can reuse `POST /api/generations` rather than adding a route.

### Flow

1. A batch finishes and `post_candidates` posts each candidate to the product's thread, as today.
2. After the candidates, it posts one more message in the thread: "None of these work?" with a **More options** button. The button has a Slack confirm dialog stating the count and estimated cost (4 images, about $0.26 at the current $0.0644 estimate).
3. Ellie taps it. The payload reaches the handler with her user ID, the channel, the message timestamp, and the button value.
4. The handler checks the approver (same rule as Approve: when `SLACK_APPROVER_USER_ID` is set, only that user), then asks the generation service to queue another batch.
5. If queued, the handler replaces the button message with "More options requested by @user", so it cannot be tapped twice. If refused, the user gets a private note saying why and the button stays.
6. Generation runs in the background exactly as a normal batch does, then posts the new candidates and a fresh More options button to the same thread.

The button message needs no stored state: Slack's payload includes the channel and message timestamp, which is all `chat.update` needs.

### Backend changes

**`backend/services/generation.py`**

- `start_generation` currently skips any product that is not `can_generate`, and `can_generate` is false once current-brief candidates exist. Add a `more` flag (or a small `request_more(sku)` function sharing the same queueing code) that allows a product with current-brief candidates.
- Refuse, with a reason, when:
  - the SKU is unknown or its inputs are no longer valid;
  - a generation or Drive save is already in progress for it;
  - the product already has its full set of approvals;
  - the button's brief version is older than the product's (the attributes changed; new candidates come from the web app);
  - the batch would exceed the per-brief cap.
- Raise `MAX_IMAGES_PER_BRIEF`. It is 6 today; with batches of 4 it would allow only the first batch. 12 allows the first batch and two rounds of More options.
- Version numbering already continues from the product's highest image version, so new candidates get the next numbers and Drive filenames stay unique.

**`backend/services/review.py`**

- Add the `more_options` action ID and its button block.
- Post the button message at the end of `post_candidates` when at least one candidate was posted. Retry posting goes through the same function, so guard against posting a second button for the same batch.
- Split `handle_block_action` into a dispatcher plus the existing approve handler and the new one.
- `generation.py` already imports `review.py`. The new handler calling generation would make the import circular, so either dispatch from `backend/app.py` or import generation inside the handler.

**`backend/services/catalog.py`**

- `product_view` gains `can_request_more` and the count of images used against the cap, so the web app and the Slack handler share one rule.

**Database**

- No schema change and no migration. New candidates are ordinary `generated_images` rows; the button message is not stored.

### Web app

- **Works without changes:** the catalog already polls every 3 seconds while a product is awaiting approval, so the row moves to "Generating", then back to "With Ellie" with the new candidates.
- **Needs a change:** the row's thumbnails show only the newest batch (`latestBatch` in `catalogApi.ts`); earlier candidates sit behind "+N earlier". After More options, the earlier candidates are still approvable, so the row should show every current-brief candidate still waiting, or at least count them.
- **Optional:** a More options button in the "With Ellie" next step, calling `POST /api/generations` with `more: true`. Useful for whoever operates the web app; not needed for Ellie.

### What changes about spending credits

Today `POST /api/generations` is the only thing that spends Luma credits. After this, a Slack button does too. Both go through the same function, both are explicit clicks, and both are bounded by the per-brief cap. `AGENTS.md`, `ASSUMPTIONS.md` and `api_endpoints_breakdown.md` state the old rule and need updating.

### Failure cases

| Case | Behaviour |
| --- | --- |
| Double tap, or Slack re-delivers the click | The second request finds a generation in progress and is refused with a private note. Queueing runs inside one `BEGIN IMMEDIATE` transaction, so two requests cannot both queue. |
| Some of the new batch fail | The rest are posted, as today. The failed ones show in the web app. |
| The whole new batch fails | Nothing new reaches Slack, and the old button is already gone. Post a short thread note ("Generation failed, try again") with a new button, so Ellie is not left waiting. |
| Server restarts mid-batch | Startup recovery marks the images failed, as today. The Slack thread needs the same note and button as above. |
| Brief changed since the button was posted | Refused with a private note, matching how Approve treats outdated candidates. |
| Cap reached | Refused with a private note naming the cap. |

### Tests to add

- More options queues a batch for a product that already has current-brief candidates.
- Each refusal reason above returns its outcome and queues nothing.
- A non-approver's click is refused when `SLACK_APPROVER_USER_ID` is set.
- Earlier candidates remain approvable after a second batch is posted.
- The cap counts across batches for one brief version.

### Open choices

1. **Cap per brief:** 12 is a proposal. It bounds one product at about $0.77.
2. **Who may tap it:** proposed as the same rule as Approve. The alternative is anyone in the channel, which lets a teammate spend credits.
3. **Earlier candidates:** proposed to stay approvable. The alternative is marking them "Not selected" when More options is tapped, which needs the stored message timestamps and extra Slack updates.
4. **Web app button:** optional; decide whether the operator needs it.
5. **Batch size:** this section assumes 4 candidates per batch, which is now the case (`IMAGES_PER_REQUEST` is 4).


### Multi-approval and batch size (implemented 2026-10-04)

- **Batch size is four** (`IMAGES_PER_REQUEST`); the cap stays at 12 non-failed images per brief.
- **One to three approvals per product and brief** (`MIN_APPROVED_PER_BRIEF = 1`, `MAX_APPROVED_PER_BRIEF = 3` in `backend/repositories/reviews.py`). Save to Drive is offered from the first approval; the third closes the review and marks the rest "Not selected". More options stays available until then.
- **Schema version 2:** the unique index `image_reviews_one_approved_per_brief` is dropped. A version-1 database is upgraded on first open and keeps its rows.
- The product view adds `approved_count`, `approvals_max` and `approval_limit_reached`.

## 2. Status report in Slack

### What it does

On a schedule, the app posts one message to a Slack channel (proposed: `#catalog-status`) saying where every product stands: how many are waiting on Ellie and which, what is approved, what is in Drive, and what is stuck. Maya reads it without opening the web app and without asking Ellie. Each product waiting on Ellie links to its thread.

It is the same information as the catalog page's tabs ("To generate", "With Ellie", "To Drive", "In Drive" and so on), consolidated into one post.

### How it works, at a high level

1. A timer inside the backend process fires at the scheduled time.
2. The app reads the catalog the same way the web app does: `get_catalog()` returns every product with its images, reviews and Drive saves, already summarised per product by `product_view`.
3. Each product is assigned its one stage (Ready to generate, Generating, With Ellie, Approved, In Drive, and the failure stages).
4. Products are grouped by stage, counted, and formatted into a Slack message.
5. The message is posted to the status channel.

There is no separate scan of the image table. The per-image states (queued, done, failed, posted, approved, delivered) are already rolled up into per-product fields by `product_view`; the report groups products by those fields.

### The one structural change: move the stage to the backend

The tabs' logic lives in the **frontend** today: `productStage` in `frontend/src/catalogApi.ts` turns a product's flags into its stage. The backend has no such function, and `backend/services/imports.py` already repeats part of the same precedence by hand (its comment says "Same precedence as productStage in catalogApi.ts").

The report needs the stage on the backend. Writing a third copy would let the Slack report and the web page disagree, so:

- Port `productStage` to Python and have `product_view` return a `stage` field.
- Change the frontend to read `row.stage` instead of computing it.
- Have `brief_case` in `imports.py` use it too.

After this, the web tabs, the import review and the Slack report all use one rule.

### What the report contains

```
Catalog status · Tue 6 Oct
40 products · 31 done · 6 waiting on Ellie · 3 need attention

Waiting on Ellie (6)
• HG-002 Stoneware Mug 12oz · 4 candidates · waiting 3 days · open thread
• HG-008 Salt + Pepper Cellar Set · 4 candidates · waiting 1 day · open thread
  …

Needs attention (3)
• HG-041 Ribbed Tumbler Set · generation failed
• HG-027 Table Lamp · not posted to Slack
• HG-016 Cutting Board · Drive save failed

Approved, not in Drive (2): HG-009, HG-012
Ready to generate (4) · No Shot Idea (24)

Open the catalog
```

| Line | Source |
| --- | --- |
| Counts per stage | Products grouped by `stage` |
| Waiting on Ellie, with days waiting | Stage `with_ellie`; age from the review's `created_at` |
| Thread link | The review's stored channel and thread timestamp, turned into a link with Slack's `chat.getPermalink` |
| Needs attention | Stages `failed` and `post_failed`, plus any product with a `delivery_error` |
| Spend line | Added by section 3 |

Long lists are cut off ("and 12 more, see the catalog"), because a Slack message allows 50 blocks and 3,000 characters per text block.

### Backend changes

**New `backend/services/status_report.py`**

- `build_report(rows)`: a pure function from catalog rows to Slack blocks. No Slack or database calls, so it is easy to test.
- `send_report()`: reads the catalog, builds the report, fetches thread links, posts it.
- The scheduler: a background thread that sleeps until the next scheduled time, sends, and repeats.

**`backend/services/catalog.py`**

- `product_view` returns `stage`.

**`backend/repositories/products.py`**

- `get_products` attaches only `state`, `approved_by` and `approved_at` to each image's review. Add `created_at`, `slack_channel` and `slack_thread_ts`, which the report needs for "waiting N days" and the thread link.

**`backend/slack.py`**

- Add a permalink method to `SlackClient`.

**`backend/app.py`**

- Start the scheduler on startup and stop it on shutdown, beside the Socket Mode listener.

**New route `POST /api/status-report`**

- Sends the report immediately. It lets someone trigger it from the web app, and makes the feature testable and demoable without waiting for the schedule.

**Settings**

| Variable | Purpose |
| --- | --- |
| `SLACK_STATUS_CHANNEL_ID` | Where the report goes. Unset means no report is sent. The bot must be invited to the channel. |
| `STATUS_REPORT_TIME`, `STATUS_REPORT_TIMEZONE` | When it is sent. |
| `APP_URL` | The "Open the catalog" link. |

**Database**

- No schema change. The report is computed from existing tables each time and nothing about it is stored.

### Scheduling

The app already runs as exactly one always-on process (Socket Mode and SQLite both require it), so an in-process timer is enough; no cron service or job queue is needed.

Because nothing is stored, the scheduler only ever sends at the *next* scheduled time after it starts. A restart never sends a duplicate. A restart at the exact scheduled minute skips that one report; the manual route covers it.

### Frontend changes

- Read `row.stage` from the API; delete the local `productStage`.
- Optional: a "Send status to Slack" button calling the new route.

### Failure cases

| Case | Behaviour |
| --- | --- |
| Channel not configured | The scheduler does not start; the manual route returns a clear error. |
| Bot not in the channel, or Slack is down | Logged; the next scheduled report runs as normal. The manual route returns Slack's error. |
| A thread link cannot be fetched | That line is sent without a link. |
| Empty catalog | No report is sent. |
| Two backend processes | Two reports. Already ruled out by the single-process requirement. |

### Tests to add

- The Python stage function matches the frontend's precedence for every stage, including a product with an earlier approval and newer failed candidates.
- `build_report` output for a mixed catalog: counts, grouping, truncation of long lists.
- "Waiting N days" uses the oldest waiting candidate of the current brief.
- The manual route posts once and reports Slack errors.
- The scheduler computes the next run correctly across a weekend and a timezone.

### Decided

- **When:** weekdays at 9:00. `STATUS_REPORT_TIME` defaults to `09:00`; no report on Saturday or Sunday.
- **Mentioning Ellie:** not for now. The report names her as plain text and notifies nobody, so it does not depend on `SLACK_APPROVER_USER_ID`. She sees it only if she follows the status channel; an @mention can be added later.

### Open choices

1. **Timezone:** 9:00 needs a timezone, and the team's is not known. `STATUS_REPORT_TIMEZONE` must be set at deploy time; the server's own clock is usually UTC.
2. **Detail level:** proposed to list products for "Waiting on Ellie" and "Needs attention" and give counts only for the rest.
3. **Quiet days:** send even when nothing changed, or skip. Proposed: always send, so silence never means "broken".

## 3. Running spend total in the report and the dashboard

### What it does

Maya sees one number, the running total spent on generation, in two places: the Slack status report (section 2) and the catalog page of the web app. In either place she can click **Reset spend**, which brings the total back to $0 for the next drop. There is no breakdown over time and no per-drop history.

### What exists today

The catalog header already shows "N images · about $X". The browser computes it as the count of finished images times a fixed estimate, `EST_COST_PER_IMAGE_USD = 0.0644` in `backend/services/catalog.py` (the upper end of Luma's published price). It cannot be reset and it does not appear in Slack.

### How the total is computed

The total is not a stored counter. It is counted from the image rows each time it is shown:

> images Luma returned since the last reset × the per-image estimate

- "Images Luma returned" means images with a Luma generation ID, whether or not they were later posted or approved.
- "Since the last reset" compares each image's existing `generated_at` with the stored reset time. With no reset yet, every image counts.

One function, `spend_total()` in a new `backend/services/spend.py`, computes it. The Slack report and the web app both read it, so they always show the same figure.

The total is an **estimate** and is labelled so. It can undercount: a generation cut off by a restart or a timeout may have been charged by Luma but has no generation ID stored.

### How reset works without a database change

A reset has to be remembered somewhere, or the total would return after the next restart. To keep the database schema untouched, the reset time is stored in a small file, `spend_reset.json`, in `DATA_DIR`. That is the same persistent volume that holds the SQLite file and the images, so it survives restarts and deploys.

| Step | What happens |
| --- | --- |
| Maya clicks Reset spend (Slack or web) | A confirmation shows the current total |
| She confirms | The file is written with the current time |
| Next read | No image is newer than the reset, so the total is $0 |
| New generations | Counted from then on |

Nothing is deleted: the image rows stay. The previous total is no longer shown anywhere, so on every reset the app posts one line to the status channel as a record:

```
Spend reset. Previous total: 64 images · about $4.12
```

### In the Slack report

One line under the status counts, with a button:

```
Spend so far (estimate): 64 images · about $4.12        [Reset spend]
```

The button is a new Socket Mode action, `reset_spend`, handled the same way as Approve and More options (section 1); it needs no HTTP endpoint. It has a Slack confirm dialog. After a reset, the button's message is updated to show $0.

### In the web app

- The catalog header's spend figure stays where it is, now read from the backend instead of computed in the browser.
- A **Reset spend** button sits beside it, with a confirmation.
- There is no separate Cost page.

### Backend changes

- **New `backend/services/spend.py`:** `spend_total()` and `reset_spend()`, including reading and writing `spend_reset.json`.
- **`backend/services/catalog.py`:** `summarize` adds a `spend` entry (image count, estimated dollars, reset time) to the catalog response, so the web app gets it with the data it already polls. No new read route.
- **New route `POST /api/spend/reset`:** for the web button.
- **`backend/services/review.py` (or the action dispatcher from section 1):** handle `reset_spend`.
- **`backend/services/status_report.py`:** add the spend line and button to the report.
- **Database:** no change and no migration.

### Frontend changes

- `CatalogView.tsx`: read `catalog.spend` for the header figure and add the Reset spend button.
- `catalogApi.ts`: the `spend` field on `Catalog` and a `resetSpend()` call.
- Per-click estimates ("about $0.26" on Generate) stay as they are.

### Failure cases

| Case | Behaviour |
| --- | --- |
| Reset clicked by mistake | The previous total is in the status channel as a posted line, but the running total cannot be restored from the app. The confirmation is the safeguard. |
| The reset file is missing or unreadable | Treated as "never reset": the total covers every image. |
| The volume is lost | The database is lost with it, so the total is $0 either way. |
| The per-image estimate is changed later | The current total is re-priced, because the price is not stored per image. |
| Reset while a generation is running | An image counts by the time it was queued, so a batch queued before the reset stays in the old total. |

### Tests to add

- The total counts returned images only, not queued or failed ones.
- After a reset the total is zero, and a later image is counted.
- A missing or corrupt reset file gives the all-images total.
- The Slack action and the web route both reset, and both post the "previous total" line.
- The catalog response and the Slack report show the same figure.

### Open choices

1. **Who may reset:** proposed as anyone, in the status channel or the web app. The web app has no login, and restricting the Slack button would need Maya's Slack user ID as a setting.
2. **Estimate or actual price:** proposed to keep the fixed estimate. I have not checked whether Luma's API reports a per-generation cost.

### Decided

- One running total with Reset spend, in Slack and the web app; no time breakdown in this version.
- Every reset posts the "previous total" line to the status channel.

### Left out on purpose

Spend by day, per-drop history, cost per approved image, a count of unused candidates, and a budget cap. The image rows already hold the data for the first four, so they can be added later without changing how images are stored.

The expected next step is a **breakdown by drop**. The "previous total" lines in the status channel are the interim record; a proper version would store each reset as a named period (one small table, the app's first migration) and show a total per drop.
