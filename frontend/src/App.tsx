import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import GenerationProgress from './GenerationProgress'
import { batchProgress, canSelect, catalogRequest, confirmCatalogUpdate, deliverToDrive, generateImages, isDelivering, isGenerating, isInReview, sendForReview, uploadCatalogPreview, type Catalog, type CatalogPreview } from './catalogApi'
import ProductTable from './ProductTable'
import { clearDriveToken, getDriveToken, prepareGoogleSignIn } from './googleAuth'

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
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [generating, setGenerating] = useState(false)
  const [generationMessage, setGenerationMessage] = useState('')
  const [batchSkus, setBatchSkus] = useState<string[]>([])
  const [sendingToSlack, setSendingToSlack] = useState(false)
  const [slackMessage, setSlackMessage] = useState('')
  const [writingToDrive, setWritingToDrive] = useState(false)
  const [driveMessage, setDriveMessage] = useState('')
  const anyGenerating = Boolean(catalog?.rows.some(isGenerating))
  const anyInReview = Boolean(catalog?.rows.some(isInReview))
  const anyDelivering = Boolean(catalog?.rows.some(isDelivering))
  const sendable = catalog ? catalog.rows.filter(row => row.can_send) : []
  const deliverable = catalog ? catalog.rows.filter(row => row.can_deliver) : []
  const perRequest = catalog?.generation_config.images_per_request ?? 2
  // Track the SKUs of the running request; after a reload, fall back to whatever is generating.
  const batchRows = catalog ? catalog.rows.filter(row => batchSkus.includes(row.sku) || isGenerating(row)) : []
  const progress = batchProgress(batchRows, perRequest)

  // Refresh while Luma jobs run, Slack is pending or Drive writes are in flight, so results appear without a reload.
  useEffect(() => {
    if (!anyGenerating && !anyInReview && !anyDelivering) return
    const timer = setInterval(() => {
      catalogRequest<Catalog>('/api/catalog').then(setCatalog).catch(() => { /* Retry on the next tick. */ })
    }, 3000)
    return () => clearInterval(timer)
  }, [anyGenerating, anyInReview, anyDelivering])

  // Drop selections that stopped being eligible after a reload or a finished job.
  useEffect(() => {
    if (!catalog) return
    const eligible = new Set(catalog.rows.filter(canSelect).map(row => row.sku))
    setSelected(previous => previous.size && [...previous].some(sku => !eligible.has(sku)) ? new Set([...previous].filter(sku => eligible.has(sku))) : previous)
  }, [catalog])

  function toggle(sku: string) {
    setSelected(previous => { const next = new Set(previous); if (next.has(sku)) next.delete(sku); else next.add(sku); return next })
  }

  async function generate() {
    if (!catalog || !selected.size || generating) return
    const { images_per_request: perRequest, est_cost_per_image_usd: unit } = catalog.generation_config
    const images = selected.size * perRequest
    if (!window.confirm(`Generate ${images} images (${perRequest} for each of ${selected.size} products)? Estimated cost up to $${(images * unit).toFixed(2)}.`)) return
    setGenerating(true)
    setGenerationMessage('')
    try {
      const result = await generateImages([...selected])
      setSelected(new Set())
      setBatchSkus(result.queued)
      if (result.skipped.length) setGenerationMessage(`Skipped ${result.skipped.length}: ${result.skipped.map(item => `${item.sku} (${item.reason})`).join(', ')}`)
      setCatalog(await catalogRequest<Catalog>('/api/catalog'))
    } catch (cause) {
      setGenerationMessage(cause instanceof Error ? cause.message : 'Could not start generation.')
    } finally {
      setGenerating(false)
    }
  }

  async function sendToSlack(skus: string[]) {
    if (!skus.length || sendingToSlack) return
    setSendingToSlack(true)
    setSlackMessage('')
    try {
      const result = await sendForReview(skus)
      if (result.skipped.length) setSlackMessage(`Skipped ${result.skipped.length}: ${result.skipped.map(item => `${item.sku} (${item.reason})`).join(', ')}`)
      setCatalog(await catalogRequest<Catalog>('/api/catalog'))
    } catch (cause) {
      setSlackMessage(cause instanceof Error ? cause.message : 'Could not send to Slack.')
    } finally {
      setSendingToSlack(false)
    }
  }

  // Load Google sign-in early so a Save to Drive click can open its popup without being blocked.
  useEffect(() => { prepareGoogleSignIn().catch(() => { /* The click reports what is missing. */ }) }, [])

  async function writeToDrive(skus: string[], confirmText?: string) {
    if (!skus.length || writingToDrive) return
    setDriveMessage('')
    let token: string
    try {
      token = await getDriveToken()  // Must start inside the click; the popup is only shown when needed.
    } catch (cause) {
      setDriveMessage(cause instanceof Error ? cause.message : 'Google sign-in failed.')
      return
    }
    if (confirmText && !window.confirm(confirmText)) return
    setWritingToDrive(true)
    try {
      const result = await deliverToDrive(skus, token)
      if (result.skipped.length) setDriveMessage(`Skipped ${result.skipped.length}: ${result.skipped.map(item => `${item.sku} (${item.reason})`).join(', ')}`)
      setCatalog(await catalogRequest<Catalog>('/api/catalog'))
    } catch (cause) {
      clearDriveToken()  // An expired or revoked token signs in again on the next click.
      setDriveMessage(cause instanceof Error ? cause.message : 'Could not save to Drive.')
    } finally {
      setWritingToDrive(false)
    }
  }

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
            <div className="review-table"><ProductTable rows={catalog.rows} selection={{ selected, toggle, disabled: generating }} slack={{ send: sku => void sendToSlack([sku]), disabled: sendingToSlack }} drive={{ deliver: sku => void writeToDrive([sku]), disabled: writingToDrive }} perRequest={perRequest} /></div>
            {generationMessage && <div className="error" role="alert">{generationMessage}</div>}
            {progress.total > 0 && <div className="batch-progress">
              <GenerationProgress label={progress.active ? 'Generating images' : 'Generation finished'} progress={progress} />
              {!progress.active && <button className="secondary-button" onClick={() => setBatchSkus([])}>Dismiss</button>}
            </div>}
            <div className="confirmation-bar generate-bar">
              <div><strong>{selected.size} selected</strong><p>{selected.size ? `${selected.size * catalog.generation_config.images_per_request} images · up to $${(selected.size * catalog.generation_config.images_per_request * catalog.generation_config.est_cost_per_image_usd).toFixed(2)}` : 'Tick products above, then generate. Nothing is generated until you click.'}</p></div>
              <button className="secondary-button" disabled={generating || !catalog.rows.some(canSelect)} onClick={() => setSelected(new Set(catalog.rows.filter(canSelect).map(row => row.sku)))}>Select all eligible</button>
              <button className="primary-button" disabled={!selected.size || generating} onClick={() => void generate()}>{generating ? 'Starting…' : 'Generate images'}</button>
            </div>
            {slackMessage && <div className="error" role="alert">{slackMessage}</div>}
            <div className="confirmation-bar generate-bar">
              <div><strong>{sendable.length} ready for Slack</strong><p>{sendable.length ? 'Posts each product’s candidates to its own Slack thread. Only Ellie’s Approve there decides.' : 'Generated images appear here once they can be sent. Nothing is posted until you click.'}</p></div>
              <button className="primary-button" disabled={!sendable.length || sendingToSlack} onClick={() => { if (window.confirm(`Send ${sendable.length} product${sendable.length === 1 ? '' : 's'} to Slack for review?`)) void sendToSlack(sendable.map(row => row.sku)) }}>{sendingToSlack ? 'Sending…' : 'Send all to Slack'}</button>
            </div>
            {driveMessage && <div className="error" role="alert">{driveMessage}</div>}
            <div className="confirmation-bar generate-bar">
              <div><strong>{deliverable.length} approved for Drive</strong><p>{deliverable.length ? 'Asks you to sign in with Google, then saves each approved image to the top of your My Drive as SKU_styled_01.' : 'Images Ellie approves in Slack appear here. Nothing is saved to Drive until you click.'}</p></div>
              <button className="primary-button" disabled={!deliverable.length || writingToDrive} onClick={() => void writeToDrive(deliverable.map(row => row.sku), `Save ${deliverable.length} approved image${deliverable.length === 1 ? '' : 's'} to your Google Drive?`)}>{writingToDrive ? 'Saving…' : 'Save all approved to Drive'}</button>
            </div>
          </> : <div className="empty-state">Upload and confirm a CSV to create your saved catalog.</div>}
        </section>
      </main>
      <footer><span>LUMA / SHOT PRODUCTION</span><span>CATALOG IMPORT</span></footer>
    </div>
  )
}
