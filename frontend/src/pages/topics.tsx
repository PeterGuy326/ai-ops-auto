import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Plus, Search } from "lucide-react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { TopicCard } from "@/components/topics/topic-card"
import { CreateTopicForm } from "@/components/topics/create-topic-form"
import { MOCK_CATEGORIES } from "@/lib/mock-topics"
import { categoryLabel } from "@/lib/topic-utils"

const ALL = "__all__"

export default function Topics() {
  const { data, isLoading } = useQuery({ queryKey: ["topics"], queryFn: () => api.topics() })
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState("")
  const [cat, setCat] = useState<string>(ALL)

  const filtered = useMemo(() => {
    const all = data ?? []
    return all.filter((t) => {
      if (cat !== ALL && (t.category ?? "") !== cat) return false
      if (q && !t.name.toLowerCase().includes(q.toLowerCase()) &&
        !(t.keywords ?? []).some((k) => k.toLowerCase().includes(q.toLowerCase())))
        return false
      return true
    })
  }, [data, q, cat])

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">专题</h1>
          <p className="text-sm text-muted-foreground">
            内容生产的源头 — 每个专题归口账号、文章、任务、数据指标
          </p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="size-4" />
              新建专题
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>创建新专题</DialogTitle>
              <DialogDescription>
                填写专题名称、分类、关键词与目标平台。创建后可在卡片中查看与管理。
              </DialogDescription>
            </DialogHeader>
            <CreateTopicForm
              onCreated={() => setOpen(false)}
              onCancel={() => setOpen(false)}
            />
          </DialogContent>
        </Dialog>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative max-w-sm flex-1">
          <Search className="text-muted-foreground absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索名称或关键词"
            className="pl-8"
          />
        </div>
        <Select value={cat} onValueChange={setCat}>
          <SelectTrigger size="sm" className="min-w-[140px]">
            <SelectValue placeholder="全部分类" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部分类</SelectItem>
            {MOCK_CATEGORIES.map((c) => (
              <SelectItem key={c} value={c}>
                {categoryLabel(c)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-muted-foreground ml-auto text-sm">
          共 {filtered.length} / {data?.length ?? 0} 个专题
        </span>
      </div>

      {isLoading ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            加载中...
          </CardContent>
        </Card>
      ) : filtered.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            没有匹配的专题。
            <button
              onClick={() => setOpen(true)}
              className="text-primary ml-1 underline-offset-4 hover:underline"
            >
              创建第一个
            </button>
            。
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((t) => (
            <TopicCard key={t.id} topic={t} />
          ))}
        </div>
      )}
    </div>
  )
}
