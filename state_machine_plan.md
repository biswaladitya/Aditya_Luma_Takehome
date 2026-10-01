# Product state machine plan (2026-10-01)

This plan replaces Phase 0.5 of `ui_redesign_plan.md` (the "a CSV change replaces an approval not yet in Drive" rule). Where they conflict, this file wins. The rest of the UI redesign (import review, catalog, image view) stays as built, adjusted as described below.

## Rules

**The brief** is the set of product details sent to Luma: Photo, Shot Idea, Product Name, Color / Finish, Material (see `build_prompt` in `backend/services/generation.py`). Price, Category and Notes are info-only.

**The CSV is the source of truth for the products it lists.** A product missing from a new CSV is kept unchanged.

**An import can do four things to a product:**

| CSV row | Effect |
|---|---|
| New SKU | Added (Needs brief, or Ready to generate) |
| Brief changed | See the state table below |
| Only Price / Category / Notes changed | Updated; state and images unaffected |
| Unchanged, or missing from the CSV | Nothing |

SKUs are uppercase only. The importer rejects a row whose SKU contains lowercase letters (Needs correction).

**Product states (for the current brief):**

| State | Moves on when… |
|---|---|
| Needs brief | A CSV adds a complete brief |
| Ready to generate | Someone clicks **Generate**, which generates two candidates **and posts them to Slack** |
| *Generating and posting* (busy) | Candidates are posted to the product's Slack thread |
| In Slack, waiting on Ellie | Ellie clicks **Approve** |
| Approved, not in Drive | Someone clicks **Save to Drive** |
| *Saving to Drive* (busy) | The save finishes |
| In Drive | Done; a later brief change offers an optional regenerate |

Failures go back one step with a Retry and the error visible: all candidates failed → Retry generation; posting failed → Retry posting; save failed → Retry save.

**When an accepted import changes a product's brief:**

| Product was… | On Accept | Next step shown |
|---|---|---|
| Never generated (or only failed attempts) | Nothing else | **Generate** |
| In Slack, waiting on Ellie | Its candidates become Outdated: they can't be approved (Approve is refused) and their Slack Approve buttons are removed | **Generate** |
| Approved, not in Drive | The approval is **kept** and can still be saved | **Save to Drive**, plus optional **Regenerate** |
| In Drive | Kept | Optional **Regenerate** |

- Regenerating is **always an explicit click**, never automatic on import. Generate is the only action that spends credits.
- **Accept waits:** if a changed product has a generation, Slack post or Drive save in progress, Accept is refused with a clear 409 naming the SKUs ("wait for it to finish, then accept again").
- **Every approved image can be saved to Drive.** Drive filenames include the image's own version number, `<SKU>_styled_v<N>.<ext>` (e.g. `HG-002_styled_v3.png`), so a save never overwrites a different image and no approved image is lost. Save to Drive saves every approved image of that product not yet in Drive.
- Files already delivered under the old `<SKU>_styled_01.<ext>` name keep it; the filename is stored per delivery and is not renamed.
- The newest approved image is the product's "current" one; the catalog shows it as current and links older ones.

## Implementation

### 1. Brief versions (schema v7, migration from v6)

The local runtime database is already on v6, so this needs a v6 → v7 migration (and fresh installs go straight to v7).

- `products.brief_version` (integer, starts at 1) goes up only when a brief field changes. `products.version` still goes up on any change; import conflict detection keeps using it.
- `generated_images.brief_version` records the brief version at queue time.
- **An image is outdated when its `brief_version` is lower than its product's.** This replaces field-by-field snapshot comparisons and the `superseded_at` / `retired_before` / `is_current` logic. Keep `generated_from` as the record of what was sent to Luma.
- Approvals: at most one approved image per `(product, brief_version)` (replace the current partial unique index). Approvals are never set aside by imports. `superseded_at` is no longer used; migrate any superseded approval to an ordinary approval of its (older) brief version. Drop the column and trigger if SQLite migration allows it cleanly; otherwise leave the column unused and document it.
- Migration backfill: for each product, group its existing images by their saved brief fields in `generated_at` order, number the groups 1..n, and set the product's `brief_version` to the group matching its current brief (or n + 1 if none matches).
- The six-image cap counts non-failed images **per brief version**.

### 2. Import (`backend/services/imports.py`, `catalog.py`)

- Remove the approval-replacement logic and its 409s from confirm.
- Refuse confirm (409) when any brief-changed product has a generation queued/processing, a Slack post in flight, or a Drive save in flight.
- After a successful confirm, in the background (best effort, failures logged, never failing the import), edit the outdated awaiting-approval Slack messages to remove the Approve button and say "Outdated: brief changed". Approve on them is refused regardless.
- Preview rows get `brief_case`: `new`, `info_only`, `unchanged`, `invalid`, or for brief changes `not_generated`, `with_ellie`, `approved_not_in_drive`, `in_drive`.
- Lowercase SKUs are invalid rows.

### 3. Generate posts to Slack (`backend/services/generation.py`, `review.py`)

- When all of a product's queued candidates for a request finish and at least one succeeded, the job calls the existing Slack send for that product.
- A failed post leaves `can_send` true and the UI shows **Retry posting** (the existing `/api/reviews` endpoint).
- Generation is allowed for Ready products and, as an optional regenerate, for products whose brief changed after an approval or delivery. It is refused while a generation, post or save is running.

### 3b. Review states: only "awaiting approval" and "approved"

Because Generate now posts to Slack, `image_reviews` drops the `pending_send` state.

- `image_reviews.state` is `awaiting_approval` or `approved` only. A review row is created **only after the Slack post succeeds**, so `slack_message_ts` is always set. Update the CHECK constraint, the forward-only trigger (`awaiting_approval` → `approved`), and the partial unique index accordingly; drop `send_error` from `image_reviews`.
- Posting is the last step of a candidate's generation: a candidate stays `processing` until it is generated **and** posted (or the post fails). "Posting in flight" for the Accept-waits rule is therefore just "a candidate is queued/processing".
- If the post fails, the candidate becomes `done` with a new `generated_images.post_error` (text) and no review row. The product shows **Retry posting**, which calls the existing `/api/reviews` endpoint to post the done, current-brief candidates that have no review row; success creates the review rows and clears `post_error`.
- Restart recovery: candidates cut off mid-post are handled like other cut-off work (a done image with no review row and a recorded `post_error` such as "Posting was interrupted by a restart"), so they can be retried.
- Migration (part of v6 → v7): existing `pending_send` rows are removed from `image_reviews`, and their `send_error` (or "Not posted") moves to the image's `post_error`. Existing `awaiting_approval` and `approved` rows are kept as they are.
- API/UI: `ReviewState` becomes `'awaiting_approval' | 'approved'`; replace the row-level `send_error` with `post_error`; `can_send` means "has current-brief done candidates with no review row and not currently posting".

### 4. Approve and Save to Drive (`review.py`, `delivery.py`, `backend/drive.py`)

- Ellie can approve only candidates made from the product's current brief version.
- Save to Drive saves every approved, undelivered image for the product, each as `<SKU>_styled_v<N>.<ext>`. Keep the existing same-name lookup only as crash recovery for the same image.

### 5. Frontend

- **Import review** groups changed rows: New · Brief changed · Brief changed, waiting on Ellie (candidates become Outdated) · Brief changed, approved, not in Drive (approval kept) · Brief changed, in Drive · Info only · Needs correction.
- **Catalog:** remove **Send to Slack** buttons and the "To send" bulk action except as **Retry posting**. Approved and In Drive rows whose brief changed show a "Brief changed" note and a secondary **Regenerate** button. Generate's confirmation says the candidates will be posted to Slack. Outdated candidates show an "Outdated" tag. The newest approved image is current; older approved images stay visible.
- **Image view:** show Outdated / Approved for brief vN / In Drive with the filename.
- Update `productStage` in `catalogApi.ts` to match the state table.

### 6. Tests and docs

- Replace `tests/test_superseded_approvals.py` with brief-version tests: one per row of the "brief changes" table, info-only changes, the busy-state 409s, auto-posting after generation (with a fake Slack client), per-brief cap, versioned filenames and saving multiple approvals, lowercase SKU rejection, and the v6 → v7 migration on a v6 database with data.
- Update `ASSUMPTIONS.md` with these rules and two deliberate departures from the handoff: Generate posts to Slack automatically (Generate is still the only credit-spending action), and an approval survives a brief change.
- Update `persistence.md` (schema v7), `api_endpoints_breakdown.md`, `google_drive_writeback.md` (versioned filenames, no reserved `_01`/`_02`/`_03`), `DEVELOPMENT.md` (deleted components, new wording), and the Implementation status section of `AGENTS.md`.

## Validation

- `uv run --with pytest python -m pytest -q`: all green.
- `cd frontend && npm run build`: passes.
- No real Luma, Slack or Drive calls; don't touch `.env.local` or `runtime/`. No commits.
