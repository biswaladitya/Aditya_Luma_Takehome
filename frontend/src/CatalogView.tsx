import { useEffect, useMemo, useRef, useState } from 'react'
import { canSelect, notSelected, rowImages, type Catalog, type CatalogRow, type GeneratedImage, type Stage } from './catalogApi'
import ImageLightbox from './ImageLightbox'
import { money, NextStep, StageLabel, type Actions } from './NextStep'

const SORT: Stage[] = ['approved', 'saving', 'post_failed', 'failed', 'ready', 'generating', 'with_ellie', 'in_drive', 'needs_input']

const ALL = 'all'
// The tabs themselves (labels, stages, counts) come from the backend; only the hint text lives here.
const HINTS: Record<string, string> = {
  [ALL]: 'Each row shows only its next step. Tick products to generate several at once.',
  generate: 'Nothing is generated until you click. Each product gets 4 candidates, posted to its Slack thread when ready.',
  generating: 'Candidates appear in the row as soon as each one finishes, then go to Slack.',
  post: 'These candidates didn’t reach Slack. Retry posts them to the product’s thread.',
  ellie: 'Team comments in Slack are advisory. Ellie’s Approve updates this page.',
  drive: 'Saves every approved image not yet in Drive to the top of your My Drive as SKU_styled_vN.',
  done: 'Approved images already saved to Drive.',
  input: 'Add a Shot Idea and a source photo to these rows in the CSV, then import it again.',
}

function Thumbnails({ row, perRequest, open }: { row: CatalogRow; perRequest: number; open: (image: GeneratedImage) => void }) {
  // The newest batch and the current attributes' other candidates; older ones stay one click away in the image view.
  const batch = rowImages(row, perRequest)
  if (!batch.length) return <span className="muted">None yet</span>
  const name = row.product_name || row.sku
  const earlier = row.images.filter(image => image.image_url && !batch.includes(image)).sort((a, b) => b.version - a.version)
  return <div className="thumbs-cell"><div className="thumbs">{batch.map(image => {
    const label = `candidate v${image.version} for ${name}`
    if (!image.image_url) {
      const failed = image.status === 'failed'
      return <div key={image.id} className={`thumb-placeholder ${failed ? 'failed' : ''}`} title={image.error ?? undefined}>{failed ? 'Failed' : image.status === 'processing' ? 'Working' : 'Queued'}</div>
    }
    const approved = image.review?.state === 'approved'
    const passed = notSelected(row, image)
    const status = approved ? (image.outdated ? ', approved earlier' : ', approved') : image.outdated ? ', outdated' : passed ? ', not selected' : ''
    return <button type="button" key={image.id} className={`thumb ${approved ? 'approved' : ''} ${passed || (image.outdated && !approved) ? 'dimmed' : ''}`} onClick={() => open(image)} aria-label={`Enlarge ${label}${status}`}>
      <img src={image.image_url} alt="" loading="lazy" />
      {approved && <span className="thumb-tick" aria-hidden="true">✓</span>}
      {image.outdated && <span className="thumb-tag" aria-hidden="true">Outdated</span>}
    </button>
  })}</div>
  {earlier.length > 0 && <button type="button" className="link-button thumbs-more" onClick={() => open(earlier[0])}>+{earlier.length} earlier</button>}
  </div>
}

export default function CatalogView({ catalog, actions, banner, onDismissBanner, notice, onDismissNotice, onImport, onSendStatus, sendingStatus }: {
  catalog: Catalog | null
  actions: Actions
  banner: { title: string; text: string } | null
  onDismissBanner: () => void
  notice: string
  onDismissNotice: () => void
  onImport: () => void
  onSendStatus: () => void
  sendingStatus: boolean
}) {
  const [tab, setTab] = useState(ALL)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [viewing, setViewing] = useState<{ sku: string; imageId: string } | null>(null)
  const perRequest = catalog?.generation_config.images_per_request ?? 4
  const unit = catalog?.generation_config.est_cost_per_image_usd ?? 0
  // Sorted by next step when a tab is opened; after that a row keeps its place as its stage changes,
  // so clicking Generate doesn't send it down the list. Products that appear later go to the end.
  const placed = useRef<{ tab: string; skus: string[] }>({ tab, skus: [] })
  const rows = useMemo(() => {
    const kept = placed.current.tab === tab ? placed.current.skus : []
    const position = new Map(kept.map((sku, index) => [sku, index]))
    const sorted = (catalog?.rows ?? []).map(row => ({ row, stage: row.stage }))
      .sort((a, b) => SORT.indexOf(a.stage) - SORT.indexOf(b.stage) || a.row.sku.localeCompare(b.row.sku))
      .sort((a, b) => (position.get(a.row.sku) ?? kept.length) - (position.get(b.row.sku) ?? kept.length))
    placed.current = { tab, skus: sorted.map(item => item.row.sku) }
    return sorted
  }, [catalog, tab])

  // Drop selections that stopped being eligible after a reload or a finished job.
  useEffect(() => {
    if (!catalog) return
    const eligible = new Set(catalog.rows.filter(canSelect).map(row => row.sku))
    setSelected(previous => previous.size && [...previous].some(sku => !eligible.has(sku)) ? new Set([...previous].filter(sku => eligible.has(sku))) : previous)
  }, [catalog])

  if (!catalog) return <main className="page"><p role="status">Loading catalog…</p></main>

  const tabs: { id: string; label: string; stages: Stage[] | null; count: number }[] =
    [{ id: ALL, label: 'All', stages: null, count: catalog.total_rows }, ...catalog.tabs]
  const current = tabs.find(item => item.id === tab) ?? tabs[0]
  const visible = current.stages ? rows.filter(item => current.stages!.includes(item.stage)) : rows
  const selectable = visible.filter(item => canSelect(item.row)).map(item => item.row.sku)
  const sendable = catalog.rows.filter(row => row.can_send).map(row => row.sku)
  const deliverable = catalog.rows.filter(row => row.can_deliver).map(row => row.sku)
  const unsaved = catalog.rows.filter(row => row.can_deliver).reduce((sum, row) => sum + row.unsaved_image_ids.length, 0)
  const spend = catalog.spend
  const images = selected.size * perRequest
  const viewingRow = viewing && catalog.rows.find(row => row.sku === viewing.sku)

  function toggle(sku: string) {
    setSelected(previous => { const next = new Set(previous); if (next.has(sku)) next.delete(sku); else next.add(sku); return next })
  }

  async function generateSelected() {
    if (await actions.generate([...selected])) setSelected(new Set())
  }

  return <main className="page page-wide">
    {banner && <div className="banner" role="status">
      <span aria-hidden="true">✓</span><span className="grow"><strong>{banner.title}</strong> {banner.text}</span>
      <button type="button" className="icon-button" aria-label="Dismiss" onClick={onDismissBanner}>✕</button>
    </div>}
    <div className="page-head">
      <div>
        <h1 className="page-title">Product catalog</h1>
        <p className="page-sub">{catalog.total_rows} products · {catalog.with_shot_idea} with a Shot Idea · 1–3 approved images per product</p>
      </div>
      <div className="head-actions">
        <div className="spend" title={`Estimate: images Luma returned × ${money(unit)} each (upper end of Luma’s published price)`}>
          <div className="spend-label">GENERATION SPEND · ESTIMATE</div>
          <div className="spend-value"><strong>{spend.images} image{spend.images === 1 ? '' : 's'}</strong> · about {money(spend.est_cost_usd)}</div>
        </div>
        <button type="button" className="secondary-button" disabled={sendingStatus} title="Posts these tabs and the spend to the Slack status channel" onClick={onSendStatus}>{sendingStatus ? 'Sending…' : 'Send status to Slack'}</button>
        <button type="button" className="secondary-button" onClick={onImport}>↑ Import CSV</button>
      </div>
    </div>

    {notice && <div className="error notice" role="alert"><span className="grow">{notice}</span><button type="button" className="icon-button" aria-label="Dismiss message" onClick={onDismissNotice}>✕</button></div>}

    {!catalog.total_rows ? <div className="empty-state">Import a CSV to create your catalog. <button type="button" className="link-button" onClick={onImport}>Import CSV</button></div> : <>
      <nav className="tabs" aria-label="Filter by stage">
        {tabs.map(item => <button type="button" key={item.id} aria-pressed={item.id === current.id} onClick={() => setTab(item.id)}>
          {item.label} <span className="tab-count">{item.count}</span>
        </button>)}
      </nav>

      <div className="toolbar">
        {selected.size > 0 ? <div className="selection-bar">
          <span className="grow"><strong>{selected.size} selected</strong> · {images} images · up to {money(images * unit)}</span>
          <button type="button" className="ghost-button" onClick={() => setSelected(new Set())}>Clear</button>
          <button type="button" className="accent-button" disabled={actions.generating} onClick={() => void generateSelected()}>{actions.generating ? 'Starting…' : `Generate ${images} images`}</button>
        </div> : <>
          <p className="grow hint">{HINTS[current.id]}</p>
          {(current.id === ALL || current.id === 'generate') && selectable.length > 0 &&
            <button type="button" className="secondary-button" disabled={actions.generating} onClick={() => setSelected(new Set(selectable))}>Select all {selectable.length} to generate</button>}
          {current.id === 'post' && sendable.length > 0 &&
            <button type="button" className="primary-button" disabled={actions.sending} onClick={() => { if (window.confirm(`Retry posting ${sendable.length} product${sendable.length === 1 ? '' : 's'} to Slack?`)) actions.send(sendable) }}>{actions.sending ? 'Posting…' : `Retry posting all ${sendable.length}`}</button>}
          {current.id === 'drive' && deliverable.length > 0 &&
            <button type="button" className="primary-button" disabled={actions.saving} onClick={() => actions.deliver(deliverable, `Save ${unsaved} approved image${unsaved === 1 ? '' : 's'} to your Google Drive?`)}>{actions.saving ? 'Saving…' : `Save all ${deliverable.length} to Drive`}</button>}
        </>}
      </div>

      <div className="panel">
        <div className="catalog-grid catalog-head" aria-hidden="true"><span /><span>PRODUCT</span><span>SHOT IDEA</span><span>CANDIDATES</span><span>STAGE</span><span>NEXT STEP</span></div>
        {visible.map(({ row, stage }) => {
          const name = row.product_name || row.sku
          return <div className="catalog-grid catalog-row" key={row.sku}>
            <div className="cell-select">{canSelect(row) && <input type="checkbox" aria-label={`Select ${name}`} checked={selected.has(row.sku)} disabled={actions.generating} onChange={() => toggle(row.sku)} />}</div>
            <div className="product-cell">
              {/^https?:\/\//i.test(row.photo) ? <img className="thumb-md" src={row.photo} alt="" loading="lazy" /> : <div className="thumb-md" />}
              <div><strong>{name}</strong><small>{row.sku}{[row.color, row.price].filter(Boolean).map(value => ` · ${value}`).join('')}</small></div>
            </div>
            <div className="cell-idea">
              {row.shot_idea || <em className="muted">No Shot Idea</em>}
              {row.brief_changed && <div className="warn">Product attributes changed after these images were made</div>}
            </div>
            <Thumbnails row={row} perRequest={perRequest} open={image => setViewing({ sku: row.sku, imageId: image.id })} />
            <StageLabel stage={stage} />
            <NextStep row={row} stage={stage} perRequest={perRequest} unit={unit} actions={actions} />
          </div>
        })}
        {!visible.length && <p className="empty-row">No products at this stage.</p>}
        <div className="panel-foot"><span>Showing {visible.length} of {catalog.total_rows}</span><span>Sorted by next step · rows keep their place until you switch tabs</span></div>
      </div>
    </>}

    {viewingRow && <ImageLightbox row={viewingRow} imageId={viewing.imageId} perRequest={perRequest} unit={unit} actions={actions} onClose={() => setViewing(null)} />}
  </main>
}
