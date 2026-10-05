# UI redesign plan: from the canvas to `frontend/src`

Design canvas: https://claude.ai/artifact/PvSt13SisKmQvgimmbyXej (private to the owner until shared). It has four screens: Import review, Catalog, Image enlarged, and Next step by stage.

**The backend doesn't need to change.** Both new screens can be built from data `/api/catalog` and the import preview already return. The one exception is the "Open Slack thread" link (see Open decisions).

## Phase 0: Shared groundwork (small, do first)

- **Page switching:** `App.tsx` gets a simple `view: 'catalog' | 'import'` state instead of a router. The header nav switches between the two. A pending import saved in `localStorage` (existing behaviour) opens straight to the import screen.
- **Split `App.tsx`:** move the import screen into `ImportReview.tsx` and the catalog into `CatalogView.tsx`. `App.tsx` keeps the header, the polling, and the Slack/Drive/generate calls, and passes them down.
- **Restyle `style.css`:** keep the current colours (`#183a2f`, `#1d513a`, `#f7f7f2`) and fonts (DM Sans, Playfair Display), and remove the unused rules (hero, dropzone-as-hero, summary cards, confirmation bars).

## Phase 0.5: Backend — a CSV change replaces an approval not yet in Drive

> Superseded (2026-10-01) by `state_machine_plan.md`: brief versions replace this rule, and approvals are no longer set aside by imports.

Decided rule: an approved image that has **not** been saved to Drive is replaced ("superseded") when an accepted CSV import changes that product. An image already saved to Drive stays final. "Changed" keeps the current rule (any of the nine CSV fields differs from the image's `generated_from` snapshot); limiting it to the fields sent to Luma is a known limitation to record in `ASSUMPTIONS.md`.

| Area | Change |
|---|---|
| `backend/db.py` (schema v6) | Add nullable `superseded_at` to `image_reviews`. Replace the unique index `image_reviews_one_approved_per_sku` with one on `(product_sku) WHERE state = 'approved' AND superseded_at IS NULL`. Add a trigger so `superseded_at` can only go from NULL to a value, and only on an `approved` row. Write the v5 → v6 migration and keep fresh installs on v6. |
| `backend/services/imports.py` | On confirm, in the same transaction as applying the import: for each changed SKU with a live (non-superseded) approval and no `delivered` Drive delivery, set `superseded_at`. If that SKU has a Drive delivery in flight (`pending` with no error), refuse the confirm with a clear 409 naming the SKUs. |
| `backend/services/catalog.py` | Approval-derived fields (`review_status`, `approved_image_id`, `can_send`, `delivery_*`, `can_deliver`) ignore superseded approvals. Each image's `review` includes `superseded_at`. Preview rows get `replaces_approval: bool` for changed products whose live approval would be replaced. |
| `backend/services/generation.py` | Skip SKUs with a live approval ("Already approved") or a delivered image ("Already in Drive"). This fixes the dead end where an approved product could be regenerated into candidates that can never be reviewed. |
| `backend/services/review.py` | Refuse Ellie's Approve (private Slack note) when the image's snapshot no longer matches the product's current details, or the product already has a live approval. |
| `backend/services/delivery.py` | Re-check that the approval is live right before writing; skip with "Approval was replaced" if not. |
| Tests | One test per rule above, plus the migration. |
| Docs | `ASSUMPTIONS.md`: approval is final once in Drive; before that a CSV change replaces it. Update `persistence.md` for schema v6. |

Frontend for this: the import review warning on changed rows with `replaces_approval` ("Ellie approved an image for this product that hasn't been saved to Drive. Accepting will replace that approval, and the product will need new images."), a faded "Replaced" tag on superseded thumbnails, and "Approved, then replaced by a CSV import" in the image view. `canSelect` must also exclude rows with a live approval or a delivered image.

## Phase 1: Import review, changes only

**New `ImportReview.tsx`:**

- **Before a file is chosen:** a small drop area plus a **Choose CSV** button, replacing the large hero.
- **After upload:**
  - Title and file line, e.g. "catalog.csv · 40 rows · Nothing changes until you accept."
  - Count chips from `preview.changed_count`, `new_count`, `invalid_count` and `unchanged_count`.
  - A table of only the rows where `change_type !== 'unchanged'`. It's filtered in the browser because the preview already returns every row. Order: needs correction first, then changed, then new.
    - **Changed rows** list each changed field as a crossed-out old value above the new one. This reuses the existing `Changes` component.
    - **New rows** show a one-line summary of their fields.
    - **Changed rows with images** add a warning: "N existing images were made from the old Shot Idea." `row.images.length` already provides the count.
  - A "Show N unchanged" toggle that reveals a plain list of SKU and name.
  - A bottom bar that stays visible while scrolling, with **Discard** and **Accept changes**. If `can_confirm` is false, the bar lists `preview.errors` and Accept is disabled.
- **After Accept:** call `confirmCatalogUpdate`, switch to the catalog, and show a dismissible banner such as "Catalog updated. 1 changed, 2 new from catalog.csv."
- **Delete:** the separate "upload success" card, the four summary cards, and the use of `ProductTable` with `preview`.

**Done when:** uploading `data/catalog copy.csv` shows exactly one row (HG-002's Shot Idea change), and Accept lands on the catalog with the banner.

## Phase 2: Catalog with a Next step column

### 1. A stage function in `catalogApi.ts`: `productStage(row, perRequest)`

It returns one of the stages below and is used by both the row and the tabs. Order matters because the first match wins:

| Stage | Condition | Next step shown |
|---|---|---|
| `in_drive` | `delivery_status === 'delivered'` | "Saved", Open in Drive link |
| `saving` | `isDelivering(row)` | "Saving to Drive…" |
| `approved` | `can_deliver` | **Save to Drive**, or **Retry save** if `delivery_error` |
| `with_ellie` | `review_status === 'awaiting_approval'`, or `pending_send` with no error | "Waiting on Ellie" / "Posting to Slack…" |
| `generating` | `isGenerating(row)` | Progress bar: "N of 2" |
| `failed` | latest batch has a failed image and `canSelect` | **Retry generation**, plus the error text |
| `candidates_ready` | `can_send` | **Send to Slack**, or **Retry send** if `send_error` |
| `ready` | `canSelect(row)` | **Generate 2 images**, about $0.13 |
| `needs_input` | `missing_input` | "Add a Shot Idea in the CSV" |

### 2. New `CatalogView.tsx`

- **Header:** title, product counts, a **Generation spend** box (finished images × `est_cost_per_image_usd`), and an **Import CSV** button.
- **Tabs:** All, To generate, Generating, To send, With Ellie, To Drive, In Drive, Needs Shot Idea. Counts are calculated from `catalog.rows` with `productStage`.
- **Toolbar:**
  - With rows ticked: "N selected · 2N images · up to $X", plus **Clear** and **Generate 2N images**.
  - Otherwise it shows a hint, and on the To send / To Drive tabs a **Send all** / **Save all** button (the existing bulk actions).
- **Rows** (a grid of divs, not a `<table>`) with the columns tick box, Product, Shot Idea, Candidates, Stage, Next step:
  - The tick box appears only when `canSelect` is true (current rule).
  - **Candidates:** the latest batch's thumbnails. The approved one gets a ring and a tick, the other is faded ("Not selected"), and queued ones show dashed placeholders. Each thumbnail is a `<button>` that opens the image view.
  - **Stage:** a label plus a 4-part progress bar (Generate, Review, Approve, Drive).
  - **Shot Idea:** adds the note "Changed after these images were made" when `generation_changes` isn't empty.
- **Sorting:** by what needs action first: approved, candidates_ready, failed, ready, generating, with_ellie, in_drive, needs_input.
- **Per-row Generate** is new. It calls `generateImages([sku])` behind the same cost confirmation as the bulk path.

### 3. New `ImageLightbox.tsx` (image view)

- Built on the browser's `<dialog>`, which gives Esc to close and focus handling for free.
- **Left:** the large image with previous/next buttons.
- **Right panel:**
  - SKU and name
  - Status: Approved by Ellie with the time from `review.approved_at`, Not selected, In Slack, or Not sent
  - The Shot Idea the image was made from
  - A strip with the source photo and all candidates
  - The same next-step button as the row

### 4. Delete

`ProductTable.tsx`, the three action bars at the bottom of the page, the four summary cards and the standalone `GenerationProgress` panel. Progress now lives in each row.

**Done when:** one product can be walked from Ready → Generating → Candidates ready → With Ellie → Approved → In Drive entirely from the Next step column, and a thumbnail opens the image view.

## Decisions (2026-10-01)

1. **"Open Slack thread" link: dropped for now.** The With Ellie row shows "Waiting on Ellie" without a link. No backend change.
2. **Approved product whose details change:** handled by Phase 0.5. Not yet in Drive → the approval is replaced and the product returns to Ready to generate. Already in Drive → stays In Drive with the "Changed after these images were made" note and cannot be regenerated.
3. **Tests:** no frontend test framework. Rely on `npm run build` (type check), `pytest` for the backend, and a manual walkthrough.
4. **Partial failure** (one candidate done, one failed): the row shows the done candidate and "1 failed"; it can go to Slack with one candidate.
5. **Spend box** is labelled as an estimate (finished images × `est_cost_per_image_usd`).

## Order and checkpoints

1. Phase 0 and Phase 1 → build and try with `data/catalog copy.csv` → commit.
2. Phase 2 → walk one real product through Slack and Drive → commit.
3. Update `APPROACH.md`, and record the video after the redesign.
