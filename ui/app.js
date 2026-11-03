/* global __TAURI__ */
const { invoke } = __TAURI__.core;
const $ = (id) => document.getElementById(id);
let running = false;
let busy = false;

async function action(fn) {
  $("error").textContent = "";
  try { return await fn(); }
  catch (error) { $("error").textContent = String(error); }
}

$("browse").onclick = () => action(async () => {
  const image = await invoke("choose_image");
  if (image) $("image").value = image;
});
$("start").onclick = () => action(async () => {
  if (!$("image").value) throw new Error("Choose a demo image first.");
  $("start").disabled = true;
  await invoke("start", { image: $("image").value });
});
$("stop").onclick = () => action(() => invoke("stop"));
$("headlamp").onclick = () => action(() => invoke("open_headlamp"));
$("access").onclick = () => action(async () => {
  const access = await invoke("credentials");
  $("token").value = access.token;
  $("copy").disabled = false;
  $("kubeconfig").textContent = `IG Desktop / kubectl kubeconfig: ${access.path}`;
});
$("copy").onclick = () => action(async () => {
  await navigator.clipboard.writeText($("token").value);
  $("copy").textContent = "Copied";
  setTimeout(() => { $("copy").textContent = "Copy"; }, 1500);
});
$("reset").onclick = () => action(async () => {
  if (!await invoke("reset")) return;
  $("token").value = "";
  $("copy").disabled = true;
  $("kubeconfig").textContent = "Demo data reset. Ready for a fresh start.";
});

async function poll() {
  if (busy) return;
  busy = true;
  try {
    const state = await invoke("status");
    running = state.running;
    $("status").textContent = state.message;
    $("status").className = state.ready ? "ready" : "";
    $("headlamp").disabled = $("access").disabled = !state.ready;
  } catch (error) {
    running = false;
    $("status").textContent = "Stopped";
    $("error").textContent = String(error);
    $("headlamp").disabled = $("access").disabled = true;
  } finally {
    $("start").disabled = running;
    $("stop").disabled = !running;
    $("browse").disabled = $("reset").disabled = running;
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
});
