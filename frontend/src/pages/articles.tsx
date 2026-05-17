import { useQuery } from "@tanstack/react-query"
import { api } from "@/lib/api"
import { useTopicFilter } from "@/hooks/use-topic-filter"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { CategoryBadge } from "@/components/topics/category-badge"
import { Label } from "@/components/ui/label"
import { categoryLabel } from "@/lib/topic-utils"

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  draft: "outline",
  ready: "secondary",
  scheduled: "secondary",
  publishing: "default",
  published: "default",
  failed: "destructive",
  dead: "destructive",
}

const ALL = "__all__"

export default function Articles() {
  const { topicId, setTopicId } = useTopicFilter()
  const { data, isLoading } = useQuery({
    queryKey: ["articles", { topic_id: topicId ?? null }],
    queryFn: () => api.articles(topicId),
  })
  const topicsQ = useQuery({ queryKey: ["topics"], queryFn: () => api.topics() })
  const topicMap = new Map(topicsQ.data?.map((t) => [t.id, t]) ?? [])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-2xl font-bold">文章</h1>
        <div className="flex items-center gap-2">
          <Label htmlFor="article-topic-filter" className="text-muted-foreground">
            专题
          </Label>
          <Select
            value={topicId == null ? ALL : String(topicId)}
            onValueChange={(v) => setTopicId(v === ALL ? null : Number(v))}
          >
            <SelectTrigger id="article-topic-filter" size="sm" className="min-w-[200px]">
              <SelectValue placeholder="全部专题" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部专题</SelectItem>
              {(topicsQ.data ?? []).map((t) => (
                <SelectItem key={t.id} value={String(t.id)}>
                  <span>{t.name}</span>
                  <span className="text-muted-foreground text-xs">
                    · {categoryLabel(t.category)}
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>文章列表（{data?.length ?? 0}）</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">加载中...</p>
          ) : data && data.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>标题</TableHead>
                  <TableHead>专题</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>目标平台</TableHead>
                  <TableHead>排程</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((a) => {
                  const t = topicMap.get(a.topic_id)
                  return (
                    <TableRow key={a.id}>
                      <TableCell className="font-mono text-xs">{a.id}</TableCell>
                      <TableCell className="max-w-md truncate font-medium">
                        {a.title}
                      </TableCell>
                      <TableCell>
                        {t ? <CategoryBadge category={t.category} /> : <span className="text-muted-foreground text-xs">—</span>}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{a.content_type}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={STATUS_VARIANT[a.status] ?? "outline"}>
                          {a.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {(a.target_platforms ?? []).join(", ")}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {a.scheduled_at
                          ? new Date(a.scheduled_at).toLocaleString("zh-CN")
                          : "—"}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">还没有文章。</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
