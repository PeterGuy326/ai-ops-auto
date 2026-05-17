// FastAPI client — 直连 /api/* 端点。
// Vite dev server 通过 vite.config.ts 的 proxy 把 /api/* 转发到 http://127.0.0.1:8000

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

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

export type Topic = {
  id: number;
  name: string;
  keywords: string[];
  heat_score: number;
  created_at: string;
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
};

export const api = {
  health: () => request<{ ok: boolean }>("/health"),
  topics: () => request<Topic[]>("/topics"),
  heatRank: (limit = 10) => request<Topic[]>(`/topics/heat-rank?limit=${limit}`),
  accounts: () => request<Account[]>("/accounts"),
  runJob: (id: number) => request(`/jobs/${id}/run`, { method: "POST" }),
  collectMetrics: (id: number) => request(`/jobs/${id}/collect`, { method: "POST" }),
  // 后端尚无 /articles GET 和 /jobs GET 列表，下面是占位，等 backend 补
  articles: () => request<Article[]>("/articles").catch(() => [] as Article[]),
  jobs: () => request<Job[]>("/jobs").catch(() => [] as Job[]),
};
