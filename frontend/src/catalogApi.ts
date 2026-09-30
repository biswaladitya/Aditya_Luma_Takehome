export type GenerationStatus = 'missing_input' | 'never_generated' | 'changed_since_generation' | 'already_generated'
export type FieldChanges = Record<string, { before: string | null; after: string | null }>

export type CatalogRow = {
  sku: string
  product_name: string
  category: string
  color: string
  material: string
  price: string
  photo: string
  shot_idea: string
  notes: string
  row_number?: number
  version?: number
  issues: string[]
  ready: boolean
  change_type?: 'new' | 'changed' | 'unchanged' | 'invalid'
  changes?: FieldChanges
  generation_status: GenerationStatus
  generation_changes: FieldChanges
  image_exists: boolean
  images: { id: string; image_url?: string; generated_at: string }[]
}

export type Catalog = {
  total_rows: number
  with_shot_idea: number
  without_shot_idea: number
  ready_to_generate: number
  with_issues: number
  generation_summary: Record<GenerationStatus, number>
  rows: CatalogRow[]
}

export type CatalogPreview = Catalog & {
  preview_id: string
  filename: string
  status: 'pending' | 'applied'
  can_confirm: boolean
  new_count: number
  changed_count: number
  unchanged_count: number
  invalid_count: number
  errors: string[]
}

export async function catalogRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new Error(data?.detail || `Catalog request failed (${response.status}). Please try again.`)
  if (data === null) throw new Error('The catalog server returned an unreadable response.')
  return data as T
}

export function uploadCatalogPreview(file: File): Promise<CatalogPreview> {
  const form = new FormData()
  form.append('file', file)
  return catalogRequest('/api/catalog/preview', { method: 'POST', body: form })
}

export function confirmCatalogUpdate(id: string): Promise<CatalogPreview> {
  return catalogRequest(`/api/catalog/imports/${encodeURIComponent(id)}/confirm`, { method: 'POST' })
}
