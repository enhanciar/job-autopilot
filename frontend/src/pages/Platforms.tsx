import WorkerHealth from "../components/WorkerHealth"
import { useState } from 'react'
import { Play, LogIn, RefreshCw } from 'lucide-react'
import { api, usePoll, ago } from '../api'
import { Page, Badge, Empty } from '../components/ui'

export default function Platforms() {
  const { data, refresh } = usePoll(api.platforms, 10000)
  const { data: llm } = usePoll(api.llmStatus, 30000)
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const go = async (label: string, fn: () => Promise<{ run_id: number }>) => {
    setBusy(label); try { const r = await fn(); setMsg(`${label} queued (run #${r.run_id}). Watch it on Runs & logs.`) } catch (e) { setMsg(String(e)) } finally { setBusy(null); refresh() }
  }
  const groups = { collector: 'Collectors (no login, public data)', skill: 'Browser skills (logged-in, human-paced)', service: 'Services' } as Record<string, string>
  return (
    <Page title="Platforms" sub="Every source from the research list. Ready = implemented; planned = skill still to be built."
      actions={<button className="btn btn-primary" disabled={!!busy} onClick={() => go('all collectors', api.runAllCollectors)}><RefreshCw className="size-4" /> Run all collectors</button>}>
      <WorkerHealth />
      {msg && <div className="card text-sm text-emerald-300">{msg}</div>}
      <div className="card flex flex-wrap gap-2 items-center text-sm"><span className="text-zinc-400 mr-1">LLM providers:</span>
        {llm && Object.entries(llm).map(([k, ok]) => <span key={k} className={`badge ${ok ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-800 text-zinc-500'}`}>{k} {ok ? 'ready' : 'not configured'}</span>)}</div>
      <div className="card space-y-2">
        <div className="text-sm font-medium text-zinc-200">Pipeline (no browser)</div>
        <div className="text-xs text-zinc-500">Re-evaluate = re-run eligibility rules on stored jobs. Score = LLM fit score 0–100. Prepare = tailored resume + cover note + fact-check → Review queue. Daily = score then prepare.</div>
        <div className="flex flex-wrap gap-1.5">
          {[['reevaluate','Re-evaluate eligibility'],['score','Score (40)'],['prepare','Prepare top jobs'],['daily','Daily: score + prepare'],['outreach_draft','Draft outreach (email + LinkedIn note)']].map(([k,l]) => (
            <button key={k} className="btn btn-sm" disabled={!!busy} onClick={() => go(l, () => api.runPipeline(k))}><Play className="size-3.5" /> {l}</button>))}
          <button className="btn btn-sm" disabled={!!busy} onClick={() => go('apply worker', () => api.runSkill('ats_apply', {}))}><Play className="size-3.5" /> Apply worker (approved only)</button>
          <button className="btn btn-sm" disabled={!!busy} onClick={() => go('sheet sync', () => api.runService('sheets_sync'))}><Play className="size-3.5" /> Sync Google Sheet</button>
          <button className="btn btn-sm" disabled={!!busy} onClick={() => go('gmail poll', () => api.runService('gmail_poll'))}><Play className="size-3.5" /> Check email replies</button>
        </div>
      </div>
      {!data && <Empty text="Loading…" />}
      {data && Object.entries(groups).map(([type, label]) => (
        <div key={type}>
          <h2 className="text-sm font-medium text-zinc-400 mb-2 mt-2">{label}</h2>
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
            {data.filter(p => p.type === type).map(p => (
              <div key={p.key} className="card space-y-2">
                <div className="flex items-start justify-between gap-2"><div className="font-medium text-zinc-100">{p.name}</div><Badge v={p.status} /></div>
                <div className="text-xs text-zinc-500 flex flex-wrap gap-x-3">
                  {p.login && <span>login: {p.logged_in == null ? 'unknown' : p.logged_in ? <span className="text-emerald-300">yes</span> : <span className="text-rose-300">no</span>}</span>}
                  {p.configured != null && <span>{p.configured ? "credentials present" : "credentials missing"}</span>}
                  {p.last_checked && <span>checked {ago(p.last_checked)}</span>}
                  {p.session_note && <span className="truncate max-w-56">{p.session_note}</span>}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {p.type === 'collector' && <button className="btn btn-sm" disabled={!p.implemented || !!busy} onClick={() => go(p.name, () => api.runCollector(p.key))}><Play className="size-3.5" /> Run</button>}
                  {p.type === 'skill' && (p.modes ?? ['run']).map(m => <button key={m} className="btn btn-sm" disabled={!p.implemented || !!busy} onClick={() => go(`${p.name} ${m}`, () => api.runSkill(p.key, { mode: m }))}><Play className="size-3.5" /> {m.replace('_', ' ')}</button>)}
                  {p.login_supported && <button className="btn btn-sm" disabled={!!busy} onClick={() => go(`${p.name} login`, () => api.openLogin(p.key))}><LogIn className="size-3.5" /> Log in</button>}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </Page>
  )
}
