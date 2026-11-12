import { useState } from "react";
import type { EventEnvelope, UserInputAnswer } from "../types/events";
import { resolveStyle, TONE } from "../lib/eventStyles";
import { Markdown } from "./Markdown";
import { ApprovalCard } from "./ApprovalCard";
import { UserInputCard } from "./UserInputCard";

/**
 * 单个事件卡片
 *
 * ★ 职责边界：
 *   本组件只负责「把一条事件画出来」，**不做任何业务判断**。
 *   颜色/图标/标签全部来自 `resolveStyle()`，不在这里写 if-else 配色。
 */
export function EventCard({
  ev,
  isLast,
  onApprovalResolve,
}: {
  ev: EventEnvelope;
  isLast: boolean;
  onApprovalResolve?: (id: string, approved: boolean) => void;
}) {
  const style = resolveStyle(ev);
  const tone = TONE[style.tone];
  const dotSize = style.emphasis ? "h-3.5 w-3.5 left-[2px]" : "h-2.5 w-2.5 left-[4px]";

  // 思考卡是**合并段**：seq 区间由 mergeThinkingEvents 写在 data 里。
  // 其它事件没有这两个键 → 退回单条 seq，行为不变。
  const seqFrom = Number((ev.data as Record<string, unknown>)?.seq_from ?? ev.seq);
  const seqTo = Number((ev.data as Record<string, unknown>)?.seq_to ?? ev.seq);

  return (
    <div className="loom-enter relative pb-3 pl-8">
      {/* 时间线竖线（最后一个不画，避免尾巴拖出去） */}
      {!isLast && (
        <div className="absolute bottom-0 left-[9px] top-[18px] w-px bg-ink-700/80" aria-hidden="true" />
      )}

      {/* 节点 */}
      <div
        className={`absolute top-[9px] rounded-full ${dotSize} ${tone.dot} ring-2 ring-ink-950`}
        aria-hidden="true"
      />

      {/* 卡片 */}
      <div className={`rounded-lg border ${tone.border} bg-ink-850/70`}>
        {/* 头部：标签 + 轮次 + 时间 */}
        <div className="flex items-center gap-2 border-b border-ink-700/50 px-3 py-1.5">
          <span className={`font-mono text-[12px] leading-none ${tone.text}`}>{style.icon}</span>
          <span className={`text-[11.5px] font-medium tracking-wide ${tone.text}`}>
            {style.label}
          </span>
          {/* ★ 前端没声明过这个事件类型：照常显示，但明确标出来（否则裸 JSON 看着像"本来就长这样"） */}
          {style.unknown && (
            <span
              className="rounded bg-tone-violet/15 px-1 py-px text-[9.5px] text-tone-violet"
              title="前端 EVENT_TYPES / EVENT_STYLES 里没有这个类型，已用兜底样式渲染"
            >
              未声明
            </span>
          )}
          {ev.turn > 0 && (
            <span className="rounded bg-ink-800 px-1 py-px font-mono text-[9.5px] text-slate-500">
              t{ev.turn}
            </span>
          )}
          <span className="ml-auto font-mono text-[9.5px] text-slate-600">
            {seqTo > seqFrom ? `#${seqFrom}–${seqTo}` : `#${ev.seq}`}
          </span>
        </div>

        {/* 内容 */}
        <div className="px-3 py-2">
          <EventBody ev={ev} isLast={isLast} onApprovalResolve={onApprovalResolve} />
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// 按事件类型渲染主体
// --------------------------------------------------------------------------- //
function EventBody({
  ev,
  isLast,
  onApprovalResolve,
}: {
  ev: EventEnvelope;
  /** 是否是时间线最后一条 —— 思考卡据此决定"边写边展开" */
  isLast: boolean;
  onApprovalResolve?: (id: string, approved: boolean) => void;
}) {
  const d = ev.data as Record<string, unknown>;

  switch (ev.type) {
    case "task.start":
      return (
        <div className="space-y-1.5">
          <p className="selectable text-[13px] leading-relaxed text-slate-200">
            {String(d.goal ?? "")}
          </p>
          <Meta k="工作区" v={String(d.workspace ?? "")} mono />
          <Meta k="模型" v={String(d.model ?? "")} />
        </div>
      );

    case "plan": {
      const steps = (d.steps as Array<{ title?: string; detail?: string; status?: string }>) ?? [];
      const note = String(d.note ?? "");
      return (
        <div className="space-y-1.5">
          <ol className="space-y-1">
            {steps.map((s, i) => {
              const done = s.status === "done";
              const doing = s.status === "doing";
              return (
                <li key={i} className="flex items-start gap-2 text-[12.5px] leading-relaxed">
                  <span
                    className={[
                      "mt-[3px] flex h-4 w-4 shrink-0 items-center justify-center rounded border text-[9px] font-mono",
                      done
                        ? "border-tone-green/40 bg-tone-green/15 text-tone-green"
                        : doing
                          ? "border-tone-amber/40 bg-tone-amber/10 text-tone-amber"
                          : "border-ink-600 text-slate-600",
                    ].join(" ")}
                  >
                    {done ? "✓" : i + 1}
                  </span>
                  <span className={done ? "text-slate-500 line-through" : "text-slate-300"}>
                    {s.title ?? ""}
                    {s.detail && (
                      <span className="ml-1.5 text-[11px] text-slate-600">{s.detail}</span>
                    )}
                  </span>
                </li>
              );
            })}
          </ol>
          {note && <p className="text-[11px] italic text-slate-500">{note}</p>}
        </div>
      );
    }

    case "thinking": {
      // ★ 这里的 data 已经是 mergeThinkingEvents 的产物：text 是多条 delta 拼起来的，
      //   parts 说明它吸收了多少条原始事件；seq 区间由卡片头部显示。
      const text = String(d.text ?? "");
      const parts = Number(d.parts ?? 1);
      const done = Boolean(d.done);
      return <ThinkingBlock text={text} parts={parts} live={isLast && !done} />;
    }

    case "act":
      return (
        <div className="flex flex-wrap items-baseline gap-2 text-[12.5px]">
          <span className="text-slate-300">{String(d.intent ?? "")}</span>
          {d.tool ? (
            <span className="rounded bg-tone-cyan/10 px-1.5 py-0.5 font-mono text-[11px] text-tone-cyan">
              {String(d.tool)}
            </span>
          ) : null}
        </div>
      );

    case "tool.call": {
      const args = (d.args as Record<string, unknown>) ?? {};
      const keyCount = Object.keys(args).length;
      // M4.1：MCP 工具的来源由事件里的 source 元数据给出（不靠解析工具名前缀）
      const src = (d.source as Record<string, unknown> | undefined) ?? undefined;
      return (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="rounded bg-tone-cyan/10 px-1.5 py-0.5 font-mono text-[11.5px] text-tone-cyan">
              {String(d.tool ?? "")}
            </span>
            {src?.kind === "mcp" && (
              <span
                className="rounded bg-tone-violet/15 px-1.5 py-0.5 text-[10px] text-tone-violet"
                title="来自外部 MCP Server 的工具"
              >
                MCP · {String(src.server ?? "")}
              </span>
            )}
            <span className="font-mono text-[10px] text-slate-600">{String(d.call_id ?? "")}</span>
          </div>
          <ArgSummary args={args} />
          {/* 参数不多时摘要已显示全部，再给一个"完整参数"只会重复 */}
          {keyCount > 3 && (
            <Collapsible
              text={JSON.stringify(args, null, 2)}
              mono
              label="完整参数"
              maxHeight={200}
            />
          )}
        </div>
      );
    }

    case "tool.result": {
      const ok = Boolean(d.ok);
      const summary = String(d.summary ?? "");
      const err = d.error as
        | { kind?: string; message?: string; hint?: string; retryable?: boolean }
        | null
        | undefined;
      const data = d.data;
      const ms = Number(d.duration_ms ?? 0);
      // M6：terminal 工具的结果信封（kind=terminal）单独渲染成"命令 + 输出"
      const term =
        data && typeof data === "object" && (data as Record<string, unknown>).kind === "terminal"
          ? (data as Record<string, unknown>)
          : null;
      const exitCode = term ? Number(term.exit_code ?? 0) : 0;
      const stdout = term ? String(term.stdout ?? "") : "";
      const stderr = term ? String(term.stderr ?? "") : "";

      return (
        <div className="space-y-1.5">
          <div className="flex items-start gap-2">
            <span className={`mt-px font-mono text-[12px] ${ok ? "text-tone-green" : "text-tone-red"}`}>
              {ok ? "✓" : "✕"}
            </span>
            <p className="selectable flex-1 text-[12.5px] leading-relaxed text-slate-200">
              {summary}
            </p>
            {ms > 0 && (
              <span className="shrink-0 font-mono text-[10px] text-slate-600">{ms}ms</span>
            )}
          </div>

          {!ok && err && (
            <div className="rounded border border-tone-red/25 bg-tone-red/5 px-2 py-1.5">
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="rounded bg-tone-red/15 px-1 py-px font-mono text-tone-red">
                  {err.kind ?? "error"}
                </span>
                {err.retryable === false && (
                  <span className="text-[10px] text-slate-500">不可重试</span>
                )}
              </div>
              {err.hint && (
                <p className="mt-1 text-[11px] leading-relaxed text-slate-400">建议：{err.hint}</p>
              )}
            </div>
          )}

          {term && (
            <div className="space-y-1.5 rounded border border-ink-700 bg-ink-900/60 px-2 py-1.5">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10.5px] text-slate-500">
                <span className="rounded bg-tone-cyan/10 px-1.5 py-0.5 text-tone-cyan">
                  {String(term.shell_label ?? term.shell ?? "shell")}
                </span>
                <span className={exitCode === 0 ? "text-tone-green" : "text-tone-red"}>
                  exit {String(term.exit_code ?? "?")}
                </span>
                <span className="truncate">cwd: {String(term.cwd ?? "")}</span>
                {term.timed_out ? <span className="text-tone-amber">超时</span> : null}
                {term.cancelled ? <span className="text-tone-amber">已取消</span> : null}
                {term.truncated ? <span className="text-tone-amber">输出已截断</span> : null}
                {Number(term.redacted ?? 0) > 0 ? (
                  <span className="text-tone-amber">已打码 {String(term.redacted)} 处密钥</span>
                ) : null}
              </div>
              <div className="selectable break-all font-mono text-[11.5px] text-slate-300">
                <span className="text-slate-500">$ </span>
                {String(term.command ?? "")}
              </div>
              {stdout ? (
                <pre className="max-h-[200px] overflow-auto whitespace-pre-wrap break-all rounded bg-ink-950/60 p-1.5 font-mono text-[11px] leading-relaxed text-slate-300">
                  {stdout}
                </pre>
              ) : null}
              {stderr ? (
                <pre className="max-h-[160px] overflow-auto whitespace-pre-wrap break-all rounded bg-tone-red/5 p-1.5 font-mono text-[11px] leading-relaxed text-tone-red/90">
                  {stderr}
                </pre>
              ) : null}
            </div>
          )}

          {data !== undefined && data !== null && (
            <Collapsible
              text={typeof data === "string" ? data : JSON.stringify(data, null, 2)}
              mono
              label="完整结果"
              maxHeight={220}
            />
          )}
        </div>
      );
    }

    case "workflow.state": {
      const wfSteps = (d.steps ?? []) as Array<Record<string, unknown>>;
      const wfStatus = String(d.status ?? "");
      const event = String(d.event ?? "");
      const tone =
        wfStatus === "completed"
          ? "text-tone-green"
          : wfStatus === "cancelled" || wfStatus === "failed" || wfStatus === "needs_attention"
            ? "text-tone-red"
            : "text-tone-violet";
      const stepTone = (st: string) =>
        st === "done"
          ? "text-tone-green"
          : st === "failed" || st === "blocked"
            ? "text-tone-red"
            : st === "running"
              ? "text-tone-cyan"
              : "text-slate-500";

      return (
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px]">
            <span className={`font-mono ${tone}`}>{wfStatus || "?"}</span>
            <span className="text-slate-500">{event}</span>
            <span className="truncate text-slate-400">{String(d.goal ?? "")}</span>
            <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-600">
              {wfSteps.filter((x) => String(x.status) === "done").length}/{wfSteps.length} 步
            </span>
          </div>
          <div className="space-y-0.5">
            {wfSteps.map((st, i) => {
              const deps = (st.depends_on ?? []) as string[];
              return (
                <div key={i} className="flex items-center gap-2 font-mono text-[10.5px]">
                  <span className={stepTone(String(st.status))}>
                    {String(st.status ?? "?")}
                  </span>
                  <span className="text-slate-300">{String(st.step_id ?? "")}</span>
                  <span className="text-slate-600">{String(st.kind ?? "")}</span>
                  {deps.length > 0 && (
                    <span className="text-slate-600">← {deps.join(",")}</span>
                  )}
                  {st.error ? (
                    <span className="truncate text-tone-red/80">{String(st.error)}</span>
                  ) : null}
                  {Number(st.elapsed_ms ?? 0) > 0 && (
                    <span className="ml-auto shrink-0 text-slate-700">
                      {String(st.elapsed_ms)}ms
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      );
    }

    case "observe":
      return <p className="text-[12px] text-slate-400">{String(d.digest ?? "")}</p>;

    case "reflect":
      return (
        <div className="space-y-1 text-[12px] leading-relaxed">
          {d.progress ? <p className="text-slate-300">{String(d.progress)}</p> : null}
          {d.next_action ? (
            <p className="text-slate-500">
              <span className="text-slate-600">下一步 · </span>
              {String(d.next_action)}
            </p>
          ) : null}
          {d.done === true && <p className="text-[11px] text-tone-green">已收敛</p>}
        </div>
      );

    case "approval.request":
      return <ApprovalCard data={d} onResolve={onApprovalResolve} />;

    case "approval.decision": {
      const approved = Boolean(d.approved);
      return (
        <div className="space-y-1 text-[12px]">
          <p className={approved ? "text-tone-green" : "text-tone-red"}>
            {approved ? "已批准" : "已拒绝"}
            <span className="ml-2 text-slate-600">
              等待 {Number(d.waited_s ?? 0).toFixed(1)}s
            </span>
          </p>
          {d.reason ? <p className="text-slate-400">理由：{String(d.reason)}</p> : null}
        </div>
      );
    }

    case "approval.expired":
      return (
        <p className="text-[12px] text-tone-red">
          等待超时（{Number(d.waited_s ?? 0).toFixed(0)}s），按拒绝处理
        </p>
      );

    case "user_input.request":
      return <UserInputCard data={d} />;

    case "user_input.response": {
      const answers = Array.isArray(d.answers) ? (d.answers as UserInputAnswer[]) : [];
      const late = Boolean(d.late);
      return (
        <div className="space-y-1 text-[12px]">
          <div className="flex items-center gap-1.5">
            <span className="text-tone-green">用户回答</span>
            {late && (
              <span
                className="rounded bg-tone-amber/15 px-1 py-px text-[9.5px] text-tone-amber"
                title="这条回答是在 Agent 等待超时之后才提交的，Agent 会在下一步看到"
              >
                补答
              </span>
            )}
          </div>
          {answers.length === 0 ? (
            <p className="text-slate-300">{String(d.answer ?? "")}</p>
          ) : (
            <ul className="space-y-0.5">
              {answers.map((a, i) => {
                const sel = a.selected ?? [];
                const custom = String(a.custom ?? "").trim();
                return (
                  <li key={`${a.id}-${i}`} className="flex flex-wrap items-baseline gap-1.5">
                    <span className="font-mono text-[10px] text-slate-600">{a.id}</span>
                    {sel.map((s) => (
                      <span
                        key={s}
                        className="rounded bg-tone-cyan/10 px-1.5 py-px text-[11px] text-tone-cyan"
                      >
                        {s}
                      </span>
                    ))}
                    {custom && (
                      <span
                        className="rounded bg-tone-violet/10 px-1.5 py-px text-[11px] text-tone-violet"
                        title="用户自己填写的内容"
                      >
                        {custom}
                      </span>
                    )}
                    {sel.length === 0 && !custom && (
                      <span className="text-[11px] text-slate-600">（跳过）</span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      );
    }

    case "user_input.expired": {
      const pending = Boolean(d.pending);
      // pending = 前台等待超时但问题没作废：直接长出一张可操作卡片，
      // 否则「仍然可以回答」只是一句话，用户还得往上翻原卡片。
      if (pending) {
        return (
          <div className="space-y-1.5">
            <p className="text-[11.5px] text-tone-amber">
              前台等待超时（不是拒绝、不是跳过）——Agent 已继续，但你仍可以回答
            </p>
            <UserInputCard data={d} />
          </div>
        );
      }
      return (
        <p className="text-[12px] text-tone-red">
          用户未在超时时间内回答（{String(d.question ?? "").slice(0, 60)}）
        </p>
      );
    }

    case "permission.mode": {
      const mode = String(d.mode ?? "");
      const source = String(d.source ?? "");
      const enforced = Boolean(d.enforced);
      const modeTone =
        mode === "read-only"
          ? "text-tone-cyan"
          : mode === "desktop"
            ? // 桌面操控是唯一的"物理动作"档，配色跟 full-access 同红但加粗提示
              "text-tone-red font-semibold"
            : mode === "full-access"
              ? "text-tone-red"
              : "text-tone-green";
      return (
        <div className="space-y-1 text-[12px]">
          <p className="flex flex-wrap items-center gap-x-2">
            <span className={`font-mono ${modeTone}`}>{mode || "?"}</span>
            <span className="text-slate-600">
              {source === "api" ? "调用方指定" : "默认值"}
            </span>
            <span className="text-slate-600">工具 {Number(d.tools ?? 0)} 个</span>
            {!enforced && (
              <span className="text-tone-amber">⚠ 已记录但未强制（注册表未切换）</span>
            )}
          </p>
        </div>
      );
    }

    case "final_answer":
      return <Markdown text={String(d.text ?? "")} />;

    case "task.end": {
      const status = String(d.status ?? "");
      const reason = String(d.reason ?? "");
      const answer = String(d.answer ?? "");
      return (
        <div className="space-y-1 text-[12px]">
          <p className="text-slate-300">
            状态 <span className="font-mono text-slate-100">{status}</span>
            {reason && <span className="ml-2 text-slate-600">({reason})</span>}
          </p>
          {answer && answer !== String(ev.data?.answer) && null}
        </div>
      );
    }

    case "error":
      return (
        <div className="space-y-1 text-[12px]">
          <div className="flex items-center gap-1.5">
            <span className="rounded bg-tone-red/15 px-1 py-px font-mono text-[10.5px] text-tone-red">
              {String(d.kind ?? "error")}
            </span>
            {d.fatal === true && (
              <span className="text-[10px] font-medium text-tone-red">致命</span>
            )}
          </div>
          <p className="selectable text-slate-300">{String(d.message ?? "")}</p>
        </div>
      );

    case "budget.exceeded":
      return (
        <p className="text-[12px] text-slate-300">
          {String(d.kind ?? "")} 已达上限
          <span className="ml-1.5 font-mono text-[11px] text-slate-500">
            {String(d.used ?? "?")} / {String(d.limit ?? "?")}
          </span>
        </p>
      );

    case "rag.retrieve": {
      const index = (d.index as Record<string, unknown>) ?? {};
      return (
        <div className="space-y-1.5">
          <p className="selectable text-[13px] leading-relaxed text-slate-200">
            查询：<span className="font-mono text-tone-cyan">{String(d.query ?? "")}</span>
          </p>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5">
            <Meta
              k="模式"
              v={d.mode === "auto" ? "任务开始自动检索" : "Agent 主动检索"}
            />
            <Meta k="返回上限" v={String(d.k ?? "")} />
          </div>
          {index.files !== undefined && (
            <Meta
              k="索引"
              v={`${String(index.files ?? 0)} 文件 / ${String(index.chunks ?? 0)} 片段`}
              mono
            />
          )}
        </div>
      );
    }

    case "rag.result": {
      const hits = (d.hits as Array<Record<string, unknown>>) ?? [];
      const count = Number(d.count ?? hits.length);
      const injected = Boolean(d.injected);
      const reason = String(d.reason ?? "");
      const error = String(d.error ?? "");
      return (
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2 text-[12.5px] text-slate-300">
            <span>{count > 0 ? `命中 ${count} 条资料` : "没有命中资料"}</span>
            <span className="font-mono text-[10.5px] text-slate-500">
              {String(d.elapsed_ms ?? 0)}ms
            </span>
            {/* ★ 三个层次要分清：检索到（count）/ 提供给了模型（injected）/ 最终引用（在答复里）*/}
            <span
              className={`rounded px-1 py-px text-[10px] ${
                injected
                  ? "bg-tone-green/15 text-tone-green"
                  : "bg-ink-800 text-slate-500"
              }`}
            >
              {injected ? "已进入模型上下文" : "未进入上下文"}
            </span>
          </div>
          {error && <p className="text-[11.5px] text-tone-red">检索失败：{error}</p>}
          {!error && count === 0 && reason && (
            <p className="text-[11px] text-slate-500">
              原因：{reason === "no_hit" ? "索引里没有匹配的词" : reason}
            </p>
          )}
          {hits.length > 0 && (
            <ul className="space-y-0.5">
              {hits.map((h, i) => (
                <li key={i} className="flex items-baseline gap-2 text-[11.5px]">
                  <span className="shrink-0 font-mono text-slate-600">[{i + 1}]</span>
                  <span className="selectable break-all font-mono text-tone-cyan">
                    {String(h.locator ?? h.path ?? "")}
                  </span>
                  <span className="truncate text-slate-500">{String(h.title ?? "")}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      );
    }

    default:
      return (
        <pre className="selectable overflow-x-auto font-mono text-[11px] text-slate-500">
          {JSON.stringify(d, null, 2)}
        </pre>
      );
  }
}

// --------------------------------------------------------------------------- //
// 小组件
// --------------------------------------------------------------------------- //
function Meta({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="shrink-0 text-slate-600">{k}</span>
      <span className={`selectable break-all text-slate-400 ${mono ? "font-mono" : ""}`}>{v}</span>
    </div>
  );
}

/** 参数摘要：只显示前 3 个键值，完整内容折叠在后面 */
function ArgSummary({ args }: { args: Record<string, unknown> }) {
  const entries = Object.entries(args).slice(0, 3);
  if (!entries.length) return null;

  return (
    <div className="space-y-0.5">
      {entries.map(([k, v]) => {
        const s = typeof v === "string" ? v : JSON.stringify(v);
        const short = s.length > 90 ? `${s.slice(0, 90)}…` : s;
        return (
          <div key={k} className="flex gap-1.5 font-mono text-[11px] leading-relaxed">
            <span className="shrink-0 text-slate-600">{k}=</span>
            <span className="selectable break-all text-slate-400">{short}</span>
          </div>
        );
      })}
      {Object.keys(args).length > 3 && (
        <div className="text-[10px] text-slate-600">
          还有 {Object.keys(args).length - 3} 个参数，展开可见
        </div>
      )}
    </div>
  );
}

/**
 * 思考段（多条 delta 合并后的一段推理）
 *
 * ★ 折叠规则（这是本次改造的重点，改之前先想清楚为什么这么定）：
 *   · **只有"正在写的那一段"默认展开** —— 流式观感保留，用户仍然能看模型在想什么
 *   · 段一旦结束（收到 done 标记 / 后面来了工具调用）就**自动收起成两行预览**
 *     → 一次任务跑完，时间线上留下的是「思考 1 段、工具 1 次、思考 1 段」的结构，
 *       而不是几十张十几字的卡片
 *   · 用户点过之后**以用户为准**（override 粘住），不再跟着 live 变
 *     —— 否则正在读的段落突然自己收起来，比不折叠更烦
 *
 * ★ 展开态的高度策略：
 *   live 时不设上限（内容随流增长，外层自动滚动能跟上）；
 *   回看历史段时才套 `max-h` + 内部滚动（避免一屏被一段超长推理占满）。
 */
function ThinkingBlock({
  text,
  parts,
  live,
}: {
  text: string;
  parts: number;
  live: boolean;
}) {
  const [override, setOverride] = useState<boolean | null>(null);
  const open = override ?? live;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOverride(true)}
        className="group block w-full text-left"
        title="展开这段思考的完整内容"
      >
        <p className="line-clamp-2 text-[12px] leading-relaxed text-slate-500 group-hover:text-slate-300">
          {previewOf(text) || "（空）"}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-slate-600 group-hover:text-slate-500">
          <span>▸ 展开</span>
          {parts > 1 && <span>合并 {parts} 段</span>}
          <span>{text.length} 字</span>
        </div>
      </button>
    );
  }

  return (
    <div>
      <pre
        className={`selectable whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-slate-400 ${
          live ? "" : "max-h-[360px] overflow-auto rounded border border-ink-700 bg-ink-950/40 px-2 py-1.5"
        }`}
      >
        {text}
      </pre>
      <button
        type="button"
        onClick={() => setOverride(false)}
        className="mt-1 flex items-center gap-1.5 text-[10.5px] text-slate-600 transition-colors hover:text-tone-cyan"
      >
        <span>▾ 收起</span>
        {(parts > 1 || !live) && (
          <span className="text-slate-700">
            {parts > 1 ? `${parts} 段 · ` : ""}
            {text.length} 字
          </span>
        )}
      </button>
    </div>
  );
}

/** 收起态的一行预览：压掉换行、截断（中文按字符算，别用 CSS 之外的单位猜） */
function previewOf(text: string, max = 160): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/**
 * 折叠块（长内容默认收起）
 *
 * 为什么不用原生 <details>：样式在深色主题下不一致，
 * 而且原生元素在时间线里会带默认 marker，不好控。
 */
export function Collapsible({
  text,
  mono,
  label = "展开查看",
  maxHeight = 200,
}: {
  text: string;
  mono?: boolean;
  label?: string;
  maxHeight?: number;
}) {
  const [open, setOpen] = useState(false);
  const long = text.length > 180 || text.split("\n").length > 3;

  if (!long) {
    return (
      <pre
        className={`selectable whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-slate-400 ${
          mono ? "font-mono" : ""
        }`}
      >
        {text}
      </pre>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-[10.5px] text-slate-500 transition-colors hover:text-tone-cyan"
      >
        <span className={`transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
        {open ? "收起" : label}
        <span className="text-slate-600">（{text.length} 字符）</span>
      </button>
      {open && (
        <pre
          className={`selectable mt-1 overflow-auto whitespace-pre-wrap break-words rounded border border-ink-700 bg-ink-950/60 px-2 py-1.5 text-[11px] leading-relaxed text-slate-400 ${
            mono ? "font-mono" : ""
          }`}
          style={{ maxHeight }}
        >
          {text}
        </pre>
      )}
    </div>
  );
}
