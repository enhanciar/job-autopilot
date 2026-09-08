import { NavLink, Outlet } from 'react-router-dom'
import { LayoutDashboard, Briefcase, ClipboardCheck, Send, Mail, Boxes, Activity, Settings as Cog, Rocket, PlayCircle, User, MessagesSquare } from 'lucide-react'
import { api, usePoll } from './api'

const nav = [
  { to: '/overview', label: 'Overview', icon: LayoutDashboard },
  { to: '/run', label: 'Run the system', icon: PlayCircle },
  { to: '/jobs', label: 'Jobs', icon: Briefcase },
  { to: '/review', label: 'Review queue', icon: ClipboardCheck },
  { to: '/applications', label: 'Applications', icon: Send },
  { to: '/outreach', label: 'Outreach', icon: Mail },
  { to: '/questions', label: 'Questions', icon: MessagesSquare },
  { to: '/platforms', label: 'Platforms', icon: Boxes },
  { to: '/runs', label: 'Runs & logs', icon: Activity },
  { to: '/profile', label: 'Profile', icon: User },
  { to: '/settings', label: 'Settings', icon: Cog },
]

export default function App() {
  const { data } = usePoll(api.overview, 10000)
  const { data: who } = usePoll(api.profile, 60000)
  const { data: q } = usePoll(api.questions, 30000)
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-zinc-800 bg-zinc-950/80 p-3 flex flex-col gap-1 sticky top-0 h-screen">
        <div className="flex items-center gap-2 px-2 py-3 mb-2">
          <Rocket className="size-5 text-emerald-400" />
          <div><div className="font-semibold leading-tight">Job Autopilot</div><div className="text-[11px] text-zinc-500 truncate max-w-[10rem]" title={who?.identity?.headline ?? ''}>{who?.identity?.name ?? 'Set up your profile'}</div></div>
        </div>
        {nav.map(n => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm ${isActive ? 'bg-zinc-800 text-white' : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100'}`}>
            <n.icon className="size-4" /> {n.label}
            {n.to === '/review' && data && data.totals.pending_review > 0 && <span className="ml-auto badge bg-amber-500/20 text-amber-300">{data.totals.pending_review}</span>}
            {n.to === '/questions' && q?.summary?.open ? ( <span className="ml-auto badge bg-sky-500/20 text-sky-300">{q.summary.open}</span>) : null}
            {n.to === '/runs' && data && data.paused_runs > 0 && <span className="ml-auto badge bg-rose-500/20 text-rose-300">{data.paused_runs} paused</span>}
          </NavLink>
        ))}
        <div className="mt-auto px-2 text-[11px] text-zinc-500 space-y-1">
          <div>Today: <span className="text-zinc-200">{data?.today.touches ?? 0}</span> / {data?.target_per_day ?? 100} touches</div>
          <div>{data?.review_mode ? 'Review mode on' : 'Auto-submit on'} · {data?.active_runs ?? 0} running</div>
        </div>
      </aside>
      <main className="flex-1 min-w-0 p-6"><Outlet /></main>
    </div>
  )
}
