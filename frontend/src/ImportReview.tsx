import { useRef, useState, type DragEvent } from 'react'
import { confirmCatalogUpdate, uploadCatalogPreview, type BriefCase, type CatalogPreview, type CatalogRow, type FieldChanges } from './catalogApi'

const fieldNames: Record<string, string> = {
  sku: 'SKU', product_name: 'Product name', category: 'Category', color: 'Color / Finish',
  material: 'Material', price: 'Price', photo: 'Photo', shot_idea: 'Shot Idea', notes: 'Notes',
}

/** Shown groups, in order; unchanged rows are listed separately. */
const GROUPS: { id: BriefCase; title: string; tag: string; tone: string; note?: string }[] = [
  { id: 'new', title: 'New', tag: 'NEW', tone: 'new' },
  { id: 'not_generated', title: 'Brief changed', tag: 'BRIEF CHANGED', tone: 'changed', note: 'No images yet, so nothing else changes.' },
  { id: 'with_ellie', title: 'Brief changed, waiting on Ellie', tag: 'BRIEF CHANGED', tone: 'changed',
    note: 'Its candidates become Outdated: Ellie can no longer approve them, and their Approve buttons are removed in Slack. Generate again from the catalog when you’re ready.' },
  { id: 'approved_not_in_drive', title: 'Brief changed, approved, not in Drive', tag: 'BRIEF CHANGED', tone: 'changed',
    note: 'Ellie’s approval is kept and can still be saved to Drive. Regenerating for the new brief is optional.' },
  { id: 'in_drive', title: 'Brief changed, in Drive', tag: 'BRIEF CHANGED', tone: 'changed',
    note: 'The image in Drive is kept. Regenerating for the new brief is optional.' },
  { id: 'info_only', title: 'Info only', tag: 'INFO ONLY', tone: 'info', note: 'Price, category or notes only. Images and approvals are unaffected.' },
  { id: 'invalid', title: 'Needs correction', tag: 'NEEDS CORRECTION', tone: 'invalid' },
]

/** Previews saved before brief cases existed only know whether a row changed. */
const caseOf = (row: CatalogRow): BriefCase => row.brief_case ?? (row.change_type === 'changed' ? 'not_generated' : row.change_type ?? 'unchanged')

function Changes({ changes }: { changes: FieldChanges }) {
  return <dl className="field-changes">
    {Object.entries(changes).map(([field, change]) => <div key={field}>
      <dt>{fieldNames[field] || field}</dt>
      <dd><s className="previous-value"><span className="visually-hidden">Was </span>{change.before || '(blank)'}</s><strong><span className="visually-hidden">Now </span>{change.after || '(blank)'}</strong></dd>
    </div>)}
  </dl>
}

function Thumb({ row }: { row: CatalogRow }) {
  return /^https?:\/\//i.test(row.photo) ? <img className="thumb-sm" src={row.photo} alt="" loading="lazy" /> : <div className="thumb-sm">PHOTO</div>
}

function WhatChanged({ row, note }: { row: CatalogRow; note?: string }) {
  if (row.change_type === 'invalid') return <ul className="row-issues">{row.issues.map(issue => <li key={issue}>{issue}</li>)}</ul>
  if (row.change_type === 'new') {
    const facts = [row.category, row.material, row.price].filter(Boolean).join(' · ')
    return <div className="new-summary">
      {facts}{facts && ' · '}{row.shot_idea ? <>Shot Idea: <strong>{row.shot_idea}</strong></> : <span className="warn">No Shot Idea yet, can’t be generated</span>}
      {row.shot_idea && row.issues.length > 0 && <div className="warn">{row.issues.join(' · ')}</div>}
    </div>
  }
  return <div>
    {row.changes && <Changes changes={row.changes} />}
    {row.issues.length > 0 && <div className="warn">{row.issues.join(' · ')}</div>}
    {note && <div className="replace-warning" role="note">{note}</div>}
  </div>
}

export default function ImportReview({ preview, onPreview, onApplied, onCancel }: {
  preview: CatalogPreview | null
  onPreview: (preview: CatalogPreview | null) => void
  onApplied: (result: CatalogPreview) => Promise<void>
  onCancel: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const busyRef = useRef(false)
  const [busy, setBusy] = useState<'uploading' | 'confirming' | null>(null)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [showUnchanged, setShowUnchanged] = useState(false)

  async function upload(file?: File) {
    if (!file || busyRef.current) return
    busyRef.current = true
    setBusy('uploading')
    setError('')
    try {
      onPreview(await uploadCatalogPreview(file))
      setShowUnchanged(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not upload this CSV.')
    } finally {
      busyRef.current = false
      setBusy(null)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  async function accept() {
    if (!preview || busyRef.current || !preview.can_confirm || preview.status !== 'pending') return
    busyRef.current = true
    setBusy('confirming')
    setError('')
    try {
      await onApplied(await confirmCatalogUpdate(preview.preview_id))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not update the catalog.')
    } finally {
      busyRef.current = false
      setBusy(null)
    }
  }

  function onDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault()
    setDragging(false)
    void upload(event.dataTransfer.files[0])
  }

  const fileInput = <input ref={inputRef} className="visually-hidden" type="file" accept=".csv,text/csv" tabIndex={-1} aria-hidden="true"
    onChange={event => void upload(event.target.files?.[0])} disabled={Boolean(busy)} />
  const errorBox = error && <div className="error" role="alert">{error}</div>

  if (!preview) return <main className="page">
    <div className="crumbs"><button type="button" className="link-button" onClick={onCancel}>Catalog</button> / Import CSV</div>
    <h1 className="page-title">Import CSV</h1>
    <p className="page-sub">Upload the latest catalog export. You’ll review what changed before anything is saved.</p>
    <div className={`dropzone ${dragging ? 'dragging' : ''}`} aria-busy={busy === 'uploading'}
      onDragOver={event => { event.preventDefault(); if (!busy) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
      <p>{busy === 'uploading' ? 'Comparing your catalog…' : 'Drop a CSV here, or'}</p>
      <button type="button" className="primary-button" onClick={() => inputRef.current?.click()} disabled={Boolean(busy)}>Choose CSV</button>
      {fileInput}
    </div>
    {errorBox}
  </main>

  const groups = GROUPS.map(group => ({ ...group, rows: preview.rows.filter(row => caseOf(row) === group.id) })).filter(group => group.rows.length)
  const unchanged = preview.rows.filter(row => caseOf(row) === 'unchanged')
  const changes = preview.changed_count + preview.new_count
  const count = (id: BriefCase) => preview.rows.filter(row => caseOf(row) === id).length
  const briefChanged = count('not_generated') + count('with_ellie') + count('approved_not_in_drive') + count('in_drive')

  return <>
    <main className="page page-with-bar">
      <div className="crumbs"><button type="button" className="link-button" onClick={onCancel}>Catalog</button> / Import review</div>
      <div className="page-head">
        <div>
          <h1 className="page-title">Review changes</h1>
          <p className="page-sub">{preview.filename} · {preview.total_rows} rows · Nothing changes until you accept.</p>
        </div>
        <button type="button" className="secondary-button" onClick={() => inputRef.current?.click()} disabled={Boolean(busy)}>{busy === 'uploading' ? 'Comparing…' : 'Upload a different CSV'}</button>
        {fileInput}
      </div>
      <div className="chips">
        <span className="chip chip-changed">{briefChanged} brief changed</span>
        <span className="chip">{count('info_only')} info only</span>
        <span className="chip chip-new">{preview.new_count} new</span>
        <span className={`chip ${preview.invalid_count ? 'chip-invalid' : ''}`}>{preview.invalid_count} need correction</span>
        <span className="chip">{preview.unchanged_count} unchanged, hidden</span>
      </div>
      <div className="panel">
        {groups.length > 0 && <div className="import-grid import-head" aria-hidden="true"><span>CHANGE</span><span>PRODUCT</span><span>WHAT CHANGED</span></div>}
        {groups.map(group => <section key={group.id} aria-label={group.title}>
          <h2 className="import-group">{group.title} <span className="tab-count">{group.rows.length}</span></h2>
          {group.rows.map((row, index) => <div className="import-grid import-row" key={`${row.sku}-${row.row_number ?? index}`}>
            <span className={`tag change-${group.tone}`}>{group.tag}</span>
            <div className="product-cell"><Thumb row={row} /><div><strong>{row.product_name || 'Unnamed product'}</strong><small>{row.sku || `Row ${row.row_number}`}{row.color ? ` · ${row.color}` : ''}</small></div></div>
            <WhatChanged row={row} note={group.note} />
          </div>)}
        </section>)}
        {!groups.length && <p className="empty-row">Nothing in this file differs from the saved catalog.</p>}
        {unchanged.length > 0 && <>
          <button type="button" className="unchanged-toggle" aria-expanded={showUnchanged} onClick={() => setShowUnchanged(!showUnchanged)}>
            {showUnchanged ? 'Hide' : 'Show'} {unchanged.length} unchanged product{unchanged.length === 1 ? '' : 's'}
          </button>
          {showUnchanged && <ul className="unchanged-list">{unchanged.map(row => <li key={row.sku}>{row.sku} {row.product_name}</li>)}</ul>}
        </>}
      </div>
    </main>
    <div className="action-bar">
      <div className="action-bar-inner">
        <div className="action-bar-text">
          {preview.can_confirm
            ? <><strong>{changes ? `Apply ${changes} change${changes === 1 ? '' : 's'} to the catalog` : 'No changes to apply'}</strong><span>Existing images and approvals stay saved. Nothing is generated yet.</span></>
            : <><strong>Correct the CSV and upload it again</strong>{preview.errors.length ? preview.errors.map((message, i) => <span key={i}>{message}</span>) : <span>This file can’t be applied.</span>}</>}
          {errorBox /* Beside Accept, so an upload or confirm failure is visible wherever the list is scrolled. */}
        </div>
        <button type="button" className="secondary-button" disabled={Boolean(busy)} onClick={() => { onPreview(null); setError('') }}>Discard</button>
        <button type="button" className="primary-button" disabled={Boolean(busy) || !preview.can_confirm} onClick={() => void accept()}>{busy === 'confirming' ? 'Applying…' : 'Accept changes'}</button>
      </div>
    </div>
  </>
}
