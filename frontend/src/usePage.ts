import { useState } from 'react'

export function usePage(scope: string) {
  const [cursor, setCursor] = useState({ scope, page: 1 })
  return { page: cursor.scope === scope ? cursor.page : 1, setPage: (page: number) => setCursor({ scope, page }) }
}
