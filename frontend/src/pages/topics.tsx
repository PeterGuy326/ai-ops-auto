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

export default function Topics() {
  const { data, isLoading } = useQuery({ queryKey: ["topics"], queryFn: api.topics });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">主题</h1>
          <p className="text-sm text-muted-foreground">内容生产的源头，按主题归档</p>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>主题列表（{data?.length ?? 0}）</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">加载中...</p>
          ) : data && data.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>关键词</TableHead>
                  <TableHead className="text-right">热度</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="font-mono text-xs">{t.id}</TableCell>
                    <TableCell className="font-medium">{t.name}</TableCell>
                    <TableCell>
                      {(t.keywords ?? []).map((k) => (
                        <Badge key={k} variant="outline" className="mr-1">
                          {k}
                        </Badge>
                      ))}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {t.heat_score.toFixed(3)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {new Date(t.created_at).toLocaleString("zh-CN")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">
              还没有主题。POST <code className="rounded bg-muted px-1 py-0.5">/topics</code> 创建第一个。
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
