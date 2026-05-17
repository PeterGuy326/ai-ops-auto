import { useQuery } from "@tanstack/react-query";
import {
  BookOpen,
  FileText,
  Users,
  ListChecks,
  TrendingUp,
  AlertTriangle,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  XAxis,
  YAxis,
} from "recharts";
import { api, type Job } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";

function StatCard({
  title,
  value,
  icon: Icon,
}: {
  title: string;
  value: number | string;
  icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
      </CardContent>
    </Card>
  );
}

// 把 jobs 按最近 7 天的"完成日"分桶
function buildTrend(jobs: Job[]): { date: string; success: number; failed: number }[] {
  const days: { date: string; success: number; failed: number }[] = [];
  const today = new Date();
  for (let i = 6; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const key = d.toISOString().slice(5, 10); // MM-DD
    days.push({ date: key, success: 0, failed: 0 });
  }
  for (const j of jobs) {
    const finished = j.finished_at ? j.finished_at.slice(5, 10) : null;
    if (!finished) continue;
    const bucket = days.find((d) => d.date === finished);
    if (!bucket) continue;
    if (j.status === "success") bucket.success += 1;
    if (j.status === "failed" || j.status === "dead") bucket.failed += 1;
  }
  return days;
}

// 按 platform 分组
function buildPlatformPie(jobs: Job[]): { platform: string; count: number; fill: string }[] {
  const map = new Map<string, number>();
  for (const j of jobs) {
    map.set(j.platform, (map.get(j.platform) ?? 0) + 1);
  }
  const palette = [
    "var(--chart-1)",
    "var(--chart-2)",
    "var(--chart-3)",
    "var(--chart-4)",
    "var(--chart-5)",
    "#8b5cf6",
    "#ec4899",
    "#06b6d4",
    "#84cc16",
    "#f97316",
  ];
  return Array.from(map.entries()).map(([platform, count], i) => ({
    platform,
    count,
    fill: palette[i % palette.length],
  }));
}

const trendConfig = {
  success: { label: "成功", color: "var(--chart-1)" },
  failed: { label: "失败", color: "var(--chart-5)" },
} satisfies ChartConfig;

const platformConfig = {
  count: { label: "任务数" },
} satisfies ChartConfig;

export default function Dashboard() {
  const topicsQ = useQuery({ queryKey: ["topics"], queryFn: api.topics });
  const accountsQ = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const articlesQ = useQuery({ queryKey: ["articles"], queryFn: api.articles });
  const jobsQ = useQuery({ queryKey: ["jobs"], queryFn: api.jobs });
  const heatQ = useQuery({ queryKey: ["heat-rank"], queryFn: () => api.heatRank(5) });

  const counts = {
    topics: topicsQ.data?.length ?? 0,
    articles: articlesQ.data?.length ?? 0,
    accounts: accountsQ.data?.length ?? 0,
    jobs: jobsQ.data?.length ?? 0,
    published: articlesQ.data?.filter((a) => a.status === "published").length ?? 0,
    failed: jobsQ.data?.filter((j) => j.status === "dead").length ?? 0,
  };

  const trendData = buildTrend(jobsQ.data ?? []);
  const platformData = buildPlatformPie(jobsQ.data ?? []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">总览</h1>
        <p className="text-sm text-muted-foreground">运营飞轮实时状态</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
        <StatCard title="主题" value={counts.topics} icon={BookOpen} />
        <StatCard title="文章" value={counts.articles} icon={FileText} />
        <StatCard title="账号" value={counts.accounts} icon={Users} />
        <StatCard title="任务" value={counts.jobs} icon={ListChecks} />
        <StatCard title="已发布" value={counts.published} icon={TrendingUp} />
        <StatCard title="失败" value={counts.failed} icon={AlertTriangle} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>近 7 天发布趋势</CardTitle>
            <CardDescription>按 finished_at 分桶，区分成功 / 失败</CardDescription>
          </CardHeader>
          <CardContent>
            <ChartContainer config={trendConfig} className="aspect-[3/1]">
              <BarChart data={trendData} accessibilityLayer>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={8} />
                <YAxis tickLine={false} axisLine={false} width={32} />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Bar dataKey="success" fill="var(--color-success)" radius={4} />
                <Bar dataKey="failed" fill="var(--color-failed)" radius={4} />
              </BarChart>
            </ChartContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>平台分布</CardTitle>
            <CardDescription>所有任务按平台聚合</CardDescription>
          </CardHeader>
          <CardContent>
            {platformData.length > 0 ? (
              <ChartContainer config={platformConfig} className="aspect-square max-h-[260px]">
                <PieChart>
                  <ChartTooltip content={<ChartTooltipContent nameKey="platform" />} />
                  <Pie data={platformData} dataKey="count" nameKey="platform" innerRadius={50}>
                    {platformData.map((entry) => (
                      <Cell key={entry.platform} fill={entry.fill} />
                    ))}
                  </Pie>
                </PieChart>
              </ChartContainer>
            ) : (
              <p className="text-sm text-muted-foreground">还没有任务数据</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <TrendingUp className="size-4" /> 热门主题（heat_score 倒序）
          </CardTitle>
          <CardDescription>数据回流飞轮的输出，驱动下一轮选题</CardDescription>
        </CardHeader>
        <CardContent>
          {heatQ.data && heatQ.data.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>主题</TableHead>
                  <TableHead>关键词</TableHead>
                  <TableHead className="text-right">热度分</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {heatQ.data.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="font-medium">{t.name}</TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {(t.keywords ?? []).slice(0, 3).map((k) => (
                        <Badge key={k} variant="secondary" className="mr-1">
                          {k}
                        </Badge>
                      ))}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {t.heat_score.toFixed(3)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">还没有热度数据。先发布几条试试。</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
