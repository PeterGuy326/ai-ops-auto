import { useQuery } from "@tanstack/react-query";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  draft: "outline",
  ready: "secondary",
  scheduled: "secondary",
  publishing: "default",
  published: "default",
  failed: "destructive",
  dead: "destructive",
};

export default function Articles() {
  const { data, isLoading } = useQuery({ queryKey: ["articles"], queryFn: api.articles });

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">文章</h1>
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
                    <TableCell className="font-medium max-w-md truncate">{a.title}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{a.content_type}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={STATUS_VARIANT[a.status] ?? "outline"}>{a.status}</Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {(a.target_platforms ?? []).join(", ")}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {a.scheduled_at ? new Date(a.scheduled_at).toLocaleString("zh-CN") : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">还没有文章。</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
