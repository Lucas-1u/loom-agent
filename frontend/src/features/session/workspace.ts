/**
 * 工作区路径规范化
 *
 * ★ 为什么必须集中在一个文件里？
 *
 *   任务书明确要求：**分组唯一标识必须是完整的规范化路径，不能只用目录名。**
 *   因为 `D:\Projects\demo-app` 和 `E:\Experiments\demo-app` 是**两个不同分组**，
 *   虽然都显示成 "demo-app"。
 *
 *   而「统一分隔符 + 去尾部分隔符 + 保留盘符根 + 忽略大小写」这几条规则
 *   必须在**同一个函数**里实现 —— 否则两个组件各算一套，key 对不上，
 *   分组就会莫名其妙地分裂成两半。
 *
 * ★ 一条容易被忽略的规则：**空路径不能归到当前工作区**。
 *   它是独立的「未设置工作区」分组，否则新建会话会污染已有分组。
 */

/** 未设置工作区时使用的 key（不可能与真实路径冲突） */
export const UNSET_WORKSPACE_KEY = "__unset__";
export const UNSET_WORKSPACE_LABEL = "未设置工作区";

/**
 * 规范化路径：统一分隔符、折叠重复分隔符、去尾部分隔符。
 * **保留原始大小写**（用于显示和存储）。
 */
export function normalizeWorkspacePath(raw: string | null | undefined): string {
  const s = (raw ?? "").trim().replace(/\//g, "\\");
  if (!s) return "";

  // UNC 前缀 `\\server\share` 要保护，否则会被下面的折叠规则压成 `\server\share`
  const isUnc = s.startsWith("\\\\");
  let p = isUnc ? s.slice(2) : s;

  p = p.replace(/\\{2,}/g, "\\");

  if (isUnc) p = `\\\\${p}`;

  // 去尾部分隔符 —— 但 `C:\` 这种盘符根（长度恰为 3）要保留
  if (p.length > 3 && p.endsWith("\\")) {
    p = p.slice(0, -1);
  }

  return p;
}

/**
 * 分组的唯一 key：规范化 + 小写。
 *
 * Windows 路径不区分大小写，所以 `D:\Proj` 和 `d:\proj` 必须落到同一组。
 */
export function workspaceKey(raw: string | null | undefined): string {
  const n = normalizeWorkspacePath(raw);
  return n ? n.toLowerCase() : UNSET_WORKSPACE_KEY;
}

/**
 * 显示名：取最后一段（保留原始大小写）。
 *
 *   D:\Loom_agent\loom        → "loom"
 *   D:\Projects\demo-app      → "demo-app"
 *   C:\                       → "C:\"（盘符根没有"最后一段"）
 *   ""                        → "未设置工作区"
 */
export function workspaceLabel(raw: string | null | undefined): string {
  const n = normalizeWorkspacePath(raw);
  if (!n) return UNSET_WORKSPACE_LABEL;

  // 盘符根
  if (/^[A-Za-z]:\\$/.test(n)) return n;

  // UNC 根：\\server\share
  const parts = n.split("\\").filter(Boolean);
  if (!parts.length) return n;
  return parts[parts.length - 1];
}

/** 比较两个路径是否指向同一工作区（忽略大小写与分隔符差异） */
export function sameWorkspace(a: string | null | undefined, b: string | null | undefined): boolean {
  return workspaceKey(a) === workspaceKey(b);
}

/**
 * 截断显示用的路径（长路径不撑破布局）。
 *
 *   D:\Loom_agent\loom → 保持不变
 *   很长很长…\very\deep\project → `D:\…\very\deep\project`（保留盘符 + 末两段）
 */
export function ellipsisPath(raw: string, maxLen = 44): string {
  const n = normalizeWorkspacePath(raw);
  if (!n || n.length <= maxLen) return n;

  const parts = n.split("\\").filter(Boolean);
  if (parts.length <= 2) return n;

  const drive = /^[A-Za-z]:$/.test(parts[0]) ? `${parts[0]}\\…` : "…";
  const tail = parts.slice(-2).join("\\");
  return `${drive}\\${tail}`;
}
