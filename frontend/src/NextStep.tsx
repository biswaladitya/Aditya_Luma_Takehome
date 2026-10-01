import { batchProgress, latestBatch, type CatalogRow, type Stage } from './catalogApi'

export type Actions = {
  generate: (skus: string[]) => Promise<boolean>
  send: (skus: string[]) => void
  deliver: (skus: string[], confirmText?: string) => void
  generating: boolean
  sending: boolean
  saving: boolean
}

export const STAGES: Record<Stage, { label: string; tone: string; done: number }> = {
  needs_input: { label: 'Needs brief', tone: 'input', done: 0 },
  ready: { label: 'Ready to generate', tone: 'neutral', done: 0 },
  failed: { label: 'Generation failed', tone: 'failed', done: 0 },
  generating: { label: 'Generating', tone: 'blue', done: 1 },
  post_failed: { label: 'Not posted', tone: 'failed', done: 1 },
  with_ellie: { label: 'With Ellie', tone: 'blue', done: 2 },
  approved: { label: 'Approved', tone: 'amber', done: 3 },
  saving: { label: 'Saving to Drive', tone: 'amber', done: 3 },
  in_drive: { label: 'In Drive', tone: 'dark', done: 4 },
}

export const money = (value: number) => `$${value.toFixed(2)}`

export function StageLabel({ stage }: { stage: Stage }) {
  const info = STAGES[stage]
  return <div>
    <span className={`pill tone-${info.tone}`}>{info.label}</span>
    <div className="stage-bar" aria-hidden="true">{[0, 1, 2, 3].map(i => <span key={i} className={i < info.done ? 'filled' : ''} />)}</div>
  </div>
}

/** After a brief change, approved and In Drive products keep their images; a new one is optional. */
function Regenerate({ row, name, actions }: { row: CatalogRow; name: string; actions: Actions }) {
  if (!row.brief_changed || !row.can_generate) return null
  return <>
    <p className="step-sub left">Brief changed since this was approved.</p>
    <button type="button" className="secondary-button block" disabled={actions.generating} onClick={() => void actions.generate([row.sku])} aria-label={`Regenerate: ${name}`}>Regenerate</button>
  </>
}

/** The single next step for a product; shared by the catalog row and the image view. */
export function NextStep({ row, stage, perRequest, unit, actions }: { row: CatalogRow; stage: Stage; perRequest: number; unit: number; actions: Actions }) {
  const batch = latestBatch(row, perRequest)
  const failed = batch.filter(image => image.status === 'failed')
  const name = row.product_name || row.sku
  switch (stage) {
    case 'in_drive':
      return <div>
        <div className="step-text step-green">Saved</div>{row.drive_url && <a href={row.drive_url} target="_blank" rel="noreferrer">Open in Drive ↗</a>}
        <Regenerate row={row} name={name} actions={actions} />
      </div>
    case 'saving':
      return <div className="step-text step-amber" role="status">Saving to Drive…</div>
    case 'approved':
      return <div>
        <button type="button" className="primary-button block" disabled={actions.saving} onClick={() => actions.deliver([row.sku])}
          aria-label={`${row.delivery_error ? 'Retry save' : 'Save to Drive'}: ${name}`}>{row.delivery_error ? 'Retry save' : 'Save to Drive'}</button>
        {row.delivery_error && <p className="step-error">{row.delivery_error}</p>}
        {/* An earlier brief's approval takes the row's stage; a newer brief's failures still need their retry. */}
        {row.can_send && <>
          <button type="button" className="outline-button danger block" disabled={actions.sending} onClick={() => actions.send([row.sku])} aria-label={`Retry posting: ${name}`}>Retry posting</button>
          <p className="step-error">{row.post_error || 'Not posted to Slack yet.'}</p>
        </>}
        {row.can_generate && failed.length > 0 && <p className="step-error">{failed[0].error || 'Generation failed.'}</p>}
        <Regenerate row={row} name={name} actions={actions} />
      </div>
    case 'with_ellie':
      return <div className="step-text step-blue">Waiting on Ellie</div>
    case 'generating': {
      const progress = batchProgress([row], perRequest)
      const percent = progress.total ? Math.max(Math.round(progress.finished / progress.total * 100), 6) : 6
      const posting = progress.total > 0 && progress.finished === progress.total
      return <div>
        <div className="step-text step-blue">{posting ? 'Posting to Slack…' : `Generating ${progress.finished} of ${progress.total}`}</div>
        <div className="progress-track active" role="progressbar" aria-label={`Generating images for ${name}`} aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.finished}>
          <div className="progress-fill" style={{ width: `${percent}%` }} />
        </div>
      </div>
    }
    case 'failed':
      return <div>
        <button type="button" className="outline-button danger block" disabled={actions.generating} onClick={() => void actions.generate([row.sku])} aria-label={`Retry generation: ${name}`}>Retry generation</button>
        <p className="step-error">{failed[0]?.error || 'Generation failed.'}</p>
      </div>
    case 'post_failed':
      return <div>
        <button type="button" className="outline-button danger block" disabled={actions.sending} onClick={() => actions.send([row.sku])} aria-label={`Retry posting: ${name}`}>Retry posting</button>
        <p className="step-error">{row.post_error || 'Not posted to Slack yet.'}</p>
        {failed.length > 0 && <p className="step-error">{failed.length} failed: {failed[0].error}</p>}
      </div>
    case 'ready':
      return <div>
        <button type="button" className="outline-button block" disabled={actions.generating} onClick={() => void actions.generate([row.sku])} aria-label={`Generate ${perRequest} images: ${name}`}>Generate {perRequest} images</button>
        <p className="step-sub">about {money(perRequest * unit)} · posted to Slack</p>
      </div>
    case 'needs_input':
      return <p className="step-sub left">{row.shot_idea ? row.issues.join(' · ') || 'Not available for generation.' : 'Add a Shot Idea in the CSV'}</p>
  }
}

