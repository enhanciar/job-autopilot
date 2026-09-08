import { useEffect, useRef, useState } from 'react'
import { Send, SkipForward } from 'lucide-react'
import { api, type OpenQuestion, type ChatTurn } from '../api'
import { Page, Empty } from '../components/ui'

export default function Questions() {
  const [, setOpen] = useState<OpenQuestion[]>([])
  const [summary, setSummary] = useState({ open: 0, answered: 0, skipped: 0 })
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [current, setCurrent] = useState<OpenQuestion | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    api.questions().then(d => {
      if (!alive) return
      setOpen(d.open); setSummary(d.summary); setTurns(d.history); setCurrent(d.open[0] ?? null)
    }).catch(e => { if (alive) setErr(String(e)) })
    return () => { alive = false }
  }, [])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns])

  const send = async () => {
    const message = draft.trim()
    if (!message || !current || busy) return
    setBusy(true); setErr(''); setDraft('')
    setTurns(t => [...t, { role: 'user', text: message, question_id: current.id }])
    try {
      const r = await api.answerQuestion(current.id, message)
      setTurns(t => [...t, { role: 'assistant', text: r.reply, question_id: current.id }])
      setOpen(r.open); setSummary(r.summary); setCurrent(r.next)
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const skip = async () => {
    if (!current) return
    try { const r = await api.skipQuestion(current.id); setOpen(r.open); setSummary(r.summary); setCurrent(r.next) }
    catch (e) { setErr(String(e)) }
  }

  return (
    <Page title="Questions" sub="Employers ask things your profile does not cover. Answer each one here once, in your own words, and every future application uses it automatically.">
      <div className="flex flex-wrap gap-4 text-sm text-zinc-400">
        <span><strong className="text-zinc-100">{summary.open}</strong> waiting</span>
        <span><strong className="text-zinc-100">{summary.answered}</strong> answered</span>
        <span><strong className="text-zinc-100">{summary.skipped}</strong> skipped</span>
      </div>
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}

      {!current && summary.open === 0 && <Empty text="No questions waiting. When an application stops on one, it appears here." />}

      {current && (
        <div className="card space-y-2">
          <div className="text-xs text-zinc-500">
            Question {summary.open > 1 ? `1 of ${summary.open}` : ''}
            {current.times_seen > 1 && ` · asked by ${current.times_seen} employers`}
            {current.companies.length > 0 && ` · ${current.companies.slice(0, 3).join(', ')}`}
          </div>
          <p className="text-zinc-100">{current.text}</p>
          {current.options && current.options.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {current.options.map(o => (
                <button key={o} className="badge bg-zinc-700/50 text-zinc-200 hover:bg-zinc-600" onClick={() => setDraft(o)}>{o}</button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="card space-y-3 min-h-[30vh]">
        {turns.length === 0 && <p className="text-sm text-zinc-500">Answer in plain words. "About 30 days", "no, I have not used Azure", "yes, at Purplle". I store it and write the rule that recognises the same question elsewhere.</p>}
        {turns.map((t, i) => (
          <div key={i} className={t.role === 'user' ? 'text-right' : ''}>
            <div className={`inline-block max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap text-left ${t.role === 'user' ? 'bg-emerald-600/20 text-emerald-50' : 'bg-zinc-800 text-zinc-200'}`}>{t.text}</div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <div className="flex items-center gap-2">
        <input className="input flex-1" value={draft} placeholder={current ? 'Your answer…' : 'Nothing to answer'}
          disabled={!current || busy} aria-label="Your answer"
          onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') send() }} />
        <button className="btn btn-primary" onClick={send} disabled={!current || busy || !draft.trim()}><Send className="size-4" /> {busy ? 'Saving…' : 'Send'}</button>
        <button className="btn" onClick={skip} disabled={!current || busy} title="Leave this one for later"><SkipForward className="size-4" /> Skip</button>
      </div>
    </Page>
  )
}
