// 创建专题表单 — Dialog 内使用。
//
// keywords / target_platforms 用逗号分隔输入；提交时 split。
// 提交成功后 invalidate ['topics'] 刷新列表。

import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { api, type TopicCategory, type TopicCreate } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { MOCK_CATEGORIES } from "@/lib/mock-topics"
import { categoryLabel } from "@/lib/topic-utils"

export function CreateTopicForm({
  onCreated,
  onCancel,
}: {
  onCreated?: () => void
  onCancel?: () => void
}) {
  const qc = useQueryClient()
  const [name, setName] = useState("")
  const [category, setCategory] = useState<TopicCategory | "">("")
  const [keywords, setKeywords] = useState("")
  const [platforms, setPlatforms] = useState("")
  const [error, setError] = useState<string | null>(null)

  const mut = useMutation({
    mutationFn: (data: TopicCreate) => api.createTopic(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["topics"] })
      onCreated?.()
    },
    onError: (e: Error) => setError(e.message),
  })

  function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!name.trim()) return setError("请输入专题名称")
    if (!category) return setError("请选择分类")
    const kw = keywords.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    if (kw.length === 0) return setError("至少输入一个关键词")
    const pls = platforms.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    mut.mutate({
      name: name.trim(),
      category,
      keywords: kw,
      target_platforms: pls,
    })
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="topic-name">专题名称 *</Label>
        <Input
          id="topic-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="例如 AI 工程化"
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="topic-category">分类 *</Label>
        <Select
          value={category}
          onValueChange={(v) => setCategory(v as TopicCategory)}
        >
          <SelectTrigger id="topic-category" className="w-full">
            <SelectValue placeholder="选择分类" />
          </SelectTrigger>
          <SelectContent>
            {MOCK_CATEGORIES.map((c) => (
              <SelectItem key={c} value={c}>
                {categoryLabel(c)}
                <span className="text-muted-foreground text-xs">· {c}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <Label htmlFor="topic-keywords">关键词 *</Label>
        <Textarea
          id="topic-keywords"
          value={keywords}
          onChange={(e) => setKeywords(e.target.value)}
          placeholder="逗号或空格分隔，例如：LLM, Agent, RAG"
          rows={2}
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="topic-platforms">目标平台</Label>
        <Input
          id="topic-platforms"
          value={platforms}
          onChange={(e) => setPlatforms(e.target.value)}
          placeholder="逗号分隔，例如：zhihu, juejin, wechat_mp"
        />
      </div>

      {error && (
        <p className="text-destructive text-sm" role="alert">
          {error}
        </p>
      )}

      <div className="flex justify-end gap-2 pt-2">
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel}>
            取消
          </Button>
        )}
        <Button type="submit" disabled={mut.isPending}>
          {mut.isPending ? "创建中..." : "创建专题"}
        </Button>
      </div>
    </form>
  )
}
