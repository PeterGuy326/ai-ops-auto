// 全局顶部专题切换器 — sticky header 右上角下拉。
// 选中后通过 useTopicFilter() 写入 URL `?topic=X`，所有列表页响应。

import { useQuery } from "@tanstack/react-query"
import { BookOpen } from "lucide-react"
import { api } from "@/lib/api"
import { useTopicFilter } from "@/hooks/use-topic-filter"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { categoryLabel } from "@/lib/topic-utils"

const ALL_VALUE = "__all__"

export function TopicSwitcher() {
  const { topicId, setTopicId } = useTopicFilter()
  const { data } = useQuery({ queryKey: ["topics"], queryFn: () => api.topics() })
  const topics = data ?? []

  const value = topicId == null ? ALL_VALUE : String(topicId)

  return (
    <Select
      value={value}
      onValueChange={(v) => setTopicId(v === ALL_VALUE ? null : Number(v))}
    >
      <SelectTrigger
        size="sm"
        className="h-8 min-w-[180px]"
        aria-label="切换专题"
      >
        <BookOpen className="size-3.5" />
        <SelectValue placeholder="全部专题" />
      </SelectTrigger>
      <SelectContent align="end">
        <SelectItem value={ALL_VALUE}>全部专题</SelectItem>
        {topics.map((t) => (
          <SelectItem key={t.id} value={String(t.id)}>
            <span>{t.name}</span>
            <span className="text-muted-foreground text-xs">
              · {categoryLabel(t.category)}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
