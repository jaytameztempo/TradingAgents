import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import Dashboard from "./pages/Dashboard";
import Scans from "./pages/Scans";
import Research from "./pages/Research";
import Strategy from "./pages/Strategy";
import Trade from "./pages/Trade";
import Settings from "./pages/Settings";

const PAGES = [
  { to: "/dashboard", label: "Dashboard", icon: "◧" },
  { to: "/scans", label: "Scans", icon: "⌕" },
  { to: "/research", label: "Research", icon: "✎" },
  { to: "/strategy", label: "Strategy", icon: "⇄" },
  { to: "/trade", label: "Trade", icon: "◎" },
  { to: "/settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 30_000 });
  const autoSend = health.data?.orders_enabled;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">B</span>
          <span className="brand-name">
            BATS
            <span className="brand-sub">Brightrise Algorithmic Trading System</span>
          </span>
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
          {autoSend ? (
            <NavLink to="/settings" className="badge badge-paper" title="The scheduler sends paper orders in your trading sessions">
              ⚡ Paper auto-send on
            </NavLink>
          ) : (
            <NavLink to="/settings" className="badge badge-locked" title="No order is sent; paper auto-send is off in Settings">
              🔒 Auto-send off
            </NavLink>
          )}
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
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  );
}
