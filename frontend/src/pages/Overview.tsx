import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, Legend } from 'recharts'
import { api, usePoll, ago } from '../api'
import { Page, Stat, Badge, Empty } from '../components/ui'
import { Link } from 'react-router-dom'

export default function Overview() {
  const { data: o, err } = usePoll(api.overview, 8000)
  const { data: events } = usePoll(() => api.events({ limit: 12, latest: true }).then(e => e.slice(-12).reverse()), 8000)
  if (err) return <Empty text={`Backend unreachable: ${err}`} />
  if (!o) return <Empty text="Loading…" />
  const pct = Math.min(100, Math.round((o.today.touches / o.target_per_day) * 100))
  return (
    <Page title="Overview" sub="Daily progress against the 100-touches target, pipeline health, caps and sessions.">
      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-3">
        <Stat label="Touches today" value={<>{o.today.touches}<span className="text-zinc-500 text-base"> / {o.target_per_day}</span></>} hint={`${o.today.applied} applied · ${o.today.outreach} outreach`} accent={pct >= 100 ? 'text-emerald-300' : ''} />
        <Stat label="Jobs found today" value={o.today.jobs_found} hint={`${o.totals.jobs} total · ${o.totals.eligible} eligible`} />
        <Stat label="Pending review" value={o.totals.pending_review} hint={o.review_mode ? 'review mode on' : 'auto-submit on'} accent={o.totals.pending_review ? 'text-amber-300' : ''} />
        <Stat label="Applied (all time)" value={o.totals.applied} hint={`${o.totals.outreach_sent} outreach sent`} />
        <Stat label="Replies / interviews" value={<>{o.totals.replies}<span className="text-zinc-500 text-base"> / {o.totals.interviews}</span></>} />
        <Stat label="Runs" value={<>{o.active_runs}<span className="text-zinc-500 text-base"> active</span></>} hint={o.paused_runs ? `${o.paused_runs} waiting for you` : 'nothing paused'} accent={o.paused_runs ? 'text-rose-300' : ''} />
      </div>
      <div className="card">
        <div className="flex items-center justify-between mb-2"><div className="text-sm font-medium">Last 14 days</div><div className="text-xs text-zinc-500">target line = {o.target_per_day}/day</div></div>
        <div className="h-56">
          <ResponsiveContainer>
            <BarChart data={o.days} margin={{ left: -20, right: 8 }}>
              <XAxis dataKey="day" tick={{ fontSize: 11, fill: '#71717a' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: '#71717a' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <ReferenceLine y={o.target_per_day} stroke="#f59e0b" strokeDasharray="4 4" />
              <Bar dataKey="found" name="found" fill="#3f3f46" radius={[3, 3, 0, 0]} />
              <Bar dataKey="applied" name="applied" fill="#10b981" radius={[3, 3, 0, 0]} />
              <Bar dataKey="outreach" name="outreach" fill="#6366f1" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="grid md:grid-cols-3 gap-3">
        <div className="card">
          <div className="text-sm font-medium mb-2">Jobs by source <span className="text-zinc-500 font-normal">(eligible / total)</span></div>
          <div className="space-y-1.5 text-sm">
            {Object.entries(o.by_source).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
              <div key={s} className="flex items-center gap-2"><span className="w-32 truncate text-zinc-300">{s}</span>
                <div className="flex-1 h-1.5 rounded bg-zinc-800 overflow-hidden"><div className="h-full bg-emerald-500/70" style={{ width: `${Math.round(((o.eligible_by_source[s] ?? 0) / n) * 100)}%` }} /></div>
                <span className="w-20 text-right text-xs text-zinc-400">{o.eligible_by_source[s] ?? 0} / {n}</span></div>
            ))}
            {!Object.keys(o.by_source).length && <div className="text-zinc-500 text-xs">No jobs yet. Run collectors from <Link className="underline" to="/platforms">Platforms</Link>.</div>}
          </div>
        </div>
        <div className="card">
          <div className="text-sm font-medium mb-2">Daily caps</div>
          <div className="space-y-2 text-xs">
            {Object.entries(o.caps).map(([p, acts]) => (
              <div key={p}><div className="text-zinc-400 mb-0.5">{p}</div>
                <div className="flex flex-wrap gap-1.5">{Object.entries(acts).map(([a, v]) => (
                  <span key={a} className={`badge ${v.used >= v.cap ? 'bg-rose-500/15 text-rose-300' : 'bg-zinc-800 text-zinc-300'}`}>{a} {v.used}/{v.cap}</span>))}</div></div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="text-sm font-medium mb-2">Sessions</div>
          <div className="space-y-1.5 text-sm">
            {o.sessions.length ? o.sessions.map(s => (
              <div key={s.platform} className="flex items-center justify-between"><span>{s.platform}</span>
                <span className={`badge ${s.logged_in ? 'bg-emerald-500/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'}`}>{s.logged_in ? 'logged in' : 'logged out'}</span></div>
            )) : <div className="text-zinc-500 text-xs">No browser sessions yet. Use “Log in” on Platforms.</div>}
          </div>
        </div>
      </div>
      <div className="card">
        <div className="text-sm font-medium mb-2">Recent activity</div>
        <div className="space-y-1 text-sm">
          {events?.map(e => (
            <div key={e.id} className="flex gap-2 items-start"><span className="text-xs text-zinc-500 w-16 shrink-0">{ago(e.ts)}</span><Badge v={e.level} /><span className="text-zinc-300">{e.message}</span></div>
          ))}
          {!events?.length && <div className="text-zinc-500 text-xs">Nothing yet.</div>}
        </div>
      </div>
    </Page>
  )
}
