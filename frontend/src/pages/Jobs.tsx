import { useState } from 'react'
import { ExternalLink, X } from 'lucide-react'
import { api, usePoll, ago, type Job } from '../api'
import { Page, Badge, Score, Empty } from '../components/ui'

export default function Jobs() {
  const [f, setF] = useState<Record<string, unknown>>({ eligible: true, sort: 'created_at', order: 'desc', page: 1, size: 50 })
  const [sel, setSel] = useState<Job | null>(null)
  const { data, refresh } = usePoll(() => api.jobs(f), 15000, [JSON.stringify(f)])
  const { data: facets } = usePoll(api.jobFacets, 30000)
  const set = (k: string, v: unknown) => setF(p => ({ ...p, [k]: v, page: 1 }))
  const open = async (j: Job) => setSel(await api.job(j.id))
  const pages = data ? Math.max(1, Math.ceil(data.total / (f.size as number))) : 1
  return (
    <Page title="Jobs" sub={`${data?.total ?? 0} matching · every posting discovered by any collector or skill`}>
      <div className="flex flex-wrap gap-2 items-center">
        <input className="input w-56" placeholder="Search title / company / location" onChange={e => set('q', e.target.value)} />
        <select className="input" onChange={e => set('source', e.target.value || undefined)}><option value="">All sources</option>{facets && Object.entries(facets.sources).map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}</select>
        <select className="input" onChange={e => set('country', e.target.value || undefined)}><option value="">All countries (eligible)</option>{facets && Object.entries(facets.countries ?? {}).map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}</select>
        <select className="input" onChange={e => set('status', e.target.value || undefined)}><option value="">All statuses</option>{facets && Object.entries(facets.statuses).map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}</select>
        <select className="input" value={String(f.eligible ?? '')} onChange={e => set('eligible', e.target.value === '' ? undefined : e.target.value === 'true')}><option value="true">Eligible only</option><option value="false">Filtered out</option><option value="">Everything</option></select>
        <label className="text-sm flex items-center gap-1.5 text-zinc-400"><input type="checkbox" onChange={e => set('sponsor', e.target.checked ? true : undefined)} /> sponsor signal</label>
        <select className="input" onChange={e => set('sort', e.target.value)}><option value="created_at">Newest found</option><option value="fit_score">Fit score</option><option value="posted_at">Posted</option><option value="company">Company</option></select>
      </div>
      <div className="card p-0 overflow-x-auto">
        <table className="data">
          <thead><tr><th>Company</th><th>Title</th><th>Location</th><th>Source</th><th>ATS</th><th>Fit</th><th>Status</th><th>Found</th></tr></thead>
          <tbody>
            {data?.items.map(j => (
              <tr key={j.id} className="cursor-pointer" onClick={() => open(j)}>
                <td className="font-medium text-zinc-100 max-w-44 truncate">{j.company}</td>
                <td className="max-w-md"><div className="truncate">{j.title}</div>{j.sponsor_flag && <span className="badge bg-violet-500/15 text-violet-300 mt-0.5">sponsorship</span>}</td>
                <td className="max-w-40 truncate text-zinc-400">{j.location ?? j.remote_scope ?? '—'}</td>
                <td className="text-zinc-400">{j.source}</td><td className="text-zinc-400">{j.ats ?? '—'}</td>
                <td><Score v={j.fit_score} /></td><td><Badge v={j.status} /></td>
                <td className="text-zinc-500 text-xs whitespace-nowrap">{ago(j.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!data?.items.length && <Empty text="No jobs match. Run collectors on the Platforms page." />}
      </div>
      <div className="flex items-center gap-2 text-sm text-zinc-400">
        <button className="btn btn-sm" disabled={(f.page as number) <= 1} onClick={() => setF(p => ({ ...p, page: (p.page as number) - 1 }))}>Prev</button>
        <span>page {String(f.page)} / {pages}</span>
        <button className="btn btn-sm" disabled={(f.page as number) >= pages} onClick={() => setF(p => ({ ...p, page: (p.page as number) + 1 }))}>Next</button>
      </div>
      {sel && (
        <div className="fixed inset-0 z-20 flex justify-end bg-black/50" onClick={() => setSel(null)}>
          <div className="w-full max-w-2xl h-full overflow-y-auto bg-zinc-950 border-l border-zinc-800 p-6 space-y-4" onClick={e => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3">
              <div><div className="text-xs text-zinc-500">{sel.source} · {sel.ats ?? 'no ATS detected'}</div><h2 className="text-lg font-semibold">{sel.title}</h2><div className="text-zinc-300">{sel.company} · {sel.location ?? sel.remote_scope}</div></div>
              <button className="btn btn-sm" onClick={() => setSel(null)}><X className="size-4" /></button>
            </div>
            <div className="flex flex-wrap gap-2 items-center text-sm">
              <Badge v={sel.status} /><Score v={sel.fit_score} />{sel.salary && <span className="badge bg-zinc-800 text-zinc-300">{sel.salary}</span>}{sel.employment_type && <span className="badge bg-zinc-800 text-zinc-300">{sel.employment_type}</span>}
              <span className="text-xs text-zinc-500">{sel.eligibility_reason}</span>
            </div>
            <div className="flex gap-2">
              <a className="btn" href={sel.url} target="_blank" rel="noreferrer"><ExternalLink className="size-4" /> Open posting</a>
              {sel.apply_url && sel.apply_url !== sel.url && <a className="btn" href={sel.apply_url} target="_blank" rel="noreferrer">Apply link</a>}
              <button className="btn btn-primary" onClick={async () => { await api.setJobStatus(sel.id, 'queued'); refresh(); setSel({ ...sel, status: 'queued' }) }}>Queue for apply</button>
              <button className="btn" onClick={async () => { await api.setJobStatus(sel.id, 'skipped'); refresh(); setSel({ ...sel, status: 'skipped' }) }}>Skip</button>
            </div>
            {sel.fit_reasons && <div className="card text-sm whitespace-pre-wrap">{sel.fit_reasons}</div>}
            <div className="text-sm text-zinc-300 whitespace-pre-wrap leading-relaxed">{sel.description ?? 'No description captured.'}</div>
          </div>
        </div>
      )}
    </Page>
  )
}
