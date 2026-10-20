import { useState } from "react";
import { ellipsisPath, normalizeWorkspacePath, UNSET_WORKSPACE_LABEL } from "../features/session/workspace";

/**
 * 底部工作区选择器
 *
 * ★ 它代表**当前 Session 的工作目录**，不是全局配置。
 *   切换会话时由 store 同步，用户改这里只会动当前会话（任务书 §5.4）。
 *
 * ★ 浏览器 vs Tauri：
 *   现在用 `prompt()` 占位（浏览器开发模式）。
 *   Phase 6 换成 Tauri 的 dialog 插件时，只改 `handlePick` 一个函数 ——
 *   **不要假装浏览器已经拿到了本地目录访问权限**，所以这里提示"开发模式"。
 */
export function WorkspaceSelector({
  workspace,
  onChange,
  disabled,
  disabledReason,
}: {
  workspace: string;
  onChange: (path: string) => void;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const [copied, setCopied] = useState(false);
  const normalized = normalizeWorkspacePath(workspace);
  const shown = normalized ? ellipsisPath(normalized, 52) : "";

  function handlePick() {
    if (disabled) return;
    const next = window.prompt(
      "输入工作区路径（浏览器开发模式）\n\n" +
        "Phase 6 桌面版会换成系统原生目录选择器。\n" +
        "注意：Agent 只能在这个目录内读写文件，越界由后端硬拒绝。",
      normalized,
    );
    if (next && next.trim()) onChange(next.trim());
  }

  async function handleCopy() {
    if (!normalized) return;
    try {
      await navigator.clipboard.writeText(normalized);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* 剪贴板不可用时静默失败 */
    }
  }

  return (
    <div className="flex items-center gap-2 border-b border-ink-700/60 pb-2">
      <span className="shrink-0 text-[10.5px] uppercase tracking-[0.1em] text-slate-600">
        工作区
      </span>

      <div
        className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-ink-700 bg-ink-950/60 px-2 py-1"
        title={normalized || UNSET_WORKSPACE_LABEL}
      >
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true" className="shrink-0">
          <path
            d="M1.8 4.4a1 1 0 0 1 1-1h3l1.3 1.4h5.8a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1v-7.4Z"
            stroke={normalized ? "#8b9bb4" : "#4b5563"}
            strokeWidth="1.2"
            strokeLinejoin="round"
          />
        </svg>

        <span
          className={`selectable min-w-0 flex-1 truncate font-mono text-[11px] ${
            normalized ? "text-slate-300" : "italic text-slate-600"
          }`}
        >
          {shown || UNSET_WORKSPACE_LABEL}
        </span>

        {normalized && (
          <button
            type="button"
            onClick={handleCopy}
            className="shrink-0 text-[10px] text-slate-500 transition-colors hover:text-tone-cyan"
            title="复制完整路径"
          >
            {copied ? "已复制" : "复制"}
          </button>
        )}
      </div>

      <button
        type="button"
        onClick={handlePick}
        disabled={disabled}
        title={disabled ? disabledReason : "选择目录（浏览器开发模式）"}
        className={[
          "shrink-0 rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
          disabled
            ? "cursor-not-allowed border-ink-700 bg-ink-850 text-slate-600"
            : "border-ink-600 bg-ink-800 text-slate-300 hover:border-tone-blue/40 hover:text-slate-100",
        ].join(" ")}
      >
        选择目录
      </button>
    </div>
  );
}
