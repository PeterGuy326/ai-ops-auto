import { Badge } from "@/components/ui/badge"
import { categoryBadgeClass, categoryLabel } from "@/lib/topic-utils"
import { cn } from "@/lib/utils"

export function CategoryBadge({
  category,
  className,
}: {
  category?: string | null
  className?: string
}) {
  return (
    <Badge
      variant="outline"
      className={cn("border", categoryBadgeClass(category), className)}
    >
      {categoryLabel(category)}
    </Badge>
  )
}
