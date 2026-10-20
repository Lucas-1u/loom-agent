import type { ConnectionState, ViewMode } from "../types/chat";
import { ViewSwitcher } from "./ViewSwitcher";

/** 可选的模型（Phase 3 接后端后会从 /api/health 拿真实列表） */
const MODELS = ["qwen3.7-plus", "qwen3.7-max", "deepseek-chat"];

export function AppHeader({
  viewMode,
  onViewChange,
  model,
  onModelChange,
  connection,
  demo,
  taskRunning,
}: {
  viewMode: ViewMode;
  onViewChange: (m: ViewMode) => void;
  model: string;
  onModelChange: (m: string) => void;
  connection: ConnectionState;
  demo: boolean;
  taskRunning: boolean;
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-ink-700 bg-ink-900 px-4">
      {/* ---- 左：品牌 ---- */}
      <div className="flex shrink-0 items-center gap-2.5">
        <Logo />
        <div className="leading-tight">
          <div className="text-[15px] font-semibold tracking-wide text-slate-100">Loom</div>
          <div className="text-[10px] uppercase tracking-[0.14em] text-slate-500">
            Agent Runtime
          </div>
        </div>
      </div>

      {/* ---- 中：视图切换（居中）---- */}
      <div className="flex flex-1 justify-center">
        <ViewSwitcher mode={viewMode} onChange={onViewChange} />
      </div>

      {/* ---- 右：模型 / 连接 / DEMO ---- */}
      <div className="flex shrink-0 items-center gap-3">
        <label className="flex items-center gap-1.5 text-[11px] text-slate-500">
          <span className="hidden xl:inline">模型</span>
          <select
            value={model}
            onChange={(e) => onModelChange(e.target.value)}
            className="cursor-pointer rounded-md border border-ink-700 bg-ink-850 px-2 py-1 text-[12px] text-slate-200 outline-none transition-colors hover:border-ink-600 focus:border-tone-cyan/50"
          >
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>

        <ConnectionBadge state={connection} running={taskRunning} />

        {demo && (
          <span
            className="rounded border border-tone-amber/40 bg-tone-amber/10 px-1.5 py-0.5 text-[10px] font-semibold tracking-wider text-tone-amber"
            title="当前界面使用演示数据，不是真实 Agent 执行记录"
          >
            DEMO
          </span>
        )}
      </div>
    </header>
  );
}

function ConnectionBadge({ state, running }: { state: ConnectionState; running: boolean }) {
  const map: Record<ConnectionState, { label: string; dot: string; text: string }> = {
    idle: { label: "未连接", dot: "bg-slate-600", text: "text-slate-500" },
    connecting: { label: "连接中", dot: "bg-tone-amber loom-blink", text: "text-tone-amber" },
    connected: { label: "已连接", dot: "bg-tone-green", text: "text-tone-green" },
    error: { label: "连接失败", dot: "bg-tone-red", text: "text-tone-red" },
  };
  const c = map[state];

  return (
    <span className="flex items-center gap-1.5 text-[11px]" title={`后端连接：${c.label}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${c.dot}`} />
      <span className={c.text}>{running ? "运行中" : c.label}</span>
    </span>
  );
}

/** 织机意象：经纬交叉 */
function Logo() {
  return (
    <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-ink-700 bg-gradient-to-br from-ink-800 to-ink-850">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <path d="M3 6h12M3 12h12" stroke="#2dd4e8" strokeWidth="1.4" strokeLinecap="round" />
        <path d="M6 3v12M12 3v12" stroke="#5b9dff" strokeWidth="1.4" strokeLinecap="round" opacity="0.75" />
      </svg>
    </div>
  );
}
