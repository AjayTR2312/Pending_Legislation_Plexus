import { useState } from "react";
import UploadPage from "./pages/UploadPage";
import JobPage from "./pages/JobPage";
import JobsDashboard from "./pages/JobsDashboard";
import BatchPage from "./pages/BatchPage";
import ReviewWorkspace from "./components/ReviewWorkspace";
import SessionsPanel from "./components/SessionsPanel";
import DownloadsPanel from "./components/DownloadsPanel";
import RulesPanel from "./components/RulesPanel";
import RunningJobs from "./components/RunningJobs";
import Icon from "./components/Icon";
import { useTheme } from "./hooks/useTheme";
import type { Job, ReviewTarget } from "./types";
import "./styles.css";

type Tab = "upload" | "downloads" | "jobs" | "sessions" | "rules";

export default function App() {
  const [tab, setTab] = useState<Tab>("upload");
  const [job, setJob] = useState<Job | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [target, setTarget] = useState<ReviewTarget | null>(null);
  const [theme, toggleTheme] = useTheme();

  function clearAll() {
    setJob(null);
    setBatchId(null);
    setTarget(null);
  }

  function renderBody() {
    if (target) return <ReviewWorkspace target={target} onBack={() => setTarget(null)} />;
    if (job) {
      return <JobPage job={job} onBack={() => setJob(null)} />;
    }
    if (batchId) {
      return <BatchPage batchId={batchId} onBack={() => setBatchId(null)} onOpen={setJob} />;
    }
    if (tab === "downloads") return <DownloadsPanel onBatchCreated={setBatchId} />;
    if (tab === "jobs") return <JobsDashboard onOpen={setJob} />;
    if (tab === "sessions") return <SessionsPanel onOpen={setTarget} />;
    if (tab === "rules") return <RulesPanel />;
    return <UploadPage onJobCreated={setJob} onBatchCreated={setBatchId} />;
  }

  // The nav stays visible on every screen so a reviewer can always jump to
  // Upload / Jobs / Pipeline Sessions without going "Back" first.
  const showTabs = true;

  return (
    <div className="app-shell">
      <header className="app-header">
        <img className="app-logo" src="/brand/thomson-reuters-primary-logo-reverse.svg" alt="Thomson Reuters" />
        <h1>Pending <em>legislation</em></h1>
        <span className="muted">Conversion and review workspace</span>
        <button className="theme-toggle" onClick={toggleTheme}>
          {theme === "light" ? "Dark" : "Light"} mode
        </button>
      </header>

      {showTabs && (
        <nav className="tab-bar">
          {(["upload", "downloads", "jobs", "sessions", "rules"] as Tab[]).map((t) => (
            <button
              key={t}
              className={tab === t && !job && !target && !batchId ? "tab active" : "tab"}
              onClick={() => {
                clearAll();
                setTab(t);
              }}
            >
              <Icon name={t === "upload" ? "upload" : t === "downloads" ? "download" : t === "jobs" ? "list" : t === "rules" ? "book" : "folder"} />
              {t === "upload" ? "Upload" : t === "downloads" ? "Downloaded files" : t === "jobs" ? "Jobs" : t === "rules" ? "Rules" : "Pipeline sessions"}
            </button>
          ))}
        </nav>
      )}

      <RunningJobs onOpen={(j) => { clearAll(); setJob(j); }} />

      <main>{renderBody()}</main>
    </div>
  );
}
