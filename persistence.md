# Catalog persistence and change review

This document records the agreed persistence flow and function contracts. SQLite storage, pending previews, explicit catalog confirmation, and generation-status queries are implemented. Luma generation execution remains a future step in the hierarchy below.

## Persistence model

The implementation uses three tables: `products` (current catalog attributes and
an integer version), `pending_imports` (reviewed rows, expected versions, status,
and confirmation result), and `generated_images` (one record per image, linked to
its product by SKU). SQLite connections and transactions live in `backend/db.py`;
database functions live in `backend/repositories/products.py`.

The default database is `runtime/catalog.sqlite3`, created automatically on first
use. `DATA_DIR` and `DATABASE_PATH` allow configuration; see `DEVELOPMENT.md` for
local and container persistence instructions.

Each product is identified by its **SKU**. Persist the following catalog attributes:

- SKU
- Product Name
- Category
- Color / Finish
- Material
- Price
- Photo
- Shot Idea
- Notes

Associate each generated image with:

| Field | Purpose |
| --- | --- |
| `storage_key` | Durable image location, relative to the local storage root's `images/` directory in this implementation. |
| `generated_from` | Snapshot of the product attributes actually submitted to Luma. |
| `generated_at` | When the image was generated. |

Derive `image_exists` from the presence of a saved image reference. An existing image can represent older product attributes, so its existence alone does not establish that the product is up to date.

The `generated_images` table supports multiple candidates per product and keeps
their historical input snapshots. Product updates retain every image record.

## Upload and change rules

- Match incoming products to saved products by SKU, regardless of filename or row order.
- Flag missing or duplicate SKUs for review before saving.
- Identify new products and differences in attributes for existing products.
- Show previous and proposed values for changed fields.
- Uploading a CSV creates a preview; it does not update the saved product catalog.
- Clicking **Update product catalog** saves the reviewed changes and retains existing images.
- Products absent from an upload are not implicitly deleted from the catalog.
- Uploading, confirming a catalog update, or detecting a discrepancy never automatically starts paid generation.
- Ellie explicitly selects products and confirms generation or regeneration.

A file hash may help identify identical file contents, but product matching and change detection operate on persisted product records. A revised CSV can reuse unchanged products and their images while introducing new products or changed attributes.

## Two comparisons

| Comparison | Question answered |
| --- | --- |
| Incoming CSV values versus current saved product values | What would this upload change in the catalog? |
| Current saved product values versus `generated_from` | Does the existing image reflect the current product details? |

These comparisons must remain distinct. After a changed Shot Idea is saved, uploading that same CSV again produces no new catalog differences. However, the existing image still represents the older idea until Ellie generates a replacement.

When generation succeeds, record the exact attributes submitted to Luma. Do not substitute whatever attributes happen to be current when the result arrives.

## Simple user flow

1. **Upload CSV:** read the file and compare products by SKU with the saved catalog.
2. **Review changes:** show new products, changed fields, and existing images. The saved catalog is unchanged.
3. **Click Update product catalog:** save the reviewed changes and retain existing images.
4. **Review generation status:** show products without images, products whose images reflect older details, and products already up to date.
5. **Select products and click Generate:** send only the explicitly selected and confirmed products to Luma.
6. **Save results:** store each generated image together with the product details used to create it.

The next upload repeats the comparison against the saved catalog.

## Function call hierarchy

The following hierarchy expresses the agreed contracts. Concrete Python function
names use snake_case. Catalog endpoints are implemented; generation execution
(`POST /api/generations`) is a future integration and is not exposed yet.

```text
FRONTEND
onCsvSelected(file)
    |
    v
uploadCatalogPreview(file)
    | POST /api/catalog/preview
    v
BACKEND
previewCatalog(file)
    +-- parseCsv(file)
    +-- loadProductsBySku(skus)
    +-- compareCatalog(incomingRows, savedProducts)
    |
    | Returns preview; makes no catalog changes
    v
FRONTEND
displayCatalogPreview(preview)
    |
    | User clicks "Update product catalog"
    v
confirmCatalogUpdate(previewId)
    | POST /api/catalog/imports/{previewId}/confirm
    v
BACKEND
confirmCatalogImport(previewId)
    +-- loadPendingImport(previewId)
    +-- checkCatalogHasNotChanged(preview)
    +-- saveCatalogChanges(changes)
    +-- getGenerationStatus(products)
    |
    v
FRONTEND
displayCatalog(products, generationSummary)
    |
    | User selects products and confirms generation
    v
generateSelectedProducts(selections)
    | POST /api/generations
    v
BACKEND
generateImages(selections)
    +-- validateGenerationSelection(selections)
    +-- generateWithLuma(productSnapshot)
    +-- storeGeneratedImage(image)
    +-- saveGenerationResult(productSnapshot, imageReference)
    |
    v
FRONTEND
refreshCatalog()
    | GET /api/catalog
    v
BACKEND
getCatalog()
    +-- loadProducts()
    +-- getGenerationStatus(products)
```

## Function inputs and outputs

Implemented catalog routes:

| Route | Python service | Behavior |
| --- | --- | --- |
| `POST /api/catalog/preview` | `preview_catalog_import` | Validate all rows, compare by SKU, persist only a pending preview. |
| `GET /api/catalog/imports/{preview_id}` | `get_catalog_import` | Reload the original review or its stored confirmation result. |
| `POST /api/catalog/imports/{preview_id}/confirm` | `confirm_catalog_import` | Check expected versions and atomically apply the reviewed catalog changes once. |
| `GET /api/catalog` | `get_catalog` | Return every saved product with image history and derived generation status. |
| `GET /api/catalog/generation-candidates` | `get_catalog(candidates_only=True)` | Return eligible products with no matching generation; performs no generation. |
| `GET /api/catalog/images/{image_id}` | `local_image_path` | Serve a recorded local image from the configured image directory. |

`record_generated_image` is an internal service for recording an image reference
and the exact input snapshot after a future generation integration stores the
file. It does not call Luma or write image files itself.

| Function | Inputs | Outputs |
| --- | --- | --- |
| `onCsvSelected` | File chosen by the user. | Starts the preview upload. |
| `uploadCatalogPreview` | CSV file. | Preview response or validation errors. |
| `previewCatalog` | Uploaded file. | Preview ID, differences, counts, and validation errors. |
| `parseCsv` | File contents. | Normalized product rows and validation errors. |
| `loadProductsBySku` | Incoming SKUs. | Existing product records, including image information. |
| `compareCatalog` | Incoming rows and saved records. | New, changed, and unchanged products; previous and proposed field values. |
| `displayCatalogPreview` | Preview response. | Review screen with an Update product catalog button. |
| `confirmCatalogUpdate` | Preview ID. | Saved catalog response, or notice that the preview needs refreshing. |
| `confirmCatalogImport` | Preview ID. | Saved products and generation summary. |
| `loadPendingImport` | Preview ID. | Proposed rows and the catalog versions used for comparison. |
| `checkCatalogHasNotChanged` | Pending preview. | Permission to proceed, or a conflict requiring review. |
| `saveCatalogChanges` | Reviewed changes. | Inserted and updated records, preserving existing images. |
| `getGenerationStatus` | Products and their generation snapshots. | Status per product and counts: missing input, never generated, changed since generation, or up to date. |
| `displayCatalog` | Products and generation summary. | Product list, existing images, discrepancies, and generation controls. |
| `generateSelectedProducts` | Selected SKUs, expected product versions, and explicit regeneration confirmation where applicable. | Generation results or errors. |
| `generateImages` | Explicitly confirmed selections. | Saved generation results or errors for the selected products. |
| `validateGenerationSelection` | Selections and current database records. | Exact product snapshots authorized for generation, or errors. |
| `generateWithLuma` | Product snapshot containing the source photo and Shot Idea. | Generated image and provider metadata. |
| `storeGeneratedImage` | Generated image. | Durable image reference. |
| `saveGenerationResult` | Image reference and the exact snapshot sent to Luma. | Saved image record and `generated_from` snapshot. |
| `refreshCatalog` | No CSV required. | Latest catalog response for the frontend. |
| `getCatalog` | Catalog read request; no CSV required. | Latest persisted products, images, and generation statuses. |
| `loadProducts` | Catalog query. | Persisted product records and associated image information. |

## Pending previews and confirmation

A pending preview contains the proposed rows and the saved catalog versions used for comparison. It has a `previewId` so confirmation refers to the exact proposal the user reviewed.

When the user confirms an update, the backend checks that the relevant catalog records have not changed since the preview was created. Perform that check and the catalog writes in one database transaction. If the records have changed, return a conflict and request a refreshed review rather than overwriting newer values.

Preview storage is separate from the saved product catalog. The catalog changes only after explicit confirmation. Expiration and retention of pending previews remain implementation choices.

## Generation status

| Status | Meaning | UI behavior |
| --- | --- | --- |
| Missing input | Required inputs such as Shot Idea or source photo are absent. | Show what must be supplied before generation. |
| Never generated | Required inputs are present and no saved generated image exists. | Offer explicit generation. |
| Changed since generation | Saved images exist, but all were made from an older brief. | Show the images as Outdated with the brief differences; offer explicit regeneration. |
| Up to date | A saved image was made from the product's current brief. | Display the existing result; it moves on through Slack review. |

The comparison uses **brief versions**, not field-by-field snapshot
comparisons (next section). Only the brief (Photo, Shot Idea, Product Name, Color /
Finish, Material: what `build_prompt` sends to Luma) counts; Price, Category and Notes
are info-only. Differences are shown against the latest image's `generated_from`,
limited to brief fields. No discrepancy silently triggers paid generation.

Generation counts and existing-image displays come from persisted product and image records. Uploading the same CSV again must not create duplicate products or start another generation.

## Brief versions

The rules are in `state_machine_plan.md` and `ASSUMPTIONS.md`.

| Column | Meaning |
| --- | --- |
| `products.brief_version` | Starts at 1 and goes up only when a brief field changes. `products.version` still goes up on any change and is what import conflict detection uses. |
| `generated_images.brief_version` | The product's brief version when the candidate was queued. `generated_from` still records exactly what was sent to Luma. |
| `generated_images.post_error` | Why the candidate is not in Slack (post failed, or a restart cut it off). Cleared when Retry posting succeeds. |
| `image_reviews.brief_version` | Copied from the image when its review row is created, so the database can index approvals per brief. |

**An image is outdated when its `brief_version` is lower than its product's.** Ellie
can approve only candidates of the current brief (`reviews.approve` returns `outdated`
otherwise). Imports never set an approval aside: an approved image stays approved and
can still be saved to Drive after the brief changes.

| Rule | Enforced by |
| --- | --- |
| At most three approved images per product and brief | Counted in `reviews.approve` (`MAX_APPROVED_PER_BRIEF`) inside the `BEGIN IMMEDIATE` transaction, so two clicks can't both pass. Schema version 2 dropped the version-1 unique index `image_reviews_one_approved_per_brief`; opening a version-1 database drops it and keeps every row. |
| Review state is `awaiting_approval` → `approved` only | `CHECK (state IN ('awaiting_approval', 'approved'))` and the `image_reviews_forward_only` trigger. |
| A review exists only for a posted candidate | `slack_message_ts NOT NULL`; `reviews.record_post` inserts the row only after Slack accepts the message. |

`image_reviews` has no "pending send" state. Posting
is the last step of a candidate's generation: a candidate stays `processing` until it is
generated **and** posted (or the post fails, leaving it `done` with a `post_error` and no
review row). Retry posting (`POST /api/reviews`) puts those candidates back to
`processing` until posted. On startup, a `processing` candidate that Luma already
returned (it has a `luma_generation_id`) becomes `done` with "Posting was interrupted
by a restart"; anything else still `queued`/`processing` fails as before.

Derived per product in `product_view`: `brief_changed` (has images, none of the current
brief), `generating` (queued/processing, which includes posting), `can_generate`,
`review_status` (current brief only), `approved_image_id` (the newest approval; older ones
stay in `approved_image_ids`), `post_error`, `can_send` (current-brief done candidates with
no review row, and nothing generating), `delivering` and `can_deliver` (any approved image
not yet in Drive). Each image carries `outdated` and `posting`.

The 12-image cap counts non-failed images of the current brief version.

### Schema changes

`backend/db.py` defines one schema (`SCHEMA_VERSION = 2`) and creates it in an empty
database. There is one upgrade step, in `_ensure_schema`: a version 1 database has its
one-approval unique index dropped and becomes version 2, keeping every row. The upgrade is
one-way, so back up `catalog.sqlite3` before deploying; version 1 code refuses a version 2
database, and the index can't be put back once a brief has two approvals. Any other
version is refused at startup. A further schema change needs its own step added there.
