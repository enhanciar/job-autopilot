export type Job = {
  id: number; source: string; ats: string | null; company: string; title: string; url: string; apply_url: string | null
  location: string | null; remote_scope: string | null; employment_type: string | null; salary: string | null
  posted_at: string | null; sponsor_flag: boolean; eligible: boolean | null; eligibility_reason: string | null
  fit_score: number | null; fit_reasons: string | null; status: string; tags: string[] | null; created_at: string; description?: string | null
}
export type Application = {
  id: number; job_id: number; platform: string; method: string; status: string; resume_path: string | null; cover_note: string | null
  answers: Record<string, unknown> | null; screenshot_before: string | null; screenshot_after: string | null; confirmation_text: string | null
  error: string | null; submitted_at: string | null; created_at: string; company?: string; title?: string; url?: string
  preparation_issue?: string | null; can_regenerate?: boolean; status_options?: string[]; country?: string | null; location?: string | null; fit_score?: number | null; source?: string
}
export type Outreach = {
  id: number; job_id: number | null; contact_id: number | null; channel: string; step: number; subject: string | null; body: string | null
  status: string; scheduled_for: string | null; sent_at: string | null; replied_at: string | null; error: string | null; created_at: string
  contact_name?: string | null; contact_title?: string | null; company?: string | null
}
export type Run = { id: number; kind: string; name: string; status: string; started_at: string; ended_at: string | null; stats: Record<string, number> | null; error: string | null }
export type Event = { id: number; run_id: number | null; ts: string; level: string; platform: string | null; message: string; data: Record<string, unknown> | null; screenshot: string | null }
export type Platform = { key: string; name: string; type: string; status: string; login: boolean; modes?: string[]; implemented: boolean; login_supported: boolean; configured: boolean | null; logged_in: boolean | null; session_note: string | null; last_checked: string | null }
export type OpenQuestion = { id: number; text: string; options: string[] | null; companies: string[]; times_seen: number }
export type ChatTurn = { role: string; text: string; question_id: number | null }
type QuestionState = { summary: { open: number; answered: number; skipped: number }; open: OpenQuestion[] }
export type FullRunPlatform = { key: string; name: string; needs_login: boolean }
export type Overview = {
  target_per_day: number; review_mode: boolean
  today: { applied: number; outreach: number; touches: number; jobs_found: number }
  totals: Record<string, number>; by_source: Record<string, number>; eligible_by_source: Record<string, number>
  days: { day: string; found: number; applied: number; outreach: number }[]
  active_runs: number; paused_runs: number
  caps: Record<string, Record<string, { used: number; cap: number }>>
  sessions: { platform: string; logged_in: boolean; last_checked: string | null; note: string | null }[]
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...init })
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json()
}
const qs = (o: Record<string, unknown>) => {
  const p = new URLSearchParams()
  Object.entries(o).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') p.set(k, String(v)) })
  const s = p.toString(); return s ? `?${s}` : ''
}

export const api = {
  overview: () => req<Overview>('/api/stats/overview'),
  jobs: (f: Record<string, unknown>) => req<{ total: number; items: Job[] }>(`/api/jobs${qs(f)}`),
  job: (id: number) => req<Job>(`/api/jobs/${id}`),
  jobFacets: () => req<{ sources: Record<string, number>; statuses: Record<string, number>; countries: Record<string, number> }>('/api/jobs/facets'),
  setJobStatus: (id: number, status: string) => req(`/api/jobs/${id}/status`, { method: 'POST', body: JSON.stringify({ status }) }),
  applications: (f: Record<string, unknown>) => req<{ total: number; items: Application[] }>(`/api/applications${qs(f)}`),
  review: (f: Record<string, unknown> = {}) => req<{ total: number; items: Application[] }>(`/api/applications/review${qs(f)}`),
  appFacets: () => req<Record<string, Record<string, number>>>('/api/applications/facets'),
  setAppStatus: (id: number, status: string, note?: string) => req(`/api/applications/${id}/status`, { method: 'POST', body: JSON.stringify({ status, note }) }),
  approveAll: (f: { country?: string; platform?: string }) => req<{ approved: number; blocked: { id: number; company: string | null; reason: string }[]; blocked_total: number }>('/api/applications/approve-all', { method: 'POST', body: JSON.stringify(f) }),
  outreach: (f: Record<string, unknown>) => req<{ total: number; items: Outreach[] }>(`/api/outreach${qs(f)}`),
  setOutreachStatus: (id: number, status: string) => req(`/api/outreach/${id}/status`, { method: 'POST', body: JSON.stringify({ status }) }),
  runs: (limit?: number) => req<Run[]>(`/api/runs${qs({ limit })}`),
  fullRunPlatforms: () => req<FullRunPlatform[]>('/api/runs/fullrun/platforms'),
  startFullRun: (platforms: string[] | null, apply: boolean) => req<{ run_id: number }>('/api/runs/fullrun', { method: 'POST', body: JSON.stringify({ params: { platforms, apply } }) }),
  events: (f: Record<string, unknown>) => req<Event[]>(`/api/runs/events${qs(f)}`),
  runCollector: (name: string) => req<{ run_id: number }>(`/api/runs/collector/${name}`, { method: 'POST' }),
  runAllCollectors: () => req<{ run_id: number }>('/api/runs/collectors/all', { method: 'POST' }),
  runSkill: (name: string, params: Record<string, unknown>) => req<{ run_id: number }>(`/api/runs/skill/${name}`, { method: 'POST', body: JSON.stringify({ params }) }),
  runPipeline: (name: string, params: Record<string, unknown> = {}) => req<{ run_id: number }>(`/api/runs/pipeline/${name}`, { method: 'POST', body: JSON.stringify({ params }) }),
  runService: (name: string) => req<{ run_id: number }>(`/api/runs/service/${name}`, { method: 'POST' }),
  retryRun: (id: number) => req<{ run_id: number }>(`/api/runs/${id}/retry`, { method: 'POST' }),
  workerHealth: () => req<{worker_active: boolean; worker_heartbeat_age_s: number | null; worker_heartbeat_fresh: boolean; worker_run_id: number | null; queued: number; running: number[]; browser_handoffs: number[]; browser: {profile: string; busy: boolean}}>('/api/runs/health'),
  stopRun: (id: number) => req(`/api/runs/${id}/stop`, { method: 'POST' }),
  platforms: () => req<Platform[]>('/api/platforms'),
  llmStatus: () => req<Record<string, boolean>>('/api/platforms/llm'),
  openLogin: (key: string) => req<{ run_id: number }>(`/api/platforms/${key}/login`, { method: 'POST' }),
  questions: () => req<QuestionState & { history: ChatTurn[] }>('/api/questions'),
  answerQuestion: (question_id: number, message: string) => req<QuestionState & { reply: string; next: OpenQuestion | null }>('/api/questions/chat', { method: 'POST', body: JSON.stringify({ question_id, message }) }),
  skipQuestion: (id: number) => req<QuestionState & { next: OpenQuestion | null }>(`/api/questions/${id}/skip`, { method: 'POST' }),
  settings: () => req<Record<string, unknown>>('/api/settings'),
  profile: () => req<{ profile: Record<string, unknown>; yaml: string; fingerprint: string; identity: { name: string | null; headline: string | null }; resume_base: string | null; answers_yaml: string; answers_count: number; stale_applications: number }>('/api/profile'),
  saveProfile: (yaml: string) => req<{ fingerprint: string; backup: string; stale_applications: number }>('/api/profile', { method: 'PUT', body: JSON.stringify({ yaml }) }),
  saveAnswers: (yaml: string) => req<{ fingerprint: string; backup: string; stale_applications: number }>('/api/profile/answers', { method: 'PUT', body: JSON.stringify({ yaml }) }),
  uploadResume: async (file: File) => {
    const body = new FormData(); body.append('file', file)
    const r = await fetch('/api/profile/resume', { method: 'POST', body })
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
    return r.json() as Promise<{ resume_path: string; text_chars: number; yaml: string; errors: string[] }>
  },
  saveSettings: (cfg: Record<string, unknown>) => req('/api/settings', { method: 'PUT', body: JSON.stringify(cfg) }),
}

export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const key = JSON.stringify(deps)
  const latestFn = useRef(fn)
  const generation = useRef(0)
  const [snapshot, setSnapshot] = useState<{ key: string; data: T | null; err: string | null }>({ key, data: null, err: null })
  useEffect(() => { latestFn.current = fn }, [fn])
  const refresh = useCallback(async () => {
    const request = ++generation.current
    try {
      const data = await latestFn.current()
      if (request === generation.current) setSnapshot({ key, data, err: null })
    } catch (e) {
      if (request === generation.current) setSnapshot(previous => ({ key, data: previous.key === key ? previous.data : null, err: String(e) }))
    }
  }, [key])
  useEffect(() => {
    let stopped = false
    const invalidate = () => { generation.current++ }
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      await refresh()
      if (!stopped) timer = setTimeout(poll, ms)
    }
    void poll()
    return () => { stopped = true; invalidate(); clearTimeout(timer) }
  }, [refresh, ms])
  return { data: snapshot.key === key ? snapshot.data : null, err: snapshot.key === key ? snapshot.err : null, refresh }
}
import { useCallback, useEffect, useRef, useState } from 'react'

export const ago = (iso: string | null) => {
  if (!iso) return '—'
  const s = (Date.now() - new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime()) / 1000
  if (s < 60) return `${Math.floor(s)}s ago`; if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return `${Math.floor(s / 86400)}d ago`
}

export const artifactUrl = (path: string) => '/' + path.replace(/^\/?data\/artifacts\//, 'artifacts/').replace(/^\//, '')
