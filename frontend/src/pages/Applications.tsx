import Pagination from "../components/Pagination"
import { usePage } from "../usePage"
import ApplicationActions from "../components/ApplicationActions"
import { artifactUrl } from '../api'
import { useState } from 'react'
import { api, usePoll, ago } from '../api'
import { Page, Badge, Empty } from '../components/ui'

const STATUSES = ['', 'pending_review', 'approved', 'submitting', 'assisting', 'submitted', 'needs_human', 'failed', 'replied', 'interview', 'rejected', 'offer', 'rejected_by_user']
const Chip = ({ label, n, active, onClick }: { label: string; n: number; active: boolean; onClick: () => void }) => (
  <button onClick={onClick} className={`badge cursor-pointer ${active ? 'bg-emerald-500/20 text-emerald-200 ring-1 ring-emerald-500/40' : 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'}`}>{label} <span className="opacity-60">{n}</span></button>)

export default function Applications() {
  const [status, setStatus] = useState('')
  const [country, setCountry] = useState('')
  const [platform, setPlatform] = useState('')
  const [q, setQ] = useState('')
  const { page, setPage } = usePage(JSON.stringify([status, country, platform, q]))
  const { data, err, refresh } = usePoll(() => api.applications({ status: status || undefined, country: country || undefined, platform: platform || undefined, q: q || undefined, size: 50, page }), 10000, [status, country, platform, q, page])
  const { data: facets } = usePoll(api.appFacets, 15000)
  const sorted = (o?: Record<string, number>) => Object.entries(o ?? {}).sort((a, b) => b[1] - a[1])
  return (
    <Page title="Applications" sub={`${data?.total ?? 0} records · filter by country, platform, status`}
      actions={<div className="flex gap-2"><input className="input" placeholder="company / title" value={q} onChange={e => setQ(e.target.value)} />
        <select className="input" value={status} onChange={e => setStatus(e.target.value)}>{STATUSES.map(s => <option key={s} value={s}>{s || 'All statuses'}</option>)}</select></div>}>
      {facets && <div className="card space-y-2">
        <div className="flex flex-wrap gap-1.5 items-center"><span className="text-xs text-zinc-500 w-24">Country</span>
          <Chip label="All" n={Object.values(facets.country).reduce((a, b) => a + b, 0)} active={!country} onClick={() => setCountry('')} />
          {sorted(facets.country).map(([k, n]) => <Chip key={k} label={k} n={n} active={country === k} onClick={() => setCountry(country === k ? '' : k)} />)}</div>
        <div className="flex flex-wrap gap-1.5 items-center"><span className="text-xs text-zinc-500 w-24">Submitted to</span>
          {sorted(facets.country_submitted).length ? sorted(facets.country_submitted).map(([k, n]) => <span key={k} className="badge bg-zinc-800 text-zinc-300">{k} <span className="opacity-60">{n}</span></span>) : <span className="text-xs text-zinc-600">nothing submitted yet</span>}</div>
        <div className="flex flex-wrap gap-1.5 items-center"><span className="text-xs text-zinc-500 w-24">Platform</span>
          <Chip label="All" n={Object.values(facets.platform).reduce((a, b) => a + b, 0)} active={!platform} onClick={() => setPlatform('')} />
          {sorted(facets.platform).map(([k, n]) => <Chip key={k} label={k} n={n} active={platform === k} onClick={() => setPlatform(platform === k ? '' : k)} />)}</div>
        <div className="flex flex-wrap gap-1.5 items-center"><span className="text-xs text-zinc-500 w-24">Status</span>
          {sorted(facets.status).map(([k, n]) => <Chip key={k} label={k} n={n} active={status === k} onClick={() => setStatus(status === k ? '' : k)} />)}</div>
      </div>}
      <div className="card p-0 overflow-x-auto">
        <table className="data">
          <thead><tr><th>When</th><th>Company</th><th>Role</th><th>Country</th><th>Score</th><th>Platform</th><th>Method</th><th>Status</th><th>Artifacts</th><th>Update</th></tr></thead>
          <tbody>{data?.items.map(a => (
            <tr key={a.id}>
              <td className="text-xs text-zinc-500 whitespace-nowrap">{ago(a.submitted_at ?? a.created_at)}</td>
              <td className="font-medium text-zinc-100">{a.company}</td>
              <td className="max-w-sm truncate"><a className="hover:underline" href={a.url} target="_blank" rel="noreferrer">{a.title}</a></td>
              <td className="text-zinc-400 whitespace-nowrap" title={a.location ?? ''}>{a.country ?? '—'}</td>
              <td className="text-zinc-300">{a.fit_score ?? '—'}</td>
              <td className="text-zinc-400">{a.platform}</td><td className="text-zinc-400">{a.method.replace('_', ' ')}</td>
              <td><Badge v={a.status} />{a.error && <div className="text-[11px] text-rose-300 max-w-48 truncate" title={a.error}>{a.error}</div>}</td>
              <td className="text-xs space-x-2">{a.resume_path && <a className="underline" href={artifactUrl(a.resume_path)} target="_blank" rel="noreferrer">resume</a>}{a.screenshot_after && <a className="underline" href={artifactUrl(a.screenshot_after)} target="_blank" rel="noreferrer">confirm</a>}</td>
              <td><ApplicationActions key={`${a.id}-${a.status}`} app={a} onSaved={refresh} /></td>
            </tr>))}</tbody>
        </table>
        {!data?.items.length && <Empty text="No applications match." />}
      </div>
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      <Pagination page={page} size={50} total={data?.total ?? 0} onPage={setPage} />
    </Page>
  )
}
