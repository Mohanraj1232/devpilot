import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";

interface ExecutionData {
  id: number;
  issue_number: number;
  issue_hash: string;
  base_sha: string;
  status: string;
  branch: string | null;
  pr_number: number | null;
  attempts: number;
  test_status: string | null;
  failure_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  attempt_list: { attempt_no: number; diff_hash: string | null; test_status: string | null; summary: string | null }[];
}

export default function ExecutionDetail() {
  const { id } = useParams<{ id: string }>();
  const [exec, setExec] = useState<ExecutionData | null>(null);

  useEffect(() => {
    if (!id) return;
    api.getExecution(parseInt(id)).then(setExec).catch(console.error);
  }, [id]);

  if (!exec) return <p>Loading...</p>;

  return (
    <>
      <h1>DevPilot Execution — Issue #{exec.issue_number}</h1>
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="stat-label">Status</div>
          <div className="stat-value">{exec.status}</div>
        </div>
        <div className="card">
          <div className="stat-label">Branch</div>
          <div className="stat-value" style={{ fontSize: 16 }}>{exec.branch ?? "—"}</div>
        </div>
        <div className="card">
          <div className="stat-label">PR</div>
          <div className="stat-value">{exec.pr_number ? `#${exec.pr_number}` : "—"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Test Status</div>
          <div className="stat-value">{exec.test_status ?? "—"}</div>
        </div>
      </div>

      {exec.failure_reason && (
        <div className="card" style={{ borderColor: "var(--danger)", marginTop: 16 }}>
          <strong>Failure Reason:</strong> {exec.failure_reason}
        </div>
      )}

      <h2 style={{ marginTop: 24 }}>Attempts ({exec.attempts})</h2>
      {exec.attempt_list.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No attempts recorded.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Diff Hash</th>
              <th>Test Status</th>
              <th>Summary</th>
            </tr>
          </thead>
          <tbody>
            {exec.attempt_list.map((a) => (
              <tr key={a.attempt_no}>
                <td>{a.attempt_no}</td>
                <td style={{ fontFamily: "monospace" }}>{a.diff_hash ?? "—"}</td>
                <td>{a.test_status ?? "—"}</td>
                <td>{a.summary ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div style={{ marginTop: 24, color: "var(--text-muted)", fontSize: 14 }}>
        <p>Started: {exec.started_at ? new Date(exec.started_at).toLocaleString() : "—"}</p>
        <p>Finished: {exec.finished_at ? new Date(exec.finished_at).toLocaleString() : "In progress"}</p>
      </div>
    </>
  );
}
