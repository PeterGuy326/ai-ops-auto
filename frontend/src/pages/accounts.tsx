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

const HEALTH_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  healthy: "default",
  degraded: "secondary",
  banned: "destructive",
  expired: "destructive",
  unknown: "outline",
};

export default function Accounts() {
  const { data, isLoading } = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">账号</h1>
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
                      <Badge variant={HEALTH_VARIANT[a.health] ?? "outline"}>{a.health}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{a.daily_quota}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {a.last_publish_at ? new Date(a.last_publish_at).toLocaleString("zh-CN") : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">还没有账号。</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
