import Pagination from "../components/Pagination"
import { usePage } from "../usePage"
import ApplicationActions from "../components/ApplicationActions"
import { artifactUrl } from '../api'
import { useState } from 'react'
import { api, usePoll, ago } from '../api'
import { Page, Badge, Empty } from '../components/ui'
import { ExternalLink } from 'lucide-react'

export default function Review() {
  const [country, setCountry] = useState('')
  const [platform, setPlatform] = useState('')
  const { page, setPage } = usePage(JSON.stringify([country, platform]))
  const { data, err, refresh } = usePoll(() => api.review({ country: country || undefined, platform: platform || undefined, size: 50, page }), 8000, [country, platform, page])
  const { data: facets } = usePoll(api.appFacets, 15000)
  return (
    <Page title="Review queue" sub={`${data?.total ?? 0} waiting · Approve → the apply worker submits it. needs-human = CAPTCHA or unknown question.`}
      actions={<div className="flex gap-2">
        <select className="input" value={country} onChange={e => setCountry(e.target.value)}><option value="">All countries</option>{Object.entries(facets?.country ?? {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}</select>
        <select className="input" value={platform} onChange={e => setPlatform(e.target.value)}><option value="">All platforms</option>{Object.entries(facets?.platform ?? {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}</select></div>}>
      {!data?.items.length && <Empty text="Queue is empty. Once the tailor step runs, applications appear here for one-click approval." />}
      <div className="space-y-3">
        {data?.items.map(a => (
          <div key={a.id} className="card space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div><div className="text-xs text-zinc-500">{a.platform} · {a.method.replace('_', ' ')} · {ago(a.created_at)}</div>
                <div className="font-medium text-zinc-100">{a.title} <span className="text-zinc-400">@ {a.company}</span></div>
                <div className="text-xs text-zinc-500">{a.country ?? '—'}{a.location ? ` · ${a.location}` : ''}{a.fit_score != null ? ` · fit ${a.fit_score}` : ''}</div></div>
              <Badge v={a.status} />
            </div>
            {a.cover_note && <div className="text-sm text-zinc-300 whitespace-pre-wrap border-l-2 border-zinc-700 pl-3">{a.cover_note}</div>}
            {a.answers && (a.answers as {factcheck?: {ok?: boolean; violations?: {claim: string}[]}}).factcheck && (
              <div className={`text-xs ${(a.answers as {factcheck: {ok?: boolean}}).factcheck.ok ? 'text-emerald-300' : 'text-amber-300'}`}>
                fact-check: {(a.answers as {factcheck: {ok?: boolean}}).factcheck.ok ? 'all claims supported by profile' : `${((a.answers as {factcheck: {violations?: unknown[]}}).factcheck.violations ?? []).length} unsupported claim(s) remain, read before approving`}</div>)}
            {a.answers && <details className="text-xs text-zinc-400"><summary className="cursor-pointer">Screening answers</summary><pre className="mt-1 whitespace-pre-wrap">{JSON.stringify(a.answers, null, 2)}</pre></details>}
            {a.error && <div className="text-xs text-rose-300">{a.error}</div>}
            <div className="flex flex-wrap gap-2">
              <ApplicationActions key={`${a.id}-${a.status}`} app={a} onSaved={refresh} />
              {a.url && <a className="btn btn-sm" href={a.url} target="_blank" rel="noreferrer"><ExternalLink className="size-3.5" /> Posting</a>}
              {a.resume_path && <a className="btn btn-sm" href={artifactUrl(a.resume_path)} target="_blank" rel="noreferrer">Resume PDF</a>}
              {a.screenshot_before && <a className="btn btn-sm" href={artifactUrl(a.screenshot_before)} target="_blank" rel="noreferrer">Screenshot</a>}
            </div>
          </div>
        ))}
      </div>
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      <Pagination page={page} size={50} total={data?.total ?? 0} onPage={setPage} />
    </Page>
  )
}
