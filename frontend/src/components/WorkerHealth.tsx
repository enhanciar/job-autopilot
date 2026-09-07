import { api, usePoll } from '../api'

export default function WorkerHealth() {
  const { data, err } = usePoll(api.workerHealth, 5000)
  if (err) return <div className="card text-sm text-rose-300" role="alert">Could not read worker status. {err}</div>
  if (!data) return null
  return <div className="card space-y-2 text-sm">
    <div className="flex flex-wrap gap-4">
      <span>Worker: <strong>{data.worker_active ? (data.worker_heartbeat_fresh ? 'running' : 'running (no recent heartbeat)') : 'not running'}</strong></span>
      {data.worker_heartbeat_age_s !== null && <span className="text-zinc-400">heartbeat {Math.round(data.worker_heartbeat_age_s)}s ago</span>}
      {data.worker_run_id && <span>working on run #{data.worker_run_id}</span>}
      <span>{data.queued} queued</span>
      <span>Automation browser: {data.browser_handoffs.length ? 'held for human attention' : data.browser.busy ? 'in use' : 'idle'}</span>
    </div>
    {!data.worker_active && data.queued > 0 && <p className="text-zinc-400">Queued tasks will wait until the worker starts. Start it with <code>./start.sh</code> in the project folder.</p>}
    {data.worker_active && !data.worker_heartbeat_fresh && <p className="text-amber-300">The worker holds the queue lock but has not written a heartbeat recently; it may be blocked inside a long step.</p>}
    {data.browser_handoffs.length > 0 && <p className="text-zinc-300">Browser work is held for run {data.browser_handoffs.map(id => `#${id}`).join(', ')}. Finish the human step, then stop or retry that run in Runs &amp; logs.</p>}
    <p className="text-xs text-zinc-500">Automation uses its own persistent Chrome profile. Sign in once here for each platform; your everyday Chrome logins are separate.</p>
  </div>
}
