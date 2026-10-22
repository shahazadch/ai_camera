import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import Layout from "./components/Layout";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import CamerasPage from "./pages/CamerasPage";
import RulesPage from "./pages/RulesPage";
import EventsPage from "./pages/EventsPage";
import AlertsPage from "./pages/AlertsPage";
import PersonsPage from "./pages/PersonsPage";
import PersonEnrollPage from "./pages/PersonEnrollPage";
import AttendancePage from "./pages/AttendancePage";
import VisitorsPage from "./pages/VisitorsPage";
import UnknownPage from "./pages/UnknownPage";
import ZonesPage from "./pages/ZonesPage";
import CrowdPage from "./pages/CrowdPage";
import ObjectsPage from "./pages/ObjectsPage";
import RiskPage from "./pages/RiskPage";
import SafetyEventsPage from "./pages/SafetyEventsPage";
import EmergencyPage from "./pages/EmergencyPage";
import NotificationsPage from "./pages/NotificationsPage";
import RecordingsPage from "./pages/RecordingsPage";
import EvidencePage from "./pages/EvidencePage";
import SystemPage from "./pages/SystemPage";
import VisionPage from "./pages/VisionPage";
import ProfilePage from "./pages/ProfilePage";

function PrivateRoute({ children }: { children: JSX.Element }) {
  const { isAuthenticated } = useAuth();
  return isAuthenticated ? children : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <PrivateRoute>
            <Layout />
          </PrivateRoute>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="cameras" element={<CamerasPage />} />
        <Route path="rules" element={<RulesPage />} />
        <Route path="vision" element={<VisionPage />} />
        <Route path="events" element={<EventsPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="persons" element={<PersonsPage />} />
        <Route path="persons/new" element={<PersonEnrollPage />} />
        <Route path="attendance" element={<AttendancePage />} />
        <Route path="visitors" element={<VisitorsPage />} />
        <Route path="unknown" element={<UnknownPage />} />
        <Route path="zones" element={<ZonesPage />} />
        <Route path="crowd" element={<CrowdPage />} />
        <Route path="objects" element={<ObjectsPage />} />
        <Route path="risk" element={<RiskPage />} />
        <Route path="fire" element={<SafetyEventsPage kind="fire" />} />
        <Route path="smoke" element={<SafetyEventsPage kind="smoke" />} />
        <Route path="weapons" element={<SafetyEventsPage kind="weapon" />} />
        <Route path="falls" element={<SafetyEventsPage kind="fall" />} />
        <Route path="ppe" element={<SafetyEventsPage kind="ppe" />} />
        <Route path="emergency" element={<EmergencyPage />} />
        <Route path="notifications" element={<NotificationsPage />} />
        <Route path="recordings" element={<RecordingsPage />} />
        <Route path="evidence" element={<EvidencePage />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="profile" element={<ProfilePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
