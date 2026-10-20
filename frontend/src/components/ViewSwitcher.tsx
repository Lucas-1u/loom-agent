import type { ViewMode } from "../types/chat";

/**
 * 视图切换器
 *
 * ★ 它只 dispatch 一个 "SET_VIEW"，**不碰任何事件或任务状态**。
 *   所以「切换视图不中断任务、不丢数据」是结构上就成立的 ——
 *   而不是靠在这里写"保存/恢复"逻辑。
 */
export function ViewSwitcher({
  mode,
  onChange,
}: {
  mode: ViewMode;
  onChange: (m: ViewMode) => void;
}) {
  return (
    <div
      className="flex items-center gap-0.5 rounded-lg border border-ink-700 bg-ink-850 p-0.5"
      role="tablist"
      aria-label="视图切换"
    >
      <Tab
        active={mode === "chat"}
        label="聊天视图"
        icon={<ChatIcon />}
        onClick={() => onChange("chat")}
      />
      <Tab
        active={mode === "execution"}
        label="执行流"
        icon={<FlowIcon />}
        onClick={() => onChange("execution")}
      />
    </div>
  );
}

function Tab({
  active,
  label,
  icon,
  onClick,
}: {
  active: boolean;
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={[
        "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
        active
          ? "bg-ink-700 text-slate-100 shadow-sm"
          : "text-slate-400 hover:bg-ink-800 hover:text-slate-200",
      ].join(" ")}
    >
      <span className={active ? "text-tone-cyan" : "text-slate-500"}>{icon}</span>
      {label}
    </button>
  );
}

function ChatIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.5 3.2h11a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1H6.2L3 14V11.6h-.5a1 1 0 0 1-1-1V4.2a1 1 0 0 1 1-1Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FlowIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="4" cy="3.2" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="4" cy="8" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="4" cy="12.8" r="1.5" stroke="currentColor" strokeWidth="1.3" />
      <path d="M7 3.2h6M7 8h4M7 12.8h5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}
