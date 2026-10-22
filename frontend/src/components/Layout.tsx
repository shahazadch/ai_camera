import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import BackgroundCameraMonitor from "./BackgroundCameraMonitor";
import RuleHitsListener from "./RuleHitsListener";
import Icon from "./Icon";

type NavItem = { to: string; label: string; icon: string; end?: boolean };

const SIDEBAR_KEY = "sentinelvision.sidebar-collapsed";

const navGroups: { title: string; items: NavItem[] }[] = [
  {
    title: "",
    items: [
      { to: "/", label: "Overview", icon: "overview", end: true },
      { to: "/cameras", label: "Cameras", icon: "camera" },
      { to: "/rules", label: "Camera Rules", icon: "bell" },
      { to: "/recordings", label: "Recordings", icon: "recording" },
      { to: "/vision", label: "AI Vision", icon: "vision" },
      { to: "/alerts", label: "Alerts", icon: "alert" },
      { to: "/persons", label: "People", icon: "people" },
      { to: "/unknown", label: "Unknown", icon: "unknown" },
      { to: "/emergency", label: "Emergency", icon: "emergency" },
    ],
  },
];

function Clock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <time className="clock" dateTime={now.toISOString()} title={now.toLocaleString()}>
      {now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
    </time>
  );
}

const PAGE_TITLES: Record<string, string> = {
  "/": "Overview",
  "/cameras": "Cameras",
  "/recordings": "Recordings",
  "/vision": "AI Vision",
  "/alerts": "Alerts",
  "/persons": "People",
  "/persons/new": "Enroll Person",
  "/unknown": "Unknown",
  "/emergency": "Emergency",
  "/profile": "Profile",
};

function pageTitle(pathname: string) {
  if (PAGE_TITLES[pathname]) return PAGE_TITLES[pathname];
  const match = Object.keys(PAGE_TITLES)
    .filter((key) => key !== "/" && pathname.startsWith(key))
    .sort((a, b) => b.length - a.length)[0];
  return match ? PAGE_TITLES[match] : "SentinelVision";
}

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false;
  }
}

export default function Layout() {
  const { logout, user } = useAuth();
  const location = useLocation();
  const initials = (user || "AD").slice(0, 2).toUpperCase();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [collapsed]);

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    document.body.classList.toggle("menu-open", mobileOpen);
    return () => document.body.classList.remove("menu-open");
  }, [mobileOpen]);

  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mobileOpen]);

  const toggleDesktop = () => setCollapsed((value) => !value);
  const toggleMobile = () => setMobileOpen((value) => !value);

  return (
    <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""} ${mobileOpen ? "sidebar-mobile-open" : ""}`}>
      {mobileOpen && <button className="sidebar-backdrop" aria-label="Close menu" onClick={() => setMobileOpen(false)} />}

      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark">SV</span>
          <div className="brand-text">
            <strong>SentinelVision</strong>
            <small>AI Security Platform</small>
          </div>
          <button
            type="button"
            className="sidebar-collapse-btn mobile-only"
            onClick={() => setMobileOpen(false)}
            title="Close menu"
            aria-label="Close menu"
          >
            <Icon name="close" />
          </button>
        </div>

        <nav>
          {navGroups.map((group) => (
            <div key={group.title || "main"} className="nav-group">
              {group.title ? <div className="nav-group-label">{group.title}</div> : null}
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  title={item.label}
                  className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
                >
                  <Icon name={item.icon} className="nav-icon" />
                  <span className="nav-label">{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <button className="logout-btn" onClick={logout} title="Sign out">
          <Icon name="logout" className="nav-icon" />
          <span className="nav-label">Sign out</span>
        </button>
      </aside>

      <main className="main-content">
        <div className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="sidebar-toggle-btn"
              onClick={() => {
                if (window.matchMedia("(max-width: 768px)").matches) toggleMobile();
                else toggleDesktop();
              }}
              title={collapsed ? "Open sidebar" : "Close sidebar"}
              aria-label={collapsed ? "Open sidebar" : "Close sidebar"}
            >
              <Icon name="menu" />
            </button>
            <div className="topbar-title-block">
              <strong className="topbar-page">{pageTitle(location.pathname)}</strong>
              <div className="status-pills">
                <span className="live-pill" title="System live">
                  <span className="live-dot" /> Live
                </span>
                <BackgroundCameraMonitor />
                <RuleHitsListener />
              </div>
            </div>
          </div>
          <div className="topbar-right">
            <Clock />
            <Link to="/profile" className="user-chip" title="Open profile" aria-label="Open profile">
              <span className="avatar">{initials}</span>
              <span className="user-name">{user || "Admin"}</span>
            </Link>
          </div>
        </div>
        <Outlet />
      </main>
    </div>
  );
}
