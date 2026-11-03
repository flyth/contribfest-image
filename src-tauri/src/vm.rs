use serde::Serialize;
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

pub const API_PORT: u16 = 18081;
pub const KUBE_PORT: u16 = 18443;
pub const HEADLAMP_PORT: u16 = 18080;
pub const REGISTRY_PORT: u16 = 15000;

#[derive(Default)]
pub struct Vm {
    pub child: Option<Child>,
    pub token: String,
    pub stopping: bool,
}

#[derive(Serialize)]
pub struct Status {
    pub running: bool,
    pub ready: bool,
    pub message: String,
}

pub fn architecture() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "amd64"
    }
}

pub fn tool(name: &str) -> PathBuf {
    let exe = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    };
    let mut dirs = vec![];
    if let Some(dir) = std::env::var_os("QEMU_DIR") {
        dirs.push(PathBuf::from(dir));
    }
    if let Some(path) = std::env::var_os("PATH") {
        dirs.extend(std::env::split_paths(&path));
    }
    dirs.extend(["/opt/homebrew/bin", "/usr/local/bin"].map(PathBuf::from));
    if let Some(dir) = std::env::var_os("ProgramFiles") {
        dirs.push(PathBuf::from(dir).join("qemu"));
    }
    dirs.into_iter()
        .map(|dir| dir.join(&exe))
        .find(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from(exe))
}

fn command(name: &str) -> Command {
    let mut cmd = Command::new(tool(name));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    cmd.stdin(Stdio::null());
    cmd
}

pub fn disk_args(path: &Path) -> Vec<String> {
    vec![
        "-blockdev".into(),
        serde_json::json!({"driver":"file","filename":path,"node-name":"diskfile"}).to_string(),
        "-blockdev".into(),
        r#"{"driver":"qcow2","file":"diskfile","node-name":"disk"}"#.into(),
        "-device".into(),
        "virtio-blk-pci,drive=disk".into(),
    ]
}

fn acceleration() -> Result<(&'static str, &'static str), String> {
    if cfg!(target_os = "macos") {
        Ok(("hvf", "host"))
    } else if cfg!(windows) {
        if cfg!(target_arch = "aarch64") {
            return Err(
                "Windows ARM64 acceleration is not yet supported; use an x64 Windows host.".into(),
            );
        }
        Ok(("whpx", "max"))
    } else if OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/kvm")
        .is_ok()
    {
        Ok(("kvm", "host"))
    } else {
        Err(
            "KVM is unavailable. Enable virtualization and grant your user access to /dev/kvm."
                .into(),
        )
    }
}

pub fn private_dir(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    }
    Ok(())
}

impl Vm {
    pub fn start(&mut self, image: &Path, data: &Path) -> Result<(), String> {
        if self.running()? {
            return Err("The demo is already running.".into());
        }
        let (accel, cpu) = acceleration()?;
        let image = image.canonicalize().map_err(|e| e.to_string())?;
        if !image.is_file() {
            return Err("Choose a demo qcow2 image.".into());
        }
        // Check before creating an overlay, so a failed start leaves no partial disk.
        for port in [API_PORT, KUBE_PORT, HEADLAMP_PORT, REGISTRY_PORT] {
            std::net::TcpListener::bind(("127.0.0.1", port))
                .map_err(|_| format!("Port {port} is in use. Stop the other demo or service."))?;
        }
        let firmware = image.parent().unwrap().join("QEMU_EFI-arm64.fd");
        if architecture() == "arm64" && !firmware.is_file() {
            return Err("Put QEMU_EFI-arm64.fd beside the arm64 image.".into());
        }
        private_dir(data)?;
        let overlay = data.join("disk.qcow2");
        let base_record = data.join("base-image.txt");
        if overlay.exists() {
            let previous = fs::read_to_string(&base_record).map_err(|e| e.to_string())?;
            if previous != image.to_string_lossy() {
                return Err(
                    "This demo has saved data for another image. Reset it before switching images."
                        .into(),
                );
            }
        } else {
            let out = command("qemu-img")
                .args(["create", "-f", "qcow2", "-F", "qcow2", "-b"])
                .arg(&image)
                .arg(&overlay)
                .output()
                .map_err(|e| format!("Install QEMU (including qemu-img): {e}"))?;
            if !out.status.success() {
                return Err(String::from_utf8_lossy(&out.stderr).into_owned());
            }
            fs::write(&base_record, image.to_string_lossy().as_bytes())
                .map_err(|e| e.to_string())?;
        }
        self.token = uuid::Uuid::new_v4().simple().to_string();
        let log = File::create(data.join("vm.log")).map_err(|e| e.to_string())?;
        let err = log.try_clone().map_err(|e| e.to_string())?;
        let mut cmd = command(if architecture() == "arm64" {
            "qemu-system-aarch64"
        } else {
            "qemu-system-x86_64"
        });
        cmd.args([
            "-m", "4096", "-smp", "2", "-display", "none", "-serial", "stdio", "-monitor",
            "none", "-no-reboot", "-accel", accel, "-cpu", cpu,
        ])
        .args(disk_args(&overlay))
        .args(["-smbios", &format!("type=11,value=io.inspektor-gadget.demo.token={}", self.token)])
        .args(["-netdev", &format!(
            "user,id=net0,hostfwd=tcp:127.0.0.1:{API_PORT}-:8081,hostfwd=tcp:127.0.0.1:{KUBE_PORT}-:8443,hostfwd=tcp:127.0.0.1:{HEADLAMP_PORT}-:30080,hostfwd=tcp:127.0.0.1:{REGISTRY_PORT}-:30500"
        )])
        .args(["-device", "virtio-net-pci,netdev=net0"]);
        if architecture() == "arm64" {
            cmd.args(["-machine", "virt,gic-version=3", "-bios"])
                .arg(firmware);
        } else {
            cmd.args(["-machine", "q35"]);
        }
        self.child = Some(
            cmd.stdout(log)
                .stderr(err)
                .spawn()
                .map_err(|e| format!("Cannot start QEMU: {e}"))?,
        );
        self.stopping = false;
        Ok(())
    }

    pub fn running(&mut self) -> Result<bool, String> {
        if let Some(child) = self.child.as_mut() {
            if let Some(code) = child.try_wait().map_err(|e| e.to_string())? {
                self.child = None;
                if !code.success() {
                    return Err(format!(
                        "QEMU exited ({code}). See vm.log in the data folder."
                    ));
                }
            }
        }
        Ok(self.child.is_some())
    }

    pub fn request(&self, path: &str) -> Result<serde_json::Value, String> {
        reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(if path == "status" { 15 } else { 60 }))
            .build()
            .map_err(|e| e.to_string())?
            .get(format!("http://127.0.0.1:{API_PORT}/{path}"))
            .bearer_auth(&self.token)
            .send()
            .and_then(|r| r.error_for_status())
            .map_err(|e| e.to_string())?
            .json()
            .map_err(|e| e.to_string())
    }

    pub fn status(&mut self) -> Result<Status, String> {
        if !self.running()? {
            return Ok(Status {
                running: false,
                ready: false,
                message: "Stopped".into(),
            });
        }
        if self.stopping {
            return Ok(Status {
                running: true,
                ready: false,
                message: "Shutting down safely".into(),
            });
        }
        match self.request("status") {
            Ok(value) => Ok(Status {
                running: true,
                ready: value["ready"].as_bool().unwrap_or(false),
                message: value["message"].as_str().unwrap_or("Starting").into(),
            }),
            Err(_) => Ok(Status {
                running: true,
                ready: false,
                message: "Booting Linux".into(),
            }),
        }
    }

    pub fn stop(&mut self) -> Result<(), String> {
        if self.running()? {
            self.request("shutdown").map_err(|_| {
                "Guest is not responding yet. Wait for Linux to boot, then stop again."
            })?;
            self.stopping = true;
        }
        Ok(())
    }
}

pub fn local_kubeconfig(config: &serde_json::Value) -> Result<String, String> {
    let mut config = config.clone();
    let server = config
        .pointer_mut("/clusters/0/cluster/server")
        .ok_or("Guest returned an invalid kubeconfig.")?;
    *server = format!("https://127.0.0.1:{KUBE_PORT}").into();
    serde_json::to_string_pretty(&config).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kubeconfig_rewrites_only_server_and_keeps_certificate_checks() {
        let input = serde_json::json!({
            "clusters": [{"cluster": {"server": "https://10.0.2.15:8443",
                "certificate-authority-data": "abc"}}],
            "users": [{"name": "server:test"}]
        });
        let output = local_kubeconfig(&input).unwrap();
        let config: serde_json::Value = serde_json::from_str(&output).unwrap();
        assert_eq!(
            config["clusters"][0]["cluster"]["server"],
            "https://127.0.0.1:18443"
        );
        assert_eq!(
            config["clusters"][0]["cluster"]["certificate-authority-data"],
            "abc"
        );
        assert_eq!(config["users"][0]["name"], "server:test");
        assert!(!output.contains("insecure-skip-tls-verify"));
        assert!(local_kubeconfig(&serde_json::json!({"users": []})).is_err());
    }

    #[test]
    fn disk_paths_are_not_qemu_option_strings() {
        let args = disk_args(Path::new("/demo/a,b c.qcow2"));
        let json: serde_json::Value = serde_json::from_str(&args[1]).unwrap();
        assert_eq!(json["filename"], "/demo/a,b c.qcow2");
        assert_eq!(json["driver"], "file");
    }
}
