//! Loom Agent 桌面壳（Tauri 2）
//!
//! 当前职责：
//! 1. 启动 WebView2 窗口加载现有 React 前端（dev 模式走 5173，build 模式走 frontend/dist）
//! 2. Rust 直接管理 Python FastAPI 后端子进程（方案 C：spawn + Drop 自动 kill）

use std::sync::Mutex;

mod backend;

use backend::BackendProcess;
use tauri::Manager;

/// 应用共享状态：持有后端子进程句柄
///
/// 用 Mutex 包裹是因为 Tauri state 要求类型可 Sync；
/// 句柄始终在主线程持有，锁竞争可忽略。
struct AppState {
    _backend: Mutex<BackendProcess>,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .setup(|app| {
      // 推导 loom 项目根目录
      let loom_root = backend::find_loom_root().expect("无法定位 loom 项目根目录");
      println!("[loom] 后端启动中，项目根：{}", loom_root.display());

      // 启动后端并等待就绪
      let backend_proc = BackendProcess::start(&loom_root)
        .expect("后端启动失败，请检查 .venv/Scripts/python.exe 和 backend.server");

      println!("[loom] 后端已就绪（8765）");

      // 存入 state，保活句柄；应用退出时 Drop 自动 kill
      app.manage(AppState {
        _backend: Mutex::new(backend_proc),
      });

      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while building tauri application");
}
