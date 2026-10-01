export type GenerationStatus = 'missing_input' | 'never_generated' | 'changed_since_generation' | 'already_generated'
export type FieldChanges = Record<string, { before: string | null; after: string | null }>

/** A review exists only once a candidate is posted to Slack. */
export type ReviewState = 'awaiting_approval' | 'approved'
export type ImageReview = { state: ReviewState; approved_by: string | null; approved_at: string | null }

export type DeliveryState = 'pending' | 'delivered'
export type ImageDelivery = { state: DeliveryState; filename: string; drive_url: string | null; delivered_at: string | null; error: string | null }

export type GeneratedImage = {
  id: string
  image_url?: string
  generated_at: string
  version: number
  status: 'queued' | 'processing' | 'done' | 'failed'
  error: string | null
  /** Luma returned it and it is being posted to Slack. */
  posting: boolean
  post_error: string | null
  brief_version: number
  /** Made from an older brief than the product's: it can't be approved. */
  outdated: boolean
  generated_from: Record<string, string>
  review: ImageReview | null
  delivery: ImageDelivery | null
}

/** What accepting an import row does to the product; see state_machine_plan.md. */
export type BriefCase = 'new' | 'info_only' | 'unchanged' | 'invalid' | 'not_generated' | 'with_ellie' | 'approved_not_in_drive' | 'in_drive'

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
  brief_version?: number
  issues: string[]
  ready: boolean
  change_type?: 'new' | 'changed' | 'unchanged' | 'invalid'
  changes?: FieldChanges
  brief_case?: BriefCase
  generation_status: GenerationStatus
  generation_changes: FieldChanges
  /** Has images, none made from the current brief. */
  brief_changed: boolean
  image_exists: boolean
  images: GeneratedImage[]
  /** A generation, including posting its candidates to Slack, is running. */
  generating: boolean
  can_generate: boolean
  /** For the current brief. */
  review_status: ReviewState | null
  /** The newest approved image; older approvals stay in approved_image_ids. */
  approved_image_id: string | null
  approved_image_ids: string[]
  post_error: string | null
  /** Has current-brief candidates that never reached Slack: Retry posting. */
  can_send: boolean
  delivery_status: DeliveryState | null
  delivery_error: string | null
  drive_url: string | null
  delivering: boolean
  can_deliver: boolean
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
  brief_case_counts?: Record<BriefCase, number>
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

/** Retry posting: posts current-brief candidates that never reached Slack. Generate posts the rest. */
export function sendForReview(skus: string[]): Promise<ReviewResult> {
  return catalogRequest('/api/reviews', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skus }),
  })
}

export type DeliveryResult = { queued: string[]; skipped: { sku: string; reason: string }[] }

/** Saves every approved image not yet in Drive to the signed-in user's Google Drive. Only this explicit request writes anything. */
export function deliverToDrive(skus: string[], accessToken: string): Promise<DeliveryResult> {
  return catalogRequest('/api/deliveries', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skus, access_token: accessToken }),
  })
}

/** Rows with a Drive write in flight; a failed write waits for a retry instead. */
export const isDelivering = (row: CatalogRow) => row.delivering

/** Rows whose Slack state can change without user action (Ellie's decision). */
export const isInReview = (row: CatalogRow) => row.review_status === 'awaiting_approval'

export const isGenerating = (row: CatalogRow) => row.generating
/** Ready products, or an optional regenerate after a brief change; never while generating, posting or saving. */
export const canSelect = (row: CatalogRow) => row.can_generate

/** An image is "not selected" when Ellie approved another candidate made from the same brief. */
export const notSelected = (row: CatalogRow, image: GeneratedImage) => image.review?.state !== 'approved'
  && row.images.some(other => other.review?.state === 'approved' && other.brief_version === image.brief_version)

/** The most recent request's candidates: the newest `perRequest` images by version. */
export function latestBatch(row: CatalogRow, perRequest: number) {
  return [...row.images].sort((a, b) => b.version - a.version).slice(0, perRequest)
}

export type BatchProgress = { total: number; finished: number; failed: number; active: boolean }

/** Luma reports no percentage, so progress counts finished images out of the images requested. */
export function batchProgress(rows: CatalogRow[], perRequest: number): BatchProgress {
  const images = rows.flatMap(row => latestBatch(row, perRequest))
  const finished = images.filter(image => image.status === 'done' || image.status === 'failed' || image.posting).length
  return {
    total: images.length, finished, failed: images.filter(image => image.status === 'failed').length,
    active: images.some(image => image.status === 'queued' || image.status === 'processing'),
  }
}

export type Stage = 'in_drive' | 'saving' | 'approved' | 'with_ellie' | 'generating' | 'failed' | 'post_failed' | 'ready' | 'needs_input'

/** The one next step for a product (state_machine_plan.md). Order matters: the first match wins. */
export function productStage(row: CatalogRow, perRequest: number): Stage {
  if (isGenerating(row)) return 'generating'
  if (isDelivering(row)) return 'saving'
  if (row.can_deliver) return 'approved'
  if (row.review_status === 'awaiting_approval') return 'with_ellie'
  if (row.can_send) return 'post_failed'
  if (row.can_generate && latestBatch(row, perRequest).some(image => image.status === 'failed')) return 'failed'
  if (row.delivery_status === 'delivered') return 'in_drive'
  if (row.can_generate) return 'ready'
  return 'needs_input'
}
