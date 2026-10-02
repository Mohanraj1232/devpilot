import { api } from "../api/client";

export default function LoginPage() {
  const handleLogin = async () => {
    try {
      const { url } = await api.login();
      window.location.href = url;
    } catch {
      alert("Failed to initiate login");
    }
  };

  return (
    <div style={{ textAlign: "center", marginTop: "100px" }}>
      <h1>DevPilot Dashboard</h1>
      <p style={{ color: "var(--text-muted)", margin: "16px 0" }}>
        Sign in with GitHub to manage your repositories
      </p>
      <button className="btn btn-primary" onClick={handleLogin}>
        Sign in with GitHub
      </button>
    </div>
  );
}
