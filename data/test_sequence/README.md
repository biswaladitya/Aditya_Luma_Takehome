# CSV test sequence

Upload these in order, starting from an empty database. Between uploads, take some products through the flow in the app so each brief-change case has a product to test it on. Total spend is about 10 generations of 2 images (≈ $1.30 at $0.0644 per image).

## 1. `01_baseline.csv`: the starting catalog

Upload and click **Accept changes**.

**Expect:** 40 new products. 16 are Ready to generate; 24 are Needs brief (no Shot Idea).

**Then set up the cases:**

| Product | Do this | Leaves it at |
|---|---|---|
| HG-010 Espresso Cup Set | Nothing | Ready to generate (never generated) |
| HG-011 Woven Throw Blanket | Generate | With Ellie |
| HG-025 Pillar Candle Set | Generate | With Ellie |
| HG-012 Throw Pillow | Generate; Ellie approves one in Slack | Approved, not in Drive |
| HG-016 Cutting Board | Generate; Ellie approves; **Save to Drive** | In Drive as `HG-016_styled_v1` or `_v2` |

Generate should post both candidates to each product's Slack thread with no separate Send step.

## 2. `02_brief_changes.csv`: every kind of change

**Expect in the import review:**

| Group | Products | After Accept |
|---|---|---|
| New | HG-101 Stoneware Mug Gift Set (2) | Added, Ready to generate |
| Brief changed (never generated) | HG-010 (Shot Idea) and HG-001 (Shot Idea added) | Ready to generate. HG-001 moves from Needs brief to Ready |
| Brief changed, waiting on Ellie | HG-011 | Ready to generate. Its Slack candidates become Outdated, and their Approve buttons are removed in Slack |
| Brief changed, approved, not in Drive | HG-012 | Approval kept: **Save to Drive** still works, plus an optional **Regenerate** |
| Brief changed, in Drive | HG-016 | Stays In Drive, with a "brief changed" note and an optional **Regenerate** |
| Info only | HG-020 (price), HG-025 (price and notes) | Updated only. HG-025 is still With Ellie and its candidates are still current |

HG-045 is missing from this file. It should be kept and stay unchanged.

**Also check:**
- In Slack, Ellie clicks **Approve** on an old HG-011 candidate. She should get a private "outdated" note, and nothing changes.
- Click **Regenerate** on HG-016, approve the new candidate, then **Save to Drive**. Drive should now hold both `HG-016_styled_v1` (or `_v2`) and the new `_v3` (or `_v4`), with nothing overwritten.
- Click **Save to Drive** on HG-012. The brief-1 image saves as `HG-012_styled_vN`.
- **Accept waits:** click Generate on HG-010, then immediately upload `04_change_back.csv` and click Accept. Accept should be refused with "wait for it to finish". Discard, and wait for HG-010 to finish posting before step 3.

## 3. `03_needs_correction.csv`: rows that block the import

It's the step 2 file plus a lowercase SKU (`hg-102`) and a second `HG-010` row.

**Expect:** both rows show under **Needs correction**, and **Accept changes** is disabled. Click **Discard**.

## 4. `04_change_back.csv`: changing a brief back

It's the step 2 file with HG-012's Shot Idea set back to the original "styled on a sofa".

Only do this if you didn't save HG-012 to Drive in step 2, or regenerate and approve it first, so it has history under the step 2 brief.

**Expect:**
- HG-012 is grouped as a brief change. After Accept, it's on a **new** brief version, so the step 1 candidates don't come back as current.
- Approved images from earlier briefs stay visible. Any not yet saved can still be saved to Drive.

Re-uploading the same file a second time should show nothing to change.
