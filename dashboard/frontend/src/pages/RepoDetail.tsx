import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Execution, Repo, ReviewRun } from "../api/client";

export default function RepoDetail() {
  const { id } = useParams<{ id: string }>();
  const [repo, setRepo] = useState<Repo | null>(null);
  const [runs, setRuns] = useState<ReviewRun[]>([]);
  const [executions, setExecutions] = useState<Execution[]>([]);

  useEffect(() => {
    if (!id) return;
    const repoId = parseInt(id);
    api.getRepo(repoId).then(setRepo).catch(console.error);
    api.listReviewRuns(repoId).then(setRuns).catch(console.error);
    api.listExecutions(repoId).then(setExecutions).catch(console.error);
  }, [id]);

  if (!repo) return <p>Loading...</p>;

  return (
    <>
      <h1>{repo.full_name}</h1>
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="stat-label">Review</div>
          <div className="stat-value">{repo.review_enabled ? "ON" : "OFF"}</div>
        </div>
        <div className="card">
          <div className="stat-label">DevPilot</div>
          <div className="stat-value">{repo.devpilot_enabled ? "ON" : "OFF"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Status</div>
          <div className="stat-value">{repo.status}</div>
        </div>
      </div>

      <h2 style={{ marginTop: 24 }}>Review Runs</h2>
      {runs.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No review runs yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>PR</th>
              <th>Gate</th>
              <th>Risk</th>
              <th>Quality</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <Link to={`/review-runs/${run.id}`}>#{run.pr_number}</Link>
                </td>
                <td>
                  <span className={`badge badge-${run.gate_result === "PASS" ? "pass" : "fail"}`}>
                    {run.gate_result ?? "—"}
                  </span>
                </td>
                <td>{run.risk_score ?? "—"}</td>
                <td>{run.quality_score ?? "—"}</td>
                <td>{new Date(run.started_at).toLocaleDateString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 style={{ marginTop: 24 }}>DevPilot Executions</h2>
      {executions.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No executions yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Issue</th>
              <th>Status</th>
              <th>PR</th>
              <th>Attempts</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {executions.map((exec) => (
              <tr key={exec.id}>
                <td>
                  <Link to={`/executions/${exec.id}`}>#{exec.issue_number}</Link>
                </td>
                <td>
                  <span className={`badge badge-${exec.status === "pr_merged" ? "pass" : exec.status === "failed" ? "fail" : "error"}`}>
                    {exec.status}
                  </span>
                </td>
                <td>{exec.pr_number ? `#${exec.pr_number}` : "—"}</td>
                <td>{exec.attempts}</td>
                <td>{exec.started_at ? new Date(exec.started_at).toLocaleDateString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
