import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Page } from '../components/ui'

export default function Profile() {
  const [info, setInfo] = useState<Awaited<ReturnType<typeof api.profile>> | null>(null)
  const [yamlText, setYamlText] = useState('')
  const [answersText, setAnswersText] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [draftErrors, setDraftErrors] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = () => api.profile().then(p => { setInfo(p); setYamlText(p.yaml); setAnswersText(p.answers_yaml) }).catch(e => setErr(String(e)))
  useEffect(() => { let alive = true; api.profile().then(p => { if (alive) { setInfo(p); setYamlText(p.yaml); setAnswersText(p.answers_yaml) } }).catch(e => { if (alive) setErr(String(e)) }); return () => { alive = false } }, [])

  const extract = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) { setErr('Choose a PDF or DOCX resume first'); return }
    setBusy(true); setErr(''); setMsg('')
    try {
      const r = await api.uploadResume(file)
      setYamlText(r.yaml); setDraftErrors(r.errors)
      setMsg(`Read ${r.text_chars} characters from ${file.name}. This is a DRAFT: check every line below, fill in the preferences the resume cannot know (salary, notice period, relocation), then press Save profile.`)
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const saveProfile = async () => {
    setErr(''); setMsg('')
    try {
      const r = await api.saveProfile(yamlText)
      setDraftErrors([])
      setMsg(`Profile saved. ${r.stale_applications ? `${r.stale_applications} prepared application(s) now need "Regenerate" before they can be approved, because their documents were built from the old facts.` : 'No prepared applications are affected.'}`)
      load()
    } catch (e) { setErr(String(e)) }
  }
  const saveAnswers = async () => {
    setErr(''); setMsg('')
    try { await api.saveAnswers(answersText); setMsg('Answer bank saved.'); load() } catch (e) { setErr(String(e)) }
  }

  return (
    <Page title="Profile" sub="The only source of facts for every resume, answer and message the system produces. Set it up from a resume, then review and edit here.">
      {msg && <p role="status" className="text-sm text-emerald-300">{msg}</p>}
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}

      <div className="card space-y-3">
        <h2 className="font-medium">Set up from a resume</h2>
        <p className="text-sm text-zinc-400">Upload a resume (PDF or DOCX). It is read on this computer and turned into a draft profile with the configured LLM. Nothing is saved until you review the draft and press Save profile.
          {info?.identity?.name && <> Current profile: <span className="text-zinc-200">{info.identity.name}</span>{info.identity.headline ? ` · ${info.identity.headline}` : ''}.</>}
          {info?.resume_base && <> Source resume on file: <code className="text-xs">{info.resume_base}</code>.</>}</p>
        <div className="flex flex-wrap items-center gap-2">
          <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.md" className="text-sm" aria-label="Resume file" />
          <button className="btn btn-primary" onClick={extract} disabled={busy}>{busy ? 'Reading resume…' : 'Extract profile from resume'}</button>
        </div>
        {draftErrors.length > 0 && <ul className="text-sm text-amber-300 list-disc pl-5">{draftErrors.map(e => <li key={e}>{e}</li>)}</ul>}
      </div>

      <div className="card space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-medium">Master profile (YAML)</h2>
          <button className="btn btn-primary btn-sm" onClick={saveProfile}>Save profile</button>
        </div>
        <p className="text-xs text-zinc-500">Saving backs up the previous version to data/backups and changes the profile version. Applications prepared with the old facts must be regenerated before approval; nothing already submitted is touched.</p>
        <textarea className="input w-full font-mono text-xs min-h-[60vh]" value={yamlText} onChange={e => setYamlText(e.target.value)} spellCheck={false} aria-label="Master profile YAML" />
      </div>

      <div className="card space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-medium">Answer bank (YAML)</h2>
          <button className="btn btn-primary btn-sm" onClick={saveAnswers}>Save answers</button>
        </div>
        <p className="text-xs text-zinc-500">Canned answers for screening questions: each entry is a regular expression (<code>match</code>) and the <code>answer</code>. The first matching entry wins, so put specific patterns before general ones. Use <code>{'{preferences.expected_salary_lpa}'}</code>-style placeholders to pull numbers from the profile. Special answers: <code>__LLM__</code> (write it from the profile, fact-checked), <code>__COVER__</code>, <code>__DATE_PLUS_30__</code>.</p>
        <textarea className="input w-full font-mono text-xs min-h-[40vh]" value={answersText} onChange={e => setAnswersText(e.target.value)} spellCheck={false} aria-label="Answer bank YAML" />
      </div>
    </Page>
  )
}
