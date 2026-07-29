import { HealthStatus } from "./health-status";
import { Icon } from "./workbench-icon";
import { RepositoryWorkbench } from "./repository-workbench";

const defaultApiBaseUrl = "http://127.0.0.1:8000/api/v1";

export default function Home() {
  const apiBaseUrl = (process.env.NEXT_PUBLIC_API_BASE_URL ?? defaultApiBaseUrl).replace(/\/+$/, "");

  return (
    <main className="workbench-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-index">02</span>
          <div>
            <p className="brand-name">FIELDNOTE</p>
            <p className="brand-subtitle">Personal agent workbench</p>
          </div>
        </div>

        <div className="topbar-context" aria-label="本地工作区">
          <span className="context-label">WORKSPACE</span>
          <span className="context-value">local development</span>
          <span className="context-divider" />
          <Icon name="branch" size={14} />
          <span className="context-value">authorized repositories</span>
        </div>

        <div className="topbar-status">
          <HealthStatus apiBaseUrl={apiBaseUrl} />
          <div className="avatar" aria-label="本地会话">LOCAL</div>
        </div>
      </header>

      <div className="workspace-grid">
        <RepositoryWorkbench apiBaseUrl={apiBaseUrl} />
      </div>
    </main>
  );
}
