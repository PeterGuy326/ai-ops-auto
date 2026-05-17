// Mock 数据 — 当后端 P7-A 的扩展字段（category / target_platforms / account_count
// / article_count）还没就绪时使用。后端返回真数据后，api.ts 的 fallback 块
// 会自然跳过这里（详见 TODO[P7-A-WIRE] 标记）。
//
// 三个内置专题对应产品文档定义：tech / exam / sports；外加一个 lifestyle 演示分类色。

import type { Topic, TopicCategory } from "./api"

export const MOCK_TOPICS: Topic[] = [
  {
    id: 1001,
    name: "AI 工程化",
    category: "tech",
    keywords: ["LLM", "Agent", "RAG", "MLOps", "Prompt"],
    target_platforms: ["zhihu", "juejin", "wechat_mp"],
    heat_score: 0.82,
    account_count: 4,
    article_count: 27,
    created_at: "2026-04-08T09:12:00Z",
  },
  {
    id: 1002,
    name: "考研冲刺",
    category: "exam",
    keywords: ["考研", "数学一", "英语二", "政治", "押题"],
    target_platforms: ["xiaohongshu", "douyin", "wechat_mp"],
    heat_score: 0.74,
    account_count: 3,
    article_count: 41,
    created_at: "2026-03-22T01:30:00Z",
  },
  {
    id: 1003,
    name: "NBA 季后赛",
    category: "sports",
    keywords: ["NBA", "季后赛", "MVP", "赛后复盘"],
    target_platforms: ["weibo", "douyin", "toutiao"],
    heat_score: 0.91,
    account_count: 5,
    article_count: 63,
    created_at: "2026-02-14T13:45:00Z",
  },
  {
    id: 1004,
    name: "都市轻生活",
    category: "lifestyle",
    keywords: ["咖啡", "city walk", "周末", "穿搭"],
    target_platforms: ["xiaohongshu", "weibo"],
    heat_score: 0.56,
    account_count: 2,
    article_count: 18,
    created_at: "2026-05-01T07:20:00Z",
  },
]

export const MOCK_CATEGORIES: TopicCategory[] = ["tech", "exam", "sports", "lifestyle"]

export function mockTopicById(id: number): Topic | undefined {
  return MOCK_TOPICS.find((t) => t.id === id)
}
