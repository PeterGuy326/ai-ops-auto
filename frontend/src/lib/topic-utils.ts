// Topic 视觉/语义工具：分类配色 + 文案
//
// 颜色策略：分类 badge 用 *-500/15 底 + *-300 字 + *-500/30 border（深色优先），
// 浅色 fallback 用 *-100 底 + *-700 字。下面的 class 都用 tailwind 直写，避免
// 引入新的 design token 文件。

import type { TopicCategory } from "./api"

export const CATEGORY_LABEL: Record<TopicCategory, string> = {
  tech: "科技",
  exam: "考试",
  sports: "体育",
  lifestyle: "生活",
}

// badge 用：背景 + 文字 + 边框，一套搞定
export const CATEGORY_BADGE_CLASS: Record<TopicCategory, string> = {
  tech: "bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-500/15 dark:text-blue-300 dark:border-blue-500/30",
  exam: "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-500/15 dark:text-amber-300 dark:border-amber-500/30",
  sports: "bg-emerald-100 text-emerald-700 border-emerald-200 dark:bg-emerald-500/15 dark:text-emerald-300 dark:border-emerald-500/30",
  lifestyle: "bg-pink-100 text-pink-700 border-pink-200 dark:bg-pink-500/15 dark:text-pink-300 dark:border-pink-500/30",
}

// 进度条 indicator 用：纯色填充
export const CATEGORY_INDICATOR_CLASS: Record<TopicCategory, string> = {
  tech: "bg-blue-500",
  exam: "bg-amber-500",
  sports: "bg-emerald-500",
  lifestyle: "bg-pink-500",
}

// 卡片上沿装饰条（让分类差异化更显眼）
export const CATEGORY_ACCENT_CLASS: Record<TopicCategory, string> = {
  tech: "bg-blue-500",
  exam: "bg-amber-500",
  sports: "bg-emerald-500",
  lifestyle: "bg-pink-500",
}

export function categoryLabel(c?: string | null): string {
  if (!c) return "未分类"
  return CATEGORY_LABEL[c as TopicCategory] ?? c
}

export function categoryBadgeClass(c?: string | null): string {
  if (!c) return "bg-muted text-muted-foreground border-border"
  return CATEGORY_BADGE_CLASS[c as TopicCategory] ?? "bg-muted text-muted-foreground border-border"
}

export function categoryIndicatorClass(c?: string | null): string {
  if (!c) return "bg-muted-foreground"
  return CATEGORY_INDICATOR_CLASS[c as TopicCategory] ?? "bg-muted-foreground"
}

export function categoryAccentClass(c?: string | null): string {
  if (!c) return "bg-muted-foreground/40"
  return CATEGORY_ACCENT_CLASS[c as TopicCategory] ?? "bg-muted-foreground/40"
}
