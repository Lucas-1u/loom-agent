import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { defineConfig } from "vite";

/** 配置所在目录（ESM 没有 __dirname，用 import.meta.url 换） */
const root = fileURLToPath(new URL(".", import.meta.url));

/**
 * Vite 配置
 *
 * 与 Tauri 相关的三个关键项（Phase 6 会用到，现在先配好）：
 *   · strictPort —— Tauri 的 devUrl 写死了端口，端口漂移会导致白屏
 *   · clearScreen: false —— 保留 Rust 编译日志，否则一屏刷掉就看不到报错
 *   · envPrefix 加 TAURI_ —— 让 TAURI_ 开头的变量也能注入前端
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],

  clearScreen: false,

  server: {
    port: 5173,
    strictPort: true,
    watch: {
      // Rust 侧改动不该触发前端 HMR
      ignored: ["**/src-tauri/**"],
    },
  },

  envPrefix: ["VITE_", "TAURI_"],

  build: {
    // Tauri 2 的 WebView2 基于较新的 Chromium，可以放心用现代语法
    target: "chrome105",
    sourcemap: !!process.env.TAURI_DEBUG,
    rollupOptions: {
      /**
       * 多页入口。
       * ★ 为什么用独立 HTML 而不是前端路由：项目没有装 react-router，
       *   而「Agent 工作树」是一个与主应用无关的静态展示页 —— 独立 HTML
       *   既满足"独立入口"，又完全不用碰 App.tsx / main.tsx，
       *   现有功能零改动。dev 访问 /agent-tree.html，build 后产物同名。
       * ★ Tauri 只加载 index.html，多出来的这个 html 不会影响桌面壳。
       */
      input: {
        main: resolve(root, "index.html"),
        "agent-tree": resolve(root, "agent-tree.html"),
        stats: resolve(root, "stats.html"),
      },
    },
  },
});
