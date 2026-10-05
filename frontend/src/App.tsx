import { useEffect, useState } from 'react'
import CatalogView from './CatalogView'
import { catalogRequest, deliverToDrive, generateImages, isDelivering, isGenerating, isInReview, sendForReview, sendStatusReport, type Catalog, type CatalogPreview } from './catalogApi'
import { preparePicker } from './drivePicker'
import { clearDriveToken, getDriveToken, prepareGoogleSignIn } from './googleAuth'
import ImportReview from './ImportReview'
import type { Actions } from './NextStep'

type Banner = { title: string; text: string }

const PENDING_IMPORT_KEY = 'luma.pendingImport'

function rememberPreview(id: string | null) {
  try {
    if (id) localStorage.setItem(PENDING_IMPORT_KEY, id)
    else localStorage.removeItem(PENDING_IMPORT_KEY)
  } catch { /* The preview remains usable when browser storage is unavailable. */ }
}

const skippedText = (skipped: { sku: string; reason: string }[]) =>
  `Skipped ${skipped.length}: ${skipped.map(item => `${item.sku} (${item.reason})`).join(', ')}`

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? '' : 's'}`

export default function App() {
  const [view, setView] = useState<'catalog' | 'import'>('catalog')
  const [preview, setPreview] = useState<CatalogPreview | null>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [loadError, setLoadError] = useState('')
  const [banner, setBanner] = useState<Banner | null>(null)
  const [notice, setNotice] = useState('')
  const [generating, setGenerating] = useState(false)
  const [sendingToSlack, setSendingToSlack] = useState(false)
  const [writingToDrive, setWritingToDrive] = useState(false)
  const [sendingStatus, setSendingStatus] = useState(false)
  const anyGenerating = Boolean(catalog?.rows.some(isGenerating))
  const anyInReview = Boolean(catalog?.rows.some(isInReview))
  const anyDelivering = Boolean(catalog?.rows.some(isDelivering))

  const refresh = async () => setCatalog(await catalogRequest<Catalog>('/api/catalog'))

  // Refresh while Luma jobs and Slack posts run, Ellie is deciding or Drive writes are in flight, so results appear without a reload.
  useEffect(() => {
    if (!anyGenerating && !anyInReview && !anyDelivering) return
    const timer = setInterval(() => { refresh().catch(() => { /* Retry on the next tick. */ }) }, 3000)
    return () => clearInterval(timer)
  }, [anyGenerating, anyInReview, anyDelivering])

  // A pending import saved in this browser opens straight to its review.
  useEffect(() => {
    let active = true
    async function restore() {
      try {
        const saved = await catalogRequest<Catalog>('/api/catalog')
        if (!active) return
        setCatalog(saved)
        let pendingId: string | null = null
        try { pendingId = localStorage.getItem(PENDING_IMPORT_KEY) } catch { /* Optional browser state. */ }
        if (pendingId) {
          const pending = await catalogRequest<CatalogPreview>(`/api/catalog/imports/${encodeURIComponent(pendingId)}`).catch(() => null)
          if (!active) return
          if (pending?.status === 'pending') { setPreview(pending); setView('import') } else rememberPreview(null)
        } else if (!saved.total_rows) setView('import')
      } catch (cause) {
        if (active) setLoadError(cause instanceof Error ? cause.message : 'Could not restore the catalog.')
      }
    }
    void restore()
    return () => { active = false }
  }, [])

  // Load Google sign-in and Picker early so a Save to Drive or Fetch from Google Drive click can open its popup without being blocked.
  useEffect(() => {
    prepareGoogleSignIn().catch(() => { /* The click reports what is missing. */ })
    preparePicker().catch(() => { /* Retried, and reported, on the click. */ })
  }, [])

  function choosePreview(next: CatalogPreview | null) {
    setPreview(next)
    rememberPreview(next?.preview_id ?? null)
  }

  async function applied(result: CatalogPreview) {
    choosePreview(null)
    const outdated = result.rows.filter(row => row.brief_case === 'with_ellie' || row.candidates_outdated).length
    setBanner({ title: 'Catalog updated.', text: `${result.changed_count} changed, ${result.new_count} new from ${result.filename}.${outdated ? ` Candidates for ${plural(outdated, 'product')} are now outdated.` : ''}` })
    setView('catalog')
    await refresh().catch(cause => setLoadError(cause instanceof Error ? cause.message : 'Could not load the catalog.'))
  }

  async function generate(skus: string[]) {
    if (!catalog || !skus.length || generating) return false
    const { images_per_request: perRequest, est_cost_per_image_usd: unit } = catalog.generation_config
    const images = skus.length * perRequest
    const name = catalog.rows.find(row => row.sku === skus[0])?.product_name || skus[0]
    const target = skus.length === 1 ? `for ${name}` : `(${perRequest} for each of ${skus.length} products)`
    if (!window.confirm(`Generate ${images} images ${target}? They’ll be posted to Slack for Ellie’s review when ready. Estimated cost up to $${(images * unit).toFixed(2)}.`)) return false
    setGenerating(true)
    setNotice('')
    try {
      const result = await generateImages(skus)
      if (result.skipped.length) setNotice(skippedText(result.skipped))
      await refresh()
      return true
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : 'Could not start generation.')
      return false
    } finally {
      setGenerating(false)
    }
  }

  async function sendToSlack(skus: string[]) {
    if (!skus.length || sendingToSlack) return
    setSendingToSlack(true)
    setNotice('')
    try {
      const result = await sendForReview(skus)
      if (result.skipped.length) setNotice(skippedText(result.skipped))
      await refresh()
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : 'Could not post to Slack.')
    } finally {
      setSendingToSlack(false)
    }
  }

  async function writeToDrive(skus: string[], confirmText?: string) {
    if (!skus.length || writingToDrive) return
    setNotice('')
    let token: string
    try {
      token = await getDriveToken()  // Must start inside the click; the popup is only shown when needed.
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : 'Google sign-in failed.')
      return
    }
    if (confirmText && !window.confirm(confirmText)) return
    setWritingToDrive(true)
    try {
      const result = await deliverToDrive(skus, token)
      if (result.skipped.length) setNotice(skippedText(result.skipped))
      await refresh()
    } catch (cause) {
      clearDriveToken()  // An expired or revoked token signs in again on the next click.
      setNotice(cause instanceof Error ? cause.message : 'Could not save to Drive.')
    } finally {
      setWritingToDrive(false)
    }
  }

  async function sendStatus() {
    if (sendingStatus) return
    setSendingStatus(true)
    setNotice('')
    try {
      const result = await sendStatusReport()
      setBanner({ title: 'Status sent to Slack.', text: `${plural(result.products, 'product')} reported in the status channel.` })
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : 'Could not send the status to Slack.')
    } finally {
      setSendingStatus(false)
    }
  }

  const actions: Actions = {
    generate, send: skus => void sendToSlack(skus), deliver: (skus, confirmText) => void writeToDrive(skus, confirmText),
    generating, sending: sendingToSlack, saving: writingToDrive,
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">LUMA <span className="brand-divider">/</span> <span className="brand-sub">STUDIO</span></div>
        <nav className="topnav" aria-label="Main">
          <button type="button" aria-current={view === 'catalog' ? 'page' : undefined} onClick={() => setView('catalog')}>Catalog</button>
          <button type="button" aria-current={view === 'import' ? 'page' : undefined} onClick={() => setView('import')}>Import CSV</button>
        </nav>
      </header>
      {loadError && <div className="page"><div className="error" role="alert">{loadError}</div></div>}
      {view === 'import'
        ? <ImportReview preview={preview} onPreview={choosePreview} onApplied={applied} onCancel={() => setView('catalog')} />
        : <CatalogView catalog={catalog} actions={actions} banner={banner} onDismissBanner={() => setBanner(null)}
          notice={notice} onDismissNotice={() => setNotice('')} onImport={() => setView('import')}
          onSendStatus={() => void sendStatus()} sendingStatus={sendingStatus} />}
    </div>
  )
}
