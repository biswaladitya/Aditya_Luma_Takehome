import GenerationProgress from './GenerationProgress'
import { batchProgress, canSelect, isGenerating, type CatalogRow, type FieldChanges, type GeneratedImage, type GenerationStatus, type ReviewState } from './catalogApi'

const fieldNames: Record<string, string> = {
  sku: 'SKU', product_name: 'Product name', category: 'Category', color: 'Color / Finish',
  material: 'Material', price: 'Price', photo: 'Photo', shot_idea: 'Shot Idea', notes: 'Notes',
}

const statusLabels: Record<GenerationStatus, string> = {
  missing_input: 'Needs input', never_generated: 'Never generated',
  changed_since_generation: 'Image needs review', already_generated: 'Up to date',
}

const reviewLabels: Record<ReviewState, string> = {
  pending_send: 'Sending to Slack', awaiting_approval: 'Awaiting Ellie', approved: 'Approved',
}

function imageLabel(image: GeneratedImage, row: CatalogRow) {
  if (!image.review) return 'Not sent'
  if (image.review.state === 'approved') return 'Approved'
  if (image.review.state === 'pending_send') return image.review.send_error ? 'Send failed' : 'Sending…'
  return row.review_status === 'approved' ? 'Not selected' : 'Awaiting Ellie'
}

function Changes({ changes }: { changes: FieldChanges }) {
  return <dl className="field-changes">
    {Object.entries(changes).map(([field, change]) => <div key={field}>
      <dt>{fieldNames[field] || field}</dt>
      <dd><span className="previous-value">{change.before || '(blank)'}</span><span aria-label="changed to"> → </span><strong>{change.after || '(blank)'}</strong></dd>
    </div>)}
  </dl>
}

export type SlackSend = { send: (sku: string) => void; disabled: boolean }

export type Selection = { selected: Set<string>; toggle: (sku: string) => void; disabled: boolean }

export default function ProductTable({ rows, preview = false, selection, slack, perRequest = 2 }: { rows: CatalogRow[]; preview?: boolean; selection?: Selection; slack?: SlackSend; perRequest?: number }) {
  if (!rows.length) return <div className="empty-state">No products to show.</div>
  return <div className="table-wrap"><table className="product-table">
    <thead><tr>{selection && <th aria-label="Select" />}<th>PRODUCT</th><th>SHOT IDEA</th>{preview && <th>CATALOG CHANGES</th>}<th>IMAGE STATUS</th></tr></thead>
    <tbody>{rows.map((row, index) => <tr key={`${row.sku}-${row.row_number ?? index}`}>
      {selection && <td className="select-cell">{canSelect(row)
        ? <input type="checkbox" aria-label={`Select ${row.product_name || row.sku}`} checked={selection.selected.has(row.sku)} disabled={selection.disabled} onChange={() => selection.toggle(row.sku)} />
        : null}</td>}
      <td>
        <div className="product-cell">
          <div className="product-image">{/^https?:\/\//i.test(row.photo) ? <img src={row.photo} alt="" loading="lazy" /> : <span>NO PHOTO</span>}</div>
          <div><strong>{row.product_name || 'Unnamed product'}</strong><small>{row.sku || `Row ${row.row_number}`}{row.color ? ` · ${row.color}` : ''}</small></div>
        </div>
        <details className="product-details"><summary>Product details</summary><dl>
          {(['category', 'color', 'material', 'price', 'photo', 'notes'] as const).map(field => <div key={field}><dt>{fieldNames[field]}</dt><dd>{row[field] || '(blank)'}</dd></div>)}
        </dl></details>
      </td>
      <td className="idea-cell">{row.shot_idea || <span className="muted">No Shot Idea yet</span>}</td>
      {preview && <td>
        <span className={`status change-${row.change_type}`}>{row.change_type === 'new' ? 'New product' : row.change_type}</span>
        {row.changes && <Changes changes={row.changes} />}
      </td>}
      <td>
        <span className={`status ${row.generation_status === 'already_generated' ? 'ready' : 'needs-input'}`}>{statusLabels[row.generation_status]}</span>
        {row.issues.length > 0 && <p className="row-issues">{row.issues.join(' · ')}</p>}
        {Object.keys(row.generation_changes).length > 0 && <details className="product-details"><summary>Changed since generation</summary><Changes changes={row.generation_changes} /></details>}
        {isGenerating(row) && <GenerationProgress label="Generating" progress={batchProgress([row], perRequest)} />}
        {row.review_status && <p><span className={`status review-${row.review_status}`}>{reviewLabels[row.review_status]}</span></p>}
        {row.send_error && <p className="row-issues">Slack: {row.send_error}</p>}
        {slack && row.can_send && <button className="secondary-button" disabled={slack.disabled} onClick={() => slack.send(row.sku)}>{row.send_error ? 'Retry send to Slack' : 'Send to Slack'}</button>}
        {row.images.some(i => i.status === 'failed') && <p className="row-issues">{row.images.filter(i => i.status === 'failed').length} failed: {row.images.find(i => i.status === 'failed')?.error}</p>}
        {row.images.some(i => i.status === 'done') && <div className="generated-images">{row.images.filter(i => i.status === 'done' && i.image_url).map(image => <figure key={image.id} className={`generated-image ${image.review?.state === 'approved' ? 'approved' : ''}`}><a href={image.image_url} target="_blank" rel="noreferrer"><img src={image.image_url} alt={`Generated image v${image.version} for ${row.product_name}`} loading="lazy" /></a><figcaption>{imageLabel(image, row)}</figcaption></figure>)}<small>{row.images.filter(i => i.status === 'done').length} saved images</small></div>}
      </td>
    </tr>)}</tbody>
  </table></div>
}
