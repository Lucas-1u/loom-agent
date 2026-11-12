/**
 * 思考片段合并（视图层聚合，不改事实）
 *
 * 背景：后端把模型的推理流按「0.15s 或 80 字符」节流成一条条 `thinking` 事件
 * （`backend/agent/core.py::_call_model`）。流得慢的时候每条只有十几个字，
 * 于是一次任务下来时间线里堆满几十上百张「思考」卡 —— 信息一条没少，
 * 但结构被冲掉了：看不出「这一段思考是在为哪个工具做铺垫」。
 *
 * ★ 这是**渲染层的聚合**，不是新的事实：
 *   · 原始事件在 store 里一条不少（`events[task_id]`），seq 连续、可回放补发
 *   · 合并只发生在「相邻 + 同轮次 + 同阶段」的 thinking 之间
 *   · **任何其它事件都会切断当前段**（tool.call / tool.result / plan / act / …）
 *     → 于是「思考 → 调工具 → 再思考」自然分成两段，这正是要的语义
 *   · 合并段的 seq 取**首条**，React key 因此在流式追加时保持稳定
 *     （否则每来一个 delta 组件就重挂载，展开状态会被重置）
 *
 * ★ 为什么不在 store 里合并：那会同时破坏「events 是事实」和
 *   `?since=` 补发去重。合并必须是一次**可以随时丢弃重算**的派生。
 *
 * ★ 为什么不直接让后端少发点：那样实时性就没了。用户要的是
 *   「一边打字一边看得见」+「事后读起来是一段话」，这两件事只能分开做。
 */

import type { EventEnvelope } from "../../types/events";

/** 合并段的 data：原始 ThinkingData + 视图字段
 *
 * 用 `type` 而不是 `interface`：对象字面量类型才有隐式索引签名，
 * 才能直接塞进 `EventEnvelope<T = Record<string, unknown>>` 的 data。 */
export type MergedThinkingData = {
  text: string;
  phase: string;
  done: boolean;
  /** 这条卡片吸收了多少条原始 thinking 事件（≥1） */
  parts: number;
  /** 覆盖的原始 seq 区间（含两端） */
  seq_from: number;
  seq_to: number;
  /** 视图层合并标记 —— 让本函数幂等（合并过的列表再进来不会被二次粘合） */
  ui_merged: true;
};

type Data = Record<string, unknown>;

/**
 * 把相邻的思考片段粘成一段，返回**渲染用**的事件列表。
 *
 * 不修改入参（每条事件要么原样透传，要么被复制成新对象）。
 * 空文本的 `done` 标记被吸收进当前段 —— 它以前会渲染成一张空卡片。
 */
export function mergeThinkingEvents(events: EventEnvelope[]): EventEnvelope[] {
  const out: EventEnvelope[] = [];
  let open: EventEnvelope | null = null;
  let openData: MergedThinkingData | null = null;

  const flush = () => {
    if (open && openData) {
      out.push({ ...open, seq: openData.seq_from, data: { ...openData } });
    }
    open = null;
    openData = null;
  };

  for (const e of events) {
    if (e.type !== "thinking") {
      // 任何一个别的事件都是分段边界（工具调用、计划、审批…）
      flush();
      out.push(e);
      continue;
    }

    const d = (e.data ?? {}) as Data;

    // 已经是合并段：原样透传（幂等，防止重复调用把两段又粘回去）
    if (d.ui_merged) {
      flush();
      out.push(e);
      continue;
    }

    const text = String(d.text ?? "");
    const phase = String(d.phase ?? "");
    const done = Boolean(d.done);

    if (!text) {
      /* 空文本只有一种用途：done=true 的「本段到此结束」。
       * ★ 以前它会渲染成一张只有标题的空卡片 —— 也是「一屏几十张卡」的来源之一。
       *   没有可收尾的段时直接丢掉，不留痕迹。 */
      if (done && open && openData) {
        openData.done = true;
        openData.seq_to = e.seq;
        flush();
      }
      continue;
    }

    if (open && openData && open.turn === e.turn && openData.phase === phase) {
      openData.text += text; // 后端切的是同一段连续文本，直接拼，不加分隔符
      openData.parts += 1;
      openData.seq_to = e.seq;
      if (done) openData.done = true;
    } else {
      flush();
      open = e;
      openData = {
        text,
        phase,
        done,
        parts: 1,
        seq_from: e.seq,
        seq_to: e.seq,
        ui_merged: true,
      };
    }

    if (done) flush(); // 结束标记连带正文：这段到此为止，后面另起一段
  }

  flush();
  return out;
}
