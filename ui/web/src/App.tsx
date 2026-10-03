import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Scans from "./pages/Scans";
import Research from "./pages/Research";
import Strategy from "./pages/Strategy";
import Trade from "./pages/Trade";

const PAGES = [
  { to: "/dashboard", label: "Dashboard", icon: "◧" },
  { to: "/scans", label: "Scans", icon: "⌕" },
  { to: "/research", label: "Research", icon: "✎" },
  { to: "/strategy", label: "Strategy", icon: "⇄" },
  { to: "/trade", label: "Trade", icon: "◎" },
];

export default function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">TA</span>
          <span>TradingAgents <span className="muted">local</span></span>
        </div>
        <nav>
          {PAGES.map((p) => (
            <NavLink key={p.to} to={p.to} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
              <span className="nav-icon" aria-hidden="true">{p.icon}</span>
              {p.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="badge badge-paper">PAPER</span>
          <span className="badge badge-locked" title="This build has no order endpoint">🔒 Orders disabled</span>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/scans" element={<Scans />} />
          <Route path="/research" element={<Research />} />
          <Route path="/strategy" element={<Strategy />} />
          <Route path="/trade" element={<Trade />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  );
}
