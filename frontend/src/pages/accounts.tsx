import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Plus } from "lucide-react"
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
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { CategoryBadge } from "@/components/topics/category-badge"
import { CreateAccountForm } from "@/components/accounts/create-account-form"

const HEALTH_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  healthy: "default",
  degraded: "secondary",
  banned: "destructive",
  expired: "destructive",
  unknown: "outline",
}

export default function Accounts() {
  const { topicId } = useTopicFilter()
  const { data, isLoading } = useQuery({
    queryKey: ["accounts", { topic_id: topicId ?? null }],
    queryFn: () => api.accounts(topicId),
  })
  const topicsQ = useQuery({ queryKey: ["topics"], queryFn: () => api.topics() })
  const topicMap = new Map(topicsQ.data?.map((t) => [t.id, t]) ?? [])
  const currentTopic = topicId ? topicMap.get(topicId) : null
  const [open, setOpen] = useState(false)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold">账号</h1>
          {currentTopic && (
            <p className="text-muted-foreground text-sm">
              当前过滤：{currentTopic.name}{" "}
              <CategoryBadge category={currentTopic.category} className="ml-1" />
            </p>
          )}
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="size-4" /> 新建账号
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>新建账号</DialogTitle>
              <DialogDescription>
                每个账号必须归属一个专题（required）。
              </DialogDescription>
            </DialogHeader>
            <CreateAccountForm
              defaultTopicId={topicId}
              onCreated={() => setOpen(false)}
              onCancel={() => setOpen(false)}
            />
          </DialogContent>
        </Dialog>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>账号列表（{data?.length ?? 0}）</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">加载中...</p>
          ) : data && data.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>平台</TableHead>
                  <TableHead>昵称</TableHead>
                  <TableHead>专题</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">日配额</TableHead>
                  <TableHead>上次发布</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((a) => {
                  const t = a.topic_id ? topicMap.get(a.topic_id) : null
                  return (
                    <TableRow key={a.id}>
                      <TableCell className="font-mono text-xs">{a.id}</TableCell>
                      <TableCell>
                        <Badge variant="outline">{a.platform}</Badge>
                      </TableCell>
                      <TableCell className="font-medium">{a.nickname}</TableCell>
                      <TableCell>
                        {t ? (
                          <span className="inline-flex items-center gap-1">
                            <span className="text-sm">{t.name}</span>
                            <CategoryBadge category={t.category} />
                          </span>
                        ) : (
                          <span className="text-muted-foreground text-xs">—</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant={HEALTH_VARIANT[a.health] ?? "outline"}>
                          {a.health}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {a.daily_quota}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {a.last_publish_at
                          ? new Date(a.last_publish_at).toLocaleString("zh-CN")
                          : "—"}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">
              还没有账号{currentTopic ? `（专题：${currentTopic.name}）` : ""}。
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
