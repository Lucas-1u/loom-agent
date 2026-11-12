/**
 * 原生能力桥（Tauri 专有能力的唯一入口）
 *
 * ★ 同一个前端会被三种宿主加载，必须都能活：
 *     ① Vite 浏览器（`loom-gui.bat` 开的那个）      → 没有原生能力
 *     ② Tauri dev（WebView + devUrl=5173）          → 有
 *     ③ Tauri 生产（WebView + tauri.localhost）      → 有
 *   所以这里**不在模块顶层静态 import 插件**：那段代码在浏览器里根本不该被加载。
 *   做法是运行时判断 + 动态 import —— `isTauri()` 为假时直接返回，调用方负责降级。
 *
 * ★ 为什么 `__TAURI_INTERNALS__` 而不是 `__TAURI__`：
 *   后者只有开了 `withGlobalTauri` 才存在（我们没开），前者是 v2 恒定注入的。
 *
 * ★ 权限在 `src-tauri/capabilities/default.json` 里开（`dialog:default`）。
 *   插件注册了但 ACL 没开，调用会报 "not allowed by ACL"，看起来像插件没装。
 */

const TAURI_INTERNALS = "__TAURI_INTERNALS__";

/** 当前是否跑在 Tauri 里（浏览器开发模式下是 false） */
export function isTauri(): boolean {
  return typeof window !== "undefined" && TAURI_INTERNALS in window;
}

/**
 * 弹系统原生的目录选择框，返回**绝对路径**（取消则返回 null）。
 *
 * ★ 这件事浏览器做不到：`<input type="file" webkitdirectory>` 只给 File 对象，
 *   `file.name` 拿不到路径。真实路径只能由宿主给 —— 这就是要走 Rust 的原因。
 */
export async function pickDirectory(defaultPath?: string): Promise<string | null> {
  if (!isTauri()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({
    directory: true,
    multiple: false,
    title: "选择工作区目录",
    defaultPath: defaultPath || undefined,
  });
  return typeof picked === "string" ? picked : null;
}
