import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Repo } from "../api/client";

export default function ReposPage() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.listRepos().then(setRepos).catch(console.error).finally(() => setLoading(false));
  }, []);

  if (loading) return <p>Loading...</p>;

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h1>Repositories</h1>
      </div>

      {repos.length === 0 ? (
        <div className="card">
          <p style={{ color: "var(--text-muted)" }}>No repositories registered yet.</p>
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Repository</th>
              <th>Review</th>
              <th>DevPilot</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {repos.map((repo) => (
              <tr key={repo.id}>
                <td>
                  <Link to={`/repos/${repo.id}`}>{repo.full_name}</Link>
                </td>
                <td>{repo.review_enabled ? "Enabled" : "Disabled"}</td>
                <td>{repo.devpilot_enabled ? "Enabled" : "Disabled"}</td>
                <td>
                  <span className={`badge badge-${repo.status === "active" ? "pass" : "error"}`}>
                    {repo.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
