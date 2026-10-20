import { useState } from "react";

/**
 * 左侧工作区面板
 *
 * ★ 在两种视图里**始终存在**（布局层保证，不是每视图各写一份）。
 *
 * Phase 2 的目录选择用 `prompt()` 占位 —— 能用，但很简陋。
 * Phase 6 会换成 Tauri 的 dialog 插件（系统原生目录选择器），
 * 届时只改 `handlePick` 这一个函数，其余不动。
 *
 * ★ 路径安全**不在这里校验**。前端只负责传路径，
 *   `resolve_in_workspace` 的 realpath + 分隔符前缀比较在后端，
 *   前端限制永远只是体验优化，不是安全边界。
 */
export function WorkspacePanel({
  workspace,
  onWorkspaceChange,
  taskRunning,
}: {
  workspace: string;
  onWorkspaceChange: (path: string) => void;
  taskRunning: boolean;
}) {
  const [copied, setCopied] = useState(false);

  function handlePick() {
    if (taskRunning) return;
    const next = window.prompt(
      "输入工作区路径（Phase 6 会换成系统目录选择器）\n\n注意：Agent 只能在这个目录内读写文件。",
      workspace,
    );
    if (next && next.trim()) onWorkspaceChange(next.trim());
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(workspace);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* 剪贴板不可用时静默失败，不值得打扰用户 */
    }
  }

  const segments = workspace.split(/[\\/]/).filter(Boolean);

  return (
    <aside className="flex w-[248px] shrink-0 flex-col border-r border-ink-700 bg-ink-900">
      {/* 标题 */}
      <div className="flex h-11 items-center gap-2 border-b border-ink-700 px-3.5">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path
            d="M1.8 4.4a1 1 0 0 1 1-1h3l1.3 1.4h5.8a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1v-7.4Z"
            stroke="#8b9bb4"
            strokeWidth="1.2"
            strokeLinejoin="round"
          />
        </svg>
        <span className="text-[12px] font-medium tracking-wide text-slate-300">工作区</span>
      </div>

      {/* 路径 */}
      <div className="px-3.5 py-3">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-[0.12em] text-slate-600">当前目录</span>
          <button
            type="button"
            onClick={handleCopy}
            className="text-[10px] text-slate-500 transition-colors hover:text-tone-cyan"
            title="复制路径"
          >
            {copied ? "已复制" : "复制"}
          </button>
        </div>

        <div className="selectable rounded-md border border-ink-700 bg-ink-950/60 px-2.5 py-2">
          {workspace ? (
            <div className="break-all font-mono text-[11px] leading-[1.7] text-slate-300">
              {segments.map((seg, i) => (
                <span key={i}>
                  {i > 0 && <span className="text-slate-600">{"\\"}</span>}
                  <span className={i === segments.length - 1 ? "text-slate-100" : ""}>{seg}</span>
                </span>
              ))}
              {workspace.endsWith("\\") && <span className="text-slate-600">{"\\"}</span>}
            </div>
          ) : (
            <div className="text-[11px] text-slate-500">尚未选择</div>
          )}
        </div>

        <button
          type="button"
          onClick={handlePick}
          disabled={taskRunning}
          className={[
            "mt-2 w-full rounded-md border px-2.5 py-1.5 text-[12px] transition-colors",
            taskRunning
              ? "cursor-not-allowed border-ink-700 bg-ink-850 text-slate-600"
              : "border-ink-600 bg-ink-800 text-slate-300 hover:border-tone-blue/40 hover:bg-ink-700 hover:text-slate-100",
          ].join(" ")}
          title={taskRunning ? "任务运行中不能切换工作区" : "选择本地文件夹"}
        >
          选择目录
        </button>

        {taskRunning && (
          <p className="mt-1.5 text-[10px] leading-relaxed text-tone-amber/80">
            任务运行中，暂不能切换工作区 —— 否则 Agent 的工作目录会在中途改变。
          </p>
        )}
      </div>

      <div className="mx-3.5 border-t border-ink-700/70" />

      {/* 说明 */}
      <div className="px-3.5 py-3">
        <p className="text-[10.5px] leading-relaxed text-slate-500">
          Agent 只能在这个目录内读写文件。
          <br />
          越界路径会被后端<strong className="text-slate-400">硬拒绝</strong>，不是前端拦的。
        </p>
      </div>

      <div className="flex-1" />

      {/* 底部：版本信息（占位，Phase 3 会显示后端版本） */}
      <div className="border-t border-ink-700 px-3.5 py-2.5">
        <div className="flex items-center justify-between text-[10px] text-slate-600">
          <span>Loom v0.1b</span>
          <span>Phase 2</span>
        </div>
      </div>
    </aside>
  );
}
