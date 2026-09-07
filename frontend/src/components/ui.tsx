import type { ReactNode } from 'react'

export const Page = ({ title, sub, actions, children }: { title: string; sub?: string; actions?: ReactNode; children: ReactNode }) => (
  <div className="space-y-4">
    <div className="flex items-start justify-between gap-4">
      <div><h1 className="text-xl font-semibold">{title}</h1>{sub && <p className="text-sm text-zinc-500 mt-0.5">{sub}</p>}</div>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
    {children}
  </div>
)

const tone: Record<string, string> = {
  new: 'bg-sky-500/15 text-sky-300', filtered: 'bg-zinc-700/50 text-zinc-400', scored: 'bg-indigo-500/15 text-indigo-300', queued: 'bg-amber-500/15 text-amber-300',
  applied: 'bg-emerald-500/15 text-emerald-300', submitted: 'bg-emerald-500/15 text-emerald-300', skipped: 'bg-zinc-700/50 text-zinc-400',
  pending_review: 'bg-amber-500/15 text-amber-300', approved: 'bg-sky-500/15 text-sky-300', submitting: 'bg-indigo-500/15 text-indigo-300', failed: 'bg-rose-500/15 text-rose-300',
  needs_human: 'bg-rose-500/15 text-rose-300', rejected_by_user: 'bg-zinc-700/50 text-zinc-400', replied: 'bg-teal-500/15 text-teal-300', interview: 'bg-fuchsia-500/15 text-fuchsia-300',
  rejected: 'bg-zinc-700/50 text-zinc-400', offer: 'bg-emerald-500/25 text-emerald-200', sent: 'bg-emerald-500/15 text-emerald-300', stopped: 'bg-zinc-700/50 text-zinc-400',
  running: 'bg-sky-500/15 text-sky-300', done: 'bg-emerald-500/15 text-emerald-300', paused_for_human: 'bg-rose-500/15 text-rose-300',
  ready: 'bg-emerald-500/15 text-emerald-300', stub: 'bg-amber-500/15 text-amber-300', planned: 'bg-zinc-700/50 text-zinc-400',
  info: 'bg-zinc-700/50 text-zinc-300', warn: 'bg-amber-500/15 text-amber-300', error: 'bg-rose-500/15 text-rose-300', human: 'bg-fuchsia-500/20 text-fuchsia-200',
}
export const Badge = ({ v }: { v: string | null | undefined }) => <span className={`badge ${tone[v ?? ''] ?? 'bg-zinc-700/50 text-zinc-300'}`}>{(v ?? '—').replace(/_/g, ' ')}</span>

export const Stat = ({ label, value, hint, accent }: { label: string; value: ReactNode; hint?: string; accent?: string }) => (
  <div className="card">
    <div className="text-xs text-zinc-500">{label}</div>
    <div className={`text-2xl font-semibold mt-1 ${accent ?? ''}`}>{value}</div>
    {hint && <div className="text-xs text-zinc-500 mt-1">{hint}</div>}
  </div>
)

export const Empty = ({ text }: { text: string }) => <div className="card text-sm text-zinc-500 text-center py-10">{text}</div>

export const Score = ({ v }: { v: number | null }) => v == null ? <span className="text-zinc-600">—</span> :
  <span className={`font-mono text-xs px-1.5 py-0.5 rounded ${v >= 75 ? 'bg-emerald-500/20 text-emerald-300' : v >= 65 ? 'bg-amber-500/20 text-amber-300' : 'bg-zinc-700/50 text-zinc-400'}`}>{v}</span>
