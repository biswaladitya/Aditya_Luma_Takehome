import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { NextStep, type Actions } from './NextStep'
import { notSelected, productStage, type CatalogRow, type GeneratedImage } from './catalogApi'

type Item = { id: string; src: string; caption: string; short: string; image: GeneratedImage | null }

function status(image: GeneratedImage | null, row: CatalogRow): { label: string; tone: string; detail: string } {
  if (!image) return { label: 'Source photo', tone: 'neutral', detail: 'White-background photo from the CSV' }
  const review = image.review
  if (review?.state === 'approved') {
    const when = review.approved_at ? ` · ${new Date(review.approved_at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}` : ''
    const drive = image.delivery?.state === 'delivered' ? ` · In Drive as ${image.delivery.filename}` : ''
    return { label: `Approved · attributes v${image.brief_version}`, tone: 'amber', detail: `Approved by Ellie in Slack${when}${drive}` }
  }
  if (image.outdated) return { label: 'Outdated', tone: 'neutral', detail: review ? 'Made from older product attributes, so it can’t be approved' : 'Made from older product attributes' }
  if (notSelected(row, image)) return { label: 'Not selected', tone: 'neutral', detail: 'Ellie approved another candidate for the same product attributes' }
  if (review?.state === 'awaiting_approval') return { label: 'In Slack', tone: 'blue', detail: 'Waiting on Ellie' }
  if (image.posting) return { label: 'Posting', tone: 'blue', detail: 'Posting to Slack…' }
  return { label: 'Not posted', tone: image.post_error ? 'failed' : 'neutral', detail: image.post_error || 'Not posted to Slack yet' }
}

export default function ImageLightbox({ row, imageId, perRequest, unit, actions, onClose }: {
  row: CatalogRow; imageId: string; perRequest: number; unit: number; actions: Actions; onClose: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [currentId, setCurrentId] = useState(imageId)
  const name = row.product_name || row.sku
  const candidates = row.images.filter(image => image.image_url).sort((a, b) => a.version - b.version)
  const items: Item[] = [
    ...(/^https?:\/\//i.test(row.photo) ? [{ id: 'source', src: row.photo, caption: 'Source photo', short: 'Source', image: null }] : []),
    ...candidates.map((image, index) => ({ id: image.id, src: image.image_url!, caption: `Candidate ${index + 1} of ${candidates.length}`, short: `Candidate ${index + 1}`, image })),
  ]
  const index = Math.max(items.findIndex(item => item.id === currentId), 0)
  const item = items[index]
  const info = status(item.image, row)
  const idea = item.image?.generated_from.shot_idea

  // The native dialog traps focus, closes on Esc and restores focus when it closes.
  useEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])

  const go = (step: number) => setCurrentId(items[(index + step + items.length) % items.length].id)

  function onKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key === 'ArrowLeft') go(-1)
    else if (event.key === 'ArrowRight') go(1)
  }

  return <dialog ref={dialogRef} className="lightbox" aria-label={`${name} images`} onClose={onClose} onKeyDown={onKeyDown}
    onClick={event => { if (event.target === dialogRef.current) dialogRef.current?.close() }}>
    <div className="lightbox-body">
      <div className="lightbox-stage">
        <img src={item.src} alt={`${item.caption} for ${name}`} />
        {items.length > 1 && <>
          <button type="button" className="nav-button prev" aria-label="Previous image" onClick={() => go(-1)}>‹</button>
          <button type="button" className="nav-button next" aria-label="Next image" onClick={() => go(1)}>›</button>
        </>}
        <div className="lightbox-caption" aria-live="polite">{item.caption}</div>
      </div>
      <aside className="lightbox-panel">
        <div className="lightbox-title">
          <div>
            <div className="muted small">{[row.sku, row.color, row.price].filter(Boolean).join(' · ')}</div>
            <h2>{name}</h2>
          </div>
          <button type="button" className="icon-button boxed" aria-label="Close" onClick={() => dialogRef.current?.close()}>✕</button>
        </div>
        <div>
          <span className={`pill tone-${info.tone}`}>{info.label}</span>
          <div className="muted small detail">{info.detail}</div>
        </div>
        {item.image && <div>
          <div className="label">SHOT IDEA USED</div>
          <p className="idea">{idea || '(blank)'}</p>
          {idea !== row.shot_idea && <p className="warn-box">The catalog’s Shot Idea is now “{row.shot_idea || '(blank)'}”.</p>}
        </div>}
        <div>
          <div className="label">ALL IMAGES</div>
          <div className="strip">{items.map(entry => <button type="button" key={entry.id} aria-label={`Show ${entry.caption}`} aria-current={entry.id === item.id}
            className={entry.id === item.id ? 'current' : ''} onClick={() => setCurrentId(entry.id)}>
            <img src={entry.src} alt="" /><span>{entry.short}</span>
          </button>)}</div>
        </div>
        <div className="lightbox-actions">
          <NextStep row={row} stage={productStage(row, perRequest)} perRequest={perRequest} unit={unit} actions={actions} />
          <a href={item.src} target="_blank" rel="noreferrer">Open original in new tab ↗</a>
        </div>
      </aside>
    </div>
  </dialog>
}
