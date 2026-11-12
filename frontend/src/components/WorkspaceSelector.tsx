import { useState } from "react";
import { ellipsisPath, normalizeWorkspacePath, UNSET_WORKSPACE_LABEL } from "../features/session/workspace";
import { isTauri, pickDirectory } from "../services/native";

/**
 * 底部工作区选择器
 *
 * ★ 它代表**当前 Session 的工作目录**，不是全局配置。
 *   切换会话时由 store 同步，用户改这里只会动当前会话（任务书 §5.4）。
 *
 * ★ 两种宿主，两种选法（2026-11-11 接上系统对话框）：
 *   · 桌面版（Tauri）：`pickDirectory()` → 系统原生目录框 → 拿**绝对路径**。
 *     这是浏览器做不到的：`<input webkitdirectory>` 只给 File 对象，拿不到路径。
 *   · 浏览器开发模式：退回 `prompt()` 手输 —— 宁可难看，也不假装"选好了"。
 *   判定放在运行时（`isTauri()`），浏览器里那支插件代码不会被加载。
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
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);
  const native = isTauri();
  const normalized = normalizeWorkspacePath(workspace);
  const shown = normalized ? ellipsisPath(normalized, 52) : "";

  async function handlePick() {
    if (disabled || picking) return;

    if (native) {
      setPicking(true);
      setPickError(null);
      try {
        const next = await pickDirectory(normalized || undefined);
        if (next) onChange(next); // 取消（null）时保持原样，不改动已有路径
      } catch (err) {
        // ★ 不静默：插件没注册 / ACL 没开时，按钮看起来"点了没反应"是最难查的一种坏
        console.error("[loom] 系统目录选择器调用失败：", err);
        setPickError(String(err));
      } finally {
        setPicking(false);
      }
      return;
    }

    promptManually();
  }

  /** 浏览器开发模式的兜底：手输（桌面版拿得到真实路径，不需要它） */
  function promptManually() {
    const next = window.prompt(
      "输入工作区路径（浏览器开发模式）\n\n" +
        "桌面版会弹系统原生的目录选择框；浏览器里拿不到真实路径，只能手输。\n" +
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
    <div className="border-b border-ink-700/60 pb-2">
      <div className="flex items-center gap-2">
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
          disabled={disabled || picking}
          title={
            disabled
              ? disabledReason
              : native
                ? "选择目录（系统原生对话框）"
                : "输入路径（浏览器开发模式：拿不到真实目录，只能手输）"
          }
          className={[
            "shrink-0 rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
            disabled || picking
              ? "cursor-not-allowed border-ink-700 bg-ink-850 text-slate-600"
              : "border-ink-600 bg-ink-800 text-slate-300 hover:border-tone-blue/40 hover:text-slate-100",
          ].join(" ")}
        >
          {picking ? "选择中…" : "选择目录"}
        </button>
      </div>

      {pickError && (
        <p className="mt-1 selectable break-all text-[10.5px] leading-relaxed text-tone-red">
          系统目录选择器不可用（{pickError}）—— 检查 dialog 插件是否注册、
          capabilities 里有没有 dialog:default。
        </p>
      )}
    </div>
  );
}
