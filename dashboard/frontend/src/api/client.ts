const BASE = "/api/v1";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    throw new Error(`API error: ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export interface Repo {
  id: number;
  github_repo_id: number;
  owner: string;
  name: string;
  full_name: string;
  review_enabled: boolean;
  devpilot_enabled: boolean;
  status: string;
  created_at: string;
}

export interface ReviewRun {
  id: number;
  repo_id: number;
  pr_number: number;
  head_sha: string;
  status: string;
  risk_score: number | null;
  quality_score: number | null;
  gate_result: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Finding {
  id: number;
  source: string;
  tool: string;
  category: string;
  severity: string;
  file: string;
  line_start: number | null;
  title: string;
  fingerprint: string;
  resolution: string;
}

export interface Execution {
  id: number;
  issue_number: number;
  status: string;
  branch: string | null;
  pr_number: number | null;
  attempts: number;
  test_status: string | null;
  failure_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface User {
  id: number;
  github_id: number;
  login: string;
  avatar_url: string | null;
}

export const api = {
  getMe: () => request<User>("/auth/me"),
  login: () => request<{ url: string }>("/auth/login"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  listRepos: () => request<Repo[]>("/repos"),
  getRepo: (id: number) => request<Repo>(`/repos/${id}`),
  registerRepo: (data: { github_repo_id: number; owner: string; name: string; full_name: string }) =>
    request<Repo>("/repos", { method: "POST", body: JSON.stringify(data) }),
  updateRepo: (id: number, data: { review_enabled?: boolean; devpilot_enabled?: boolean }) =>
    request<Repo>(`/repos/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  listReviewRuns: (repoId: number) => request<ReviewRun[]>(`/repos/${repoId}/review-runs`),
  getReviewRun: (id: number) => request<ReviewRun & { findings: Finding[]; checks: unknown[] }>(`/review-runs/${id}`),

  listExecutions: (repoId: number) => request<Execution[]>(`/repos/${repoId}/devpilot-executions`),
  getExecution: (id: number) => request<Execution & { attempt_list: unknown[] }>(`/devpilot-executions/${id}`),

  getAnalytics: {
    findingsOverTime: (repoId?: number) =>
      request<{ date: string; count: number }[]>(`/analytics/findings-over-time${repoId ? `?repo_id=${repoId}` : ""}`),
    categories: (repoId?: number) =>
      request<{ category: string; count: number }[]>(`/analytics/categories${repoId ? `?repo_id=${repoId}` : ""}`),
    gateFailures: () => request<{ reason: string; count: number }[]>("/analytics/gate-failures"),
    devpilotSuccess: (repoId?: number) =>
      request<{ total: number; successful: number; rate: number }>(`/analytics/devpilot-success${repoId ? `?repo_id=${repoId}` : ""}`),
    repoTrends: () => request<{ repo_id: number; full_name: string; total_runs: number; pass_rate: number }[]>("/analytics/repo-trends"),
  },
};
