import { useEffect, useRef, useState } from 'react'
import { Send, Wand2 } from 'lucide-react'
import { api, type Bundle, type ChatTurn } from '../api'
import { Page, Empty } from '../components/ui'

export default function Questions() {
  const [bundles, setBundles] = useState<Bundle[]>([])
  const [summary, setSummary] = useState({ open: 0, answered: 0, skipped: 0 })
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [active, setActive] = useState<string>('')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  const load = (asked?: string) => api.questionBundles().then(d => {
    setBundles(d.bundles); setSummary(d.summary); setTurns(d.history)
    setActive(a => (asked ?? (d.bundles.some(b => b.theme === a) ? a : d.bundles[0]?.theme ?? '')))
  }).catch(e => setErr(String(e)))

  useEffect(() => { let alive = true; api.questionBundles().then(d => {
    if (!alive) return
    setBundles(d.bundles); setSummary(d.summary); setTurns(d.history); setActive(d.bundles[0]?.theme ?? '')
  }).catch(e => { if (alive) setErr(String(e)) }); return () => { alive = false } }, [])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns])

  const current = bundles.find(b => b.theme === active) ?? bundles[0]

  const send = async () => {
    const message = draft.trim()
    if (!message || !current || busy) return
    setBusy(true); setErr(''); setDraft('')
    setTurns(t => [...t, { role: 'user', text: message, question_id: null }])
    try {
      const r = await api.answerBundle(current.ids, message)
      setTurns(t => [...t, { role: 'assistant', text: r.reply, question_id: null }])
      setBundles(r.bundles); setSummary(r.summary)
      if (!r.bundles.some(b => b.theme === current.theme)) setActive(r.bundles[0]?.theme ?? '')
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }

  const auto = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.autoAnswer()
      const text = r.resolved.length
        ? `Answered ${r.resolved.length} from your profile:\n` + r.resolved.map(x => `• ${x.question} → ${x.answer}`).join('\n')
        : 'Nothing left that your profile already answers.'
      setTurns(t => [...t, { role: 'assistant', text, question_id: null }])
      setSummary(r.summary); load()
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }

  return (
    <Page title="Questions" sub="Employers ask things your profile does not cover. Answer each group once here, in your own words, and every future application uses it."
      actions={<button className="btn btn-sm" onClick={auto} disabled={busy}><Wand2 className="size-3.5" /> Answer what you can from my profile</button>}>
      <div className="flex flex-wrap gap-4 text-sm text-zinc-400">
        <span><strong className="text-zinc-100">{summary.open}</strong> waiting</span>
        <span><strong className="text-zinc-100">{summary.answered}</strong> answered</span>
        <span><strong className="text-zinc-100">{summary.skipped}</strong> skipped</span>
      </div>
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      {!bundles.length && <Empty text="Nothing waiting. When an application stops on a question, it appears here." />}

      {bundles.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {bundles.map(b => (
            <button key={b.theme} onClick={() => setActive(b.theme)}
              className={`btn btn-sm ${active === b.theme ? 'btn-primary' : ''}`}>{b.theme} ({b.count})</button>))}
        </div>)}

      <div className="card space-y-3 min-h-[34vh]">
        {turns.length === 0 && !current && <p className="text-sm text-zinc-500">Nothing to answer right now.</p>}
        {turns.map((t, i) => (
          <div key={i} className={t.role === 'user' ? 'text-right' : ''}>
            <div className={`inline-block max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap text-left ${t.role === 'user' ? 'bg-emerald-600/20 text-emerald-50' : 'bg-zinc-800 text-zinc-200'}`}>{t.text}</div>
          </div>))}
        {current && (
          <div>
            <div className="inline-block max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap bg-zinc-800 text-zinc-200 border border-emerald-700/40">{current.prompt}</div>
            {current.asked_by.length > 0 && <div className="text-xs text-zinc-500 mt-1">asked by {current.asked_by.join(', ')}</div>}
          </div>)}
        <div ref={endRef} />
      </div>

      <div className="flex items-center gap-2">
        <input className="input flex-1" value={draft} placeholder={current ? 'Answer as many of them as you like, in your own words…' : 'Nothing to answer'}
          disabled={!current || busy} aria-label="Your answer"
          onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') send() }} />
        <button className="btn btn-primary" onClick={send} disabled={!current || busy || !draft.trim()}><Send className="size-4" /> {busy ? 'Saving…' : 'Send'}</button>
      </div>
      <p className="text-xs text-zinc-500">An answer only settles the question it actually addresses. Anything your reply does not cover stays open rather than being guessed at.</p>
    </Page>
  )
}
