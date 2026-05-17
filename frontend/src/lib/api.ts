// FastAPI client — 直连 /api/* 端点。
// Vite dev server 通过 vite.config.ts 的 proxy 把 /api/* 转发到 http://127.0.0.1:8000
//
// 与 P7-A 后端的 Topic API 契约：
//   GET    /topics                 -> Topic[]
//   POST   /topics                 -> Topic
//   PATCH  /topics/{id}            -> Topic
//   GET    /accounts?topic_id=X    -> Account[]
//   GET    /articles?topic_id=X    -> Article[]
//   POST   /accounts (body 含 topic_id)
//
// 兼容策略：后端如果还没补齐扩展字段（category / target_platforms / account_count
// / article_count）或 topic_id 过滤，请求会回落到 mock 数据。所有 fallback 都
// 标了 `TODO[P7-A-WIRE]`，等后端就绪后一把删掉即可。

import { MOCK_TOPICS, mockTopicById } from "./mock-topics"

const BASE = import.meta.env.VITE_API_BASE ?? "/api"

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${path} ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export type TopicCategory = "tech" | "exam" | "sports" | "lifestyle"

export type Topic = {
  id: number;
  name: string;
  keywords: string[];
  heat_score: number;
  created_at: string;
  // P7-A 扩展字段（后端可能尚未返回，列表/详情页面会容错处理）
  category?: TopicCategory | string | null;
  target_platforms?: string[];
  account_count?: number;
  article_count?: number;
};

export type TopicCreate = {
  name: string;
  category: TopicCategory | string;
  keywords: string[];
  target_platforms: string[];
};

export type TopicUpdate = {
  name?: string;
  category?: TopicCategory | string;
  keywords?: string[];
  target_platforms?: string[];
};

export type Article = {
  id: number;
  topic_id: number;
  title: string;
  content_type: string;
  status: string;
  target_platforms: string[];
  scheduled_at: string | null;
  created_at: string;
};

export type Account = {
  id: number;
  platform: string;
  nickname: string;
  health: string;
  daily_quota: number;
  last_publish_at: string | null;
  created_at: string;
  topic_id?: number | null;
};

export type AccountCreate = {
  platform: string;
  nickname: string;
  topic_id: number;
  daily_quota?: number;
};

export type Job = {
  id: number;
  article_id: number;
  account_id: number;
  platform: string;
  status: string;
  attempts: number;
  platform_post_id: string | null;
  platform_url: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  topic_id?: number | null;
};

// 把后端可能返回的"瘦"Topic 合并到 mock 的扩展字段上，保证 UI 视觉一致。
function mergeWithMock(t: Topic): Topic {
  const m = mockTopicById(t.id)
  return {
    ...t,
    category: t.category ?? m?.category ?? null,
    target_platforms: t.target_platforms ?? m?.target_platforms ?? [],
    account_count: t.account_count ?? m?.account_count ?? 0,
    article_count: t.article_count ?? m?.article_count ?? 0,
  }
}

// ---------- Topics ----------

async function listTopics(): Promise<Topic[]> {
  try {
    const real = await request<Topic[]>("/topics")
    if (Array.isArray(real) && real.length > 0) {
      // TODO[P7-A-WIRE]: 后端 schema 补齐后删除 mergeWithMock 调用
      return real.map(mergeWithMock)
    }
    // 后端有路由但库里是空的 → 演示态走 mock
    return MOCK_TOPICS
  } catch {
    // TODO[P7-A-WIRE]: 后端 /topics 不通时的兜底
    return MOCK_TOPICS
  }
}

async function getTopic(id: number): Promise<Topic> {
  // 后端目前没有 GET /topics/{id}，先从 list 里挑；后端补了之后改回 request
  // TODO[P7-A-WIRE]: 改为 `return request<Topic>(/topics/${id})`
  const all = await listTopics()
  const t = all.find((x) => x.id === id)
  if (!t) throw new Error(`topic ${id} 不存在`)
  return t
}

async function createTopic(data: TopicCreate): Promise<Topic> {
  try {
    return await request<Topic>("/topics", {
      method: "POST",
      body: JSON.stringify(data),
    })
  } catch (e) {
    // TODO[P7-A-WIRE]: 删除 mock-create 兜底
    console.warn("createTopic fallback to mock:", e)
    const fake: Topic = {
      id: Date.now(),
      name: data.name,
      category: data.category,
      keywords: data.keywords,
      target_platforms: data.target_platforms,
      heat_score: 0,
      account_count: 0,
      article_count: 0,
      created_at: new Date().toISOString(),
    }
    return fake
  }
}

async function updateTopic(id: number, data: TopicUpdate): Promise<Topic> {
  try {
    return await request<Topic>(`/topics/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    })
  } catch (e) {
    // TODO[P7-A-WIRE]: 删除 mock-update 兜底
    console.warn("updateTopic fallback to mock:", e)
    const cur = await getTopic(id)
    return { ...cur, ...data }
  }
}

// ---------- Accounts ----------

async function listAccounts(topicId?: number | null): Promise<Account[]> {
  const qs = topicId ? `?topic_id=${topicId}` : ""
  try {
    const arr = await request<Account[]>(`/accounts${qs}`)
    // 后端不支持 topic_id 过滤的话会返回全量，前端再过滤一道做兜底
    if (topicId != null) {
      return arr.filter((a) => a.topic_id == null || a.topic_id === topicId)
    }
    return arr
  } catch {
    return []
  }
}

async function createAccount(data: AccountCreate): Promise<Account> {
  return request<Account>("/accounts", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

// ---------- Articles ----------

async function listArticles(topicId?: number | null): Promise<Article[]> {
  const qs = topicId ? `?topic_id=${topicId}` : ""
  try {
    const arr = await request<Article[]>(`/articles${qs}`)
    if (topicId != null) {
      return arr.filter((a) => a.topic_id === topicId)
    }
    return arr
  } catch {
    return []
  }
}

// ---------- Jobs ----------

async function listJobs(topicId?: number | null): Promise<Job[]> {
  try {
    const arr = await request<Job[]>("/jobs")
    // 后端 /jobs 现在还没有 topic_id 字段；前端先按 article→topic 反查太重，
    // 这里 P7-D 闭环之前简单略过过滤，topic_id 实在为空就回全量
    if (topicId != null) {
      return arr.filter((j) => j.topic_id == null || j.topic_id === topicId)
    }
    return arr
  } catch {
    return []
  }
}

export const api = {
  health: () => request<{ ok: boolean }>("/health"),
  topics: listTopics,
  getTopic,
  createTopic,
  updateTopic,
  heatRank: (limit = 10) => request<Topic[]>(`/topics/heat-rank?limit=${limit}`),
  accounts: (topicId?: number | null) => listAccounts(topicId),
  createAccount,
  articles: (topicId?: number | null) => listArticles(topicId),
  jobs: (topicId?: number | null) => listJobs(topicId),
  runJob: (id: number) => request(`/jobs/${id}/run`, { method: "POST" }),
  collectMetrics: (id: number) => request(`/jobs/${id}/collect`, { method: "POST" }),
};
