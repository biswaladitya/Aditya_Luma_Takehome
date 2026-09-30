import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { catalogRequest, confirmCatalogUpdate, uploadCatalogPreview, type Catalog, type CatalogPreview } from './catalogApi'
import ProductTable from './ProductTable'

const PENDING_IMPORT_KEY = 'luma.pendingImport'

function rememberPreview(id: string | null) {
  try {
    if (id) localStorage.setItem(PENDING_IMPORT_KEY, id)
    else localStorage.removeItem(PENDING_IMPORT_KEY)
  } catch { /* The preview remains usable when browser storage is unavailable. */ }
}

export default function App() {
  const inputRef = useRef<HTMLInputElement>(null)
  const operationRef = useRef(false)
  const [preview, setPreview] = useState<CatalogPreview | null>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [busy, setBusy] = useState<'loading' | 'uploading' | 'confirming' | null>('loading')
  const [error, setError] = useState('')
  const [confirmationError, setConfirmationError] = useState('')
  const [dragging, setDragging] = useState(false)

  useEffect(() => {
    let active = true
    async function restore() {
      operationRef.current = true
      try {
        const saved = await catalogRequest<Catalog>('/api/catalog')
        if (!active) return
        setCatalog(saved)
        let pendingId: string | null = null
        try { pendingId = localStorage.getItem(PENDING_IMPORT_KEY) } catch { /* Optional browser state. */ }
        if (pendingId) {
          const pending = await catalogRequest<CatalogPreview>(`/api/catalog/imports/${encodeURIComponent(pendingId)}`)
          if (!active) return
          setPreview(pending)
          if (pending.status === 'applied') rememberPreview(null)
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : 'Could not restore the catalog.')
      } finally {
        if (active) { operationRef.current = false; setBusy(null) }
      }
    }
    void restore()
    return () => { active = false }
  }, [])

  async function upload(file?: File) {
    if (!file || operationRef.current) return
    operationRef.current = true
    setBusy('uploading')
    setError('')
    setConfirmationError('')
    try {
      const result = await uploadCatalogPreview(file)
      setPreview(result)
      rememberPreview(result.preview_id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not upload this CSV.')
    } finally {
      operationRef.current = false
      setBusy(null)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  async function confirm() {
    if (!preview || operationRef.current || !preview.can_confirm || preview.status !== 'pending') return
    operationRef.current = true
    setBusy('confirming')
    setError('')
    setConfirmationError('')
    try {
      const applied = await confirmCatalogUpdate(preview.preview_id)
      setPreview(applied)
      rememberPreview(null)
      setCatalog(await catalogRequest<Catalog>('/api/catalog'))
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'Could not update the catalog.'
      setConfirmationError(message)
      setError(message)
    } finally {
      operationRef.current = false
      setBusy(null)
    }
  }

  function onFileChange(event: ChangeEvent<HTMLInputElement>) {
    void upload(event.target.files?.[0])
  }

  function onDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault()
    setDragging(false)
    void upload(event.dataTransfer.files[0])
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">✦</span> LUMA <span className="brand-divider">/</span> <span className="brand-sub">STUDIO</span></div>
        <span className="topbar-label">SHOT PRODUCTION</span>
      </header>

      <main>
        <div className="eyebrow"><span className="eyebrow-line" /> WORKSPACE <span className="eyebrow-muted">/ CATALOG</span></div>
        <section className="intro">
          <div>
            <h1>Bring your catalog<br /><em>into focus.</em></h1>
            <p>Upload your latest CSV, review what changed, and update your saved product catalog when you’re ready.</p>
          </div>
          <div className="step-indicator"><span>01</span><span className="step-rule" /><span>IMPORT & REVIEW</span></div>
        </section>

        <section className="upload-section" aria-label="Upload catalog">
          <div className="section-heading"><span>01 / SOURCE FILE</span><span>CSV FORMAT</span></div>
          <div className={`dropzone ${dragging ? 'dragging' : ''}`}
            onDragOver={event => { event.preventDefault(); if (!busy) setDragging(true) }}
            onDragLeave={() => setDragging(false)} onDrop={onDrop} aria-busy={busy === 'uploading'}>
            <div className="upload-icon">↑</div>
            <h2>{busy === 'uploading' ? 'Comparing your catalog…' : 'Drop your catalog here'}</h2>
            <p>or choose a CSV from your computer</p>
            <input ref={inputRef} id="catalog-file" type="file" accept=".csv,text/csv" onChange={onFileChange} disabled={Boolean(busy)} />
            <button type="button" className="primary-button" onClick={() => inputRef.current?.click()} disabled={Boolean(busy)}>Choose CSV <span>↗</span></button>
            <small>You’ll review changes before updating the catalog.</small>
          </div>
          {error && <div className="error" role="alert">{error}</div>}
        </section>

        {preview && <section className="results" aria-label="Import review">
          <div className="upload-success" role="status">
            <span className="success-icon" aria-hidden="true">✓</span>
            <div><strong>{preview.status === 'applied' ? 'Product catalog updated' : 'CSV uploaded successfully — ready for review'}</strong><span>{preview.filename} · {preview.total_rows} products · {preview.with_shot_idea} with Shot Ideas · {preview.without_shot_idea} without</span></div>
          </div>
          {preview.status === 'pending' && <>
            <div className="catalog-title"><h2>Review your changes</h2><p>These changes have not been applied.</p></div>
            <div className="summary-grid">
              <div className="summary-card accent"><span>NEW PRODUCTS</span><strong>{preview.new_count}</strong><small>Not in the saved catalog</small></div>
              <div className="summary-card"><span>CHANGED PRODUCTS</span><strong>{preview.changed_count}</strong><small>Review previous and proposed values</small></div>
              <div className="summary-card"><span>UNCHANGED</span><strong>{preview.unchanged_count}</strong><small>Existing results will be retained</small></div>
              <div className="summary-card"><span>NEEDS CORRECTION</span><strong>{preview.invalid_count}</strong><small>Rows that cannot be matched by SKU</small></div>
            </div>
            {!preview.can_confirm && <div className="issue-note">{preview.errors.length ? preview.errors.map((message, i) => <p key={i}>{message}</p>) : <p>Correct the CSV and upload it again before updating the catalog.</p>}</div>}
            <div className="review-table"><ProductTable rows={preview.rows} preview /></div>
            {confirmationError && <div className="error" role="alert">{confirmationError}</div>}
            <div className="confirmation-bar">
              <div><strong>Apply this upload to the product catalog</strong><p>Existing images stay saved. Generating images is a separate action.</p></div>
              <button className="secondary-button" disabled={Boolean(busy)} onClick={() => { setPreview(null); rememberPreview(null); setError(''); setConfirmationError('') }}>Dismiss preview</button>
              <button className="primary-button" disabled={Boolean(busy) || !preview.can_confirm} onClick={() => void confirm()}>{busy === 'confirming' ? 'Updating catalog…' : 'Update product catalog'}</button>
            </div>
          </>}
        </section>}

        <section className="catalog-section" aria-label="Saved product catalog">
          <div className="section-heading"><span>02 / SAVED CATALOG</span><span>{catalog?.total_rows ?? 0} PRODUCTS</span></div>
          <div className="catalog-title"><h2>Your product catalog</h2><p>Confirmed products and saved generation results.</p></div>
          {busy === 'loading' ? <p role="status">Loading saved catalog…</p> : catalog && catalog.total_rows > 0 ? <>
            <div className="summary-grid">
              <div className="summary-card accent"><span>NEEDS GENERATION</span><strong>{catalog.generation_summary.never_generated + catalog.generation_summary.changed_since_generation}</strong><small>New images or changed product details</small></div>
              <div className="summary-card"><span>UP TO DATE</span><strong>{catalog.generation_summary.already_generated}</strong><small>Images match current details</small></div>
              <div className="summary-card"><span>NO SHOT IDEA</span><strong>{catalog.without_shot_idea}</strong><small>Products without a requested scene</small></div>
              <div className="summary-card"><span>NEEDS INPUT</span><strong>{catalog.generation_summary.missing_input}</strong><small>Missing details for generation</small></div>
            </div>
            <div className="review-table"><ProductTable rows={catalog.rows} /></div>
          </> : <div className="empty-state">Upload and confirm a CSV to create your saved catalog.</div>}
        </section>
      </main>
      <footer><span>LUMA / SHOT PRODUCTION</span><span>CATALOG IMPORT</span></footer>
    </div>
  )
}
