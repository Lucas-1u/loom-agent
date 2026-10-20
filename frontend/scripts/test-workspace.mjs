/**
 * 路径规范化 / 分组 的单元测试
 *
 * 跑法：node scripts/test-workspace.mjs
 * （先由 esbuild 把 workspace.ts / selectors.ts 转译到 .tmp/ 再 import）
 */
import assert from "node:assert/strict";
import {
  normalizeWorkspacePath,
  workspaceKey,
  workspaceLabel,
  sameWorkspace,
  ellipsisPath,
  UNSET_WORKSPACE_KEY,
  UNSET_WORKSPACE_LABEL,
} from "../.tmp/workspace.mjs";
import { groupSessionsByWorkspace, disambiguateGroupLabels } from "../.tmp/selectors.mjs";

let pass = 0;
let fail = 0;

function eq(actual, expected, label) {
  try {
    assert.equal(actual, expected);
    pass += 1;
    console.log(`  [ok] ${label}`);
  } catch {
    fail += 1;
    console.log(`  [X]  ${label}\n       期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
  }
}

function ok(cond, label) {
  if (cond) {
    pass += 1;
    console.log(`  [ok] ${label}`);
  } else {
    fail += 1;
    console.log(`  [X]  ${label}`);
  }
}

console.log("\n[1] 规范化：分隔符 / 尾部分隔符 / 盘符根");
eq(normalizeWorkspacePath("D:/Loom_agent/loom"), "D:\\Loom_agent\\loom", "正斜杠 → 反斜杠");
eq(normalizeWorkspacePath("D:\\Loom_agent\\loom\\"), "D:\\Loom_agent\\loom", "去掉尾部分隔符");
eq(normalizeWorkspacePath("D:\\\\Loom_agent\\\\loom"), "D:\\Loom_agent\\loom", "折叠重复分隔符");
eq(normalizeWorkspacePath("C:\\"), "C:\\", "★ 盘符根保留尾部分隔符");
eq(normalizeWorkspacePath("  D:\\proj  "), "D:\\proj", "去首尾空格");
eq(normalizeWorkspacePath("\\\\server\\share\\x"), "\\\\server\\share\\x", "UNC 路径不被折叠");
eq(normalizeWorkspacePath(""), "", "空路径 → 空");
eq(normalizeWorkspacePath(null), "", "null → 空");

console.log("\n[2] 分组 key：忽略大小写");
eq(workspaceKey("D:\\Proj"), workspaceKey("d:\\proj"), "大小写不同 → 同一 key");
eq(workspaceKey(""), UNSET_WORKSPACE_KEY, "空路径 → 未设置 key");
ok(workspaceKey("D:\\proj") !== UNSET_WORKSPACE_KEY, "真实路径不会撞上未设置 key");
ok(
  workspaceKey("D:\\Projects\\demo-app") !== workspaceKey("E:\\Experiments\\demo-app"),
  "★★ 同名不同盘 → 不同 key（绝不能被合并）",
);

console.log("\n[3] 显示名：最后一段");
eq(workspaceLabel("D:\\Loom_agent\\loom"), "loom", "取最后一段");
eq(workspaceLabel("D:\\Projects\\demo-app"), "demo-app", "带连字符");
eq(workspaceLabel("C:\\"), "C:\\", "盘符根原样");
eq(workspaceLabel(""), UNSET_WORKSPACE_LABEL, "空 → 未设置工作区");

console.log("\n[4] 同工作区判定");
ok(sameWorkspace("D:/proj/", "d:\\PROJ"), "斜杠+大小写+尾部差异都忽略");
ok(!sameWorkspace("D:\\Projects\\demo-app", "E:\\Experiments\\demo-app"), "同名不同盘 → 不相等");

console.log("\n[5] 长路径截断（不撑破布局）");
ok(ellipsisPath("D:\\a\\b\\c\\d\\e\\f\\g\\h\\i\\j", 20).length <= 24, "截断后不超长");
ok(ellipsisPath("D:\\short", 40) === "D:\\short", "短路径不动");

console.log("\n[6] 分组：排序与结构");
const now = Date.now() / 1000;
const sessions = [
  { session_id: "a", title: "A", workspace_path: "D:\\Projects\\demo-app", created_at: now - 300, updated_at: now - 100 },
  { session_id: "b", title: "B", workspace_path: "E:\\Experiments\\demo-app", created_at: now - 300, updated_at: now - 200 },
  { session_id: "c", title: "C", workspace_path: "D:/Projects/demo-app/", created_at: now - 300, updated_at: now - 50 },
  { session_id: "d", title: "D", workspace_path: "", created_at: now - 900, updated_at: now - 900 },
];
const groups = groupSessionsByWorkspace(sessions);

eq(groups.length, 3, "★ 4 个会话 → 3 个组（两个 demo-app 未合并，空路径单独一组）");
eq(groups[groups.length - 1].key, UNSET_WORKSPACE_KEY, "★ 未设置工作区永远排最后");
eq(groups[0].sessions.length, 2, "D:/Projects 组有 2 个会话（正/反斜杠写法被合并）");
eq(groups[0].sessions[0].session_id, "c", "组内按 updated_at 倒序（最新的在前）");

const fixed = disambiguateGroupLabels(groups);
const labels = fixed.map((g) => g.label);
ok(labels.includes("Projects\\demo-app") && labels.includes("Experiments\\demo-app"),
   "★ 重名组自动加长显示名，可区分");
ok(labels.filter((l) => l === "Projects\\demo-app").length === 1, "加长后不产生新的重名");

console.log(`\n${"=".repeat(52)}`);
console.log(`结果：${pass}/${pass + fail} 通过`);
if (fail) {
  console.log(`\n失败 ${fail} 条 —— 见上面 [X]`);
  process.exit(1);
}
console.log("=".repeat(52));
