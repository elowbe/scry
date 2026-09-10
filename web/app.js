"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  games: [], selected: null, config: null, pc: null, channel: null, pointerChannel: null, pointerTimer: null,
  remoteStream: null, statsTimer: null, sessionTimer: null, sessionCheckBusy: false, gamepadFrame: null,
  quality: 4, settings: null, dlssEnabled: false, settingsBusy: false, lastJitterDelay: 0, lastJitterCount: 0,
  keyDown: new Set(), lastPadState: "", lastBytes: 0, lastStatsAt: 0,
};

const DOM_CODES = [
  "Escape", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
  "Backquote", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9", "Digit0", "Minus", "Equal", "Backspace",
  "Tab", "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI", "KeyO", "KeyP", "BracketLeft", "BracketRight", "Backslash",
  "CapsLock", "KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK", "KeyL", "Semicolon", "Quote", "Enter",
  "ShiftLeft", "KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM", "Comma", "Period", "Slash", "ShiftRight",
  "ControlLeft", "MetaLeft", "AltLeft", "Space", "AltRight", "MetaRight", "ContextMenu", "ControlRight",
  "Insert", "Delete", "Home", "End", "PageUp", "PageDown", "ArrowUp", "ArrowLeft", "ArrowDown", "ArrowRight",
  "NumLock", "NumpadDivide", "NumpadMultiply", "NumpadSubtract", "Numpad7", "Numpad8", "Numpad9", "NumpadAdd", "Numpad4", "Numpad5", "Numpad6", "Numpad1", "Numpad2", "Numpad3", "Numpad0", "NumpadDecimal", "NumpadEnter",
  "PrintScreen", "ScrollLock", "Pause", "AudioVolumeMute", "AudioVolumeDown", "AudioVolumeUp", "MediaTrackPrevious", "MediaPlayPause", "MediaTrackNext",
  "IntlBackslash", "IntlRo", "IntlYen", "Convert", "NonConvert", "KanaMode", "Lang1", "Lang2",
  "NumpadEqual", "NumpadComma", "BrowserBack", "BrowserForward", "BrowserRefresh", "BrowserHome",
  "LaunchMail", "LaunchApp1", "LaunchApp2", "MediaStop",
];
const CODE_IDS = new Map(DOM_CODES.map((code, index) => [code, index]));

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw new Error(body?.error || `${response.status} ${response.statusText}`);
  return body;
}

function initials(name) {
  return name.split(/\s+/).slice(0, 3).map((part) => part[0]).join("").toUpperCase();
}

function bytesLabel(bytes) {
  if (!bytes) return "Installed";
  return `${(bytes / 1024 ** 3).toFixed(bytes > 10 * 1024 ** 3 ? 0 : 1)} GB`;
}

function gameCard(game) {
  const button = document.createElement("button");
  button.className = "game-card";
  button.dataset.search = `${game.name} ${game.provider}`.toLowerCase();
  const cover = document.createElement("span");
  cover.className = "cover";
  const image = document.createElement("img");
  image.src = game.artwork_url;
  image.alt = "";
  image.loading = "lazy";
  image.addEventListener("error", () => {
    image.remove();
    const fallback = document.createElement("span");
    fallback.className = "cover-fallback";
    fallback.textContent = initials(game.name);
    cover.prepend(fallback);
  }, { once: true });
  const provider = document.createElement("span");
  provider.className = `provider-pill ${game.provider}`;
  provider.textContent = game.provider;
  cover.append(image, provider);
  const title = document.createElement("span");
  title.className = "game-title";
  title.textContent = game.name;
  const subtitle = document.createElement("span");
  subtitle.className = "game-subtitle";
  subtitle.textContent = game.provider === "desktop" ? "Stream your host desktop" : `${bytesLabel(game.size_bytes)} · Installed`;
  button.append(cover, title, subtitle);
  button.addEventListener("click", () => openGame(game));
  return button;
}

function renderGames(query = "") {
  const grid = $("#gameGrid");
  grid.replaceChildren();
  const normalized = query.trim().toLowerCase();
  const games = state.games.filter((game) => `${game.name} ${game.provider}`.toLowerCase().includes(normalized));
  games.forEach((game) => grid.append(gameCard(game)));
  $("#gameCount").textContent = `${games.length} of ${state.games.length} streaming options`;
  $("#emptyState").classList.toggle("hidden", games.length !== 0);
}

function openGame(game) {
  state.selected = game;
  $("#launchButton").textContent = game.provider === "desktop" ? "Stream desktop" : "Launch game";
  $("#dialogProvider").textContent = game.provider === "desktop" ? "Desktop streaming" : `${game.provider}`;
  $("#dialogTitle").textContent = game.name;
  $("#dialogMeta").textContent = game.provider === "desktop" ? "Share the configured host monitor with audio, keyboard and mouse control" : `${bytesLabel(game.size_bytes)} on the host · ${game.proton_ready ? "compatibility prefix ready" : "prefix created on first launch"}`;
  $("#dialogArt").style.backgroundImage = `linear-gradient(90deg, transparent 55%, #0e1317), url("${game.artwork_url}")`;
  $("#dialogError").classList.add("hidden");
  $("#dlssToggle").checked = false;
  updateQualityPreview();
  $("#gameDialog").showModal();
}

function updateQualityPreview() {
  const enabled = $("#dlssToggle").checked;
  const preset = state.config?.quality_options?.[state.quality];
  if (preset) { $("#dialogResolutionName").textContent = `${preset.height}p`; $("#dialogResolution").textContent = `${preset.width} × ${preset.height}`; }
  const fps = enabled ? (state.config?.dlss_target_fps || 16) : (state.config?.fps || 60);
  $("#dialogFps").textContent = fps;
  $("#dialogFpsLabel").textContent = enabled ? "enhanced fps target" : "frames / sec";
  $("#dlssDescription").textContent = enabled
    ? `Feature 18 · ${fps} fps target · live latency shown in player`
    : "Independent post-process · adds render latency";
}

async function loadHealth() {
  const grid = $("#healthGrid");
  grid.innerHTML = '<div class="health-card"><span class="health-indicator"></span><div><h3>Running checks</h3><p>Reading host capabilities…</p></div></div>';
  try {
    const health = await api("/api/health");
    grid.replaceChildren(...health.checks.map((check) => {
      const card = document.createElement("article");
      card.className = `health-card ${check.status}`;
      const indicator = document.createElement("span");
      indicator.className = "health-indicator";
      const copy = document.createElement("div");
      const title = document.createElement("h3");
      title.textContent = check.label;
      const detail = document.createElement("p");
      detail.textContent = check.detail;
      copy.append(title, detail);
      card.append(indicator, copy);
      return card;
    }));
    $("#hostDot").className = `status-dot ${health.ready ? "ready" : "error"}`;
    $("#hostState").textContent = health.ready ? "Host ready" : "Host needs setup";
    $("#hostDetail").textContent = health.ready ? "Secure stream available" : "Open Host status";
  } catch (error) {
    $("#hostDot").className = "status-dot error";
    $("#hostState").textContent = "Host unavailable";
    $("#hostDetail").textContent = error.message;
  }
}

function switchView(name) {
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $("#libraryView").classList.toggle("active", name === "library");
  $("#systemView").classList.toggle("active", name === "system");
  if (name === "system") loadHealth();
}

async function launchSelected() {
  if (!state.selected) return;
  const button = $("#launchButton");
  const errorText = $("#dialogError");
  button.disabled = true;
  const priorLabel = button.innerHTML;
  button.textContent = state.selected.provider === "desktop" ? "Starting desktop…" : "Launching game…";
  errorText.classList.add("hidden");
  try {
    await api("/api/session/start", {
      method: "POST",
      body: JSON.stringify({
        provider: state.selected.provider,
        game_id: state.selected.id,
        dlss_enabled: $("#dlssToggle").checked,
        quality: state.quality,
      }),
    });
    $("#gameDialog").close();
    await openPlayer(state.selected);
  } catch (error) {
    errorText.textContent = error.message;
    errorText.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.innerHTML = priorLabel;
  }
}

function waitForIce(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const finish = (error) => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", listener);
      error ? reject(error) : resolve();
    };
    const listener = () => {
      if (pc.iceGatheringState === "complete") finish();
    };
    const timer = setTimeout(() => finish(new Error("Network candidate gathering timed out. Check the client network or configured STUN/TURN service.")), 15000);
    pc.addEventListener("icegatheringstatechange", listener);
    listener();
  });
}

function reportConnection(pc, event, detail = "") {
  return api("/api/webrtc/diagnostics", {method: "POST", body: JSON.stringify({
    event, ice_state: pc.iceConnectionState, connection_state: pc.connectionState, detail,
  })}).catch(() => {});
}

function renderQuality() {
  const preset = state.config?.quality_options?.[Number($("#qualitySlider").value)];
  if (!preset) return;
  $("#qualityName").textContent = preset.name;
  const s = state.settings?.stream || preset;
  $("#qualityDetail").textContent = `${s.width}×${s.height} · ${s.bitrate_mbps || "Quality-driven"} Mbps · ${state.dlssEnabled ? (state.settings?.dlss.target_fps || state.config.dlss_target_fps) : s.fps} fps target`;
}

function buildAdvancedSettings() {
  const root = $("#settingsFields");
  root.replaceChildren();
  for (const [section, fields] of Object.entries(state.config.settings_schema || {})) {
    const group = document.createElement("details");
    group.open = section === "stream";
    const title = document.createElement("summary");
    title.textContent = section === "stream" ? "Video & network" : "DLSS5 rendering";
    group.append(title);
    for (const [key, [label, kind, values]] of Object.entries(fields)) {
      const row = document.createElement("label");
      row.className = "setting-row";
      const text = document.createElement("span"); text.textContent = label;
      const input = document.createElement(kind === "choice" ? "select" : "input");
      input.id = `setting-${section}-${key}`;
      if (kind === "choice") {
        for (const value of values) {
          const option = document.createElement("option"); option.value = String(value);
          option.textContent = key === "upscaling_factor" ? ({1:"DLAA / native",1.5:"Quality",1.724:"Balanced",2:"Performance",3:"Ultra Performance"}[value]) : key === "temporal_method" ? ({static_regions:"Static regions only (no warping)", optical_flow:"Optical flow (experimental; may smear)"}[value]) : String(value);
          input.append(option);
        }
      } else {
        input.type = kind === "bool" ? "checkbox" : "number";
        if (kind !== "bool") { input.min = values[0]; input.max = values[1]; input.step = kind === "int" ? "1" : "0.05"; input.required = true; }
      }
      row.append(text, input); group.append(row);
    }
    root.append(group);
  }
  fillAdvancedSettings();
}

function fillAdvancedSettings() {
  for (const [section, fields] of Object.entries(state.settings || {})) {
    for (const [key, value] of Object.entries(fields)) {
      const input = $(`#setting-${section}-${key}`);
      if (!input) continue;
      if (typeof value === "boolean") input.checked = value;
      else input.value = String(value);
    }
  }
}

function readAdvancedSettings() {
  const settings = {};
  for (const [section, fields] of Object.entries(state.config.settings_schema)) {
    settings[section] = {};
    for (const [key, [, kind, values]] of Object.entries(fields)) {
      const input = $(`#setting-${section}-${key}`);
      settings[section][key] = kind === "bool" ? input.checked :
        (kind !== "choice" || typeof values[0] === "number" ? Number(input.value) : input.value);
    }
  }
  return settings;
}

function renderLiveSettings() {
  $("#qualitySlider").value = state.quality;
  $("#qualitySlider").disabled = state.settingsBusy;
  $("#liveDlss").disabled = state.settingsBusy;
  $("#applyAdvanced").disabled = state.settingsBusy;
  $("#resetAdvanced").disabled = state.settingsBusy;
  $("#originalRenderer").disabled = state.settingsBusy;
  const dlss = state.settings?.dlss;
  $("#rendererDetail").textContent = dlss?.temporal_stability
    ? (dlss.temporal_method === "optical_flow"
      ? "Experimental optical flow: may smear or warp during motion."
      : "Static regions only: changed regions use the current independent render. Motion may still flicker.")
    : "Original independent rendering: no stabilization or motion history.";
  $("#liveDlss").textContent = `DLSS5 ${state.dlssEnabled ? "on" : "off"}`;
  $("#liveDlss").setAttribute("aria-pressed", String(state.dlssEnabled));
  $("#dlssStat").classList.toggle("hidden", !state.dlssEnabled);
  renderQuality();
}

function applyReceiverLatency() {
  const preset = state.config?.quality_options?.[state.quality];
  for (const receiver of state.pc?.getReceivers() || []) {
    try {
      const ms = state.settings?.stream.jitter_ms ?? preset?.jitter_ms ?? 20;
      if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = ms;
      else if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = ms / 1000;
    } catch (_) { /* The browser chooses its supported buffering limits. */ }
  }
}

async function applyStreamSettings(changes) {
  if (state.settingsBusy) return;
  state.settingsBusy = true;
  renderLiveSettings();
  $("#settingsStatus").textContent = changes.dlss_enabled ? "Warming up DLSS5…" : "Applying stream settings…";
  try {
    const result = await api("/api/session/settings", {method: "POST", body: JSON.stringify(changes)});
    state.quality = result.quality;
    state.dlssEnabled = result.dlss_enabled;
    if (result.settings) { state.settings = result.settings; fillAdvancedSettings(); }
    try { localStorage.setItem("game-stream-quality", String(state.quality)); } catch (_) {}
    applyReceiverLatency();
    $("#settingsStatus").textContent = state.dlssEnabled
      ? `DLSS5 on · ${state.settings?.dlss.target_fps || state.config.dlss_target_fps} fps target; adds processing latency.`
      : "Settings applied. Game and connection stayed running.";
    showToast(changes.dlss_enabled !== undefined ? `DLSS5 ${state.dlssEnabled ? "enabled" : "disabled"}` : "Stream quality updated");
  } catch (error) {
    $("#settingsStatus").textContent = error.message;
    showToast(error.message);
  } finally {
    state.settingsBusy = false;
    renderLiveSettings();
  }
}

async function sendHostEscape() {
  state.keyDown.clear();
  try { await api("/api/input/escape", {method: "POST", body: "{}"}); }
  catch (error) { showToast(error.message); }
}

async function connectWebRtc() {
  closePeer();
  const session = await api("/api/session");
  state.quality = session.quality;
  state.dlssEnabled = session.dlss_enabled;
  state.settings = session.settings || state.config.settings;
  fillAdvancedSettings();
  renderLiveSettings();
  const pc = new RTCPeerConnection({ iceServers: state.config.ice_servers || [], bundlePolicy: "max-bundle" });
  state.pc = pc;
  startSessionMonitor();
  state.remoteStream = new MediaStream();
  $("#video").srcObject = state.remoteStream;
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  const channel = pc.createDataChannel("input", { ordered: false, maxRetransmits: 0 });
  channel.binaryType = "arraybuffer";
  state.channel = channel;
  const pointer = pc.createDataChannel("pointer", {ordered: true});
  pointer.binaryType = "arraybuffer";
  state.pointerChannel = pointer;
  pointer.addEventListener("message", event => {
    if (state.pc !== pc || typeof event.data !== "string") return;
    try { receiveCursor(JSON.parse(event.data)); } catch (_) { /* Invalid metadata. */ }
  });
  pointer.addEventListener("open", () => {
    if (state.pc !== pc) return;
    state.pointerTimer = setInterval(() => flushPointer(), 16);
  });
  pointer.addEventListener("close", () => {
    if (state.pc === pc) {
      resetCursor();
      if (state.pointerTimer) clearInterval(state.pointerTimer);
      state.pointerTimer = null;
      document.exitPointerLock?.();
    }
  });

  pc.addEventListener("track", (event) => {
    state.remoteStream.addTrack(event.track);
    applyReceiverLatency();
    if (event.track.kind === "video") {
      $("#video").play().catch(() => {});
    }
  });
  pc.addEventListener("connectionstatechange", () => {
    if (state.pc !== pc) return;
    reportConnection(pc, "connectionstatechange");
    const status = pc.connectionState;
    $("#playerStatus").textContent = status === "connected" ? "Stream connected" : `Stream ${status}`;
    if (status === "failed") {
      const message = pc.iceConnectionState === "failed"
        ? "Media connection failed (ICE). Check LAN isolation / UDP firewall rules. Host diagnostics have been saved."
        : "The WebRTC transport failed. Host diagnostics have been saved.";
      $("#playerHint").textContent = message;
      showToast(message);
    } else if (status === "disconnected") showToast("Connection interrupted — use reconnect");
  });
  pc.addEventListener("iceconnectionstatechange", () => reportConnection(pc, "iceconnectionstatechange"));
  pc.addEventListener("icecandidateerror", (event) => {
    reportConnection(pc, "icecandidateerror", `${event.errorCode}: ${event.errorText}`);
  });
  channel.addEventListener("open", () => {
    $("#playerBackdrop").classList.add("ready");
    $("#playerStatus").textContent = "Stream connected";
    startGamepadLoop();
  });
  channel.addEventListener("message", (event) => {
    if (typeof event.data !== "string") return;
    try {
      const message = JSON.parse(event.data);
      if (message.type === "pipeline" && message.last_error) showToast(message.last_error);
      if (message.type === "pipeline" && message.dlss_enabled) {
        $("#dlssStat").classList.remove("hidden");
        $("#statDlss").textContent = message.last_error
          ? "error"
          : `${message.dlss_processing_ms.toFixed(0)}ms · ${message.output_fps}fps`;
      }
    } catch (_) { /* Ignore non-control text. */ }
  });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitForIce(pc);
  const answer = await api("/api/webrtc/offer", {
    method: "POST",
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
  });
  await pc.setRemoteDescription(answer);
  applyReceiverLatency();
  startStats();
}

async function openPlayer(game) {
  state.selected = game;
  $("#stopButton").title = game.provider === "desktop" ? "End desktop stream (applications stay open)" : "Save your game, then end the stream and quit the game";
  $("#player").classList.remove("hidden");
  $("#playerBackdrop").classList.remove("ready");
  $("#playerStatus").textContent = "Starting stream";
  $("#playerHint").textContent = game.provider === "desktop" ? "Connecting to the host desktop…" : "Waiting for the first captured frame…";
  $("#playingName").textContent = game.name;
  try {
    await connectWebRtc();
  } catch (error) {
    $("#playerStatus").textContent = "Stream could not start";
    $("#playerHint").textContent = error.message;
    showToast(error.message);
  }
}

function closePeer() {
  releaseAll();
  resetCursor();
  if (state.pointerTimer) clearInterval(state.pointerTimer);
  state.pointerTimer = null;
  if (state.pointerChannel) state.pointerChannel.close();
  state.pointerChannel = null;
  if (state.statsTimer) clearInterval(state.statsTimer);
  state.statsTimer = null;
  if (state.sessionTimer) clearInterval(state.sessionTimer);
  state.sessionTimer = null;
  state.sessionCheckBusy = false;
  if (state.gamepadFrame) cancelAnimationFrame(state.gamepadFrame);
  state.gamepadFrame = null;
  if (state.channel) state.channel.close();
  if (state.pc) state.pc.close();
  state.pc = state.channel = null;
  state.remoteStream = null;
  state.lastPadState = "";
}

async function stopSession() {
  if (state.selected?.provider !== "desktop" && !window.confirm("Save your game before closing. Ending this session will also quit the game. Continue?")) return;
  closePeer();
  document.exitPointerLock?.();
  try { await api("/api/session/stop", { method: "POST", body: "{}" }); }
  finally {
    $("#player").classList.add("hidden");
    $("#video").srcObject = null;
    $("#dlssStat").classList.add("hidden");
  }
}

async function checkSession() {
  if (state.sessionCheckBusy) return;
  state.sessionCheckBusy = true;
  try {
    const session = await api("/api/session");
    if (session.active) return;
    closePeer();
    document.exitPointerLock?.();
    $("#player").classList.add("hidden");
    $("#video").srcObject = null;
    $("#dlssStat").classList.add("hidden");
    showToast(session.last_error || "The game closed, so the stream ended.");
  } catch (_) { /* A temporary API failure must not tear down a live game. */ }
  finally { state.sessionCheckBusy = false; }
}

function startSessionMonitor() {
  if (state.sessionTimer) clearInterval(state.sessionTimer);
  state.sessionTimer = setInterval(checkSession, 1000);
}

function canSend() { return state.channel?.readyState === "open"; }
function sendBytes(buffer) {
  const type = new Uint8Array(buffer)[0];
  if (type !== 0x10 && state.pointerChannel?.readyState === "open") state.pointerChannel.send(buffer);
  else if (canSend()) state.channel.send(buffer);
}

function sendKey(code, down) {
  const id = CODE_IDS.get(code);
  if (id === undefined) return;
  const buffer = new ArrayBuffer(4);
  const view = new DataView(buffer);
  view.setUint8(0, 0x01);
  view.setUint8(1, down ? 1 : 0);
  view.setUint16(2, id, true);
  sendBytes(buffer);
}

const DEFAULT_CURSOR_IMAGE = "data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2224%22 height=%2224%22%3E%3Cpath d=%22M2 2L2 21L7 16L11 23L15 21L11 14L19 14Z%22 fill=%22white%22 stroke=%22black%22/%3E%3C/svg%3E";
let cursor = {x: .5, y: .5, epoch: 0, ready: false, visible: true, pending: false, relativeX: 0, relativeY: 0};

function resetCursor() {
  cursor = {x: .5, y: .5, epoch: 0, ready: false, visible: true, pending: false, relativeX: 0, relativeY: 0};
  $("#cursorLayer").classList.add("hidden");
  $("#localCursor").src = DEFAULT_CURSOR_IMAGE;
}

function cursorRect() {
  const video = $("#video"), rect = video.getBoundingClientRect();
  const width = video.videoWidth || state.config?.width || 1920;
  const height = video.videoHeight || state.config?.height || 1080;
  const scale = Math.min(rect.width / width, rect.height / height);
  return {left: rect.left + (rect.width-width*scale)/2, top: rect.top + (rect.height-height*scale)/2,
    width: width*scale, height: height*scale};
}

function renderCursor() {
  const layer = $("#cursorLayer");
  const visible = cursor.visible && document.pointerLockElement === $("#video");
  layer.classList.toggle("hidden", !visible);
  if (!visible) return;
  const rect = cursorRect();
  Object.assign(layer.style, {left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`});
  const image = $("#localCursor");
  if (cursor.image && image.getAttribute("src") !== cursor.image) image.src = cursor.image;
  const sx = rect.width / (cursor.width || rect.width), sy = rect.height / (cursor.height || rect.height);
  Object.assign(image.style, {width: `${(cursor.image_width || 24)*sx}px`, height: `${(cursor.image_height || 24)*sy}px`,
    transform: `translate(${cursor.x*rect.width-(cursor.hotspot?.[0]||0)*sx}px, ${cursor.y*rect.height-(cursor.hotspot?.[1]||0)*sy}px)`});
}

function receiveCursor(message) {
  if (message.type === "cursor_error") { showToast(`Cursor image unavailable: ${message.message}`); return; }
  if (message.type === "cursor_image") {
    if (!Number.isInteger(message.total) || message.total <= 0 || message.total > 1048576 || typeof message.data !== "string") return;
    if (message.offset === 0) cursor.assembly = {id: message.id, total: message.total, data: ""};
    const a = cursor.assembly;
    if (!a || a.id !== message.id || a.data.length !== message.offset || a.data.length + message.data.length > a.total) return;
    a.data += message.data;
    if (a.data.length === a.total && a.data.startsWith("data:image/png;base64,")) {
      cursor.image = a.data; cursor.imageId = a.id; cursor.assembly = null;
    }
    return;
  }
  if (message.type !== "cursor" || !Number.isInteger(message.epoch) || message.epoch < cursor.epoch ||
      ![message.x,message.y,message.width,message.height].every(Number.isFinite)) return;
  const wasReady = cursor.ready;
  if (message.epoch > cursor.epoch) {
    cursor.epoch = message.epoch;
    // Only explicit host warps/mode changes can move the local pointer.
    // Ordinary cursor-image updates never pull it back to a delayed host position.
    const verifiedReset = message.warp_reason === "external" || message.warp_reason === "visibility" ||
      message.visible !== cursor.visible || !wasReady;
    if (message.warp && verifiedReset && (wasReady || !cursor.seeded)) {
      cursor.x = Math.max(0, Math.min(1, message.x)); cursor.y = Math.max(0, Math.min(1, message.y));
      cursor.pending = false;
    } else if (message.visible && document.pointerLockElement === $("#video")) {
      // Older hosts may label stale metadata as a warp. Keep our position and
      // resend it with the new epoch so their stale-packet guard accepts it.
      cursor.pending = true;
    }
  }
  if (message.image_id === 0) {
    cursor.image = null; cursor.imageId = 0; cursor.assembly = null;
    $("#localCursor").src = DEFAULT_CURSOR_IMAGE;
  }
  cursor.ready = true;
  if (cursor.visible !== message.visible) { cursor.relativeX = cursor.relativeY = 0; }
  cursor.visible = message.visible;
  cursor.width = message.width; cursor.height = message.height;
  cursor.hotspot = message.hotspot ?? cursor.hotspot;
  cursor.image_width = message.image_width ?? cursor.image_width;
  cursor.image_height = message.image_height ?? cursor.image_height;
  if (!wasReady && cursor.seeded) cursor.pending = true;
  renderCursor();
}

function flushPointer(force = false) {
  if (!cursor.pending || state.pointerChannel?.readyState !== "open") return;
  if (!force && state.pointerChannel.bufferedAmount > 4096) return;
  cursor.pending = false;
  if (!cursor.visible) {
    // Raw-input FPS games need relative camera motion while their cursor is hidden.
    const dx = cursor.relativeX, dy = cursor.relativeY;
    cursor.relativeX = cursor.relativeY = 0;
    sendRelativeMouse(dx, dy);
    return;
  }
  const buffer = new ArrayBuffer(9), view = new DataView(buffer);
  view.setUint8(0, 0x05); view.setUint32(1, cursor.epoch, true);
  view.setUint16(5, Math.round(cursor.x*65535), true); view.setUint16(7, Math.round(cursor.y*65535), true);
  state.pointerChannel.send(buffer);
}

function sendMouseMove(dx, dy) {
  if (state.pointerChannel?.readyState !== "open") { sendRelativeMouse(dx, dy); return; }
  const rect = cursorRect();
  if (cursor.visible && rect.width > 0 && rect.height > 0) {
    cursor.x = Math.max(0, Math.min(1, cursor.x + dx/rect.width));
    cursor.y = Math.max(0, Math.min(1, cursor.y + dy/rect.height));
  } else {
    cursor.relativeX += dx; cursor.relativeY += dy;
  }
  cursor.pending = true;
  renderCursor();
}

function sendRelativeMouse(dx, dy) {
  dx = Math.round(dx); dy = Math.round(dy);
  while (dx || dy) {
    const x = Math.max(-32768, Math.min(32767, dx)), y = Math.max(-32768, Math.min(32767, dy));
    const buffer = new ArrayBuffer(5), view = new DataView(buffer);
    view.setUint8(0, 0x02); view.setInt16(1, x, true); view.setInt16(3, y, true);
    sendBytes(buffer);
    dx -= x; dy -= y;
  }
}

function sendMouseButton(button, down) {
  if (button < 0 || button > 4) return;
  flushPointer(true);
  const buffer = new Uint8Array([0x03, button, down ? 1 : 0]);
  sendBytes(buffer.buffer);
}

function sendWheel(dx, dy) {
  flushPointer(true);
  const buffer = new ArrayBuffer(5);
  const view = new DataView(buffer);
  view.setUint8(0, 0x04);
  view.setInt16(1, Math.max(-32768, Math.min(32767, Math.round(dx))), true);
  view.setInt16(3, Math.max(-32768, Math.min(32767, Math.round(dy))), true);
  sendBytes(buffer);
}

function releaseAll() {
  cursor.pending = false; cursor.relativeX = cursor.relativeY = 0;
  state.keyDown.clear();
  sendBytes(new Uint8Array([0x7f]).buffer);
}

function sendGamepad(gamepad, connected = true) {
  const buffer = new ArrayBuffer(19);
  const view = new DataView(buffer);
  let buttons = 0;
  if (gamepad) gamepad.buttons.slice(0, 17).forEach((button, index) => { if (button.pressed) buttons |= (1 << index); });
  const axis = (index) => Math.round(Math.max(-1, Math.min(1, gamepad?.axes[index] || 0)) * 32767);
  const trigger = (index) => Math.round(Math.max(0, Math.min(1, gamepad?.buttons[index]?.value || 0)) * 65535);
  view.setUint8(0, 0x10);
  view.setUint8(1, gamepad?.index || 0);
  view.setUint8(2, connected ? 1 : 0);
  view.setUint32(3, buttons >>> 0, true);
  view.setInt16(7, axis(0), true); view.setInt16(9, axis(1), true);
  view.setInt16(11, axis(2), true); view.setInt16(13, axis(3), true);
  view.setUint16(15, trigger(6), true); view.setUint16(17, trigger(7), true);
  sendBytes(buffer);
  return Array.from(new Uint8Array(buffer)).join(",");
}

function startGamepadLoop() {
  const poll = () => {
    const gamepad = Array.from(navigator.getGamepads?.() || []).find((pad) => pad?.connected && pad.mapping === "standard");
    if (gamepad) {
      const signature = [
        ...gamepad.axes.map((axis) => axis.toFixed(3)),
        ...gamepad.buttons.map((button) => `${button.pressed ? 1 : 0}:${button.value.toFixed(3)}`),
      ].join("|");
      if (signature !== state.lastPadState) {
        sendGamepad(gamepad);
        state.lastPadState = signature;
      }
    } else if (state.lastPadState) {
      sendGamepad(null, false);
      state.lastPadState = "";
    }
    state.gamepadFrame = requestAnimationFrame(poll);
  };
  state.gamepadFrame = requestAnimationFrame(poll);
}

async function updateStats() {
  if (!state.pc) return;
  const reports = await state.pc.getStats();
  let inbound;
  let roundTripTime;
  reports.forEach((report) => {
    if (report.type === "inbound-rtp" && report.kind === "video") inbound = report;
    if (report.type === "candidate-pair" && report.state === "succeeded" && report.nominated && report.currentRoundTripTime !== undefined) {
      roundTripTime = report.currentRoundTripTime;
    }
  });
  if (!inbound) return;
  const now = performance.now();
  const elapsed = (now - state.lastStatsAt) / 1000;
  const bitrate = state.lastStatsAt && elapsed > 0 ? ((inbound.bytesReceived - state.lastBytes) * 8 / elapsed / 1e6) : 0;
  state.lastBytes = inbound.bytesReceived;
  state.lastStatsAt = now;
  $("#statResolution").textContent = inbound.frameWidth ? `${inbound.frameWidth}×${inbound.frameHeight}` : "—";
  $("#statFps").textContent = inbound.framesPerSecond ? Math.round(inbound.framesPerSecond) : "—";
  $("#statBitrate").textContent = bitrate ? bitrate.toFixed(1) : "—";
  const emitted = inbound.jitterBufferEmittedCount || 0;
  const delay = inbound.jitterBufferDelay || 0;
  const deltaCount = emitted - state.lastJitterCount;
  $("#statBuffer").textContent = deltaCount > 0 ? `${Math.round((delay - state.lastJitterDelay) / deltaCount * 1000)}ms` : "—";
  state.lastJitterCount = emitted;
  state.lastJitterDelay = delay;
  $("#statLatency").textContent = roundTripTime !== undefined ? `${Math.round(roundTripTime * 1000)}ms` : "—";
  const received = inbound.packetsReceived || 0;
  const lost = Math.max(0, inbound.packetsLost || 0);
  const loss = received + lost ? (100 * lost / (received + lost)).toFixed(2) : "0.00";
  $("#networkDetail").textContent = `Session packet loss ${loss}% · jitter ${Math.round((inbound.jitter || 0) * 1000)} ms · dropped frames ${inbound.framesDropped ?? "—"} · freezes ${inbound.freezeCount ?? "—"}`;
}

function startStats() {
  state.lastBytes = state.lastStatsAt = state.lastJitterDelay = state.lastJitterCount = 0;
  state.statsTimer = setInterval(() => updateStats().catch(() => {}), 1000);
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 4500);
}

function capturePointer(event) {
  // Locked click coordinates stay at the original lock position. Only seed
  // from them when entering pointer lock, never for in-game left clicks.
  if (document.pointerLockElement === $("#video")) return;
  if (event?.target === $("#video") && Number.isFinite(event.clientX)) {
    const rect = cursorRect();
    cursor.x = Math.max(0, Math.min(1, (event.clientX-rect.left)/rect.width));
    cursor.y = Math.max(0, Math.min(1, (event.clientY-rect.top)/rect.height));
    cursor.seeded = true;
    cursor.pending = true;
  }
  if (!$("#video").requestPointerLock) return;
  try {
    const result = $("#video").requestPointerLock({ unadjustedMovement: true });
    if (result?.catch) result.catch(() => $("#video").requestPointerLock());
  } catch (_) {
    $("#video").requestPointerLock();
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $("#refreshHealth").addEventListener("click", loadHealth);
  $("#searchInput").addEventListener("input", (event) => renderGames(event.target.value));
  document.addEventListener("keydown", (event) => {
    if ($("#player").classList.contains("hidden")) {
      if (event.key === "/" && document.activeElement !== $("#searchInput")) {
        event.preventDefault(); $("#searchInput").focus();
      }
      return;
    }
    if (event.ctrlKey && event.altKey && event.code === "KeyD") {
      event.preventDefault();
      if (!event.repeat) {
        releaseAll(); applyStreamSettings({dlss_enabled: !state.dlssEnabled});
      }
      return;
    }
    // Escape belongs to browser pointer lock. Toolbar/slider keys remain local.
    if (event.code === "Escape" || document.pointerLockElement !== $("#video")) return;
    if (!CODE_IDS.has(event.code) || state.keyDown.has(event.code)) return;
    event.preventDefault();
    state.keyDown.add(event.code);
    sendKey(event.code, true);
  });
  document.addEventListener("keyup", (event) => {
    if ($("#player").classList.contains("hidden") || !state.keyDown.has(event.code)) return;
    event.preventDefault(); state.keyDown.delete(event.code); sendKey(event.code, false);
  });
  window.addEventListener("resize", renderCursor);
  $("#video").addEventListener("resize", renderCursor);
  window.addEventListener("blur", releaseAll);
  document.addEventListener("visibilitychange", () => { if (document.hidden) releaseAll(); });
  $("#closeDialog").addEventListener("click", () => $("#gameDialog").close());
  $("#dlssToggle").addEventListener("change", updateQualityPreview);
  $("#gameDialog").addEventListener("click", (event) => { if (event.target === $("#gameDialog")) $("#gameDialog").close(); });
  $("#launchButton").addEventListener("click", launchSelected);
  $("#stopButton").addEventListener("click", stopSession);
  $("#reconnectButton").addEventListener("click", () => {
    $("#playerBackdrop").classList.remove("ready"); connectWebRtc().catch((error) => showToast(error.message));
  });
  $("#fullscreenButton").addEventListener("click", () => $("#player").requestFullscreen?.());
  $("#sendEscape").addEventListener("click", sendHostEscape);
  $("#liveDlss").addEventListener("click", () => applyStreamSettings({dlss_enabled: !state.dlssEnabled}));
  $("#qualitySlider").addEventListener("input", renderQuality);
  $("#qualitySlider").addEventListener("change", (event) => applyStreamSettings({quality: Number(event.target.value)}));
  $("#advancedSettings").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.target.reportValidity()) applyStreamSettings({settings: readAdvancedSettings()});
  });
  $("#originalRenderer").addEventListener("click", () => applyStreamSettings({settings: {dlss: {temporal_stability: false, neural_before_upscale: false}}}));
  $("#resetAdvanced").addEventListener("click", () => applyStreamSettings({quality: state.quality}));
  $("#settingsButton").addEventListener("click", () => {
    const open = $("#streamSettings").classList.toggle("hidden") === false;
    $("#settingsButton").setAttribute("aria-expanded", String(open));
    if (open) document.exitPointerLock?.();
  });
  $("#capturePrompt").addEventListener("click", capturePointer);
  $("#video").addEventListener("click", capturePointer);
  document.addEventListener("pointerlockchange", () => {
    const locked = document.pointerLockElement === $("#video");
    $("#capturePrompt").classList.toggle("hidden", locked);
    $("#player").classList.toggle("pointer-locked", locked);
    renderCursor();
    if (locked) cursor.pending = true;
    if (!locked) releaseAll();
    else { $("#streamSettings").classList.add("hidden"); $("#settingsButton").setAttribute("aria-expanded", "false"); }
  });
  document.addEventListener("mousemove", (event) => {
    if (document.pointerLockElement === $("#video")) sendMouseMove(event.movementX, event.movementY);
  });
  document.addEventListener("mousedown", (event) => {
    if (document.pointerLockElement === $("#video")) { event.preventDefault(); sendMouseButton(event.button, true); }
  });
  document.addEventListener("mouseup", (event) => {
    if (document.pointerLockElement === $("#video")) { event.preventDefault(); sendMouseButton(event.button, false); }
  });
  document.addEventListener("wheel", (event) => {
    if (document.pointerLockElement === $("#video")) { event.preventDefault(); sendWheel(event.deltaX, event.deltaY); }
  }, { passive: false });
  document.addEventListener("contextmenu", (event) => { if (document.pointerLockElement === $("#video")) event.preventDefault(); });
}

async function initialize() {
  bindEvents();
  try {
    const [config, games] = await Promise.all([api("/api/client-config"), api("/api/games")]);
    state.config = config;
    state.quality = config.quality;
    state.settings = config.settings;
    buildAdvancedSettings();
    renderLiveSettings();
    state.games = games.games;
    $("#specResolution").textContent = `${config.width} × ${config.height}`;
    $("#specFps").textContent = `${config.fps} FPS`;
    renderGames();
    await loadHealth();
    const session = await api("/api/session");
    if (session.active && session.game) { state.selected = session.game; await openPlayer(session.game); }
  } catch (error) {
    $("#gameCount").textContent = error.message;
    $("#hostDot").className = "status-dot error";
  }
}

initialize();
