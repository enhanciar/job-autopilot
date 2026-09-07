export default function Pagination({ page, size, total, onPage }: { page: number; size: number; total: number; onPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size))
  return <nav aria-label="Pagination" className="flex items-center justify-between gap-4 text-sm text-zinc-400">
    <span>{total ? `${(page - 1) * size + 1}–${Math.min(page * size, total)} of ${total}` : '0 records'}</span>
    <div className="flex gap-2 items-center">
      <button className="btn btn-sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</button>
      <span>Page {page} of {pages}</span>
      <button className="btn btn-sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</button>
    </div>
  </nav>
}
