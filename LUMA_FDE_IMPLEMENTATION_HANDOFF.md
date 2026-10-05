# Luma FDE Take-Home — Implementation Handoff Spec

> **Current MVP decisions (2026-09-29):** Ellie imports the CSV in the app,
> selects rows with Shot Ideas, explicitly generates two candidates, and then
> explicitly sends ready candidates to a product-specific Slack thread. Team
> comments are advisory; only Ellie's Approve or Reject button is authoritative.
> The app records her decisions. Once a request has at least two approved images,
> the operator clicks **Write approved images to Drive**. Drive delivery is the
> boundary of this version. Feedback-guided regeneration and product-page
> publishing are outside this version's scope. Where older sections below
> describe automatic Slack posting, regeneration, or publication status, these
> current decisions take precedence.

## 1. Purpose

This document is the implementation handoff for the Luma Forward Deployed Engineer take-home.

The customer is a six-person home-goods brand with roughly 300 products. Today, creative requests for styled product photography are fragmented across Google Sheets, Slack, and Gmail. A single operator, Ellie, manually reconstructs the backlog, sends shot requests to a freelance photographer, gathers feedback in Slack, makes the final decision, and then manually ensures approved images land in Google Drive and eventually on product pages.

The goal is not to build a generic AI image-generation dashboard.

The goal is to build a workflow that takes a structured shot request and reliably turns it into approved, traceable, correctly stored creative assets while fitting the tools the team already uses.

The implementation should prioritize a complete, reliable slice over broad but incomplete automation.

---

## 2. The Problem Is Actually Two Problems

The current workflow contains two distinct product problems.

### Problem A — Disparate Intent Aggregation

Creative intent is fragmented across:
- Google Sheets
- Slack
- Gmail
- ad hoc human memory

Examples:
- A product has a `Shot Idea` in the spreadsheet.
- Someone posts a request in Slack, everyone reacts to it, but nobody adds it to the sheet.
- Someone emails Ellie with an idea that never makes it into the canonical backlog.

Today, Ellie manually consolidates all of this into a spreadsheet.

A fully automated version of this problem would require:
- ingestion from multiple systems,
- entity resolution to products/SKUs,
- duplicate detection,
- ambiguity handling,
- provenance tracking,
- human confirmation of whether a message is actually a real production request.

This is a meaningful problem, but it is not the first implementation target.

### Problem B — Shot Production and Approval

Assume the CSV is already populated correctly.

For each row that has:
- a product identifier / SKU,
- a source product photo,
- a non-empty `Shot Idea`,

the system should:
1. import the row,
2. show what is eligible,
3. allow explicit generation,
4. generate one or more candidate styled images,
5. send candidates to Slack for team feedback,
6. let Ellie explicitly approve, reject, or request regeneration,
7. reflect Slack decisions back into the product,
8. collect approved images,
9. rename them deterministically,
10. upload approved assets to Google Drive,
11. expose current status to Maya.

**Problem B is the primary scope for the take-home implementation.**

The system should solve Problem B end-to-end before attempting ambitious automation for Problem A.

---

## 3. Product Principle

Do not centralize every user interaction.

Centralize the workflow state.

Use the team's existing tools for the jobs they are already good at:

- CSV / Google Sheet export → structured input
- Slack → team discussion and Ellie's explicit approval
- Google Drive → final asset storage
- Web app → orchestration, generation controls, operational status, and Maya's overview

The web app is not intended to replace Slack or Google Drive.

It is the workflow engine and source of truth.

---

## 4. Primary Users

### Ellie — Operator

Ellie owns the operational workflow.

Her main questions are:
- What is ready to generate?
- What is currently generating?
- What is waiting for team review?
- What needs my final decision?
- Which approved assets are ready to deliver?

Ellie should not need a complex dashboard.

Her product surface should prioritize actions and exceptions.

### Maya — Founder / CEO

Maya does not care about the mechanics of image generation.

Her main questions are:
- How many products are ready?
- What is blocked?
- What is waiting on Ellie?
- How close is the 40-product drop to completion?
- How much does the process cost?
- How many generations are needed per approved image?

Maya should receive a compact status / KPI dashboard.

---

## 5. Scope of the MVP

### In Scope

- CSV upload
- CSV validation
- preview of eligible rows
- explicit generation controls
- generation through Luma
- persistence of shot requests and generations
- Slack posting
- Slack interactive approval
- Slack rejection
- Slack regeneration with feedback
- generation lineage/history
- approved-image collection
- deterministic filenames
- Google Drive upload
- status tracking
- 40-product launch/drop view
- Maya status dashboard
- deployment

### Out of Scope for V1

- autonomous reading of all Slack history
- autonomous reading of all Gmail
- automatic interpretation of every Slack conversation
- live two-way Google Sheets synchronization
- Shopify / CMS publishing
- sophisticated image editing tools
- complex RBAC / enterprise auth
- multi-team collaboration
- native mobile app
- a generic creative dashboard
- automatic generation immediately on CSV upload

### Highest-Priority Next Features

1. Slack `Add to Shot Queue` message action
2. forwarded-email capture
3. Google Sheets live sync
4. CMS / Shopify publishing
5. duplicate recommendation / merge tools for upstream idea aggregation

---

## 6. Core End-to-End Workflow

```text
CSV
 ↓
Import + validate
 ↓
Preview eligible shot requests
 ↓
Explicit user selection
 ↓
Generate one first-pass candidate
 ↓
Show generation in web app
 ↓
Send product to Slack
 ↓
Team discusses in thread
 ↓
Ellie:
   Approve
   Regenerate
   Reject
 ↓
Slack action calls backend
 ↓
Backend updates canonical state
 ↓
Approved images appear in app
 ↓
Rename deterministically
 ↓
Upload selected approved images to Google Drive
 ↓
Update delivery status
 ↓
Maya dashboard reflects progress
```

---

## 7. CSV Import Experience

The system must support fresh CSVs with the same general schema.

The initial entry point should be a standard upload flow.

Google Drive file-picking can be added later if time permits.

### Example screen

```text
Import catalog

[ Drop catalog.csv here ]

[ Choose file ]
```

After upload, do not spend money.

Parse and classify the rows first.

### Example result

```text
40 products imported

32 ready to generate
5 missing Shot Idea
2 missing source image
1 malformed row
```

Show the valid requests in a table.

Suggested columns:
- SKU
- Product name
- Color
- Source photo
- Shot Idea
- Eligibility
- Existing workflow state

### Generation Controls

Support:
- generate selected
- generate first N
- generate all eligible

Before generation, show:
- number of products selected,
- number of first-pass generations,
- estimated spend if available.

Uploading a CSV must never automatically trigger paid generation.

---

## 8. Idempotency and Re-Imports

Fresh exports will continue to arrive.

The system must not blindly duplicate work when the same CSV or same product rows are re-imported.

Use SKU as the primary customer-facing identity where possible.

A useful internal request signature is:

```text
hash(
  sku
  + source_photo_url
  + shot_idea
  + prompt_version
)
```

If the same request has already been created:
- preserve its workflow state,
- do not regenerate automatically.

If the `Shot Idea` or source photo changes:
- create a new request version or mark the existing request as changed,
- require explicit generation again.

---

## 9. Image Generation Strategy

### First-Pass Behavior

For V1:

**Generate one first-pass concept per shot request.**

Do not generate 3–4 images immediately.

Reason:
- image generation costs money,
- Ellie may reject the overall direction,
- generating several variants before creative-direction approval wastes money and attention.

### Staged Generation

Recommended strategy:

```text
Shot Request
   ↓
1 concept
   ↓
Ellie/team review
   ↓
direction approved
   ↓
generate 2 additional finals
   ↓
Ellie selects 2–3 approved assets
```

This directly addresses Maya's instruction:

> Do not burn budget on images Ellie will reject.

### Prompting

The generation prompt should incorporate both:
- the source product image,
- the human `Shot Idea`.

It should explicitly preserve product identity.

Example generated prompt:

```text
Create a photorealistic ecommerce lifestyle image.

Preserve the exact product shown in the source image:
- preserve shape
- preserve color
- preserve material
- preserve logo and markings
- preserve proportions

Requested scene:
"Morning kitchen counter, steam, warm light."

Use natural residential styling.
The product remains the visual subject.
Do not add text.
Do not modify the product itself.
```

Product fidelity is a first-class quality requirement.

---

## 10. Core Domain Model

### Product

```text
Product
- id
- sku
- name
- color
- price
- source_photo_url
- created_at
- updated_at
```

### ShotRequest

```text
ShotRequest
- id
- product_id
- shot_idea
- request_version
- request_signature
- batch_id / drop_id (optional)
- status
- created_at
- updated_at
```

### Generation

```text
Generation
- id
- shot_request_id
- parent_generation_id (nullable)
- provider_generation_id
- image_url
- local/storage reference
- prompt
- feedback
- generation_type
- status
- estimated_cost
- created_at
```

### Approval

```text
Approval
- id
- generation_id
- decision
- approved_by
- slack_user_id
- created_at
```

### Asset

```text
Asset
- id
- product_id
- shot_request_id
- generation_id
- canonical_filename
- drive_file_id
- drive_url
- status
- created_at
```

### Batch / Drop

```text
Batch
- id
- name
- target_product_count
- created_at
```

Use this for the upcoming 40-product launch.

---

## 11. Workflow State Machine

A useful state model:

```text
IMPORTED
  ↓
READY_TO_GENERATE
  ↓
GENERATING
  ↓
READY_FOR_REVIEW
  ↓
IN_SLACK_REVIEW
  ↓
  ├── REGENERATION_REQUESTED
  │        ↓
  │    GENERATING
  │        ↓
  │    IN_SLACK_REVIEW
  │
  ├── REJECTED
  │
  └── APPROVED
           ↓
      READY_FOR_DRIVE
           ↓
        IN_DRIVE
           ↓
       PUBLISHED
```

`PUBLISHED` may remain manually tracked in V1 because no ecommerce CMS API is provided.

---

## 12. Slack Review Model

Slack is the collaboration surface.

The web app posts candidate images to Slack.

Do not send twenty products in one giant message.

Batching is allowed at the control level, but each product should have an independent Slack message/thread.

Example:

```text
Ceramic Vase · SKU VASE-042

Shot idea:
"Holiday mantel with evergreen"

[ GENERATED IMAGE ]

[ Approve ]
[ Regenerate ]
[ Reject ]
```

### Team Feedback

Anyone can:
- react,
- comment,
- discuss in the thread.

These comments are advisory.

The system does not automatically infer approval from natural-language Slack comments.

### Ellie Is the Decision-Maker

Only Ellie's explicit structured action should change canonical workflow state.

Do not treat:
- emoji reactions,
- generic thread comments,
- casual messages

as production decisions.

---

## 13. Slack → Backend Interaction

This is a key architectural requirement.

Slack and the web app are not independent systems that need synchronization.

They are two clients of the same backend.

When posting a Slack message, button metadata should identify the canonical generation.

Example conceptually:

```text
action_id = approve_generation
value = generation_839
```

Ellie clicks Approve.

Slack sends an interaction payload to:

```text
POST /api/slack/actions
```

The backend validates:
- Slack signature,
- acting user,
- action,
- generation identity.

Then updates the DB.

Example:

```text
generation.status = APPROVED
approval.approved_by = Ellie
approval.slack_user_id = ...
approval.created_at = now
```

The website simply reads the same database.

Therefore the next refresh / poll / realtime update reflects the Slack decision immediately.

---

## 14. Approval Behavior

### Approve

Ellie clicks:

```text
Approve
```

The generation becomes approved.

If this is the first concept:
- either mark direction approved,
- or start the final-generation step.

If enough final images are approved:
- mark the `ShotRequest` complete.

### Reject

Ellie clicks:

```text
Reject
```

The candidate remains stored for history but is no longer active.

Never delete generation history.

### Regenerate

Ellie clicks:

```text
Regenerate
```

Open a Slack modal.

Example:

```text
What should change?

[ Less staged, warmer light, keep the vase larger ]

[ Generate again ]
```

Persist this feedback.

Create a new `Generation` record:

```text
parent_generation_id = prior_generation.id
feedback = "Less staged..."
status = GENERATING
```

Generate a replacement and post it back into the same Slack thread.

Example:

```text
New version generated

Ellie's feedback:
"Less staged, warmer light."

[ NEW IMAGE ]

[ Approve ]
[ Regenerate ]
[ Reject ]
```

Generation history should look like:

```text
v1 — rejected
     too staged

v2 — approved
```

This lineage supports:
- auditability,
- cost analysis,
- debugging,
- quality analysis.

---

## 15. Direction Approval vs Final Approval

The requirement says a completed request means:

> 2–3 approved images matching the shot idea.

A cost-conscious strategy is:

### Stage 1 — Direction

Generate 1 image.

Slack asks:

```text
Approve direction
Regenerate
Reject
```

### Stage 2 — Finals

Once direction is approved:
- generate 2 more images,
- present 3 total or 2–3 final candidates,
- let Ellie approve the final set.

Then:

```text
ShotRequest.status = APPROVED
```

when the configured number of final images has been approved.

This should be configurable enough to simplify implementation if needed.

---

## 16. Approved Assets and Naming

Approved assets should never preserve arbitrary provider or camera-style filenames.

Do not ship:

```text
IMG_4382.jpg
```

Use deterministic names:

```text
VASE-042_styled_01.jpg
VASE-042_styled_02.jpg
```

Possible internal variant:

```text
VASE-042_shot-123_styled_01.jpg
```

The customer-facing filename should remain simple.

---

## 17. Google Drive Delivery

After approval, the web app should expose a final delivery screen.

Example:

```text
READY FOR DRIVE

8 approved images

VASE-042_styled_01.jpg
VASE-042_styled_02.jpg
MUG-018_styled_01.jpg
MUG-018_styled_02.jpg

[ Upload 8 to Google Drive ]
```

Upload should:
1. download or retrieve the approved generated asset,
2. rename it,
3. place it into the configured Drive destination,
4. persist Drive metadata.

Store:
- Drive file ID,
- Drive URL,
- upload timestamp.

Recommended folder structure:

```text
Styled Product Photos/
  October Drop/
    VASE-042_styled_01.jpg
    VASE-042_styled_02.jpg
```

or:

```text
Styled Product Photos/
  VASE-042/
    VASE-042_styled_01.jpg
```

Choose one and document the assumption.

---

## 18. Ellie Web Experience

Ellie's web view should be operational.

Suggested sections:

```text
Needs input
Ready to generate
Generating
Ready for Slack
Waiting for review
Needs my decision
Approved
Ready for Drive
```

Possible UI:

```text
SHOT PRODUCTION

Ready to generate      12
Generating              4
Team reviewing          7
Needs my approval       3
Approved                 6
Ready for Drive          4
```

Prioritize:
- what needs action,
- failures,
- blocked work.

Avoid vanity analytics.

---

## 19. Maya Web Experience

Maya receives a separate status-oriented view.

For the 40-product launch:

```text
OCTOBER DROP

27 / 40 ready

3 missing shot ideas
2 generating
5 waiting for Ellie
3 ready for Drive
27 complete
```

Useful KPIs:
- completion percentage,
- first-pass approval rate,
- average generations per approved image,
- estimated cost per approved image,
- median time from generation to approval,
- number blocked by human review.

Maya's primary need is launch readiness and bottleneck visibility.

---

## 20. 40-Product Drop

Model the upcoming launch as a batch.

Example:

```text
Batch: October Drop
Target: 40 products
```

Every `ShotRequest` may optionally belong to a batch.

The dashboard should answer:

```text
How many of the 40 are:
- missing input,
- ready,
- generating,
- in review,
- approved,
- delivered?
```

This should be one of the strongest visual elements in the demo.

---

## 21. Failure Handling

Handle at least the following:

### CSV
- malformed rows,
- duplicate SKUs,
- missing source photo,
- missing Shot Idea,
- invalid URLs.

### Generation
- provider timeout,
- provider failure,
- invalid source image,
- generation polling failure.

### Slack
- failed message post,
- expired / invalid action,
- unauthorized user attempting approval.

### Drive
- upload failure,
- auth failure,
- filename collision.

Failures should be visible and retryable.

Do not silently swallow errors.

---

## 22. Authorization

For V1, configure one Slack user as Ellie / authorized approver.

When an approval action arrives:
- verify Slack signature,
- verify acting Slack user,
- allow the configured approver,
- reject or no-op other users for final approval.

Other users may still discuss in the thread.

This preserves the existing workflow rule:

> everyone gives feedback, Ellie decides.

---

## 23. Unit Economics

Track enough information to compute:

```text
generation_count
estimated_generation_cost
approved_image_count
```

Then calculate:

```text
cost_per_approved_image
=
total_generation_cost
/
approved_image_count
```

Also track:

```text
generations_per_approved_image
```

The staged-generation strategy should be explained as a cost-control mechanism.

At 10x catalog size:
- async generation,
- queueing,
- retries,
- provider rate limits,
- storage costs,
- Slack volume,
- batch operations

will matter more.

Do not build unnecessary 10x infrastructure now, but structure the implementation so background work is not tied to a single browser request.

---

## 24. Suggested Architecture

```text
                   ┌────────────────────┐
CSV Upload ───────▶│     Web App/API    │
                   └─────────┬──────────┘
                             │
                         Database
                             │
             ┌───────────────┼───────────────┐
             │               │               │
             ▼               ▼               ▼
       Generation Worker   Slack API    Google Drive API
             │               │               │
             ▼               │               ▼
          Luma API            │           Final assets
             │               │
             ▼               │
      Generated images        │
             │               │
             └──────▶ Slack review
                              │
                              ▼
                     Ellie action webhook
                              │
                              ▼
                          Backend DB
```

Use:
- background processing for generation,
- durable workflow state,
- provider IDs persisted in DB,
- explicit retries.

---

## 25. Recommended Build Order

Build in this exact order unless a dependency forces otherwise.

### Milestone 1 — Core Model
- database schema,
- Product,
- ShotRequest,
- Generation,
- Approval,
- Asset,
- Batch,
- state enums.

### Milestone 2 — CSV
- upload,
- parse,
- validate,
- preview,
- idempotent import.

### Milestone 3 — One Generation
- select one eligible request,
- call Luma,
- persist provider job ID,
- poll / await completion,
- store result,
- render image.

### Milestone 4 — Slack Happy Path
- configure Slack app,
- post one generated candidate,
- include Approve button,
- receive Slack action callback,
- update DB,
- reflect approval in web UI.

At this point the critical product loop exists:

```text
CSV row
→ Luma
→ Slack
→ Ellie clicks Approve
→ DB says APPROVED
```

### Milestone 5 — Slack Feedback Loop
- Reject,
- Regenerate,
- modal feedback,
- parent generation lineage,
- new generation posted into same thread.

### Milestone 6 — Batch Operations
- generate multiple selected rows,
- send selected generations to Slack,
- separate message/thread per product.

### Milestone 7 — Drive
- approved-assets page,
- deterministic filenames,
- upload selected assets,
- persist Drive metadata.

### Milestone 8 — Maya
- batch/drop dashboard,
- workflow counts,
- completion percentage,
- basic unit economics.

### Milestone 9 — Production Hardening
- retries,
- idempotency,
- failure states,
- duplicate protection,
- auth validation,
- deployment,
- demo dataset.

---

## 26. Critical Demo Flow

The demo should show the real deployed system.

Recommended demo:

1. Open deployed app.
2. Upload `catalog.csv`.
3. Show import summary.
4. Select a small number of valid rows.
5. Click Generate.
6. Show a real Luma generation returning.
7. Send the generated candidate to Slack.
8. Open Slack.
9. Show the team-review message.
10. Click Regenerate once and provide feedback.
11. Show the new image appear in the same thread.
12. Click Approve as Ellie.
13. Return to the app.
14. Show the same generation now marked approved.
15. Open `Ready for Drive`.
16. Show canonical filenames.
17. Upload approved assets to Drive.
18. Show successful Drive delivery.
19. Show Maya's 40-product launch status screen.

This demonstrates:
- real external integrations,
- human-in-the-loop control,
- state coherence,
- cost-conscious generation,
- final delivery.

---

## 27. Problem A — Future Intent Aggregation

Do not attempt broad autonomous Slack/Gmail archaeology in the MVP.

The first incremental solution should be explicit capture.

### Slack `Add to Shot Queue`

Someone posts:

```text
"The blue lamp would look great beside a reading chair."
```

A Slack message action:

```text
Add to Shot Queue
```

captures:
- raw message,
- author,
- permalink,
- timestamp,
- source channel.

AI may suggest:

```text
Product:
Blue Lamp · SKU LAMP-012

Suggested Shot Idea:
"Blue lamp beside a reading chair in a cozy evening living room."
```

Ellie confirms or edits it.

Only then does it become a canonical `ShotRequest`.

This follows the rule:

> machines retrieve and structure; humans commit meaning.

### Future Gmail

A later version may support:
- forwarding to a dedicated inbox,
- parsing the message,
- suggesting product + shot brief,
- sending it to Ellie for confirmation.

### Important Constraint

Do not allow an autonomous model to silently convert random Slack or email conversation into paid generation work.

Human confirmation must happen before a new canonical production request is committed.

---

## 28. Main Product Insight

The customer does not primarily need another AI image generator.

They need a reliable production workflow around creative intent.

The system should solve:

```text
creative intent
      ↓
structured shot request
      ↓
controlled generation
      ↓
human feedback
      ↓
explicit approval
      ↓
traceable asset
      ↓
correct delivery
```

The system centralizes state, not every interaction.

Slack remains where people talk.
Drive remains where final files live.
The web app becomes the orchestration and visibility layer.

---

## 29. Implementation Guardrails for Coding Agents

Coding agents should follow these rules:

1. Do not automatically generate on import.
2. Do not overwrite prior generations.
3. Preserve generation lineage.
4. Treat Slack discussion as advisory.
5. Only explicit Ellie actions change approval state.
6. Never use emoji reactions as canonical approval.
7. Use deterministic asset names.
8. Avoid posting many products into one Slack thread.
9. Persist external provider IDs.
10. Make externally-triggered actions idempotent.
11. Validate Slack signatures.
12. Keep secrets in environment variables.
13. Do not commit `.env.local`.
14. Prefer background jobs for Luma polling / long-running work.
15. Make failure states visible and retryable.
16. Optimize for a reliable demo path over feature breadth.
17. Do not begin Problem A until the CSV → generation → Slack → approval → Drive loop works.
18. Document assumptions and any intentionally simplified production behavior.

---

## 30. Definition of Success

The MVP is successful when the following is real, deployed, and repeatable:

```text
1. A user uploads a populated CSV.
2. The app identifies rows eligible for generation.
3. The user explicitly starts generation.
4. Luma returns a real styled image.
5. The app posts that image into Slack.
6. Ellie can approve, reject, or request regeneration in Slack.
7. Regeneration feedback produces a new candidate.
8. Slack decisions are reflected in the web app.
9. Approved images receive deterministic filenames.
10. Approved images can be uploaded to Google Drive.
11. Maya can see current launch / workflow status without asking Ellie.
```

Everything else is secondary until this loop works.
