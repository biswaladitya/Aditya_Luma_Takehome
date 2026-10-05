# Assumptions for the first release

I couldn't ask the team questions while building, so this file records what I would have asked, what I assumed instead, and how each assumption shaped the build. It has three parts: the assumptions that shaped the build most, how the design came about, and tables listing every assumption in the build.

## The assumptions that shaped the build most

### 1. The image problem is worth solving before the request-gathering problem

**What I'd ask:** which hurts more today: rebuilding the wishlist from the sheet, Slack and email, or waiting weeks for photos once the list exists?

**What I assumed:** the second. A complete list of shot ideas is worth nothing until someone makes the images, while generation and approval are useful from day one with the sixteen ideas already in the sheet. Gathering requests is also mostly a problem of team habits, and software that guesses at intent from Slack threads would spend money on wrong requests.

**What it changed:** the CSV is the only way requests enter the app. Nothing reads Slack history or Gmail. Someone still has to put the Shot Idea in the sheet before export.

### 2. "Done" means two to three approved images per product

**What I'd ask:** is one great image enough to ship a product page, or does it really need two or three?

**What I assumed:** the team's stated goal of 2–3 is the target, but one approved image shouldn't be held back waiting for a second.

**What it changed:** each batch is four candidates, so two or three approvals can come from one request. Ellie can approve up to three per product; the third closes the review. Save to Drive is available from the first approval, so the app allows 2–3 but does not insist on it.

### 3. Twelve generated images per product is enough, and a hard limit is wanted

**What I'd ask:** how many tries is a product worth before the shot idea itself is the problem?

**What I assumed:** twelve, which is three batches of four. Maya's "don't burn our budget on stuff she'll reject" reads as a request for a ceiling, not just a report.

**What it changed:** a product can have at most 12 generated images for the same brief; failed ones don't count. **More options** in Slack stops being offered at the cap. After that, the only way to get new candidates is to change the Shot Idea in the CSV, which starts a new count. The most one product and brief can cost is about $0.77.

### 4. Spend can be estimated from Luma's API price

**What I'd ask:** does Maya need the exact amount billed, or a figure close enough to make decisions with?

**What I assumed:** an estimate is enough. The app does not read Luma's billing; it multiplies the number of images Luma returned by **$0.0644**, the upper end of Luma's published price per image, so the figure errs high.

**What it changed:** the catalog page and the status report show "images · about $X" labelled as an estimate. The confirm dialogs before Generate and More options state the count and estimated cost before any credits are spent. If Luma's price changes, the figure is wrong until the constant in `backend/services/catalog.py` is updated, and it should be checked against a real invoice.

### 5. Ellie is the only approver

**What I'd ask:** does anyone ever approve when Ellie is away?

**What I assumed:** no. The brief says her pick is the decision and there is no other approval step. Teammates' comments in the thread are advice.

**What it changed:** only an **Approve** tap changes a product's state; comments and reactions do nothing. Setting `SLACK_APPROVER_USER_ID` to Ellie's Slack ID makes the app refuse everyone else's tap with a private note. **In the deployed test workspace that setting is left empty, so anyone in the channel can approve.** That is deliberate, so reviewers whose Slack IDs I don't know can try the flow, and it must be set before the team uses it.

### 6. The team wants to see how product attributes changed

**What I'd ask:** when a new export arrives, do you want it applied quietly or do you want to see what's different first?

**What I assumed:** they want to see it. A changed Shot Idea or photo affects work already done, and a team that once shipped the wrong file for three weeks shouldn't have changes applied out of sight.

**What it changed:** an upload is only a preview. The import review shows old → new values side by side, grouped by what accepting will do (new product, attributes changed, info only, needs correction), and nothing is saved until **Accept changes**. Every generated image also keeps a copy of the exact attributes it was made from, so an older image can always be traced to its brief.

### 7. Anyone on the team may upload a catalog, and the newest one wins

**What I'd ask:** who owns the sheet, and should anyone else be able to replace the catalog?

**What I assumed:** it's a six-person team that trusts each other, and Maya's export is the source of truth for the products it lists.

**What it changed:** there are no upload permissions. Anyone with the link can upload a CSV that overrides the saved attributes of every product it lists. The safeguards are the preview and Accept step, a refusal if the catalog changed since the preview or if an affected product is mid-generation, and that an import never deletes a product or an approved image.

### 8. No login

**What I'd ask:** would Ellie and Maya accept signing in to one more thing?

**What I assumed:** no. Ellie said she won't install anything new, and nobody logged in to the last dashboard after week one.

**What it changed:** the web app has no accounts. Anyone with the link can view the catalog, upload a CSV and click Generate, which spends credits. The link has to be treated as private. This is the first thing I'd change before real use; see "Next" in `APPROACH.md`.

## How the design came about

### 1. Designing the screens before writing code

The first working version had every feature but was hard to use. The upload screen repeated the full catalog in two tables. Generate, Send to Slack and Save to Drive were three separate bars at the bottom of the page. You had to read several status badges to work out what a product needed next.

Before changing any code, I used Claude's design canvas to mock up four screens with the real product photos and the Luma images already generated:

1. **Import review:** shows only new, changed and invalid rows, with old → new values side by side. Unchanged products collapse into one line.
2. **Catalog:** one row per product, with a single **Next step** column that shows the one thing that product needs: Generate, Waiting on Ellie, Save to Drive and so on. Tabs filter by stage.
3. **Image view:** click any thumbnail to see it large, with the brief it was made from and its approval status.
4. **Next step, by stage:** every state's label and button side by side, so the wording could be agreed before building.

Reviewing clickable mockups first was much cheaper than iterating on React code. It also surfaced the real question: a single "next step" per product only works if every product is always in exactly one well-defined state.

### 2. A catalog that changes is a state machine

Maya's CSV isn't a one-off. New exports keep coming, and the drop is next month. So a product's details can change **while work on it is already under way**: after images were generated, while Ellie is looking at them in Slack, or after she has approved one.

Each product moves through a lifecycle (generate → review → approve → save to Drive), and a new CSV can arrive at any point in it. The question "what happens to the work already done?" has a different answer at each point. That makes it a state machine: a set of states, the actions that move a product between them, and a rule for what an import does in each one.

### 3. The full state machine had too many states

Written out in full, a product whose brief changes could be in any of these states, each with its own choices:

| Product was… | Options if the brief changes |
|---|---|
| Never generated | Generate with the new brief |
| Generated, not yet sent to Slack | Send the old images anyway, or regenerate |
| In Slack, waiting on Ellie | Let her finish with the old images, or replace them |
| Approved, not yet in Drive | Save the old approval, regenerate, or both |
| In Drive | Keep it, or regenerate and replace it |

On top of these, there are "busy" moments (generating, posting, saving) and failures at every step. Each combination needs a label, a button and an explanation in the import review. That's far too much for a six-person team, most of whom just want to know what to click next.

### 4. How it was compressed

I made five simplifying choices, each removing states or options:

1. **Generate also posts to Slack.** A generated image has nowhere else to go, so "generated, not sent" disappears. Generate and the **More options** button in Slack are the only actions that spend credits.
2. **Only the brief matters.** The brief is what Luma sees: photo, Shot Idea, name, color and material. The app calls these **product attributes**; "brief" is the internal name (`brief_version` in the code). Price, category and notes can change freely without touching images or approvals.
3. **One rule per state, no menus.** On a brief change:
   - **Waiting on Ellie:** the old candidates become Outdated.
   - **Approved:** the approval is kept and can still be saved to Drive.
   - **In Drive:** it stays in Drive.

   Regenerating is always an optional, explicit click.
4. **No approved image is ever lost.** Drive filenames include the image's version (`HG-002_styled_v3.png`), so a new approval never overwrites an old one.
5. **Accept waits while work is in progress.** An import can't be accepted while a product is generating, posting or saving. This avoids a whole class of timing problems without anyone needing to know about them.

The result has **seven product states** (Needs brief, Ready to generate, Generating and posting, Waiting on Ellie, Approved, Saving to Drive, In Drive). A CSV row can do **four things**: add a product, change its brief, change info only, or nothing. Products missing from a CSV are kept unchanged. The catalog always shows exactly one next step, and the import review groups changes by what they will do before anyone clicks Accept.

### 5. What a forward deployed engineer would check first

All of the above are my guesses about how this team works. They're reasonable, but in a real engagement I'd walk Ellie, Maya and the web person through the state table before building, using the mockups, and expect some answers to change it. The questions I'd bring:

- **Ellie:** if the Shot Idea changes after you approved an image, do you still want the old image saved, or is it now wrong? (I assumed it's still saved.)
- **Ellie:** is getting candidates in Slack the moment they're generated helpful, or would you rather someone filter them first? (I assumed straight to Slack.)
- **Maya:** do your Notes ever carry art direction? The real CSV has notes like "El: shoot with the mugs maybe". If so, Notes should be part of the brief and sent to Luma. (I assumed not.)
- **Ellie and Maya:** when none of the candidates work, what should happen? (There's no Reject button; **More options** gives another four, up to twelve per brief.)
- **The web person:** do versioned filenames work for you, and which image should the product page use when there are several? (I assumed the newest.)
- **Everyone:** this build lets a product be saved to Drive with one approved image and allows up to three. Should it require two before saving, as the brief's "2–3" suggests?

## Assumptions in the build

| Question I would ask | Assumption used | Effect on the build |
| --- | --- | --- |
| Where do new requests enter? | Maya's exported CSV is the source of requested shots. | Imports accept the same columns; Slack and Gmail history capture is deferred. |
| Who has the final say? | The approval is Ellie's. **Testing-phase choice:** while `SLACK_APPROVER_USER_ID` is unset, anyone in the review channel can approve, so testers whose Slack IDs aren't known can use the app. Setting it restores Ellie-only approval. | With the variable unset, any Approve click is accepted and the clicker's Slack ID is recorded. With it set, clicks from anyone else are refused with a private note. Teammates can comment in threads either way. |
| What counts as a completed image set? | **One to three approved images per product and brief.** One is enough to save to Drive; three is the limit. The customer's goal is 2–3, so the build allows it but does not insist on the second. | Each batch is four candidates. The third approval marks the remaining candidates "Not selected"; until then they stay approvable and More options stays available. The limit is enforced in `reviews.approve` inside the write transaction. Approvals are final and survive later brief changes, and each brief version has its own count of three. |
| What happens after a rejection? | There is no Reject button in this version. If the candidates are unusable, the approver taps **More options** in the product's Slack thread for another batch from the same attributes; earlier candidates stay approvable. | Reject and feedback-guided regeneration are deferred. The 12-image cap counts non-failed images per brief version, so More options is bounded; after that, a brief change in the CSV is the way to get new candidates. |
| What does an image cost? | Luma's published API price is close enough; the app does not read actual billing. | Spend is the number of images Luma returned × $0.0644 (the upper end of the published price), shown as an estimate. Failed generations are not counted. |
| Can Luma reach the product photos? | The Photo links in the CSV are public and stay valid. | The link is passed to Luma as it is; the app does not copy or host the white-background photo. A private or dead link shows as a failed generation on the product. |
| Will future exports look like this one? | Same columns, and a SKU always means the same product. | The importer matches rows by SKU and expects the same column names. A renamed column or a reused SKU would need a change to the importer. |
| Where does this first slice end? | The existing web person uploads from Drive to the site later. | The app ends at confirmed Drive delivery and does not claim product-page publication. |
| What happens when the CSV changes? | The CSV is the source of truth for the products it lists; a product missing from a new CSV is kept unchanged. A changed **brief** (Photo, Shot Idea, Product Name, Color / Finish, Material: what is sent to Luma) is a new request version; unchanged re-imports have no effect. | Each brief change raises the product's `brief_version`. Images made from an older brief are **Outdated**: they stay saved with the details they were made from, but can't be approved. Regenerating is always an explicit click, never automatic on import. |
| Which changes count? | Only the brief. Price, Category and Notes are info-only. | An info-only change updates the product and leaves its state, images and approvals untouched. |
| What if a CSV changes the brief of a product already in progress? | Waiting candidates become outdated; **approvals are kept**. | **Waiting on Ellie:** the candidates' Slack Approve buttons are replaced with "Outdated: brief changed" (in the background, best effort), Approve is refused, and the product is ready to generate. **Approved but not in Drive:** the approval is kept and can still be saved, with an optional **Regenerate**. **In Drive:** kept, with an optional **Regenerate**. The import review groups rows by these cases before Accept. Accept is refused (409, naming the SKUs) while such a product has a generation, Slack post or Drive save in progress; wait and accept again. |
| How are images named? | The SKU identifies the product; there is no shot-request ID in the schema. | Files are written to the top level of the signed-in user's My Drive as `<SKU>_styled_v<N>.<ext>`, N being the image's own candidate version (e.g. `HG-002_styled_v3.png`), with the extension taken from the real image type. Different images never share a name, so no approved image is overwritten or lost. A delivery's filename is stored and never renamed. |
| Are SKUs safe to use in filenames? | SKUs are uppercase and contain only filename-safe characters. | The importer rejects a row whose SKU has lowercase letters (Needs correction), so each product maps to one filename prefix. Other filename-unsafe characters are not checked. |
| How is the app hosted? | One web process with a persistent volume is available. | SQLite and downloaded images live together on that volume. Multiple workers require a database and object storage upgrade. |

The first customer conversations would also verify the CMS, Drive ownership, Ellie's Slack account, which products belong to the 40-product drop, and a dollar cap per drop. Those answers can change the integration and batch controls.

## Slack review (MVP choices)

- **Transport:** Socket Mode, so no public URL and no request signing secret. The connection is authenticated by the app-level token, and when `SLACK_APPROVER_USER_ID` is set the clicker's Slack user ID is checked against it on every click; while it is unset (testing phase) anyone in the channel can approve. Run exactly one backend process, or every process receives each click.
- **Generate posts to Slack (departure from the handoff):** the handoff has a separate, explicit "send to Slack" step. Here **Generate** posts the candidates to the product's thread as soon as they finish, because a generated candidate has nowhere else to go and the extra click only delayed Ellie. Generate and **More options** (a Slack button with a confirm dialog, same approver rule as Approve) are the only actions that spend credits, and both are explicit clicks. A candidate shows as generating until it is posted; a failed post stays visible with **Retry posting**.
- **Threads:** one thread per product, reused for later candidates. Only candidates made from the product's current brief are posted.
- **Approval is final and survives a brief change (departure from the handoff):** Ellie cannot reverse it, and an accepted CSV change doesn't set it aside. The approved image matched the brief Ellie approved it for, so it stays approved and can be saved to Drive. The catalog shows the newest approval as current and keeps older ones reachable. Approve clicks on candidates made from an older brief are refused with a private note.

## Drive delivery (MVP choices)

- **Explicit write only:** approval never writes to Drive. Someone clicks **Save to Drive** on a product or **Save all to Drive**; every approved image of the product not yet in Drive is saved once.
- **Why approval doesn't save to Drive automatically:** I considered it, since it would remove another state. But Ellie approves in Slack, where no browser is signed in to Google. The backend would need its own long-lived Google credential to store and protect, and a service account can't write to a personal Drive. One explicit click was the simpler trade.
- **Whose Drive:** whoever clicks signs in with Google in the browser, and the files go to the top level of *their* My Drive. The app holds no Google account of its own and stores no Google credentials: the browser gets a one-hour access token and sends it with that request only. Delivery records do not say whose Drive a file went to, so once someone saves a product it shows as saved for everyone, and the Drive link opens only for that person.
- **Overwrite in place (crash recovery only):** if the user's My Drive already holds a file with the exact target name at the top level (for example after a crash between upload and recording, or after a database reset), its content is replaced with the approved image, keeping the same file ID and link. With versioned names, that file is the same image. Drive keeps the previous content as a revision, which it purges after about 30 days (or sooner once a file has 100 revisions). If more than one file has that name, the product shows an error and nothing is written.
- **Least-privilege scope, no folders:** the app uses the `drive.file` scope, so it sees only files it created for that user. Images land loose at the top of My Drive; the user can move them afterwards. Delivering into the team's existing shared folder would need Google Picker or the broader `drive` scope.
- **Restarts:** a server restart during a save loses the in-memory token; the product shows an error and **Retry** signs in again.

## Import from Google Drive (MVP choices)

- **One-time import of a picked file, not live sync:** **Fetch from Google Drive** on the Import CSV page opens Google Picker; the user picks one CSV or Google Sheet and it goes through the same preview → review → **Accept changes** flow as a local upload. Nothing watches the file afterwards; live two-way Sheets sync stays with intent consolidation. The import does not record which Drive file it came from.
- **A Sheet exports only its first tab:** the catalog must be on the first tab of a multi-tab Sheet.
- **One sign-in for import and Save to Drive:** both use the same `drive.file` token. Picking a file in Picker grants the app access to that file only; other files stay hidden.
- **Downloaded in the browser:** the browser downloads the file and posts it to the existing preview endpoint, so validation stays in one place and no token reaches the server for import. A large file is downloaded in full before the backend's 5 MB check rejects it.
