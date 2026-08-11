export function QueryError({ error }: { error: unknown }) {
  if (!error) return null

  const message = error instanceof Error ? error.message : String(error)
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive"
    >
      后端请求失败：{message}
    </div>
  )
}
