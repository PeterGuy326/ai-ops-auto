// 账号创建表单 — Dialog 内使用。Topic 是 required。
//
// 提交成功后 invalidate ['accounts'] / ['topics'] 让卡片计数刷新。

import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type AccountCreate } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { categoryLabel } from "@/lib/topic-utils"

const PLATFORMS = [
  { value: "zhihu", label: "知乎" },
  { value: "juejin", label: "掘金" },
  { value: "wechat_mp", label: "微信公众号" },
  { value: "xiaohongshu", label: "小红书" },
  { value: "douyin", label: "抖音" },
  { value: "weibo", label: "微博" },
  { value: "toutiao", label: "今日头条" },
]

export function CreateAccountForm({
  defaultTopicId,
  onCreated,
  onCancel,
}: {
  defaultTopicId?: number | null
  onCreated?: () => void
  onCancel?: () => void
}) {
  const qc = useQueryClient()
  const topicsQ = useQuery({ queryKey: ["topics"], queryFn: () => api.topics() })

  const [platform, setPlatform] = useState<string>("")
  const [nickname, setNickname] = useState("")
  const [topicId, setTopicId] = useState<string>(
    defaultTopicId ? String(defaultTopicId) : "",
  )
  const [quota, setQuota] = useState<string>("5")
  const [error, setError] = useState<string | null>(null)

  const mut = useMutation({
    mutationFn: (data: AccountCreate) => api.createAccount(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["accounts"] })
      qc.invalidateQueries({ queryKey: ["topics"] })
      onCreated?.()
    },
    onError: (e: Error) => setError(e.message),
  })

  function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!platform) return setError("请选择平台")
    if (!nickname.trim()) return setError("请输入昵称")
    if (!topicId) return setError("请选择所属专题（必选）")
    mut.mutate({
      platform,
      nickname: nickname.trim(),
      topic_id: Number(topicId),
      daily_quota: Number(quota) || 5,
    })
  }

  const topics = topicsQ.data ?? []

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="account-platform">平台 *</Label>
        <Select value={platform} onValueChange={setPlatform}>
          <SelectTrigger id="account-platform" className="w-full">
            <SelectValue placeholder="选择发布平台" />
          </SelectTrigger>
          <SelectContent>
            {PLATFORMS.map((p) => (
              <SelectItem key={p.value} value={p.value}>
                {p.label}
                <span className="text-muted-foreground text-xs">
                  · {p.value}
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <Label htmlFor="account-nickname">昵称 *</Label>
        <Input
          id="account-nickname"
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          placeholder="例如 ai-ops-tech-01"
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="account-topic">
          所属专题 * <span className="text-muted-foreground text-xs">（required）</span>
        </Label>
        <Select value={topicId} onValueChange={setTopicId}>
          <SelectTrigger id="account-topic" className="w-full">
            <SelectValue placeholder="选择一个专题" />
          </SelectTrigger>
          <SelectContent>
            {topics.length === 0 ? (
              <div className="text-muted-foreground px-2 py-3 text-xs">
                暂无专题，请先在"专题"页创建
              </div>
            ) : (
              topics.map((t) => (
                <SelectItem key={t.id} value={String(t.id)}>
                  <span>{t.name}</span>
                  <span className="text-muted-foreground text-xs">
                    · {categoryLabel(t.category)}
                  </span>
                </SelectItem>
              ))
            )}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <Label htmlFor="account-quota">日发布配额</Label>
        <Input
          id="account-quota"
          type="number"
          min={1}
          max={50}
          value={quota}
          onChange={(e) => setQuota(e.target.value)}
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
          {mut.isPending ? "创建中..." : "创建账号"}
        </Button>
      </div>
    </form>
  )
}
