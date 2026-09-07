import { useEffect, useState } from 'react'
import { api, type Event } from './api'

export function useRunEvents(runId: number | null, live: boolean) {
  const [snapshot, setSnapshot] = useState<{ id: number | null; events: Event[]; error: string | null }>({ id: null, events: [], error: null })
  useEffect(() => {
    if (runId == null) return
    let active = true
    let cursor = 0
    let accumulated: Event[] = []
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      let more = false
      try {
        for (let count = 0; count < 100 && active; count++) {
          const batch = await api.events({ run_id: runId, after_id: cursor, limit: 200 })
          if (!active) return
          const fresh = batch.filter(event => event.id > cursor)
          if (fresh.length) {
            cursor = Math.max(...fresh.map(event => event.id))
            accumulated = [...accumulated, ...fresh]
          }
          more = batch.length === 200 && fresh.length > 0
          if (!more) break
        }
        if (active) setSnapshot({ id: runId, events: accumulated, error: null })
      } catch (e) {
        if (active) setSnapshot({ id: runId, events: accumulated, error: String(e) })
        more = true // Retry transient failures for completed runs too.
      }
      if (active && (live || more)) timer = setTimeout(poll, 2000)
    }
    void poll()
    return () => { active = false; clearTimeout(timer) }
  }, [runId, live])
  return snapshot.id === runId ? snapshot : { id: runId, events: [], error: null }
}
