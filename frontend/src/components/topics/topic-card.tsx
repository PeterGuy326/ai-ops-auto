import { Link } from "react-router-dom"
import { FileText, Users, Flame } from "lucide-react"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import {
  categoryAccentClass,
  categoryIndicatorClass,
} from "@/lib/topic-utils"
import { CategoryBadge } from "./category-badge"
import { cn } from "@/lib/utils"
import type { Topic } from "@/lib/api"

export function TopicCard({ topic }: { topic: Topic }) {
  const heat = Math.round((topic.heat_score ?? 0) * 100)
  const accountCount = topic.account_count ?? 0
  const articleCount = topic.article_count ?? 0

  return (
    <Link
      to={`/topics/${topic.id}`}
      className="group focus-visible:outline-ring focus-visible:outline-2 focus-visible:outline-offset-2 rounded-lg"
    >
      <Card className="relative overflow-hidden transition-all group-hover:border-primary/50 group-hover:shadow-md h-full">
        {/* 分类装饰条 */}
        <div
          className={cn(
            "absolute inset-x-0 top-0 h-1",
            categoryAccentClass(topic.category),
          )}
        />
        <CardHeader>
          <div className="flex items-start justify-between gap-2">
            <CardTitle className="text-lg leading-tight">{topic.name}</CardTitle>
            <CategoryBadge category={topic.category} />
          </div>
          <CardDescription className="line-clamp-1">
            {(topic.keywords ?? []).slice(0, 4).map((k) => (
              <Badge
                key={k}
                variant="secondary"
                className="mr-1 font-normal"
              >
                {k}
              </Badge>
            ))}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="flex items-center gap-2">
              <Users className="size-3.5 text-muted-foreground" />
              <span className="tabular-nums">{accountCount}</span>
              <span className="text-muted-foreground text-xs">账号</span>
            </div>
            <div className="flex items-center gap-2">
              <FileText className="size-3.5 text-muted-foreground" />
              <span className="tabular-nums">{articleCount}</span>
              <span className="text-muted-foreground text-xs">文章</span>
            </div>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-1 text-muted-foreground">
                <Flame className="size-3" />
                热度分
              </span>
              <span className="tabular-nums font-medium">
                {(topic.heat_score ?? 0).toFixed(3)}
              </span>
            </div>
            <Progress
              value={heat}
              indicatorClassName={categoryIndicatorClass(topic.category)}
            />
          </div>
          {topic.target_platforms && topic.target_platforms.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {topic.target_platforms.map((p) => (
                <Badge key={p} variant="outline" className="text-xs font-normal">
                  {p}
                </Badge>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </Link>
  )
}
