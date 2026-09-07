import { useState } from 'react'
import { api, type Application } from '../api'

export default function ApplicationActions({ app, onSaved }: { app: Application; onSaved: () => void }) {
  const [status, setStatus] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const needsEvidence = app.status === 'assisting' || status === 'submitted' || (status === 'approved' && ['needs_human', 'failed'].includes(app.status))
  const options = app.status_options ?? []
  const save = async () => {
    setBusy(true); setError('')
    try { await api.setAppStatus(app.id, status, note.trim() || undefined); setStatus(''); setNote(''); onSaved() }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  const regenerate = async () => {
    setBusy(true); setError('')
    try { const result = await api.runPipeline('regenerate', { application_id: app.id }); setMessage(`Document regeneration queued as run #${result.run_id}. Review the new documents when it finishes.`); onSaved() }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  if (!options.length && !app.can_regenerate) return <span className="text-xs text-zinc-500">No manual changes available</span>
  return <div className="space-y-2 min-w-48">
    <div className="flex gap-2">
      <select aria-label={`Update application ${app.id}`} className="input text-xs" value={status} disabled={busy} onChange={e => setStatus(e.target.value)}>
        <option value="">Choose action</option>
        {options.map(s => <option key={s} value={s}>{s === 'approved' ? 'Approve' : s.replaceAll('_', ' ')}</option>)}
      </select>
      <button className="btn btn-sm" disabled={busy || !status || (needsEvidence && !note.trim())} onClick={save}>{busy ? 'Saving…' : 'Save'}</button>
    </div>
    {needsEvidence && <label className="block text-xs text-zinc-400 space-y-1">
      <span>{status === 'submitted' ? 'Employer confirmation evidence' : 'Check employer history and explain why retrying is safe'}</span>
      <textarea className="input w-full" value={note} onChange={e => setNote(e.target.value)} rows={2} />
    </label>}
    {app.preparation_issue && <p className="text-xs text-zinc-400">{app.preparation_issue}</p>}
    {app.can_regenerate && <button className="btn btn-sm" disabled={busy} onClick={regenerate}>Regenerate documents</button>}
    {message && <p role="status" className="text-xs text-zinc-400">{message}</p>}
    {error && <p role="alert" className="text-xs text-rose-300">{error}</p>}
  </div>
}
