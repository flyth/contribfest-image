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
pub const GRAFANA_PORT: u16 = 18090;
pub const REGISTRY_PORT: u16 = 15000;
const HOST_PORTS: [u16; 5] = [
    API_PORT,
    KUBE_PORT,
    HEADLAMP_PORT,
    GRAFANA_PORT,
    REGISTRY_PORT,
];

#[derive(Default)]
pub struct Vm {
    pub child: Option<Child>,
    pub token: String,
    pub stopping: bool,
    data: Option<PathBuf>,
    migration_observed: bool,
}

#[derive(Serialize)]
pub struct Status {
    pub running: bool,
    pub ready: bool,
    pub message: String,
    /// Optional image layers the guest reports, e.g. "observability".
    pub layers: Vec<String>,
}

pub fn architecture() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "amd64"
    }
}

/// QEMU bundled with the app (macOS): `bin/`, `lib/` and `share/qemu/`.
static RUNTIME: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

/// Use the QEMU runtime in `dir` when it holds a `bin/` folder; later calls are ignored.
pub fn use_runtime(dir: PathBuf) {
    if dir.join("bin").is_dir() {
        let _ = RUNTIME.set(dir);
    }
}

fn find_tool(name: &str, runtime: Option<&Path>) -> PathBuf {
    let exe = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    };
    let mut dirs = vec![];
    if let Some(runtime) = runtime {
        dirs.push(runtime.join("bin"));
    }
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

pub fn tool(name: &str) -> PathBuf {
    find_tool(name, RUNTIME.get().map(PathBuf::as_path))
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

fn disk_args(path: &Path, name: &str, serial: &str) -> Vec<String> {
    let file = format!("{name}-file");
    vec![
        "-blockdev".into(),
        serde_json::json!({"driver":"file","filename":path,"node-name":file}).to_string(),
        "-blockdev".into(),
        serde_json::json!({"driver":"qcow2","file":file,"node-name":name}).to_string(),
        "-device".into(),
        format!("virtio-blk-pci,drive={name},serial={serial}"),
    ]
}

fn image_command(cmd: &mut Command) -> Result<(), String> {
    let output = cmd
        .stdout(Stdio::null())
        .output()
        .map_err(|e| format!("Install QEMU (including qemu-img): {e}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "qemu-img failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

fn create_overlay(image: &Path, overlay: &Path) -> Result<(), String> {
    image_command(
        command("qemu-img")
            .args(["create", "-f", "qcow2", "-F", "qcow2", "-b"])
            .arg(image)
            .arg(overlay),
    )
}

fn remove_file(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("Cannot remove {}: {e}", path.display())),
    }
}

fn require_stopped_disks(data: &Path) -> Result<(), String> {
    for name in ["session.qcow2", "migration.qcow2", "registry.qcow2"] {
        let path = data.join(name);
        if path.try_exists().map_err(|e| e.to_string())? {
            // QEMU may outlive its launcher; do not bypass its image locks with -U.
            image_command(command("qemu-img").args(["info", "-f", "qcow2"]).arg(&path))
                .map_err(|e| format!("Cannot access {name}. Stop any remaining QEMU process before changing demo data. {e}"))?;
        }
    }
    Ok(())
}

fn clear_ephemeral(data: &Path) -> Result<(), String> {
    for name in ["session.qcow2", "migration.qcow2", "kubeconfig"] {
        remove_file(&data.join(name))?;
    }
    Ok(())
}

fn prepare_disks(image: &Path, data: &Path) -> Result<PathBuf, String> {
    let registry = data.join("registry.qcow2");
    let seed = image
        .parent()
        .ok_or("Choose a demo qcow2 image.")?
        .join(format!("ig-registry-seed-{}.qcow2", architecture()));
    if !registry.try_exists().map_err(|e| e.to_string())? && !seed.is_file() {
        return Err(format!(
            "Put {} beside the demo image before creating registry storage.",
            seed.file_name().unwrap().to_string_lossy()
        ));
    }
    require_stopped_disks(data)?;
    private_dir(data)?;
    let data = data.canonicalize().map_err(|e| e.to_string())?;
    let registry = data.join("registry.qcow2");
    if !registry.try_exists().map_err(|e| e.to_string())? {
        let temporary = data.join(format!("registry-{}.tmp", uuid::Uuid::new_v4().simple()));
        let result = (|| {
            image_command(
                command("qemu-img")
                    .args(["convert", "-f", "qcow2", "-O", "qcow2"])
                    .arg(&seed)
                    .arg(&temporary),
            )?;
            OpenOptions::new()
                .write(true)
                .open(&temporary)
                .and_then(|file| file.sync_all())
                .map_err(|e| e.to_string())?;
            // Publish without overwriting: ordinary rename replaces existing files on Unix.
            fs::hard_link(&temporary, &registry).map_err(|e| e.to_string())
        })();
        let cleanup = remove_file(&temporary);
        result?;
        cleanup?;
        #[cfg(unix)]
        File::open(&data)
            .and_then(|directory| directory.sync_all())
            .map_err(|e| e.to_string())?;
    }
    clear_ephemeral(&data)?;
    create_overlay(image, &data.join("session.qcow2"))?;
    let legacy = data.join("disk.qcow2");
    if legacy.try_exists().map_err(|e| e.to_string())? {
        create_overlay(&legacy, &data.join("migration.qcow2"))?;
    }
    fs::write(
        data.join("base-image.txt"),
        image.to_string_lossy().as_bytes(),
    )
    .map_err(|e| e.to_string())?;
    Ok(data)
}

fn netdev() -> String {
    format!(
        "user,id=net0,hostfwd=tcp:127.0.0.1:{API_PORT}-:8081,hostfwd=tcp:127.0.0.1:{KUBE_PORT}-:8443,hostfwd=tcp:127.0.0.1:{HEADLAMP_PORT}-:30080,hostfwd=tcp:127.0.0.1:{GRAFANA_PORT}-:30090,hostfwd=tcp:127.0.0.1:{REGISTRY_PORT}-:30500"
    )
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
        for port in HOST_PORTS {
            std::net::TcpListener::bind(("127.0.0.1", port))
                .map_err(|_| format!("Port {port} is in use. Stop the other demo or service."))?;
        }
        let firmware = image.parent().unwrap().join("QEMU_EFI-arm64.fd");
        if architecture() == "arm64" && !firmware.is_file() {
            return Err("Put QEMU_EFI-arm64.fd beside the arm64 image.".into());
        }
        let data = prepare_disks(&image, data)?;
        self.token = uuid::Uuid::new_v4().simple().to_string();
        let log = File::create(data.join("vm.log")).map_err(|e| e.to_string())?;
        let err = log.try_clone().map_err(|e| e.to_string())?;
        let mut cmd = command(if architecture() == "arm64" {
            "qemu-system-aarch64"
        } else {
            "qemu-system-x86_64"
        });
        cmd.args([
            "-m",
            "4096",
            "-smp",
            "2",
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-no-reboot",
            "-accel",
            accel,
            "-cpu",
            cpu,
        ]);
        if let Some(runtime) = RUNTIME.get() {
            cmd.arg("-L").arg(runtime.join("share").join("qemu"));
        }
        cmd.args(disk_args(&data.join("session.qcow2"), "session", "ig-os"))
            .args(disk_args(
                &data.join("registry.qcow2"),
                "registry",
                "ig-registry",
            ))
            .args([
                "-smbios",
                &format!(
                    "type=11,value=io.inspektor-gadget.demo.token={}",
                    self.token
                ),
            ])
            .args(["-netdev", &netdev()])
            .args(["-device", "virtio-net-pci,netdev=net0"]);
        if data.join("migration.qcow2").exists() {
            cmd.args(disk_args(
                &data.join("migration.qcow2"),
                "legacy",
                "ig-legacy",
            ));
        }
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
        self.data = Some(data);
        self.migration_observed = false;
        Ok(())
    }

    pub fn running(&mut self) -> Result<bool, String> {
        if let Some(child) = self.child.as_mut() {
            if let Some(code) = child.try_wait().map_err(|e| e.to_string())? {
                self.child = None;
                self.finish(code.success())?;
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
                layers: vec![],
            });
        }
        if self.stopping {
            return Ok(Status {
                running: true,
                ready: false,
                message: "Shutting down safely".into(),
                layers: vec![],
            });
        }
        match self.request("status") {
            Ok(value) => Ok(self.guest_status(&value)),
            Err(_) => Ok(Status {
                running: true,
                ready: false,
                message: "Booting Linux".into(),
                layers: vec![],
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

    fn guest_status(&mut self, value: &serde_json::Value) -> Status {
        let guest_ready = value["ready"].as_bool() == Some(true);
        let persistent = value["registry_persistent"].as_bool() == Some(true);
        let ready = guest_ready && persistent;
        self.migration_observed |= ready && value["registry_migrated"].as_bool() == Some(true);
        Status {
            running: true,
            ready,
            message: if guest_ready && !persistent {
                "This image does not provide persistent registry storage. Choose an updated demo image.".into()
            } else {
                value["message"].as_str().unwrap_or("Starting").into()
            },
            layers: value["layers"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|layer| layer.as_str().map(String::from))
                .collect(),
        }
    }

    fn finish(&mut self, successful: bool) -> Result<(), String> {
        let commit = successful && self.stopping && self.migration_observed;
        self.stopping = false;
        self.migration_observed = false;
        self.token.clear();
        if let Some(data) = self.data.take() {
            clear_ephemeral(&data)?;
            if commit {
                remove_file(&data.join("disk.qcow2"))?;
            }
        }
        Ok(())
    }

    pub fn reset(&mut self, data: &Path) -> Result<(), String> {
        if self.running()? {
            return Err("Stop the VM before resetting demo data.".into());
        }
        if data
            .join("disk.qcow2")
            .try_exists()
            .map_err(|e| e.to_string())?
        {
            return Err("Legacy registry data has not been safely migrated. Start an updated demo image, wait until ready, then stop it cleanly before resetting or deleting registry data.".into());
        }
        require_stopped_disks(data)?;
        clear_ephemeral(data)?;
        remove_file(&data.join("vm.log"))?;
        self.token.clear();
        self.stopping = false;
        self.migration_observed = false;
        self.data = None;
        Ok(())
    }

    pub fn delete_registry(&mut self, data: &Path) -> Result<(), String> {
        self.reset(data)?;
        remove_file(&data.join("registry.qcow2"))
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
    fn network_forwards_every_service_only_on_loopback() {
        let network = netdev();
        for forward in [
            "hostfwd=tcp:127.0.0.1:18081-:8081",
            "hostfwd=tcp:127.0.0.1:18443-:8443",
            "hostfwd=tcp:127.0.0.1:18080-:30080",
            "hostfwd=tcp:127.0.0.1:18090-:30090",
            "hostfwd=tcp:127.0.0.1:15000-:30500",
        ] {
            assert!(
                network.contains(forward),
                "{forward} missing from {network}"
            );
        }
        assert_eq!(network.matches("hostfwd=").count(), HOST_PORTS.len());
        assert_eq!(network.matches("127.0.0.1").count(), HOST_PORTS.len());
    }

    #[test]
    fn guest_status_reports_layers() {
        let mut vm = Vm::default();
        let full = vm.guest_status(&serde_json::json!({
            "ready": true, "registry_persistent": true, "layers": ["observability"],
        }));
        assert_eq!(full.layers, ["observability"]);
        let base =
            vm.guest_status(&serde_json::json!({"ready": true, "registry_persistent": true}));
        assert!(base.layers.is_empty());
    }

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

    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("ig-vm,a b-{}", uuid::Uuid::new_v4().simple()));
            fs::create_dir_all(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }

        fn image(&self, name: &str) -> PathBuf {
            let path = self.0.join(name);
            image_command(
                command("qemu-img")
                    .args(["create", "-f", "qcow2"])
                    .arg(&path)
                    .arg("16M"),
            )
            .unwrap();
            path
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn image_info(path: &Path) -> serde_json::Value {
        let output = command("qemu-img")
            .args(["info", "--output=json"])
            .arg(path)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }

    #[test]
    fn bundled_runtime_wins_over_system_qemu() {
        let scratch = Scratch::new();
        let bin = scratch.0.join("bin");
        fs::create_dir(&bin).unwrap();
        let exe = if cfg!(windows) {
            "qemu-img.exe"
        } else {
            "qemu-img"
        };
        fs::write(bin.join(exe), "").unwrap();
        assert_eq!(find_tool("qemu-img", Some(&scratch.0)), bin.join(exe));
        // Without a bundled copy the system search still applies.
        assert_ne!(find_tool("qemu-img", None), bin.join(exe));
        let empty = scratch.0.join("empty");
        fs::create_dir(&empty).unwrap();
        assert_eq!(
            find_tool("qemu-img", Some(&empty)),
            find_tool("qemu-img", None)
        );
    }

    #[test]
    fn failed_session_cleanup_preserves_legacy_source() {
        let scratch = Scratch::new();
        let legacy = scratch.0.join("disk.qcow2");
        let registry = scratch.0.join("registry.qcow2");
        fs::write(&legacy, "legacy registry source").unwrap();
        fs::write(&registry, "migrated registry").unwrap();
        fs::create_dir(scratch.0.join("session.qcow2")).unwrap();
        let mut vm = Vm {
            data: Some(scratch.0.clone()),
            stopping: true,
            migration_observed: true,
            ..Vm::default()
        };
        assert!(vm.finish(true).is_err());
        assert_eq!(
            fs::read_to_string(legacy).unwrap(),
            "legacy registry source"
        );
        assert_eq!(fs::read_to_string(registry).unwrap(), "migrated registry");
    }

    #[test]
    fn orphaned_qemu_disk_lock_blocks_reset_and_deletion() {
        use std::io::Read;
        struct Writer(Child);
        impl Drop for Writer {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
        let scratch = Scratch::new();
        let registry = scratch.image("registry.qcow2");
        let mut writer = Writer(
            command("qemu-io")
                .args(["-f", "qcow2"])
                .arg(&registry)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap(),
        );
        let mut prompt = [0; 9];
        writer
            .0
            .stdout
            .as_mut()
            .unwrap()
            .read_exact(&mut prompt)
            .unwrap();
        assert_eq!(&prompt, b"qemu-io> ");
        let before = fs::read(&registry).unwrap();
        assert!(require_stopped_disks(&scratch.0).is_err());
        let mut vm = Vm::default();
        assert!(vm.reset(&scratch.0).is_err());
        assert!(vm.delete_registry(&scratch.0).is_err());
        assert_eq!(fs::read(&registry).unwrap(), before);
    }

    #[test]
    fn registry_survives_os_changes_and_reset_but_requires_seed_only_once() {
        let scratch = Scratch::new();
        let image = scratch.image("os.qcow2");
        let seed = scratch.image(&format!("ig-registry-seed-{}.qcow2", architecture()));
        let data = scratch.0.join("data");
        prepare_disks(&image, &data).unwrap();
        let registry = data.join("registry.qcow2");
        assert!(image_info(&registry).get("backing-filename").is_none());
        assert_eq!(
            image_info(&data.join("session.qcow2"))["backing-filename"],
            image.to_string_lossy().as_ref()
        );
        let registry_before = fs::read(&registry).unwrap();
        fs::remove_file(seed).unwrap();
        let newer = scratch.image("new-os.qcow2");
        prepare_disks(&newer, &data).unwrap();
        assert_eq!(fs::read(&registry).unwrap(), registry_before);
        assert_eq!(
            fs::read_to_string(data.join("base-image.txt")).unwrap(),
            newer.to_string_lossy()
        );
        let invalid_image = scratch.0.join("invalid-os.qcow2");
        fs::write(&invalid_image, "not a qcow2 image").unwrap();
        assert!(prepare_disks(&invalid_image, &data).is_err());
        assert_eq!(fs::read(&registry).unwrap(), registry_before);
        fs::write(data.join("kubeconfig"), "credentials").unwrap();
        let mut vm = Vm {
            token: "secret".into(),
            ..Vm::default()
        };
        vm.reset(&data).unwrap();
        assert_eq!(fs::read(&registry).unwrap(), registry_before);
        assert!(data.join("base-image.txt").is_file());
        assert!(!data.join("session.qcow2").exists());
        assert!(!data.join("kubeconfig").exists());
        assert!(vm.token.is_empty());
        vm.delete_registry(&data).unwrap();
        assert!(!registry.exists());
        assert!(data.join("base-image.txt").is_file());
        fs::write(data.join("kubeconfig"), "preserve on failed start").unwrap();
        assert!(prepare_disks(&newer, &data).unwrap_err().contains("seed"));
        assert!(!registry.exists());
        assert_eq!(
            fs::read_to_string(data.join("kubeconfig")).unwrap(),
            "preserve on failed start"
        );
        fs::write(
            scratch
                .0
                .join(format!("ig-registry-seed-{}.qcow2", architecture())),
            "invalid seed",
        )
        .unwrap();
        assert!(prepare_disks(&newer, &data).is_err());
        assert!(!registry.exists());
        assert_eq!(
            fs::read_to_string(data.join("kubeconfig")).unwrap(),
            "preserve on failed start"
        );
        assert!(fs::read_dir(&data).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".tmp")));
    }

    #[test]
    fn legacy_is_untouched_until_migrated_ready_and_successful_requested_shutdown() {
        let scratch = Scratch::new();
        let image = scratch.image("os.qcow2");
        scratch.image(&format!("ig-registry-seed-{}.qcow2", architecture()));
        let data = scratch.0.join("data");
        fs::create_dir(&data).unwrap();
        let legacy = data.join("disk.qcow2");
        create_overlay(&image, &legacy).unwrap();
        let original = fs::read(&legacy).unwrap();
        prepare_disks(&image, &data).unwrap();
        assert_eq!(
            image_info(&data.join("migration.qcow2"))["backing-filename"],
            legacy.to_string_lossy().as_ref()
        );
        assert_eq!(fs::read(&legacy).unwrap(), original);
        let registry_before = fs::read(data.join("registry.qcow2")).unwrap();
        let mut vm = Vm::default();
        assert!(vm.reset(&data).unwrap_err().contains("migrat"));
        assert!(vm.delete_registry(&data).is_err());
        assert!(data.join("session.qcow2").is_file());
        for (ready, persistent, migrated, stopping, successful) in [
            (true, false, true, true, true),
            (false, true, true, true, true),
            (true, true, false, true, true),
            (true, true, true, false, true),
            (true, true, true, true, false),
        ] {
            vm.data = Some(data.clone());
            vm.stopping = stopping;
            let status = vm.guest_status(&serde_json::json!({
                "ready": ready, "registry_persistent": persistent, "registry_migrated": migrated,
            }));
            assert_eq!(status.ready, ready && persistent);
            vm.finish(successful).unwrap();
            assert_eq!(fs::read(&legacy).unwrap(), original);
            assert_eq!(
                fs::read(data.join("registry.qcow2")).unwrap(),
                registry_before
            );
        }
        vm.data = Some(data.clone());
        vm.stopping = true;
        assert!(
            vm.guest_status(&serde_json::json!({
                "ready": true, "registry_persistent": true, "registry_migrated": true,
            }))
            .ready
        );
        vm.finish(true).unwrap();
        assert!(!legacy.exists());
        assert!(!data.join("migration.qcow2").exists());
        assert_eq!(
            fs::read(data.join("registry.qcow2")).unwrap(),
            registry_before
        );
    }
}
