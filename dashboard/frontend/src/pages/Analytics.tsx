import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";

export default function AnalyticsPage() {
  const [findingsData, setFindingsData] = useState<{ date: string; count: number }[]>([]);
  const [categories, setCategories] = useState<{ category: string; count: number }[]>([]);
  const [successRate, setSuccessRate] = useState<{ total: number; successful: number; rate: number } | null>(null);
  const [trends, setTrends] = useState<{ full_name: string; total_runs: number; pass_rate: number }[]>([]);

  useEffect(() => {
    api.getAnalytics.findingsOverTime().then(setFindingsData).catch(console.error);
    api.getAnalytics.categories().then(setCategories).catch(console.error);
    api.getAnalytics.devpilotSuccess().then(setSuccessRate).catch(console.error);
    api.getAnalytics.repoTrends().then(setTrends).catch(console.error);
  }, []);

  return (
    <>
      <h1>Analytics</h1>

      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="stat-label">DevPilot Success Rate</div>
          <div className="stat-value">{successRate ? `${successRate.rate}%` : "—"}</div>
          <div className="stat-label">
            {successRate ? `${successRate.successful}/${successRate.total} executions` : ""}
          </div>
        </div>
      </div>

      <h2 style={{ marginTop: 24 }}>Findings Over Time</h2>
      <div className="card">
        {findingsData.length === 0 ? (
          <p style={{ color: "var(--text-muted)" }}>No data yet.</p>
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={findingsData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="date" stroke="var(--text-muted)" />
              <YAxis stroke="var(--text-muted)" />
              <Tooltip />
              <Line type="monotone" dataKey="count" stroke="var(--accent)" strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      <h2 style={{ marginTop: 24 }}>Finding Categories</h2>
      <div className="card">
        {categories.length === 0 ? (
          <p style={{ color: "var(--text-muted)" }}>No data yet.</p>
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={categories}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="category" stroke="var(--text-muted)" />
              <YAxis stroke="var(--text-muted)" />
              <Tooltip />
              <Bar dataKey="count" fill="var(--accent)" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      <h2 style={{ marginTop: 24 }}>Repository Trends</h2>
      {trends.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No data yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Repository</th>
              <th>Total Runs</th>
              <th>Pass Rate</th>
            </tr>
          </thead>
          <tbody>
            {trends.map((t) => (
              <tr key={t.full_name}>
                <td>{t.full_name}</td>
                <td>{t.total_runs}</td>
                <td>{t.pass_rate}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
