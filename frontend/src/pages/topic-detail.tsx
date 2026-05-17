import { useState } from "react"
import { Link, useParams } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, Flame, Plus, Play, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
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
import { categoryIndicatorClass } from "@/lib/topic-utils"

const HEALTH_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  healthy: "default",
  degraded: "secondary",
  banned: "destructive",
  expired: "destructive",
  unknown: "outline",
}

const ARTICLE_STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  draft: "outline",
  ready: "secondary",
  scheduled: "secondary",
  publishing: "default",
  published: "default",
  failed: "destructive",
  dead: "destructive",
}

const JOB_STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  pending: "outline",
  running: "secondary",
  retrying: "secondary",
  success: "default",
  failed: "destructive",
  dead: "destructive",
}

export default function TopicDetail() {
  const { id } = useParams<{ id: string }>()
  const topicId = id ? Number(id) : NaN

  const topicQ = useQuery({
    queryKey: ["topic", topicId],
    queryFn: () => api.getTopic(topicId),
    enabled: Number.isFinite(topicId),
  })

  if (!Number.isFinite(topicId)) {
    return <div className="text-sm text-destructive">非法的专题 ID</div>
  }
  if (topicQ.isLoading) {
    return <div className="text-sm text-muted-foreground">加载专题中...</div>
  }
  if (topicQ.isError || !topicQ.data) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-destructive">
          找不到专题 #{topicId}
        </p>
        <Button asChild variant="ghost">
          <Link to="/topics">
            <ArrowLeft className="size-4" /> 返回专题列表
          </Link>
        </Button>
      </div>
    )
  }

  const topic = topicQ.data
  const heat = Math.round((topic.heat_score ?? 0) * 100)

  return (
    <div className="space-y-6">
      <div>
        <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
          <Link to="/topics">
            <ArrowLeft className="size-4" /> 专题列表
          </Link>
        </Button>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-bold">{topic.name}</h1>
              <CategoryBadge category={topic.category} />
            </div>
            <div className="flex flex-wrap gap-1">
              {(topic.keywords ?? []).map((k) => (
                <Badge key={k} variant="secondary" className="font-normal">
                  {k}
                </Badge>
              ))}
            </div>
          </div>
          <div className="grid w-full max-w-md grid-cols-3 gap-3 sm:w-auto">
            <Stat label="账号" value={topic.account_count ?? 0} />
            <Stat label="文章" value={topic.article_count ?? 0} />
            <div className="rounded-md border p-3">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="flex items-center gap-1">
                  <Flame className="size-3" /> 热度
                </span>
                <span className="tabular-nums font-medium text-foreground">
                  {(topic.heat_score ?? 0).toFixed(3)}
                </span>
              </div>
              <Progress
                value={heat}
                indicatorClassName={categoryIndicatorClass(topic.category)}
                className="mt-2"
              />
            </div>
          </div>
        </div>
        {topic.target_platforms && topic.target_platforms.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1">
            <span className="text-muted-foreground text-xs">目标平台：</span>
            {topic.target_platforms.map((p) => (
              <Badge key={p} variant="outline" className="text-xs font-normal">
                {p}
              </Badge>
            ))}
          </div>
        )}
      </div>

      <Tabs defaultValue="accounts" className="space-y-4">
        <TabsList>
          <TabsTrigger value="accounts">账号</TabsTrigger>
          <TabsTrigger value="articles">文章</TabsTrigger>
          <TabsTrigger value="jobs">发布任务</TabsTrigger>
          <TabsTrigger value="metrics">数据指标</TabsTrigger>
        </TabsList>

        <TabsContent value="accounts">
          <AccountsTab topicId={topicId} />
        </TabsContent>
        <TabsContent value="articles">
          <ArticlesTab topicId={topicId} />
        </TabsContent>
        <TabsContent value="jobs">
          <JobsTab topicId={topicId} />
        </TabsContent>
        <TabsContent value="metrics">
          <MetricsTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-muted-foreground text-xs">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
    </div>
  )
}

// ---------- Tab: Accounts ----------

function AccountsTab({ topicId }: { topicId: number }) {
  const { data, isLoading } = useQuery({
    queryKey: ["accounts", { topic_id: topicId }],
    queryFn: () => api.accounts(topicId),
  })
  const [open, setOpen] = useState(false)

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>账号（{data?.length ?? 0}）</CardTitle>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button size="sm">
              <Plus className="size-4" /> 新建账号
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>新建账号</DialogTitle>
              <DialogDescription>
                账号将归属当前专题 #{topicId}。
              </DialogDescription>
            </DialogHeader>
            <CreateAccountForm
              defaultTopicId={topicId}
              onCreated={() => setOpen(false)}
              onCancel={() => setOpen(false)}
            />
          </DialogContent>
        </Dialog>
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
                <TableHead>状态</TableHead>
                <TableHead className="text-right">日配额</TableHead>
                <TableHead>上次发布</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="font-mono text-xs">{a.id}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{a.platform}</Badge>
                  </TableCell>
                  <TableCell className="font-medium">{a.nickname}</TableCell>
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
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="text-sm text-muted-foreground">
            该专题还没有账号。点右上"新建账号"开始。
          </p>
        )}
      </CardContent>
    </Card>
  )
}

// ---------- Tab: Articles ----------

function ArticlesTab({ topicId }: { topicId: number }) {
  const { data, isLoading } = useQuery({
    queryKey: ["articles", { topic_id: topicId }],
    queryFn: () => api.articles(topicId),
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>文章（{data?.length ?? 0}）</CardTitle>
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
                <TableHead>类型</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>目标平台</TableHead>
                <TableHead>排程</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="font-mono text-xs">{a.id}</TableCell>
                  <TableCell className="max-w-md truncate font-medium">
                    {a.title}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{a.content_type}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={ARTICLE_STATUS_VARIANT[a.status] ?? "outline"}>
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
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="text-sm text-muted-foreground">该专题还没有文章。</p>
        )}
      </CardContent>
    </Card>
  )
}

// ---------- Tab: Jobs ----------

function JobsTab({ topicId }: { topicId: number }) {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["jobs", { topic_id: topicId }],
    queryFn: () => api.jobs(topicId),
  })
  const runMut = useMutation({
    mutationFn: (jobId: number) => api.runJob(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  })
  const collectMut = useMutation({
    mutationFn: (jobId: number) => api.collectMetrics(jobId),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>发布任务（{data?.length ?? 0}）</CardTitle>
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
                <TableHead>状态</TableHead>
                <TableHead className="text-right">尝试</TableHead>
                <TableHead>结果</TableHead>
                <TableHead>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((j) => (
                <TableRow key={j.id}>
                  <TableCell className="font-mono text-xs">{j.id}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{j.platform}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={JOB_STATUS_VARIANT[j.status] ?? "outline"}>
                      {j.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {j.attempts}
                  </TableCell>
                  <TableCell className="max-w-xs truncate">
                    {j.platform_url ? (
                      <a
                        href={j.platform_url}
                        target="_blank"
                        rel="noopener"
                        className="text-primary underline-offset-4 hover:underline"
                      >
                        {j.platform_url}
                      </a>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => runMut.mutate(j.id)}
                        disabled={runMut.isPending}
                      >
                        <Play className="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => collectMut.mutate(j.id)}
                        disabled={collectMut.isPending}
                      >
                        <RefreshCw className="size-3" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="text-sm text-muted-foreground">该专题还没有发布任务。</p>
        )}
      </CardContent>
    </Card>
  )
}

// ---------- Tab: Metrics ----------

function MetricsTab() {
  return (
    <Card>
      <CardContent className="flex flex-col items-center justify-center gap-2 py-16 text-center">
        <div className="text-muted-foreground text-sm">
          专题数据指标视图（CTR / 阅读量 / 转化漏斗 / heat_score 趋势）
        </div>
        <Badge variant="outline" className="font-normal">
          待 P7-D 闭环上线
        </Badge>
      </CardContent>
    </Card>
  )
}
