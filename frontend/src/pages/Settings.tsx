import { useEffect, useState } from 'react'
import { api } from '../api'
import { Page } from '../components/ui'

export default function Settings() {
  const [text, setText] = useState('')
  const [msg, setMsg] = useState('')
  useEffect(() => { api.settings().then(c => setText(JSON.stringify(c, null, 2))) }, [])
  const save = async () => { try { await api.saveSettings(JSON.parse(text)); setMsg('Saved to config.yaml') } catch (e) { setMsg(String(e)) } }
  return (
    <Page title="Settings" sub="Live view of config.yaml: target, review mode, LLM routing, humanization, per-platform caps, filters, seed companies." actions={<button className="btn btn-primary" onClick={save}>Save</button>}>
      {msg && <div className="text-sm text-emerald-300">{msg}</div>}
      <textarea className="input w-full font-mono text-xs min-h-[70vh]" value={text} onChange={e => setText(e.target.value)} spellCheck={false} />
    </Page>
  )
}
