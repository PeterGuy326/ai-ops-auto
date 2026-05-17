// 全局 ?topic=X 同步 hook
//
// 设计：URL query 是单一来源；setTopicId 写 URL，组件从 URL 读。
// 这样 TopicSwitcher / 列表内联过滤器 / 直接粘贴 URL 三种来源都自然一致。

import { useCallback } from "react"
import { useSearchParams } from "react-router-dom"

const QS_KEY = "topic"

export function useTopicFilter(): {
  topicId: number | null
  setTopicId: (id: number | null) => void
} {
  const [params, setParams] = useSearchParams()
  const raw = params.get(QS_KEY)
  const topicId = raw && /^\d+$/.test(raw) ? Number(raw) : null

  const setTopicId = useCallback(
    (id: number | null) => {
      const next = new URLSearchParams(params)
      if (id == null) next.delete(QS_KEY)
      else next.set(QS_KEY, String(id))
      setParams(next, { replace: false })
    },
    [params, setParams],
  )

  return { topicId, setTopicId }
}
