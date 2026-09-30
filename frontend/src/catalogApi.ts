export type GenerationStatus = 'missing_input' | 'never_generated' | 'changed_since_generation' | 'already_generated'
export type FieldChanges = Record<string, { before: string | null; after: string | null }>

export type ReviewState = 'pending_send' | 'awaiting_approval' | 'approved'
export type ImageReview = { state: ReviewState; send_error: string | null; approved_by: string | null; approved_at: string | null }

export type GeneratedImage = {
  id: string
  image_url?: string
  generated_at: string
  version: number
  status: 'queued' | 'processing' | 'done' | 'failed'
  error: string | null
  review: ImageReview | null
}

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
  images: GeneratedImage[]
  review_status: ReviewState | null
  approved_image_id: string | null
  send_error: string | null
  can_send: boolean
}

export type Catalog = {
  total_rows: number
  with_shot_idea: number
  without_shot_idea: number
  ready_to_generate: number
  with_issues: number
  generation_summary: Record<GenerationStatus, number>
  generation_config: { images_per_request: number; est_cost_per_image_usd: number }
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

export type GenerationResult = { queued: string[]; skipped: { sku: string; reason: string }[]; images_queued: number }

export function generateImages(skus: string[]): Promise<GenerationResult> {
  return catalogRequest('/api/generations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skus }),
  })
}

export type ReviewResult = { queued: string[]; skipped: { sku: string; reason: string }[] }

/** Posts candidates to Slack. Only this explicit request sends anything. */
export function sendForReview(skus: string[]): Promise<ReviewResult> {
  return catalogRequest('/api/reviews', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skus }),
  })
}

/** Rows whose Slack state can still change without user action (a send or Ellie's decision). */
export const isInReview = (row: CatalogRow) => row.review_status === 'pending_send' && !row.send_error || row.review_status === 'awaiting_approval'

const ACTIVE = ['queued', 'processing']
export const isGenerating = (row: CatalogRow) => row.images.some(image => ACTIVE.includes(image.status))
export const canSelect = (row: CatalogRow) =>
  (row.generation_status === 'never_generated' || row.generation_status === 'changed_since_generation') && !isGenerating(row)

/** The most recent request's candidates: the newest `perRequest` images by version. */
export function latestBatch(row: CatalogRow, perRequest: number) {
  return [...row.images].sort((a, b) => b.version - a.version).slice(0, perRequest)
}

export type BatchProgress = { total: number; finished: number; failed: number; active: boolean }

/** Luma reports no percentage, so progress counts finished images out of the images requested. */
export function batchProgress(rows: CatalogRow[], perRequest: number): BatchProgress {
  const images = rows.flatMap(row => latestBatch(row, perRequest))
  const finished = images.filter(image => image.status === 'done' || image.status === 'failed').length
  return {
    total: images.length, finished, failed: images.filter(image => image.status === 'failed').length,
    active: images.some(image => image.status === 'queued' || image.status === 'processing'),
  }
}
