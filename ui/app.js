/* global __TAURI__ */
const { invoke } = __TAURI__.core;
const { listen } = __TAURI__.event;
const $ = (id) => document.getElementById(id);
let running = false;
let busy = false;
let view = "demo";
let appsOpen = false;
let catalog = { installed: [], available: null, error: null };
let selected = null; // { release, variant } for downloaded disks, null for a local file.
let downloading = false;
let pruneAfterReady = null;
let observability = false;
const VARIANTS = ["minimal", "observability"];

function gb(bytes) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}

function igVersion(versions) {
  return versions && versions.IG_VERSION ? ` · IG ${versions.IG_VERSION}` : "";
}

// Newest installed release that holds the whole variant.
function installedVariant(variant) {
  return catalog.installed.find((release) => release.variants.includes(variant));
}

function select(release, variant) {
  const local = catalog.installed.find((item) => item.release === release);
  selected = { release, variant };
  $("image").value = local.paths[variant];
  render();
}

// Local releases first (instant), then GitHub (may be slow or offline).
async function refreshCatalog() {
  for (const online of [false, true]) {
    const next = await invoke("image_catalog", { online });
    catalog = online || !catalog.available ? next : { ...next, available: catalog.available, error: catalog.error };
    if (!selected && next.selected) {
      selected = next.selected;
    } else if (!selected && !$("image").value) {
      const preferred = ["observability", "minimal"].find(installedVariant);
      if (preferred) select(installedVariant(preferred).release, preferred);
    }
    render();
  }
}

function render() {
  const { available } = catalog;
  for (const variant of VARIANTS) {
    const card = $(`variant-${variant}`);
    const state = card.querySelector(".variant-state");
    const button = card.querySelector(".variant-action");
    const local = installedVariant(variant);
    const offer = available && available.variants[variant];
    card.setAttribute("aria-pressed", String(!!selected && selected.variant === variant));
    card.setAttribute("aria-disabled", String(!local || running));
    button.hidden = true;
    state.className = "variant-state";
    if (local) {
      state.textContent = `Downloaded · ${local.release}${igVersion(local.versions)}`;
      state.className = "variant-state ready";
    } else if (offer) {
      const partial = offer.missing < offer.size;
      state.textContent = `${gb(offer.missing)}${partial ? " more" : ""}${igVersion(available.versions)}`;
      button.textContent = partial ? "Add" : "Download";
      button.hidden = false;
      button.disabled = downloading;
    } else {
      state.textContent = catalog.error ? "Not downloaded" : "Checking…";
    }
  }
  const current = selected && catalog.installed.find((item) => item.release === selected.release);
  const update = available && selected && current && available.release !== current.release
    && available.variants[selected.variant] && !catalog.installed.some((item) => item.release === available.release
      && item.variants.includes(selected.variant));
  $("update").hidden = !update;
  if (update) {
    $("update-text").textContent = `Update available: ${available.release}${igVersion(available.versions)} · ${gb(available.variants[selected.variant].missing)}`;
    $("update-button").disabled = downloading;
  }
  $("catalog-error").hidden = !catalog.error;
  $("catalog-error").textContent = catalog.error ? `${catalog.error} Downloaded and local disks still work.` : "";
}

async function download(variant) {
  if (downloading) return;
  const { available } = catalog;
  downloading = true;
  $("download").hidden = false;
  $("progress").value = 0;
  $("progress-text").textContent = "Starting download…";
  render();
  try {
    await invoke("download_variant", { release: available.release, variant });
    await refreshCatalog();
    // The next start uses it; a running VM keeps its current disk.
    select(available.release, variant);
  } finally {
    downloading = false;
    $("download").hidden = true;
    render();
  }
}

listen("image-progress", ({ payload }) => {
  $("progress").value = payload.total ? payload.done / payload.total : 0;
  $("progress-text").textContent = `${gb(payload.done)} of ${gb(payload.total)}`;
});
for (const variant of VARIANTS) {
  const card = $(`variant-${variant}`);
  card.onclick = (event) => action(async () => {
    if (event.target.closest(".variant-action")) return download(variant);
    const local = installedVariant(variant);
    if (local && !running) select(local.release, variant);
  });
  card.onkeydown = (event) => {
    if (event.target === card && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      card.click();
    }
  };
}
$("cancel-download").onclick = () => invoke("cancel_download");
$("update-button").onclick = () => action(() => download(selected.variant));

async function action(fn) {
  $("error").textContent = "";
  try { return await fn(); }
  catch (error) { $("error").textContent = String(error); }
}

$("browse").onclick = () => action(async () => {
  const image = await invoke("choose_image");
  if (image) {
    $("image").value = image;
    selected = null;
    render();
  }
});
$("start").onclick = () => action(async () => {
  if (!$("image").value) throw new Error("Choose a demo image first.");
  $("start").disabled = true;
  await invoke("start", { image: $("image").value });
  pruneAfterReady = selected && selected.release;
  $("kubeconfig").textContent = "Get access credentials for this fresh cluster.";
});
$("stop").onclick = () => action(() => invoke("stop"));
$("access").onclick = () => action(async () => {
  const access = await invoke("credentials");
  $("kubeconfig").textContent = `IG Desktop / kubectl kubeconfig: ${access.path}`;
});
$("reset").onclick = () => action(async () => {
  if (!await invoke("reset")) return;
  $("kubeconfig").textContent = "Cluster reset. Registry images are kept.";
});
$("delete-registry").onclick = () => action(async () => {
  if (!await invoke("delete_registry")) return;
  $("kubeconfig").textContent = "Registry images deleted. The next start restores the bundled demo images.";
});

function bounds() {
  const { x, y, width, height } = $("app-host").getBoundingClientRect();
  return { x, y, width, height };
}

function show(next) {
  view = next;
  $("demo-view").hidden = next !== "demo";
  $("app-view").hidden = next === "demo";
  for (const name of ["demo", "headlamp", "grafana"]) {
    const item = $(`nav-${name}`);
    if (name === next) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
}

async function open(name) {
  // Logging in takes a moment; switching away meanwhile would strand the new page on top.
  $("nav-demo").disabled = $("nav-headlamp").disabled = $("nav-grafana").disabled = true;
  try {
    show(name);
    $("location").textContent = "Opening…";
    await invoke("open_app", { name, bounds: bounds() });
    appsOpen = true;
    await updateLocation();
  } catch (error) {
    show("demo");
    throw error;
  } finally {
    $("nav-demo").disabled = false;
    await poll();
  }
}

async function updateLocation() {
  if (view === "demo") return;
  try { $("location").textContent = await invoke("app_location", { name: view }); }
  catch { /* The page is still being created or was just closed. */ }
}

$("nav-demo").onclick = () => action(async () => {
  show("demo");
  await invoke("hide_apps");
});
$("nav-headlamp").onclick = () => action(() => open("headlamp"));
$("nav-grafana").onclick = () => action(() => open("grafana"));
for (const control of ["back", "forward", "reload"]) {
  $(control).onclick = () => action(() => invoke("navigate_app", { name: view, action: control }));
}
new ResizeObserver(() => {
  if (view !== "demo" && appsOpen) {
    invoke("set_app_bounds", { name: view, bounds: bounds() }).catch(() => {});
  }
}).observe($("app-host"));
setInterval(updateLocation, 1000);

async function closeApps() {
  show("demo");
  if (appsOpen) {
    appsOpen = false;
    await invoke("close_apps");
  }
}

async function poll() {
  if (busy) return;
  busy = true;
  try {
    const state = await invoke("status");
    running = state.running;
    $("status").textContent = state.message;
    $("status").className = state.ready ? "ready" : "";
    $("rail-status").textContent = state.ready ? "Ready" : state.running ? "Starting" : "Stopped";
    $("rail-status").className = state.ready ? "rail-status ready" : "rail-status";
    observability = state.layers.includes("observability");
    $("nav-headlamp").hidden = $("nav-grafana").hidden = !observability;
    $("nav-headlamp").disabled = $("nav-grafana").disabled = !state.ready || !observability;
    $("access").disabled = !state.ready;
    if (state.ready && pruneAfterReady) {
      // The new release booted; older downloads are no longer needed.
      const keep = pruneAfterReady;
      pruneAfterReady = null;
      invoke("prune_images", { keep }).then(refreshCatalog).catch(() => {});
    }
    if (!running) await closeApps();
  } catch (error) {
    running = false;
    $("status").textContent = "Stopped";
    $("error").textContent = String(error);
    $("rail-status").textContent = "Stopped";
    $("rail-status").className = "rail-status";
    $("nav-headlamp").disabled = $("nav-grafana").disabled = $("access").disabled = true;
    $("nav-headlamp").hidden = $("nav-grafana").hidden = true;
    await closeApps().catch(() => {});
  } finally {
    $("start").disabled = running;
    $("stop").disabled = !running;
    $("browse").disabled = $("reset").disabled = $("delete-registry").disabled = running;
    render();
    busy = false;
  }
}
action(async () => {
  const info = await invoke("info");
  $("arch").textContent = info.arch;
  $("image").value = info.image;
  $("data").textContent = `Data & logs: ${info.data}`;
  await poll();
  setInterval(poll, 3000);
  await refreshCatalog();
});
