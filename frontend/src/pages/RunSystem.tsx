import { useRunEvents } from "../useRunEvents"
import WorkerHealth from "../components/WorkerHealth"
import { artifactUrl } from '../api'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Play, Square, Lock, CheckSquare, ListChecks } from 'lucide-react'
import { api, usePoll, type Event, type Run } from '../api'
import { Page, Badge, Empty } from '../components/ui'

const isFullRun = (r: Run) => r.kind === 'fullrun' || r.name === 'fullrun' || r.kind === 'full_run' || r.name === 'full_run'
const LIVE = new Set(['queued', 'running', 'paused_for_human'])

const dur = (a: string | null, b: string | null) => {
  if (!a) return '—'
  const t0 = new Date(a + (a.endsWith('Z') ? '' : 'Z')).getTime()
  const t1 = b ? new Date(b + (b.endsWith('Z') ? '' : 'Z')).getTime() : Date.now()
  const s = Math.max(0, Math.round((t1 - t0) / 1000))
  const m = Math.floor(s / 60), h = Math.floor(m / 60)
  return h ? `${h}h ${m % 60}m ${s % 60}s` : m ? `${m}m ${s % 60}s` : `${s}s`
}

const StatsGrid = ({ stats }: { stats: Record<string, number> | null }) => {
  const e = Object.entries(stats ?? {})
  if (!e.length) return <div className="text-xs text-zinc-500">No stats yet.</div>
  return (
    <div className="flex flex-wrap gap-2">
      {e.map(([k, v]) => (
        <div key={k} className="rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-1.5">
          <div className="text-[11px] text-zinc-500">{k.replace(/_/g, ' ')}</div>
          <div className="text-sm font-semibold text-zinc-100">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</div>
        </div>
      ))}
    </div>
  )
}

function EventLog({ events, live }: { events: Event[]; live: boolean }) {
  const box = useRef<HTMLDivElement>(null)
  const [stick, setStick] = useState(true)
  useEffect(() => {
    const el = box.current
    if (el && stick) el.scrollTop = el.scrollHeight
  }, [events, stick])
  const onScroll = () => {
    const el = box.current
    if (!el) return
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }
  return (
    <div className="relative">
      <div ref={box} onScroll={onScroll} className="font-mono text-xs max-h-[52vh] overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/60">
        {events.map(e => e.message.startsWith('▶') ? (
          <div key={e.id} className="px-3 py-2 mt-1 border-y border-emerald-800/40 bg-emerald-500/10 text-emerald-300 font-semibold tracking-wide">
            {e.message.replace(/^▶\s*/, '')}
          </div>
        ) : (
          <div key={e.id} className={`px-3 py-1 flex gap-2 border-b border-zinc-800/40 ${e.level === 'error' ? 'bg-rose-500/10 text-rose-300' : e.level === 'warn' ? 'bg-amber-500/10 text-amber-300' : e.level === 'human' ? 'bg-fuchsia-500/10 text-fuchsia-200' : 'text-zinc-300'}`}>
            <span className="text-zinc-600 shrink-0">{new Date(e.ts + (e.ts.endsWith('Z') ? '' : 'Z')).toLocaleTimeString()}</span>
            {e.platform && <span className="text-zinc-500 shrink-0">{e.platform}</span>}
            <span className="break-words min-w-0">{e.message}
              {e.screenshot && <a className="ml-2 underline text-sky-300" href={artifactUrl(e.screenshot)} target="_blank" rel="noreferrer">screenshot</a>}</span>
          </div>
        ))}
        {!events.length && <div className="p-4 text-zinc-500">{live ? 'Waiting for the first log line…' : 'No log lines.'}</div>}
      </div>
      {!stick && (
        <button className="btn btn-sm absolute bottom-3 right-4" onClick={() => { setStick(true); const el = box.current; if (el) el.scrollTop = el.scrollHeight }}>
          Jump to latest
        </button>
      )}
    </div>
  )
}

export default function RunSystem() {
  const { data: platforms, err: platErr } = usePoll(api.fullRunPlatforms, 120000)
  const { data: runs } = usePoll(() => api.runs(40), 3000)

  const [sel, setSel] = useState<string[]>([])
  const [apply, setApply] = useState(false)
  const [starting, setStarting] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [watchId, setWatchId] = useState<number | null>(null)
  const [stopReq, setStopReq] = useState<number | null>(null)

  const fullRuns = useMemo(() => (runs ?? []).filter(isFullRun), [runs])
  const activeRun = useMemo(() => fullRuns.find(r => LIVE.has(r.status)) ?? null, [fullRuns])
  const lastRun = fullRuns[0] ?? null

  const selectedId = watchId ?? activeRun?.id ?? lastRun?.id ?? null
  const run: Run | null = fullRuns.find(r => r.id === selectedId) ?? null
  const live = !!run && LIVE.has(run.status)
  const { events, error: logErr } = useRunEvents(selectedId, live)

  // Elapsed ticker
  const [, forceTick] = useState(0)
  useEffect(() => { if (!live) return; const t = setInterval(() => forceTick(n => n + 1), 1000); return () => clearInterval(t) }, [live])

  const stage = (() => {
    for (let i = events.length - 1; i >= 0; i--) if (events[i].message.startsWith('▶')) return events[i].message.replace(/^▶\s*/, '')
    return null
  })()

  const toggle = (k: string) => setSel(s => s.includes(k) ? s.filter(x => x !== k) : [...s, k])

  const start = async () => {
    setStarting(true); setMsg(null)
    try {
      const r = await api.startFullRun(sel.length ? sel : null, apply)
      setWatchId(r.run_id)
      setMsg(`Run #${r.run_id} queued.`)
    } catch (e) { setMsg(String(e)) } finally { setStarting(false) }
  }

  const stopping = !!run && stopReq === run.id
  const stop = async () => {
    if (!run) return
    setStopReq(run.id)
    try { await api.stopRun(run.id) } catch (e) { setMsg(String(e)); setStopReq(null) }
  }

  return (
    <Page title="Run the system" sub="Launch the full pipeline for one platform, a few, or all of them — then watch it live.">
      <WorkerHealth />
      {/* ── Controls ───────────────────────────────────────────── */}
      <div className="card space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="text-sm font-medium text-zinc-200">Platforms</div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-zinc-500 mr-1">{sel.length ? `${sel.length} selected` : 'none selected → all platforms'}</span>
            <button className="btn btn-sm" onClick={() => setSel((platforms ?? []).map(p => p.key))}><CheckSquare className="size-3.5" /> All platforms</button>
            <button className="btn btn-sm" onClick={() => setSel([])}>Clear</button>
          </div>
        </div>
        {platErr && <div className="text-xs text-rose-300">Could not load platforms: {platErr}</div>}
        {!platforms && !platErr && <div className="text-xs text-zinc-500">Loading platforms…</div>}
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-1.5">
          {platforms?.map(p => {
            const on = sel.includes(p.key)
            return (
              <button key={p.key} onClick={() => toggle(p.key)}
                className={`flex items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-sm transition ${on ? 'border-emerald-600 bg-emerald-600/15 text-white' : 'border-zinc-800 bg-zinc-950/50 text-zinc-300 hover:bg-zinc-900'}`}>
                <span className={`size-3.5 shrink-0 rounded border ${on ? 'bg-emerald-500 border-emerald-500' : 'border-zinc-600'}`} />
                <span className="truncate">{p.name}</span>
                {p.needs_login && <Lock className="size-3.5 ml-auto shrink-0 text-amber-400" />}
              </button>
            )
          })}
        </div>
        <div className="text-[11px] text-zinc-500 flex items-center gap-1.5"><Lock className="size-3 text-amber-400" /> needs a browser login — it will take over the shared Chrome profile.</div>

        <div className="border-t border-zinc-800 pt-3 flex flex-wrap items-center justify-between gap-4">
          <label className="flex items-start gap-2.5 cursor-pointer select-none">
            <button type="button" onClick={() => setApply(a => !a)} aria-pressed={apply}
              className={`mt-0.5 h-5 w-9 shrink-0 rounded-full transition ${apply ? 'bg-emerald-600' : 'bg-zinc-700'}`}>
              <span className={`block size-4 rounded-full bg-white transition-transform ${apply ? 'translate-x-4' : 'translate-x-0.5'}`} />
            </button>
            <span>
              <span className="text-sm text-zinc-200">Apply automatically</span>
              <span className="block text-[11px] text-zinc-500 max-w-md">
                {apply
                  ? 'ON — submit approved applications. New applications still require review when Review mode is enabled.'
                  : 'OFF — the run collects, scores and prepares, then stops at the Review queue for you to approve.'}
              </span>
            </span>
          </label>
          <div className="flex items-center gap-2">
            {live && <button className="btn btn-danger" disabled={stopping} onClick={stop}><Square className="size-4" /> {stopping ? 'Stopping…' : 'Stop run'}</button>}
            <button className="btn btn-primary px-5 py-2.5 text-base" disabled={starting || !!activeRun || !platforms} onClick={start}>
              <Play className="size-4" /> {activeRun ? 'Run in progress' : starting ? 'Starting…' : sel.length ? `Start (${sel.length})` : 'Start all platforms'}
            </button>
          </div>
        </div>
        {msg && <div className="text-xs text-emerald-300">{msg}</div>}
      </div>

      {/* ── Live / last run ────────────────────────────────────── */}
      {!run && <Empty text="No full run yet. Pick your platforms and press Start." />}
      {run && (
        <div className="card space-y-3">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2">
              <span className="font-medium text-zinc-100">Run #{run.id} · {run.name}</span>
              <Badge v={run.status} />
              {!live && <span className="text-xs text-zinc-500">last full run</span>}
            </div>
            <div className="text-xs text-zinc-500 flex items-center gap-3">
              <span>{live ? 'elapsed' : 'took'} <span className="font-mono text-zinc-300">{dur(run.started_at, run.ended_at)}</span></span>
              <Link className="btn btn-sm" to="/runs"><ListChecks className="size-3.5" /> Runs & logs</Link>
            </div>
          </div>

          {live && <div className="text-sm"><span className="text-zinc-500 text-xs">stage </span><span className="text-emerald-300">{stage ?? 'starting up…'}</span></div>}
          {run.status === 'paused_for_human' && (
            <div className="rounded-lg border border-fuchsia-700/50 bg-fuchsia-500/10 px-3 py-2 text-sm text-fuchsia-200">
              This run needs a human step (login, CAPTCHA or an unknown question). Open <Link className="underline" to="/runs">Runs &amp; logs</Link> to see what it is asking for.
            </div>
          )}
          {run.status === 'failed' && run.error && (
            <div className="rounded-lg border border-rose-800/60 bg-rose-500/10 px-3 py-2 text-sm text-rose-200 whitespace-pre-wrap break-words">{run.error}</div>
          )}
          {run.status === 'stopped' && <div className="text-sm text-zinc-400">Stopped{run.error ? ` — ${run.error}` : '.'}</div>}

          <StatsGrid stats={run.stats} />
          {logErr && <div className="text-xs text-amber-300">Log polling is failing ({logErr}) — retrying.</div>}
          <EventLog events={events} live={live} />
        </div>
      )}
    </Page>
  )
}
