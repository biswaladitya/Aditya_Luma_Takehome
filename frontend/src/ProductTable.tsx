import type { CatalogRow, FieldChanges, GenerationStatus } from './catalogApi'

const fieldNames: Record<string, string> = {
  sku: 'SKU', product_name: 'Product name', category: 'Category', color: 'Color / Finish',
  material: 'Material', price: 'Price', photo: 'Photo', shot_idea: 'Shot Idea', notes: 'Notes',
}

const statusLabels: Record<GenerationStatus, string> = {
  missing_input: 'Needs input', never_generated: 'Never generated',
  changed_since_generation: 'Image needs review', already_generated: 'Up to date',
}

function Changes({ changes }: { changes: FieldChanges }) {
  return <dl className="field-changes">
    {Object.entries(changes).map(([field, change]) => <div key={field}>
      <dt>{fieldNames[field] || field}</dt>
      <dd><span className="previous-value">{change.before || '(blank)'}</span><span aria-label="changed to"> → </span><strong>{change.after || '(blank)'}</strong></dd>
    </div>)}
  </dl>
}

export default function ProductTable({ rows, preview = false }: { rows: CatalogRow[]; preview?: boolean }) {
  if (!rows.length) return <div className="empty-state">No products to show.</div>
  return <div className="table-wrap"><table className="product-table">
    <thead><tr><th>PRODUCT</th><th>SHOT IDEA</th>{preview && <th>CATALOG CHANGES</th>}<th>IMAGE STATUS</th></tr></thead>
    <tbody>{rows.map((row, index) => <tr key={`${row.sku}-${row.row_number ?? index}`}>
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
        {row.images.length > 0 && <div className="generated-images">{row.images.map(image => image.image_url ? <a key={image.id} href={image.image_url} target="_blank" rel="noreferrer"><img src={image.image_url} alt={`Generated image for ${row.product_name}`} loading="lazy" /></a> : null)}<small>{row.images.length} saved {row.images.length === 1 ? 'image' : 'images'}</small></div>}
      </td>
    </tr>)}</tbody>
  </table></div>
}
