import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Finding } from "../api/client";

interface RunDetail {
  id: number;
  pr_number: number;
  head_sha: string;
  status: string;
  risk_score: number | null;
  quality_score: number | null;
  gate_result: string | null;
  started_at: string;
  findings: Finding[];
}

export default function ReviewRunDetail() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);

  useEffect(() => {
    if (!id) return;
    api.getReviewRun(parseInt(id)).then(setRun).catch(console.error);
  }, [id]);

  if (!run) return <p>Loading...</p>;

  const severityOrder: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
  const sortedFindings = [...run.findings].sort(
    (a, b) => (severityOrder[a.severity] ?? 5) - (severityOrder[b.severity] ?? 5),
  );

  return (
    <>
      <h1>Review Run — PR #{run.pr_number}</h1>
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="stat-label">Gate</div>
          <div className="stat-value">
            <span className={`badge badge-${run.gate_result === "PASS" ? "pass" : "fail"}`}>
              {run.gate_result ?? "—"}
            </span>
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Risk Score</div>
          <div className="stat-value">{run.risk_score ?? "—"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Quality Score</div>
          <div className="stat-value">{run.quality_score ?? "—"}</div>
        </div>
      </div>

      <h2 style={{ marginTop: 24 }}>Findings ({run.findings.length})</h2>
      {sortedFindings.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No findings.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Category</th>
              <th>Title</th>
              <th>File</th>
              <th>Tool</th>
              <th>Resolution</th>
            </tr>
          </thead>
          <tbody>
            {sortedFindings.map((f) => (
              <tr key={f.id}>
                <td>
                  <span
                    className={`badge badge-${f.severity === "critical" || f.severity === "high" ? "fail" : f.severity === "medium" ? "error" : "pass"}`}
                  >
                    {f.severity}
                  </span>
                </td>
                <td>{f.category}</td>
                <td>{f.title}</td>
                <td>
                  {f.file}
                  {f.line_start ? `:${f.line_start}` : ""}
                </td>
                <td>{f.tool}</td>
                <td>{f.resolution}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
