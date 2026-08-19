const deviceForm = document.querySelector("#device-form");
const deviceKey = document.querySelector("#device-key");
const toggleKey = document.querySelector("#toggle-key");
const modeButtons = [...document.querySelectorAll("[data-mode]")];
const microphoneInput = document.querySelector("#microphone-input");
const wavInput = document.querySelector("#wav-input");
const localRecord = document.querySelector("#local-record");
const localRecordLabel = document.querySelector("#local-record-label");
const wavFile = document.querySelector("#wav-file");
const dropZone = document.querySelector("#drop-zone");
const fileName = document.querySelector("#file-name");
const runTest = document.querySelector("#run-test");
const runTestLabel = runTest.querySelector("span");
const resetDevice = document.querySelector("#reset-device");
const deviceError = document.querySelector("#device-error");
const phasePill = document.querySelector("#phase-pill");
const phase = document.querySelector("#phase");
const statusMessage = document.querySelector("#status-message");
const transcript = document.querySelector("#transcript");
const answer = document.querySelector("#answer");
const audio = document.querySelector("#device-audio");
const audioHint = document.querySelector("#audio-hint");
const latencyCurrentTab = document.querySelector("#latency-current-tab");
const latencyStatsTab = document.querySelector("#latency-stats-tab");
const latencyRefresh = document.querySelector("#latency-refresh");
const latencyCurrentView = document.querySelector("#latency-current-view");
const latencyStatsView = document.querySelector("#latency-stats-view");
const latencyEmpty = document.querySelector("#latency-empty");
const latencyCurrentContent = document.querySelector("#latency-current-content");
const latencyOutcome = document.querySelector("#latency-outcome");
const latencyRecordedAt = document.querySelector("#latency-recorded-at");
const latencyTotal = document.querySelector("#latency-total");
const latencyFailure = document.querySelector("#latency-failure");
const latencyStages = document.querySelector("#latency-stages");
const latencyStatsBody = document.querySelector("#latency-stats-body");

const phaseLabels = {
  idle: "待机",
  recording: "录音",
  transcribing: "语音识别",
  thinking: "查询知识库",
  speaking: "合成完成",
  degraded: "服务降级",
  error: "出现错误",
};

let audioObjectUrl = null;
let pollTimer = null;
let stateRequestPending = false;
let metricsRequestPending = false;
let operationPending = false;
let currentPhase = "idle";
let inputMode = "microphone";
let localPlaybackActive = false;

const outcomeLabels = {
  success: "成功",
  degraded: "降级",
  error: "失败",
};

const metricLabels = [
  ["asr_ms", "ASR 语音识别"],
  ["xzkb_queue_ms", "知识库排队"],
  ["xzkb_headers_ms", "请求到响应头"],
  ["xzkb_first_sse_ms", "等待首个 SSE"],
  ["xzkb_first_content_ms", "SSE 到正文首字"],
  ["xzkb_ttft_ms", "首字总耗时"],
  ["xzkb_generation_ms", "正文生成"],
  ["xzkb_total_ms", "知识库总耗时"],
  ["tts_queue_ms", "TTS 排队"],
  ["tts_synthesis_ms", "TTS 合成"],
  ["server_pipeline_total_ms", "服务端总耗时"],
];

const xzkbSubstages = [
  ["请求到响应头", "xzkb_headers_ms"],
  ["等待首个 SSE", "xzkb_first_sse_ms"],
  ["SSE 到正文首字", "xzkb_first_content_ms"],
  ["正文生成", "xzkb_generation_ms"],
  ["首字总耗时", "xzkb_ttft_ms"],
  ["知识库总耗时", "xzkb_total_ms"],
];

function requireKey() {
  const key = deviceKey.value.trim();
  if (!key) throw new Error("请输入设备密钥");
  return key;
}

async function responseError(response) {
  const messages = {
    401: "设备凭证无效",
    409: "设备正在处理上一轮，请稍后重试或重置",
    413: "WAV 文件超过服务端限制",
    415: "音频不是 16 kHz、单声道、16-bit PCM WAV",
    503: "语音或知识库服务暂时不可用",
  };
  let detail = "";
  try {
    detail = (await response.json()).detail || "";
  } catch {
    detail = "";
  }
  return new Error(detail || messages[response.status] || `请求失败（${response.status}）`);
}

async function request(path, options = {}) {
  requireKey();
  const headers = new Headers(options.headers || {});
  headers.set("X-Device-Key", deviceKey.value.trim());
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) throw await responseError(response);
  return response;
}

function showError(error) {
  deviceError.textContent = error instanceof TypeError
    ? "无法连接展厅服务，请检查网络或服务状态"
    : error.message || "请求失败，请稍后重试";
}

function clearError() {
  deviceError.textContent = "";
}

function renderState(snapshot) {
  currentPhase = snapshot.phase || "idle";
  phasePill.dataset.phase = currentPhase;
  phase.textContent = phaseLabels[currentPhase] || "处理中";
  statusMessage.textContent = snapshot.message || "设备状态已更新";
  transcript.textContent = snapshot.transcript || "尚未识别";
  answer.textContent = snapshot.answer || "回答会显示在这里";
  if (inputMode === "microphone" && currentPhase === "speaking") {
    localPlaybackActive = true;
    audioHint.textContent = "正在由树莓派扬声器播放";
  }
  if (localPlaybackActive && currentPhase === "idle") {
    audioHint.textContent = "树莓派扬声器播放完成";
    localPlaybackActive = false;
  } else if (localPlaybackActive && currentPhase === "error") {
    audioHint.textContent = "树莓派扬声器播放失败";
    localPlaybackActive = false;
  }
  updateLocalRecordControl();
}

function clearAudio() {
  audio.pause();
  audio.hidden = true;
  audio.removeAttribute("src");
  audio.load();
  if (audioObjectUrl) {
    URL.revokeObjectURL(audioObjectUrl);
    audioObjectUrl = null;
  }
}

function clearResult() {
  clearAudio();
  transcript.textContent = "尚未识别";
  answer.textContent = "回答会显示在这里";
  audioHint.textContent = "语音生成后可播放";
  clearError();
}

function setOperationPending(pending) {
  operationPending = pending;
  runTest.disabled = pending;
  resetDevice.disabled = pending;
  runTestLabel.textContent = pending ? "正在处理" : "开始测试";
  updateLocalRecordControl();
}

function updateLocalRecordControl() {
  const keyReady = Boolean(deviceKey.value.trim());
  const processing = ["transcribing", "thinking", "speaking"].includes(currentPhase);
  for (const button of modeButtons) {
    button.disabled = operationPending || currentPhase === "recording" || processing;
  }
  localRecord.dataset.recording = String(currentPhase === "recording");
  localRecord.disabled = operationPending || processing || !keyReady;
  if (operationPending) {
    localRecordLabel.textContent = "正在处理";
  } else if (currentPhase === "recording") {
    localRecordLabel.textContent = "结束并提交";
  } else {
    localRecordLabel.textContent = "开始录音";
  }
}

function showInputMode(mode) {
  inputMode = mode;
  microphoneInput.hidden = mode !== "microphone";
  wavInput.hidden = mode !== "wav";
  for (const button of modeButtons) {
    button.setAttribute("aria-selected", String(button.dataset.mode === mode));
  }
}

async function renderTurnResult(payload, { localPlayback = false } = {}) {
  transcript.textContent = payload.transcript || "未识别到有效内容";
  answer.textContent = payload.answer || "知识库未返回回答";
  if (payload.warning) deviceError.textContent = payload.warning;
  if (localPlayback) {
    clearAudio();
    if (payload.audio_url) {
      localPlaybackActive = true;
      audioHint.textContent = "正在由树莓派扬声器播放";
    } else {
      localPlaybackActive = false;
      audioHint.textContent = "本次仅返回文字内容";
    }
  } else if (payload.audio_url) {
    await loadProtectedAudio(payload);
  } else {
    audioHint.textContent = "本次仅返回文字内容";
  }
}

async function refreshState({ showFailure = false } = {}) {
  if (!deviceKey.value.trim() || stateRequestPending || document.hidden) return;
  stateRequestPending = true;
  try {
    const response = await request("/api/device/state");
    renderState(await response.json());
  } catch (error) {
    if (error.message === "设备凭证无效") stopPolling();
    if (showFailure || error.message === "设备凭证无效") showError(error);
  } finally {
    stateRequestPending = false;
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPolling() {
  stopPolling();
  if (!deviceKey.value.trim() || document.hidden) return;
  refreshState({ showFailure: true });
  pollTimer = window.setInterval(refreshState, 2000);
}

function formatDuration(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "--";
  }
  const milliseconds = Number(value);
  if (milliseconds < 1) return "< 1ms";
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  return `${(milliseconds / 1000).toFixed(2)}s`;
}

function formatRecordedAt(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function numberOrNull(value) {
  if (value === null || value === undefined) return null;
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

function appendTextElement(parent, tagName, className, value) {
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = value;
  parent.append(element);
  return element;
}

function appendSubstages(parent, latest) {
  const substages = document.createElement("div");
  substages.className = "latency-substages";
  for (const [label, name] of xzkbSubstages) {
    const row = document.createElement("div");
    row.className = "latency-substage";
    appendTextElement(row, "span", "latency-substage-name", label);
    appendTextElement(row, "span", "latency-substage-time", formatDuration(latest[name]));
    substages.append(row);
  }
  parent.append(substages);
}

function renderLatencyStages(latest) {
  latencyStages.replaceChildren();
  const total = numberOrNull(latest.server_pipeline_total_ms);
  const knownDurations = [
    latest.asr_ms,
    latest.xzkb_queue_ms,
    latest.xzkb_total_ms,
    latest.tts_queue_ms,
    latest.tts_synthesis_ms,
  ].map(numberOrNull);
  const other = total === null ? null : Math.max(0, total - knownDurations.reduce(
    (sum, value) => sum + (value === null ? 0 : value),
    0,
  ));
  const stages = [
    ["ASR 语音识别", numberOrNull(latest.asr_ms)],
    ["知识库排队", numberOrNull(latest.xzkb_queue_ms)],
    ["知识库处理", numberOrNull(latest.xzkb_total_ms), true],
    ["TTS 排队", numberOrNull(latest.tts_queue_ms)],
    ["TTS 合成", numberOrNull(latest.tts_synthesis_ms)],
    ["其他处理", other],
  ];
  const maxDuration = Math.max(0, ...stages.map(([, value]) => value === null ? 0 : value));

  for (const [label, duration, hasSubstages] of stages) {
    const stage = document.createElement("div");
    const row = document.createElement("div");
    row.className = "latency-stage-row";
    const labelArea = document.createElement("div");
    appendTextElement(labelArea, "span", "latency-stage-name", label);
    const track = document.createElement("div");
    track.className = "latency-track";
    const fill = document.createElement("div");
    fill.className = "latency-fill";
    fill.style.width = duration === null || maxDuration === 0 ? "0%" : `${(duration / maxDuration) * 100}%`;
    track.append(fill);
    labelArea.append(track);
    row.append(labelArea);
    appendTextElement(row, "span", "latency-stage-time", formatDuration(duration));
    stage.append(row);
    if (hasSubstages) appendSubstages(stage, latest);
    latencyStages.append(stage);
  }
}

function renderMetrics(snapshot) {
  const latest = snapshot && snapshot.latest;
  latencyEmpty.hidden = Boolean(latest);
  latencyCurrentContent.hidden = !latest;
  if (latest) {
    const outcome = latest.outcome || "error";
    latencyOutcome.textContent = outcomeLabels[outcome] || outcome;
    latencyOutcome.dataset.outcome = outcome;
    latencyRecordedAt.textContent = formatRecordedAt(latest.recorded_at);
    latencyTotal.textContent = formatDuration(latest.server_pipeline_total_ms);
    const hasFailure = Boolean(latest.failure_stage || latest.error_type);
    latencyFailure.hidden = !hasFailure;
    latencyFailure.textContent = hasFailure
      ? `失败阶段：${latest.failure_stage || "--"}；错误类型：${latest.error_type || "--"}`
      : "";
    renderLatencyStages(latest);
  }

  latencyStatsBody.replaceChildren();
  const metrics = snapshot && snapshot.metrics ? snapshot.metrics : {};
  for (const [name, label] of metricLabels) {
    const item = metrics[name] || {};
    const row = document.createElement("tr");
    appendTextElement(row, "td", "", label);
    appendTextElement(row, "td", "", String(item.samples ?? 0));
    appendTextElement(row, "td", "", formatDuration(item.p50));
    appendTextElement(row, "td", "", formatDuration(item.p95));
    latencyStatsBody.append(row);
  }
}

function showLatencyView(view) {
  const showCurrent = view === "current";
  latencyCurrentTab.setAttribute("aria-selected", String(showCurrent));
  latencyStatsTab.setAttribute("aria-selected", String(!showCurrent));
  latencyCurrentView.hidden = !showCurrent;
  latencyStatsView.hidden = showCurrent;
}

async function refreshMetrics({ showFailure = false } = {}) {
  if (!deviceKey.value.trim() || metricsRequestPending) return;
  metricsRequestPending = true;
  latencyRefresh.disabled = true;
  try {
    const response = await request("/api/device/metrics");
    renderMetrics(await response.json());
  } catch (error) {
    if (error.message === "设备凭证无效") stopPolling();
    if (showFailure || error.message === "设备凭证无效") showError(error);
  } finally {
    metricsRequestPending = false;
    latencyRefresh.disabled = false;
  }
}

async function loadProtectedAudio(payload) {
  clearAudio();
  const response = await request(payload.audio_url);
  const audioBlob = await response.blob();
  audioObjectUrl = URL.createObjectURL(audioBlob);
  audio.src = audioObjectUrl;
  audio.hidden = false;
  audioHint.textContent = "语音讲解已准备好";
  audio.load();
  try {
    await audio.play();
  } catch {
    deviceError.textContent = "浏览器阻止了自动播放，请点击播放器开始播放。";
  }
}

function updateSelectedFile(files) {
  const selected = files && files[0];
  fileName.textContent = selected ? selected.name : "选择或拖放 WAV 文件";
  if (selected && !selected.name.toLowerCase().endsWith(".wav")) {
    deviceError.textContent = "请选择 WAV 文件";
  } else {
    clearError();
  }
}

toggleKey.addEventListener("click", () => {
  const showing = deviceKey.type === "text";
  deviceKey.type = showing ? "password" : "text";
  toggleKey.textContent = showing ? "显示" : "隐藏";
  toggleKey.setAttribute("aria-pressed", String(!showing));
  toggleKey.setAttribute("aria-label", showing ? "显示设备密钥" : "隐藏设备密钥");
  deviceKey.focus();
});

for (const button of modeButtons) {
  button.addEventListener("click", () => showInputMode(button.dataset.mode));
}

deviceKey.addEventListener("change", () => {
  startPolling();
  refreshMetrics({ showFailure: true });
  updateLocalRecordControl();
});
deviceKey.addEventListener("input", () => {
  updateLocalRecordControl();
  if (!deviceKey.value.trim()) {
    stopPolling();
    statusMessage.textContent = "填写密钥后开始测试";
    renderMetrics(null);
    clearError();
  }
});

wavFile.addEventListener("change", () => updateSelectedFile(wavFile.files));

for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.dataset.dragging = "true";
  });
}

for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.dataset.dragging = "false";
  });
}

dropZone.addEventListener("drop", (event) => {
  if (!event.dataTransfer.files.length) return;
  wavFile.files = event.dataTransfer.files;
  updateSelectedFile(wavFile.files);
});

localRecord.addEventListener("click", async () => {
  clearError();
  try {
    requireKey();
    const stopping = currentPhase === "recording";
    setOperationPending(true);
    if (stopping) {
      statusMessage.textContent = "正在结束录音并执行完整语音链路";
      const response = await request("/api/device/recording/stop", { method: "POST" });
      const payload = await response.json();
      await renderTurnResult(payload, { localPlayback: true });
      await refreshMetrics();
    } else {
      clearResult();
      const response = await request("/api/device/recording/start", { method: "POST" });
      renderState(await response.json());
    }
  } catch (error) {
    showError(error);
  } finally {
    setOperationPending(false);
    startPolling();
  }
});

deviceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  try {
    requireKey();
    if (inputMode !== "wav") throw new Error("请先切换到 WAV 文件模式");
    const selected = wavFile.files[0];
    if (!selected) throw new Error("请选择用于测试的 WAV 文件");
    if (!selected.name.toLowerCase().endsWith(".wav")) throw new Error("请选择 WAV 文件");

    setOperationPending(true);
    clearAudio();
    audioHint.textContent = "正在生成语音讲解";
    statusMessage.textContent = "正在上传音频并执行完整链路";

    const body = new FormData();
    body.append("file", selected, selected.name);
    const response = await request("/api/device/turn", { method: "POST", body });
    const payload = await response.json();
    await renderTurnResult(payload);
  } catch (error) {
    showError(error);
    audioHint.textContent = "语音尚未生成";
  } finally {
    await refreshMetrics();
    setOperationPending(false);
    startPolling();
  }
});

resetDevice.addEventListener("click", async () => {
  clearError();
  try {
    requireKey();
    setOperationPending(true);
    await request("/api/device/reset", { method: "POST" });
    clearResult();
    localPlaybackActive = false;
    currentPhase = "idle";
    phasePill.dataset.phase = "idle";
    phase.textContent = "待机";
    statusMessage.textContent = "设备已重置，可以开始新一轮测试";
    updateLocalRecordControl();
  } catch (error) {
    showError(error);
  } finally {
    setOperationPending(false);
    startPolling();
  }
});

audio.addEventListener("ended", async () => {
  try {
    await request("/api/device/playback-finished", { method: "POST" });
    audioHint.textContent = "播放完成，设备已收到回执";
    await refreshState({ showFailure: true });
  } catch (error) {
    showError(error);
  }
});

latencyCurrentTab.addEventListener("click", () => showLatencyView("current"));
latencyStatsTab.addEventListener("click", () => showLatencyView("stats"));
latencyRefresh.addEventListener("click", () => refreshMetrics({ showFailure: true }));

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else startPolling();
});

window.addEventListener("beforeunload", () => {
  stopPolling();
  if (audioObjectUrl) URL.revokeObjectURL(audioObjectUrl);
});

renderMetrics(null);
showInputMode("microphone");
updateLocalRecordControl();
if (deviceKey.value.trim()) refreshMetrics({ showFailure: true });
