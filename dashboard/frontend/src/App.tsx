import { Link, Route, Routes } from "react-router-dom";
import AnalyticsPage from "./pages/Analytics";
import ExecutionDetail from "./pages/ExecutionDetail";
import LoginPage from "./pages/Login";
import RepoDetail from "./pages/RepoDetail";
import ReposPage from "./pages/Repos";
import ReviewRunDetail from "./pages/ReviewRunDetail";

export default function App() {
  return (
    <>
      <nav>
        <span className="logo">DevPilot</span>
        <Link to="/">Repositories</Link>
        <Link to="/analytics">Analytics</Link>
      </nav>
      <div className="container">
        <Routes>
          <Route path="/" element={<ReposPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/repos/:id" element={<RepoDetail />} />
          <Route path="/review-runs/:id" element={<ReviewRunDetail />} />
          <Route path="/executions/:id" element={<ExecutionDetail />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
        </Routes>
      </div>
    </>
  );
}
