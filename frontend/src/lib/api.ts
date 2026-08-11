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
// 失败必须显式抛给 React Query / 表单。控制台绝不能用 mock 数据伪造一次成功写入，
// 否则运营人员会误以为任务已经落库或发布。

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

// ---------- Topics ----------

async function listTopics(): Promise<Topic[]> {
  return request<Topic[]>("/topics")
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
  return request<Topic>("/topics", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

async function updateTopic(id: number, data: TopicUpdate): Promise<Topic> {
  return request<Topic>(`/topics/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  })
}

// ---------- Accounts ----------

async function listAccounts(topicId?: number | null): Promise<Account[]> {
  const qs = topicId ? `?topic_id=${topicId}` : ""
  return request<Account[]>(`/accounts${qs}`)
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
  const arr = await request<Article[]>(`/articles${qs}`)
  if (topicId != null) {
    return arr.filter((a) => a.topic_id === topicId)
  }
  return arr
}

// ---------- Jobs ----------

async function listJobs(topicId?: number | null): Promise<Job[]> {
  const arr = await request<Job[]>("/jobs")
  if (topicId != null) {
    return arr.filter((j) => j.topic_id === topicId)
  }
  return arr
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
