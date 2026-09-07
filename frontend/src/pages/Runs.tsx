import WorkerHealth from "../components/WorkerHealth"
import { artifactUrl } from '../api'
import { useState } from 'react'
import { Square } from 'lucide-react'
import { api, usePoll, ago } from '../api'
import { Page, Badge, Empty } from '../components/ui'

export default function Runs() {
  const [sel, setSel] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [level, setLevel] = useState('')
  const { data: runs, refresh } = usePoll(api.runs, 5000)
  const { data: events } = usePoll(() => api.events({ run_id: sel ?? undefined, level: level || undefined, limit: 300, latest: true }).then(e => e.reverse()), 4000, [sel, level])
  return (
    <Page title="Runs & logs" sub="Every collector and skill execution. Paused-for-human means a run is waiting for you (login, CAPTCHA, unknown question).">
      <WorkerHealth />
      {error && <p role="alert" className="text-sm text-rose-300">{error}</p>}
      <div className="grid lg:grid-cols-[360px_1fr] gap-4">
        <div className="card p-0 max-h-[75vh] overflow-y-auto">
          <button className={`w-full text-left px-3 py-2 text-sm border-b border-zinc-800 ${sel == null ? 'bg-zinc-800' : 'hover:bg-zinc-900'}`} onClick={() => setSel(null)}>All events</button>
          {runs?.map(r => (
            <div key={r.id} className={`w-full text-left px-3 py-2 border-b border-zinc-800/70 ${sel === r.id ? 'bg-zinc-800' : 'hover:bg-zinc-900'}`}>
              <button className="w-full text-left" onClick={() => setSel(r.id)}>
              <div className="flex items-center justify-between gap-2 text-sm"><span className="font-medium text-zinc-100">#{r.id} {r.name}</span><Badge v={r.status} /></div>
              <div className="text-[11px] text-zinc-500 flex justify-between mt-0.5"><span>{r.kind} · {ago(r.started_at)}</span>
                <span>{r.stats && Object.entries(r.stats).map(([k, v]) => `${k}:${v}`).join(' ')}</span></div>
              </button>
              {(['queued', 'running', 'paused_for_human'].includes(r.status)) && <button className="btn btn-sm btn-danger mt-1" onClick={async e => { e.stopPropagation(); try { await api.stopRun(r.id); refresh() } catch (e) { setError(String(e)) } }}><Square className="size-3" /> stop</button>}
              {r.ended_at && ['failed', 'interrupted', 'stopped', 'partially_completed', 'paused_for_human'].includes(r.status) && <button className="btn btn-sm mt-1 ml-2" onClick={async () => { try { const result = await api.retryRun(r.id); setSel(result.run_id); refresh() } catch (e) { setError(String(e)) } }}>Retry unfinished work</button>}
              {r.error && <div className="text-[11px] text-rose-300 truncate">{r.error}</div>}
            </div>
          ))}
          {!runs?.length && <Empty text="No runs yet." />}
        </div>
        <div className="card p-0 max-h-[75vh] overflow-y-auto">
          <div className="flex gap-2 p-2 border-b border-zinc-800 sticky top-0 bg-zinc-900">
            {['', 'info', 'warn', 'error', 'human'].map(l => <button key={l} className={`btn btn-sm ${level === l ? 'bg-zinc-700' : ''}`} onClick={() => setLevel(l)}>{l || 'all'}</button>)}
          </div>
          <div className="font-mono text-xs">
            {events?.map(e => (
              <div key={e.id} className={`px-3 py-1.5 border-b border-zinc-800/50 flex gap-2 ${e.level === 'human' ? 'bg-fuchsia-500/10' : e.level === 'error' ? 'bg-rose-500/5' : ''}`}>
                <span className="text-zinc-500 shrink-0">{new Date(e.ts + 'Z').toLocaleTimeString()}</span><Badge v={e.level} />
                <span className="text-zinc-500 shrink-0">{e.platform ?? ''}</span><span className="text-zinc-200 break-words">{e.message}
                  {e.screenshot && <a className="ml-2 underline text-sky-300" href={artifactUrl(e.screenshot)} target="_blank" rel="noreferrer">screenshot</a>}</span>
              </div>
            ))}
            {!events?.length && <div className="p-4 text-zinc-500">No events.</div>}
          </div>
        </div>
      </div>
    </Page>
  )
}
