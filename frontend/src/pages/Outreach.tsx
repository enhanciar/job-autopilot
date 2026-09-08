import Pagination from "../components/Pagination"
import { usePage } from "../usePage"
import { useState } from 'react'
import { api, usePoll, ago } from '../api'
import { Page, Badge, Empty } from '../components/ui'

const CHANNEL_LABELS: Record<string, string> = {
  email: 'Email', linkedin_connect: 'LinkedIn invite', linkedin_dm: 'LinkedIn message',
  x_dm: 'X direct message', x_reply: 'X reply', reddit: 'Reddit',
}
const Chip = ({ label, n, active, onClick, hint }: { label: string; n: number; active: boolean; onClick: () => void; hint?: string }) => (
  <button onClick={onClick} title={hint}
    className={`badge cursor-pointer ${active ? 'bg-emerald-500/20 text-emerald-200 ring-1 ring-emerald-500/40' : 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'}`}>
    {label} <span className="opacity-60">{n}</span>{hint ? <span className="ml-1 text-amber-300">•</span> : null}</button>)

export default function OutreachPage() {
  const [actionError, setActionError] = useState('')
  const act = async (id: number, status: string) => { try { await api.setOutreachStatus(id, status); refresh() } catch (e) { setActionError(String(e)) } }
  const [status, setStatus] = useState('')
  const [channel, setChannel] = useState('')
  const [source, setSource] = useState('')
  const { page, setPage } = usePage(JSON.stringify([status, channel, source]))
  const { data, err, refresh } = usePoll(() => api.outreach({ status: status || undefined, channel: channel || undefined, source: source || undefined, size: 50, page }), 10000, [status, channel, source, page])
  const { data: facets } = usePoll(api.outreachFacets, 15000)
  const [view, setView] = useState<'messages' | 'people'>('messages')
  const [company, setCompany] = useState('')
  const [who, setWho] = useState('')
  const { data: people } = usePoll(() => api.outreachPeople({ company: company || undefined, q: who || undefined, size: 100 }), 12000, [company, who, view])
  const [notice, setNotice] = useState('')
  const service = async (name: string, label: string) => {
    try { const r = await api.runService(name); setNotice(`${label} queued as run #${r.run_id}. Watch it in Runs & logs.`) } catch (e) { setActionError(String(e)) }
  }
  return (
    <Page title="Outreach" sub={`${data?.total ?? 0} messages · pick a channel or the platform a job came from to see that outreach on its own`}
      actions={<div className="flex flex-wrap items-center gap-2">
        <button className="btn btn-sm" title="Send only messages you approved, respecting daily caps and scheduled dates" onClick={() => service('gmail_send', 'Send approved emails')}>Send approved emails</button>
        <button className="btn btn-sm" title="Check sent threads for replies and bounces" onClick={() => service('gmail_poll', 'Reply check')}>Check replies</button>
        <button className="btn btn-sm" title="Draft day-4 / day-9 follow-ups for review; nothing is sent" onClick={() => service('gmail_followups', 'Follow-up drafting')}>Draft follow-ups</button>
        <select className="input" value={status} onChange={e => setStatus(e.target.value)}>{['', 'pending_review', 'approved', 'sending', 'sent', 'submission_unverified', 'replied', 'bounced', 'failed', 'stopped'].map(s => <option key={s} value={s}>{s || 'All statuses'}</option>)}</select></div>}>
      {notice && <p role="status" className="text-sm text-emerald-300">{notice}</p>}
      <div className="flex gap-2">
        {(['messages', 'people'] as const).map(v => (
          <button key={v} onClick={() => setView(v)}
            className={`btn btn-sm ${view === v ? 'btn-primary' : ''}`}>{v === 'messages' ? 'Messages' : `People (${people?.total ?? 0})`}</button>))}
      </div>

      {view === 'people' && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 items-center">
            <input className="input" placeholder="name, company or title" value={who} onChange={e => setWho(e.target.value)} />
            <select className="input" value={company} onChange={e => setCompany(e.target.value)}>
              <option value="">All companies ({Object.keys(people?.by_company ?? {}).length})</option>
              {Object.entries(people?.by_company ?? {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
            </select>
          </div>
          <div className="card p-0 overflow-x-auto">
            <table className="data">
              <thead><tr><th>Person</th><th>Company</th><th>Role they hire for</th><th>LinkedIn</th><th>Email</th><th>Last sent</th></tr></thead>
              <tbody>{people?.items.map(p => (
                <tr key={p.id}>
                  <td><div className="font-medium text-zinc-100">{p.linkedin_url ? <a className="hover:underline" href={p.linkedin_url} target="_blank" rel="noreferrer">{p.name}</a> : p.name}</div>
                    <div className="text-xs text-zinc-500 max-w-xs truncate" title={p.title ?? ''}>{p.title}</div></td>
                  <td className="text-zinc-300">{p.company}</td>
                  <td className="text-xs text-zinc-400 max-w-xs truncate" title={p.roles.join(', ')}>{p.roles.join(', ') || '—'}</td>
                  <td>{p.linkedin_state ? <Badge v={p.linkedin_state} /> : <span className="text-zinc-600 text-xs">not contacted</span>}</td>
                  <td>{p.email_state ? <Badge v={p.email_state} /> : <span className="text-zinc-600 text-xs">{p.email ? 'not contacted' : 'no address'}</span>}</td>
                  <td className="text-xs text-zinc-500 whitespace-nowrap">{p.last_sent ? ago(p.last_sent) : '—'}</td>
                </tr>))}</tbody>
            </table>
            {!people?.items.length && <Empty text="No people yet. Run the LinkedIn people finder to fill this in." />}
          </div>
        </div>)}

      {view === 'messages' && facets && <div className="card space-y-2">
        <div className="flex flex-wrap gap-1.5 items-center">
          <span className="text-xs text-zinc-500 w-20">Channel</span>
          <Chip label="All" n={Object.values(facets.channel).reduce((a, b) => a + b, 0)} active={!channel} onClick={() => setChannel('')} />
          {Object.entries(facets.channel).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
            <Chip key={k} label={CHANNEL_LABELS[k] ?? k.replace('_', ' ')} n={n} active={channel === k} onClick={() => setChannel(channel === k ? '' : k)}
              hint={facets.pending_by_channel[k] ? `${facets.pending_by_channel[k]} waiting for you` : undefined} />))}
        </div>
        <div className="flex flex-wrap gap-1.5 items-center">
          <span className="text-xs text-zinc-500 w-20">Found on</span>
          <Chip label="All" n={Object.values(facets.source).reduce((a, b) => a + b, 0)} active={!source} onClick={() => setSource('')} />
          {Object.entries(facets.source).sort((a, b) => b[1] - a[1]).slice(0, 12).map(([k, n]) => (
            <Chip key={k} label={k} n={n} active={source === k} onClick={() => setSource(source === k ? '' : k)} />))}
        </div>
      </div>}
      {view === 'messages' && !data?.items.length && <Empty text="No outreach yet. Draft messages first, review them, then run the approved-message sender." />}
      {view === 'messages' && <div className="space-y-2">
        {data?.items.map(o => (
          <div key={o.id} className="card">
            <div className="flex items-start justify-between gap-3">
              <div className="text-sm"><span className="text-zinc-500 text-xs">{o.channel.replace('_', ' ')} · step {o.step} · {ago(o.sent_at ?? o.created_at)}</span>
                <div className="font-medium text-zinc-100">{o.contact_name ?? 'Unknown contact'} <span className="text-zinc-400 font-normal">{o.contact_title ? `· ${o.contact_title}` : ''} {o.company ? `@ ${o.company}` : ''}</span></div>
                {o.subject && <div className="text-zinc-300">{o.subject}</div>}</div>
              <div className="flex items-center gap-2"><Badge v={o.status} />
                {o.status === 'pending_review' && <><button className="btn btn-primary btn-sm" onClick={() => act(o.id, 'approved')}>Approve</button>
                  <button className="btn btn-sm" onClick={() => act(o.id, 'stopped')}>Stop</button></>}</div>
            </div>
            {o.error && <p className="text-xs text-rose-300 mt-2">{o.error}</p>}
            {o.body && <details className="mt-2 text-sm text-zinc-300"><summary className="cursor-pointer text-xs text-zinc-500">message</summary><div className="whitespace-pre-wrap mt-1">{o.body}</div></details>}
          </div>
        ))}
      </div>}
      {actionError && <p role="alert" className="text-sm text-rose-300">{actionError}</p>}
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      {view === 'messages' && <Pagination page={page} size={50} total={data?.total ?? 0} onPage={setPage} />}
    </Page>
  )
}
