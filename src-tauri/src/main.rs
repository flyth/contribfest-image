#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod vm;

use std::{path::PathBuf, sync::Mutex};
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use vm::{Status, Vm};

struct Demo(Mutex<Vm>);

fn data_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path().app_data_dir().map_err(|e| e.to_string())
}

#[tauri::command]
async fn choose_image(app: tauri::AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .add_filter("Demo disk", &["qcow2"])
            .blocking_pick_file()
            .map(|file| file.into_path().map(|p| p.to_string_lossy().into_owned()))
            .transpose()
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn info(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    Ok(serde_json::json!({
        "arch": vm::architecture(),
        "data": data_dir(&app)?,
        "image": std::fs::read_to_string(data_dir(&app)?.join("base-image.txt")).unwrap_or_default()
    }))
}

#[tauri::command]
async fn start(app: tauri::AppHandle, image: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.state::<Demo>()
            .0
            .lock()
            .unwrap()
            .start(&PathBuf::from(image), &data_dir(&app)?)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn status(app: tauri::AppHandle) -> Result<Status, String> {
    tauri::async_runtime::spawn_blocking(move || app.state::<Demo>().0.lock().unwrap().status())
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn stop(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || app.state::<Demo>().0.lock().unwrap().stop())
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn credentials(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let demo = app.state::<Demo>();
        let vm = demo.0.lock().unwrap();
        let value = vm.request("credentials")?;
        let config = vm::local_kubeconfig(&value["kubeconfig"])?;
        let data = data_dir(&app)?;
        vm::private_dir(&data)?;
        let path = data.join("kubeconfig");
        std::fs::write(&path, config).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
                .map_err(|e| e.to_string())?;
        }
        Ok(serde_json::json!({"path": path, "token": value["token"]}))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn reset(app: tauri::AppHandle) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let confirmed = app
            .dialog()
            .message("Delete all saved cluster and registry data? The base image is kept.")
            .title("Reset demo data")
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::OkCancel)
            .blocking_show();
        if !confirmed {
            return Ok(false);
        }
        let demo = app.state::<Demo>();
        let mut vm = demo.0.lock().unwrap();
        if vm.running()? {
            return Err("Stop the VM before resetting demo data.".into());
        }
        let dir = data_dir(&app)?;
        for name in ["disk.qcow2", "base-image.txt", "kubeconfig"] {
            let path = dir.join(name);
            if path.exists() {
                std::fs::remove_file(path).map_err(|e| e.to_string())?;
            }
        }
        Ok(true)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn open_headlamp() -> Result<(), String> {
    let url = "http://127.0.0.1:18080";
    let mut cmd = if cfg!(target_os = "macos") {
        std::process::Command::new("open")
    } else if cfg!(windows) {
        let mut command = std::process::Command::new("rundll32.exe");
        command.arg("url.dll,FileProtocolHandler");
        command
    } else {
        std::process::Command::new("xdg-open")
    };
    cmd.arg(url).spawn().map_err(|e| e.to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Demo(Mutex::new(Vm::default())))
        .invoke_handler(tauri::generate_handler![
            choose_image,
            info,
            start,
            status,
            stop,
            credentials,
            reset,
            open_headlamp
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let demo = window.state::<Demo>();
                if let Ok(vm) = demo.0.try_lock() {
                    if vm.child.is_none() {
                        return;
                    }
                }
                api.prevent_close();
                let app = window.app_handle().clone();
                // Never block the UI thread or kill a VM with mounted etcd data.
                tauri::async_runtime::spawn(async move {
                    let _ = stop(app).await;
                });
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run IG Demo");
}
