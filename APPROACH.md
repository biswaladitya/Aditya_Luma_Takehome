# Approach

## Try it

- **Live app:** https://adityalumatakehome-production.up.railway.app/
- **Slack workspace (where approval happens):** https://join.slack.com/t/lumatestgroup/shared_invite/zt-4bygv5fv4-kspVCxqZNZHchfsABFi5zw
- **Video:** linked in `video.md`

### Joining the Slack workspace

1. Open the invite link above and sign up with any email address. Slack works in the browser, so nothing needs installing; the phone app works too.
2. Open the `#shot-review` channel. Each product has one thread there, holding its candidate images.
3. Tap **Approve** under a candidate to approve it. While the app is in its testing phase, anyone in the channel can approve (see `ASSUMPTIONS.md`).
4. If none of the candidates work, tap **More options** in the thread. It asks for confirmation, then generates four more for the same product.
5. Open the `#maya-status-update` channel to see the status reports Maya reads: where every product stands and the estimated generation spend.

### A full pass through the app

1. Open the live app and go to **Import CSV**. Upload `data/catalog.csv` or any CSV with the same columns.
2. Review the changes and click **Accept changes**. Nothing is generated yet.
3. Select products that have a Shot Idea and click **Generate**. Four candidates per product are generated and posted to that product's Slack thread.
4. Approve one to three candidates in Slack. The web app updates within a few seconds.
5. Click **Save to Drive** and sign in with Google. The approved images are written to your My Drive as `<SKU>_styled_v<N>.<ext>`.

Two things to know before trying it:

- **The app itself has no login.** Anyone with the link can use it, and Generate spends real Luma credits.
- **Save to Drive and Fetch from Google Drive use your own Google account.** Sign in through the Google popup the app opens; no setup is needed beforehand.

## What I built and why

The team's old process had two separate problems:

1. **Gathering requests.** Shot ideas are spread across the sheet, Slack and email, and Ellie rebuilds the list by hand.
2. **Producing and approving images.** Getting from a shot idea to approved files in Drive took weeks and a freelance photographer, and nobody could say which requests were done.

I built the second. It replaces the photographer and the email chain with a flow that takes Maya's CSV as it is:

| Step | What happens | Who it serves |
| --- | --- | --- |
| Import | Upload a CSV, or fetch one from Google Drive. The app shows what is new, what changed (old → new values) and which rows need correcting. Nothing is saved until **Accept changes**. | Whoever runs the batch |
| Generate | Select products and click **Generate**. Four candidates per product come back from Luma and are posted to the product's Slack thread. | Whoever runs the batch |
| Review | Ellie taps **Approve** in Slack, on her phone. The team can comment in the thread; only the Approve tap is the decision. | Ellie |
| Deliver | **Save to Drive** writes each approved image with a name that identifies the product and version. | The web person |
| Status | The catalog page shows one next step per product, tabs by stage and an estimate of generation spend. The same summary is sent to the `#maya-status-update` Slack channel on request. | Maya |

Each part answers something the team said:

- *"It has to work from my phone, and I don't want to install anything new."* Ellie's whole job in this flow is tapping Approve in Slack, which she already uses.
- *"Don't burn our budget on stuff she'll reject."* Importing a CSV spends nothing. Only two explicit clicks spend credits: **Generate** in the web app and **More options** in Slack. A product can never have more than 12 generated images for the same brief.
- *"I'd want to see where things stand without having to ask Ellie."* Every product is in exactly one of seven states, shown on the catalog page and in the Slack status report.
- *The wrong `IMG_43xx.jpg` shipped to a product page.* Files are named `<SKU>_styled_v<N>.<ext>`, so the name says which product and which image it is.
- *"A fresh CSV needs a way in."* A new export is uploaded the same way as the first, and the import review shows exactly what it will change before anyone accepts it.

## Key decisions and tradeoffs

| Decision | Why | What it costs |
| --- | --- | --- |
| Approval happens in Slack, not in the web app | It is the only surface Ellie already has on her phone. | Slack buttons are a small interface: approve and "more options", nothing richer such as side-by-side comparison or written feedback to the model. |

| A web app still exists for import, generation and status | A CSV upload with a change review doesn't fit in a Slack message. | This team stopped logging in to their last dashboard. The app has no login and Ellie never needs it, but someone must open it to start a batch. |

| Generate posts to Slack automatically | A generated image has nowhere else to go, and an extra "send" click only delayed Ellie. | Nobody filters candidates before Ellie sees them. |

| Four candidates per batch, up to 12 per brief | Four gives Ellie a real choice from one request; the cap bounds spend at about $0.77 per product and brief. | If 12 aren't enough, the only way forward is changing the Shot Idea in the CSV. |

| One to three approved images per product | The team's goal is 2–3. The app allows saving at one so a product isn't blocked, and closes the review at three. | The app doesn't enforce the "at least two" part of the team's definition of done. |

| Only changes to what Luma sees count as a new request | Photo, Shot Idea, name, color and material are the brief. Price, category and notes can change without affecting images. | Notes that carry art direction ("shoot with the mugs maybe") are ignored. |

| Approval is final and survives a later CSV change | The image matched the brief Ellie approved it for, and no approved image should ever be lost. | Ellie can't undo an approval, and there is no Reject button. |

| Save to Drive is a separate click with a Google sign-in | The app stores no Google credential. The browser gets a one-hour token and the server uses it for that batch only. | One more click, and files land in the My Drive of whoever clicked, not in the team's shared folder. |
| SQLite and image files on one server with a disk | One container to deploy and nothing else to run. | Exactly one server process; see "What breaks first". |

| No login, and anyone in Slack can approve for now | Reviewers and testers can try it without account setup. | The link must stay private. Setting `SLACK_APPROVER_USER_ID` restricts approval to Ellie; the web app has no equivalent yet. |

The reasoning behind the product states and the CSV-change rules is in `ASSUMPTIONS.md`.

## The road not taken

The strongest design I considered was solving the first problem: **gathering the requests automatically**. The app would connect to Slack, Gmail and the sheet, find the shot ideas people mentioned and never wrote down, and fill in the Shot Idea column itself. Ellie would stop rebuilding the wishlist from scrollback, which is the part of her job the team described most vividly.

I didn't build it, for three reasons:

1. **It has no value on its own.** A complete list of shot ideas still needs someone to make the images. Generation and approval, on the other hand, are useful from the first day with the sixteen ideas already in the sheet.
2. **It is a problem of habits more than software.** If people reliably wrote ideas down in one place, they would already be using the sheet. Reading intent out of Slack threads and email chains could produce wrong and duplicate requests, and each wrong one costs money once it is generated.
3. **It needs access I couldn't test.** It depends on the team's real Slack history and Ellie's inbox. Without those, I could only have built it against invented data.

The 40-product drop also pointed the same way: those products arrive as a CSV, so the CSV had to work first.

## Scope ledger

### In

| What | Why |
| --- | --- |
| CSV import with a change review, from a file or Google Drive | New exports will keep coming, and the team should see what one changes before accepting it. |
| Generation with Luma, four candidates per product | The core of Maya's ask. |
| Approval in Slack, one thread per product | Ellie's condition for using it at all. |
| **More options** in Slack, capped at 12 images per brief | Gives Ellie a way forward when no candidate works, with bounded spend. |
| Save to Drive with product-linked filenames | Fixes the "which file is final" problem. |
| Status by stage, spend estimate and a Slack status report | Maya's visibility without asking Ellie. |
| Visible, retryable failures at every step | A stuck product should say so, not sit silently. |

### Out

| What | Why |
| --- | --- |
| Publishing to the product page | Part of the team's definition of done, but it needs access to their site's CMS. The web person still uploads from Drive, now from clearly named files. |
| Gathering requests from Slack and Gmail | See "The road not taken". |
| Reject button and regeneration guided by feedback | **More options** covers the common case. Using Ellie's comments to steer the next batch needs real feedback to design against. |
| Login and roles | A six-person team that trusts each other; the cost is listed under tradeoffs. |
| Live two-way sync with the sheet | The brief says nobody is asking for it. |
| Writing into the team's shared Drive folder | Needs a broader Google permission than the app asks for. Files land at the top of the signed-in user's My Drive. |

### Next

In the order I'd do them:

1. **Restrict who can approve and who can generate.** Set Ellie as the Slack approver and put the web app behind a login. This comes first because it is the only thing between a leaked link and spent credits.
2. **A budget cap per drop.** Maya sets a dollar limit and the app refuses to generate past it.
3. **Deliver to the shared Drive folder,** then publish to the product page once the CMS is known.
4. **Feedback-guided regeneration,** using what Ellie writes in the thread.
5. **A scheduled status report.** It is sent on request today.
6. **Request gathering from Slack and Gmail.**

## Unit economics

### Dollars

The app estimates **$0.0644 per generated image**, the upper end of Luma's published price. A batch of four costs about **$0.26**.

The cost of an *approved* image depends on how many candidates Ellie approves:

| Outcome for one product | Generated | Approved | Cost per approved image |
| --- | --- | --- | --- |
| Three approved from the first batch | 4 | 3 | $0.09 |
| Two approved from the first batch | 4 | 2 | $0.13 |
| One approved from the first batch | 4 | 1 | $0.26 |
| Two approved after one **More options** | 8 | 2 | $0.26 |
| One approved at the cap | 12 | 1 | $0.77 |

For whole batches:

| Batch | First batch only | Every product at the 12-image cap |
| --- | --- | --- |
| 40-product drop | 160 images, about $10 | 480 images, about $31 |
| Full catalog, 300 products | 1,200 images, about $77 | 3,600 images, about $232 |
| 10× the catalog, 3,000 products | 12,000 images, about $773 | 36,000 images, about $2,318 |

Hosting is one small always-on container with a disk, which doesn't change with the number of images at this size.

### Minutes

I haven't measured these, so I'm giving the limits the code sets and not an average:

- **Generation:** the app runs four Luma requests at a time and gives each up to five minutes before marking it failed. One product's four candidates run together. Total time for a batch is roughly (images ÷ 4) × the time one image takes.
- **Ellie's time:** one tap per approved image. The real cost is how long a thread waits before she looks at it, which the app doesn't shorten.
- **Operator's time:** one upload, one Accept, one Generate and one Save to Drive per batch, regardless of batch size.

### What changes at 10× the catalog

- **Dollars scale in a straight line,** and at about $773 for a first pass - this would need to be carefully reviewed and we'd need to validate that our product still gives them good ROI. 

- **Generation time becomes the limit.** Four at a time is fine for 160 images and too slow for 12,000. It needs a proper job queue and a higher concurrency agreed with Luma.

- **Ellie becomes the bottleneck.** 3,000 Slack threads is not reviewable by one person in one channel. Review would need batching, such as one drop or category at a time, and probably a second approver.

- **Storage moves.** Images on one server's disk and a SQLite file would move to object storage and a hosted database.

## What breaks first under pressure

1. **Ellie's attention.** Every candidate goes straight to Slack. A 40-product batch posts 160 images in 40 threads at once. I'd watch how long threads wait and whether she approves or stops opening them.
2. **No login on a public URL.** Anyone with the link can upload a catalog over the saved one or spend credits. The 12-image cap limits the damage per product, not in total.
3. **One server process.** Slack approvals arrive over a single connection held by that process, and the database is one file. A second copy of the app would receive every click twice. A restart during generation marks the unfinished work as failed so it can be retried, but it doesn't resume.
4. **Generation throughput.** Four at a time, and no handling of Luma's rate limits beyond marking an image failed.
5. **Drive sign-in.** Each person who saves signs in with their own Google account, and the token lasts an hour. A long Save to Drive session can need a second sign-in, and files end up spread across personal Drives.

After shipping, the three numbers I'd watch are the share of candidates Ellie approves (the cost of an approved image), how long a thread waits for her, and whether anyone other than me opens the web app.
