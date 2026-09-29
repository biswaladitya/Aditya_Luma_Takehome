import { useRef, useState, type ChangeEvent, type DragEvent } from 'react'

type CatalogRow = {
  row_number: number
  sku: string
  product_name: string
  category: string
  color: string
  photo: string
  shot_idea: string
  issues: string[]
  ready: boolean
}

type CatalogPreview = {
  filename: string
  total_rows: number
  with_shot_idea: number
  without_shot_idea: number
  ready_to_generate: number
  with_issues: number
  rows_with_shot_idea: CatalogRow[]
}

export default function App() {
  const inputRef = useRef<HTMLInputElement>(null)
  const [preview, setPreview] = useState<CatalogPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)

  async function upload(file?: File) {
    if (!file) return
    setLoading(true)
    setError('')
    setPreview(null)
    const form = new FormData()
    form.append('file', file)
    try {
      const response = await fetch('/api/catalog/preview', { method: 'POST', body: form })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not read this CSV.')
      setPreview(data as CatalogPreview)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not upload this CSV.')
    } finally {
      setLoading(false)
      if (inputRef.current) inputRef.current.value = ''
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
        <div className="eyebrow"><span className="eyebrow-line" /> WORKSPACE <span className="eyebrow-muted">/ 01 IMPORT</span></div>
        <section className="intro">
          <div>
            <h1>Bring your catalog<br /><em>into focus.</em></h1>
            <p>Upload the latest CSV to see which products have a shot idea and which are ready for the next step.</p>
          </div>
          <div className="step-indicator"><span>01</span><span className="step-rule" /><span>IMPORT CATALOG</span></div>
        </section>

        <section className="upload-section" aria-label="Upload catalog">
          <div className="section-heading"><span>01 / SOURCE FILE</span><span>CSV FORMAT</span></div>
          <div
            className={`dropzone ${dragging ? 'dragging' : ''}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <div className="upload-icon">↑</div>
            <h2>{loading ? 'Reading your catalog…' : 'Drop your catalog here'}</h2>
            <p>or choose a CSV from your computer</p>
            <input ref={inputRef} id="catalog-file" type="file" accept=".csv,text/csv" onChange={onFileChange} disabled={loading} />
            <button type="button" className="primary-button" onClick={() => inputRef.current?.click()} disabled={loading}>Choose CSV <span>↗</span></button>
            <small>No images are generated when you upload.</small>
          </div>
          {preview && <div className="upload-success" role="status">
            <span className="success-icon" aria-hidden="true">✓</span>
            <div><strong>CSV uploaded successfully</strong><span>{preview.filename} · {preview.total_rows} {preview.total_rows === 1 ? 'product' : 'products'} processed</span></div>
          </div>}
          {error && <div className="error" role="alert">{error}</div>}
        </section>

        {preview && <>
          <section className="results" aria-live="polite">
            <div className="section-heading"><span>02 / IMPORT PREVIEW</span><span className="filename">{preview.filename}</span></div>
            <div className="summary-grid">
              <div className="summary-card"><span>TOTAL PRODUCTS</span><strong>{preview.total_rows}</strong><small>Rows in this catalog</small></div>
              <div className="summary-card accent"><span>WITH SHOT IDEA</span><strong>{preview.with_shot_idea}</strong><small>Requests captured</small></div>
              <div className="summary-card"><span>NO SHOT IDEA</span><strong>{preview.without_shot_idea}</strong><small>Not requested yet</small></div>
              <div className="summary-card"><span>READY</span><strong>{preview.ready_to_generate}</strong><small>Idea and required details present</small></div>
            </div>
            {preview.with_issues > 0 && <p className="issue-note">{preview.with_issues} {preview.with_issues === 1 ? 'row has' : 'rows have'} missing or duplicate details. Check the labels below before proceeding.</p>}
          </section>

          <section className="catalog-section">
            <div className="section-heading"><span>03 / SHOT REQUESTS</span><span>{preview.with_shot_idea} ROWS</span></div>
            <div className="catalog-title"><h2>Ideas in the queue</h2><p>Rows with a populated Shot Idea.</p></div>
            {preview.rows_with_shot_idea.length === 0 ? <div className="empty-state">No shot ideas were found in this CSV.</div> :
              <div className="table-wrap"><table>
                <thead><tr><th>PRODUCT</th><th>SHOT IDEA</th><th>STATUS</th></tr></thead>
                <tbody>{preview.rows_with_shot_idea.map((row) => <tr key={row.row_number}>
                  <td><div className="product-cell"><div className="product-image">{row.photo ? <img src={row.photo} alt="" loading="lazy" /> : <span>NO PHOTO</span>}</div><div><strong>{row.product_name || 'Unnamed product'}</strong><small>{row.sku || `Row ${row.row_number}`}{row.color ? ` · ${row.color}` : ''}</small></div></div></td>
                  <td className="idea-cell">{row.shot_idea}</td>
                  <td>{row.ready ? <span className="status ready">READY</span> : <span className="status needs-input" title={row.issues.join(', ')}>{row.issues.join(', ')}</span>}</td>
                </tr>)}</tbody>
              </table></div>}
          </section>
        </>}
      </main>
      <footer><span>LUMA / SHOT PRODUCTION</span><span>CATALOG IMPORT</span></footer>
    </div>
  )
}
