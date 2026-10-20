import { useState } from "react";
import { Collapsible } from "./EventCard";

/**
 * 审批卡片（执行流内）
 *
 * ★ Phase 2 定位：**只有视觉，没有真实交互。**
 *   按钮是 disabled 的，并明确写着「Phase 5 接入」——
 *   宁可看起来"还没做完"，也不要让用户以为点了真的会批准。
 *
 * Phase 5 要做的事：
 *   · 把 onResolve 接到 `POST /api/approvals/{id}/decision`
 *   · 提交期间禁用按钮（防重复点击）
 *   · 收到 approval.decision 事件后由 store 更新状态，卡片自己从 props 读
 *
 * ★ 注意：卡片**不自己保存"是否已批准"的状态**。
 *   它只从事件派生（父组件传 resolved）。否则聊天视图和执行流各存一份，
 *   必然出现"一边显示已批准、一边还显示等待中"。
 */
export function ApprovalCard({
  data,
  onResolve,
}: {
  data: Record<string, unknown>;
  onResolve?: (id: string, approved: boolean) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const approvalId = String(data.approval_id ?? "");
  const action = String(data.action ?? "操作");
  const target = String(data.target ?? "");
  const impact = String(data.impact ?? "");
  const preview = String(data.preview ?? "");
  const risk = String(data.risk ?? "write");
  const tool = String(data.tool ?? "");

  const riskStyle =
    risk === "delete"
      ? "border-tone-red/40 bg-tone-red/10 text-tone-red"
      : risk === "write"
        ? "border-tone-amber/40 bg-tone-amber/10 text-tone-amber"
        : "border-ink-600 bg-ink-800 text-slate-400";

  /** Phase 2 未接后端：点按钮只提示 */
  function handleClick(approved: boolean) {
    if (onResolve) {
      onResolve(approvalId, approved);
      return;
    }
    window.alert(
      `审批交互将在 Phase 5 接入后端。\n\n本次演示的操作是：${approved ? "批准" : "拒绝"} ${action} ${target}`,
    );
  }

  return (
    <div className="space-y-2">
      {/* 标题行 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12.5px] font-medium text-slate-100">{action}</span>
        <span className={`rounded border px-1.5 py-px text-[10px] font-medium ${riskStyle}`}>
          {risk}
        </span>
        {tool && (
          <span className="rounded bg-ink-800 px-1.5 py-px font-mono text-[10.5px] text-slate-400">
            {tool}
          </span>
        )}
      </div>

      {/* 目标 */}
      {target && (
        <div className="flex gap-2 text-[11.5px]">
          <span className="shrink-0 text-slate-600">目标</span>
          <span className="selectable break-all font-mono text-slate-300">{target}</span>
        </div>
      )}

      {/* 影响说明（后端生成的，不是模型自由发挥） */}
      {impact && (
        <div className="rounded border border-tone-amber/25 bg-tone-amber/5 px-2.5 py-1.5">
          <p className="selectable text-[11.5px] leading-relaxed text-slate-300">{impact}</p>
        </div>
      )}

      {/* 变更预览 */}
      {preview && (
        <div>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="flex items-center gap-1 text-[10.5px] text-slate-500 transition-colors hover:text-tone-cyan"
          >
            <span className={`transition-transform ${expanded ? "rotate-90" : ""}`}>▸</span>
            {expanded ? "收起变更预览" : "查看变更预览"}
          </button>
          {expanded && (
            <pre className="selectable mt-1 max-h-[220px] overflow-auto whitespace-pre-wrap break-words rounded border border-ink-700 bg-ink-950/70 px-2.5 py-2 font-mono text-[11px] leading-relaxed">
              {preview.split("\n").map((line, i) => {
                const neg = line.startsWith("- ");
                const pos = line.startsWith("+ ");
                return (
                  <div
                    key={i}
                    className={
                      neg ? "text-tone-red/85" : pos ? "text-tone-green/85" : "text-slate-500"
                    }
                  >
                    {line || " "}
                  </div>
                );
              })}
            </pre>
          )}
        </div>
      )}

      {/* approval_id（便于排查，也提醒这是真实凭证） */}
      <Collapsible text={approvalId} mono label="approval_id" maxHeight={40} />

      {/* 按钮 */}
      <div className="flex items-center gap-2 pt-0.5">
        <button
          type="button"
          onClick={() => handleClick(true)}
          className="rounded-md border border-tone-green/35 bg-tone-green/12 px-3 py-1 text-[12px] font-medium text-tone-green transition-colors hover:bg-tone-green/20"
        >
          批准
        </button>
        <button
          type="button"
          onClick={() => handleClick(false)}
          className="rounded-md border border-tone-red/35 bg-tone-red/10 px-3 py-1 text-[12px] font-medium text-tone-red transition-colors hover:bg-tone-red/18"
        >
          拒绝
        </button>
        <span className="ml-1 text-[10px] text-slate-600">
          Phase 5 接入真实审批（当前为演示按钮）
        </span>
      </div>
    </div>
  );
}
