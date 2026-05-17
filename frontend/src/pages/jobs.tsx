import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Play, RefreshCw } from "lucide-react";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  pending: "outline",
  running: "secondary",
  retrying: "secondary",
  success: "default",
  failed: "destructive",
  dead: "destructive",
};

export default function Jobs() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["jobs"], queryFn: api.jobs });

  const runMut = useMutation({
    mutationFn: (id: number) => api.runJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const collectMut = useMutation({
    mutationFn: (id: number) => api.collectMetrics(id),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">任务</h1>
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
                  <TableHead>开始</TableHead>
                  <TableHead>结果链接</TableHead>
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
                      <Badge variant={STATUS_VARIANT[j.status] ?? "outline"}>{j.status}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{j.attempts}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {j.started_at ? new Date(j.started_at).toLocaleString("zh-CN") : "—"}
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
            <p className="text-sm text-muted-foreground">还没有任务。</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
