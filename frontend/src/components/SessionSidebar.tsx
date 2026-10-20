import type { Session, WorkspaceGroup } from "../features/session/types";
import { displayTitle, relativeTime } from "../features/session/selectors";
import { UNSET_WORKSPACE_KEY } from "../features/session/workspace";

/**
 * 左侧历史会话栏（按工作目录分组）
 *
 * ★ 分组 key 来自 `workspaceKey()`（完整规范化路径），**不是目录名**。
 *   所以 `D:\Projects\demo-app` 和 `E:\Experiments\demo-app` 是两个组，
 *   虽然都显示成 "demo-app"。这里只负责显示，不自己算 key。
 *
 * ★ 独立滚动：`flex-1 min-h-0 overflow-y-auto`
 *   外层 App 用 `min-h-0` 保证它能真正收缩，否则会把整个页面撑高。
 */
export function SessionSidebar({
  groups,
  activeSessionId,
  collapsedGroups,
  onToggleGroup,
  onSelectSession,
  onNewSession,
  runningSessionIds,
  busy,
}: {
  groups: WorkspaceGroup[];
  activeSessionId: string | null;
  collapsedGroups: Record<string, true>;
  onToggleGroup: (key: string) => void;
  onSelectSession: (s: Session) => void;
  onNewSession: () => void;
  runningSessionIds: Set<string>;
  busy: boolean;
}) {
  return (
    <aside className="flex w-[264px] shrink-0 flex-col border-r border-ink-700 bg-ink-900">
      {/* 头部：新建会话 */}
      <div className="shrink-0 border-b border-ink-700 px-3 py-2.5">
        <button
          type="button"
          onClick={onNewSession}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-ink-600 bg-ink-850 py-1.5 text-[12px] text-slate-300 transition-colors hover:border-tone-blue/40 hover:bg-ink-800 hover:text-slate-100"
        >
          <span className="text-[14px] leading-none text-tone-blue">+</span>
          新建会话
        </button>
      </div>

      {/* 分组列表（独立滚动） */}
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-1.5 py-2">
        {groups.length === 0 && (
          <p className="px-2 py-6 text-center text-[11.5px] leading-relaxed text-slate-600">
            还没有历史会话。
            <br />
            点上面「新建会话」开始。
          </p>
        )}

        {groups.map((g) => {
          const collapsed = Boolean(collapsedGroups[g.key]);
          const isUnset = g.key === UNSET_WORKSPACE_KEY;

          return (
            <div key={g.key} className="mb-0.5">
              {/* 分组头 */}
              <button
                type="button"
                onClick={() => onToggleGroup(g.key)}
                title={g.path || "这个会话还没有绑定工作目录"}
                className="group flex w-full items-center gap-1.5 rounded-md px-1.5 py-1.5 text-left transition-colors hover:bg-ink-850"
              >
                <span
                  className={`w-2.5 shrink-0 text-[9px] text-slate-600 transition-transform ${
                    collapsed ? "" : "rotate-90"
                  }`}
                >
                  ▶
                </span>

                {isUnset ? <FolderIcon dim /> : <FolderIcon />}

                <span
                  className={`min-w-0 flex-1 truncate text-[12px] ${
                    isUnset ? "italic text-slate-500" : "text-slate-300"
                  }`}
                >
                  {g.label}
                </span>

                <span className="shrink-0 rounded bg-ink-800 px-1 font-mono text-[9.5px] text-slate-500">
                  {g.sessions.length}
                </span>
              </button>

              {/* 组内会话 */}
              {!collapsed && (
                <div className="ml-[13px] border-l border-ink-700/70 pl-1.5">
                  {g.sessions.map((s) => {
                    const active = s.session_id === activeSessionId;
                    const running = runningSessionIds.has(s.session_id);

                    return (
                      <button
                        key={s.session_id}
                        type="button"
                        disabled={busy && !active}
                        onClick={() => onSelectSession(s)}
                        title={`${displayTitle(s)}\n${s.workspace_path || "（未设置工作区）"}`}
                        className={[
                          "mb-0.5 flex w-full items-start gap-1.5 rounded px-2 py-1.5 text-left transition-colors",
                          active
                            ? "bg-tone-blue/12 text-slate-100"
                            : "text-slate-400 hover:bg-ink-850 hover:text-slate-200",
                          busy && !active ? "cursor-not-allowed opacity-50" : "",
                        ].join(" ")}
                      >
                        <span
                          className={`mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full ${
                            running
                              ? "loom-blink bg-tone-amber"
                              : active
                                ? "bg-tone-blue"
                                : "bg-slate-700"
                          }`}
                        />

                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[12px] leading-[1.4]">
                            {displayTitle(s)}
                          </span>
                          <span className="block text-[10px] text-slate-600">
                            {running ? "运行中 · " : ""}
                            {relativeTime(s.updated_at)}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* 底部：版本 */}
      <div className="shrink-0 border-t border-ink-700 px-3 py-2">
        <div className="flex items-center justify-between text-[10px] text-slate-600">
          <span>Loom v0.1b</span>
          <span>Phase 2</span>
        </div>
      </div>
    </aside>
  );
}

function FolderIcon({ dim }: { dim?: boolean }) {
  const c = dim ? "#4b5563" : "#8b9bb4";
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="shrink-0" aria-hidden="true">
      <path
        d="M1.8 4.4a1 1 0 0 1 1-1h3l1.3 1.4h5.8a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1v-7.4Z"
        stroke={c}
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}
