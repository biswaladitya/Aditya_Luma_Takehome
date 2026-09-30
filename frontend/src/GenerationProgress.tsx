import type { BatchProgress } from './catalogApi'

export default function GenerationProgress({ progress, label }: { progress: BatchProgress; label: string }) {
  const percent = progress.total ? Math.round((progress.finished / progress.total) * 100) : 0
  return <div className="progress">
    <div className="progress-label"><span>{label}</span><span>{progress.finished} of {progress.total} images{progress.failed ? ` · ${progress.failed} failed` : ''}</span></div>
    <div className={`progress-track ${progress.active ? 'active' : ''}`} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.finished}>
      <div className="progress-fill" style={{ width: `${progress.active ? Math.max(percent, 6) : percent}%` }} />
    </div>
  </div>
}
