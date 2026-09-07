import Pagination from "../components/Pagination"
import { usePage } from "../usePage"
import { useState } from 'react'
import { api, usePoll, ago } from '../api'
import { Page, Badge, Empty } from '../components/ui'

export default function OutreachPage() {
  const [actionError, setActionError] = useState('')
  const act = async (id: number, status: string) => { try { await api.setOutreachStatus(id, status); refresh() } catch (e) { setActionError(String(e)) } }
  const [status, setStatus] = useState('')
  const { page, setPage } = usePage(JSON.stringify([status]))
  const { data, err, refresh } = usePoll(() => api.outreach({ status: status || undefined, size: 50, page }), 10000, [status, page])
  const [notice, setNotice] = useState('')
  const service = async (name: string, label: string) => {
    try { const r = await api.runService(name); setNotice(`${label} queued as run #${r.run_id}. Watch it in Runs & logs.`) } catch (e) { setActionError(String(e)) }
  }
  return (
    <Page title="Outreach" sub={`${data?.total ?? 0} messages · recruiter / hiring-manager emails, LinkedIn connects and DMs, X replies`}
      actions={<div className="flex flex-wrap items-center gap-2">
        <button className="btn btn-sm" title="Send only messages you approved, respecting daily caps and scheduled dates" onClick={() => service('gmail_send', 'Send approved emails')}>Send approved emails</button>
        <button className="btn btn-sm" title="Check sent threads for replies and bounces" onClick={() => service('gmail_poll', 'Reply check')}>Check replies</button>
        <button className="btn btn-sm" title="Draft day-4 / day-9 follow-ups for review; nothing is sent" onClick={() => service('gmail_followups', 'Follow-up drafting')}>Draft follow-ups</button>
        <select className="input" value={status} onChange={e => setStatus(e.target.value)}>{['', 'pending_review', 'approved', 'sending', 'sent', 'submission_unverified', 'replied', 'bounced', 'failed', 'stopped'].map(s => <option key={s} value={s}>{s || 'All statuses'}</option>)}</select></div>}>
      {notice && <p role="status" className="text-sm text-emerald-300">{notice}</p>}
      {!data?.items.length && <Empty text="No outreach yet. Draft messages first, review them, then run the approved-message sender." />}
      <div className="space-y-2">
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
      </div>
      {actionError && <p role="alert" className="text-sm text-rose-300">{actionError}</p>}
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      <Pagination page={page} size={50} total={data?.total ?? 0} onPage={setPage} />
    </Page>
  )
}
