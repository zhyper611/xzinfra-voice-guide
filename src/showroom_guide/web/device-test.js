const deviceForm = document.querySelector("#device-form");
const deviceKey = document.querySelector("#device-key");
const toggleKey = document.querySelector("#toggle-key");
const modeButtons = [...document.querySelectorAll("[data-mode]")];
const microphoneInput = document.querySelector("#microphone-input");
const wavInput = document.querySelector("#wav-input");
const knowledgeInput = document.querySelector("#knowledge-input");
const localRecord = document.querySelector("#local-record");
const localRecordLabel = document.querySelector("#local-record-label");
const replayRecording = document.querySelector("#replay-recording");
const replayRecordingLabel = document.querySelector("#replay-recording-label");
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
const knowledgeControlState = document.querySelector("#knowledge-control-state");
const knowledgeModeState = document.querySelector("#knowledge-mode-state");
const knowledgeProcessingStage = document.querySelector("#knowledge-processing-stage");
const knowledgeDraft = document.querySelector("#knowledge-draft");
const knowledgeSync = document.querySelector("#knowledge-sync");
const knowledgeSyncState = document.querySelector("#knowledge-sync-state");
const knowledgeShortPress = document.querySelector("#knowledge-short-press");
const knowledgeLongPress = document.querySelector("#knowledge-long-press");

const phaseLabels = {
  idle: "待机",
  recording: "录音",
  transcribing: "语音识别",
  thinking: "查询知识库",
  speaking: "合成完成",
  degraded: "服务降级",
  error: "出现错误",
};
const NO_SPEECH_MESSAGE = "没有听清您的声音，请靠近麦克风后再试一次。";
const REQUEST_TIMEOUT_MS = 180000;
const STATUS_REQUEST_TIMEOUT_MS = 15000;
const KNOWLEDGE_LEASE_KEY = "showroom-knowledge-lease";

let audioObjectUrl = null;
let pollTimer = null;
let stateRequestPending = false;
let stateRequestGeneration = null;
let stateRequestId = 0;
let metricsRequestPending = false;
let metricsRequestGeneration = null;
let metricsRequestId = 0;
let operationPending = false;
let currentPhase = "idle";
let inputMode = "microphone";
let localPlaybackActive = false;
let hasLastRecording = false;
let replayPending = false;
let knowledgePollTimer = null;
let knowledgeStateRequestPending = false;
let knowledgeStateRequestGeneration = null;
let knowledgeStateRequestId = 0;
let knowledgeEntryRequestPending = false;
let knowledgeEntryRequestGeneration = null;
let knowledgeEntryRequestId = 0;
let knowledgeOperationPending = false;
let knowledgeEntryId = null;
let knowledgeLastEntryId = null;
let knowledgeSnapshot = null;
let knowledgeLeaseToken = null;
let knowledgeOperationNeedsResync = false;
let deviceKeyGeneration = 0;

try {
  knowledgeLeaseToken = sessionStorage.getItem(KNOWLEDGE_LEASE_KEY);
} catch {
  knowledgeLeaseToken = null;
}

const outcomeLabels = {
  success: "成功",
  degraded: "降级",
  error: "失败",
};

const knowledgeControlLabels = {
  available: "可接管",
  owned: "本页控制",
  observed: "只读观察",
};

const knowledgeModeLabels = {
  inactive: "未进入",
  ready: "准备录入",
  recording: "正在录音",
  processing: "处理中",
  confirming: "等待确认",
};

const knowledgeStageLabels = {
  transcribing: "ASR 识别",
  synthesizing: "TTS 合成",
  playing_review: "复述播放",
};

const knowledgeSyncLabels = {
  local_saved: "已保存到本机",
  uploading: "正在上传",
  processing: "知识库处理中",
  retrying: "同步重试中",
  synced: "已同步",
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

function isCurrentDeviceKeyRequest(generation, key) {
  return generation === deviceKeyGeneration && key === deviceKey.value.trim();
}

async function responseError(response) {
  const messages = {
    401: "设备凭证无效",
    404: "没有可播放的录音",
    409: "设备正在处理上一轮，请稍后重试或重置",
    413: "WAV 文件超过服务端限制",
    415: "音频不是 16 kHz、单声道、16-bit PCM WAV",
    422: "没有听清您的声音，请靠近麦克风后再试一次。",
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

async function request(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS, requestKey = null) {
  const key = requestKey === null ? requireKey() : requestKey;
  if (!key) throw new Error("请输入设备密钥");
  const headers = new Headers(options.headers || {});
  headers.set("X-Device-Key", key);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...options,
      headers,
      signal: controller.signal,
    });
    if (!response.ok) throw await responseError(response);
    return response;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("请求超时，请检查网络后重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function knowledgeResponseError(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  const error = new Error(payload.detail || `知识补充请求失败（${response.status}）`);
  error.code = payload.code;
  error.payload = payload;
  return error;
}

async function knowledgeRequest(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS, requestKey = null) {
  const key = requestKey === null ? requireKey() : requestKey;
  if (!key) throw new Error("请输入设备密钥");
  const headers = new Headers(options.headers || {});
  headers.set("X-Device-Key", key);
  if (knowledgeLeaseToken) {
    headers.set("X-Knowledge-Lease", knowledgeLeaseToken);
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...options,
      headers,
      signal: controller.signal,
    });
    if (!response.ok) throw await knowledgeResponseError(response);
    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error("知识补充操作超时，正在校准设备状态");
      timeoutError.code = "knowledge_timeout";
      timeoutError.payload = null;
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function persistKnowledgeLease(token) {
  knowledgeLeaseToken = token;
  try {
    sessionStorage.setItem(KNOWLEDGE_LEASE_KEY, knowledgeLeaseToken);
  } catch {
    knowledgeLeaseToken = token;
  }
}

function clearKnowledgeLease() {
  knowledgeLeaseToken = null;
  try {
    sessionStorage.removeItem(KNOWLEDGE_LEASE_KEY);
  } catch {
    knowledgeLeaseToken = null;
  }
}

function showError(error) {
  deviceError.textContent = error instanceof TypeError
    ? "无法连接展厅服务，请检查网络或服务状态"
    : error.message || "请求失败，请稍后重试";
}

function clearError() {
  deviceError.textContent = "";
}

function showKnowledgeError(error) {
  deviceError.textContent = error instanceof TypeError
    ? "无法连接展厅服务，请检查网络或服务状态"
    : error.message || "知识补充请求失败，请稍后重试";
}

function updateKnowledgeControls() {
  const snapshot = knowledgeSnapshot;
  const keyReady = Boolean(deviceKey.value.trim());
  const enabled = snapshot ? snapshot.enabled !== false : false;
  const owned = snapshot && snapshot.control_state === "owned" && Boolean(knowledgeLeaseToken);
  const observed = snapshot && snapshot.control_state === "observed";
  const mode = snapshot ? snapshot.mode_state : "inactive";

  knowledgeShortPress.textContent = "开始录入";
  knowledgeLongPress.textContent = "进入知识补充";
  knowledgeShortPress.disabled = true;
  knowledgeLongPress.disabled = true;

  if (!keyReady || !enabled || observed || knowledgeOperationPending) return;
  if (!owned) {
    knowledgeLongPress.disabled = false;
    return;
  }

  if (mode === "ready") {
    knowledgeShortPress.disabled = false;
    knowledgeLongPress.disabled = false;
    knowledgeLongPress.textContent = "退出知识模式";
  } else if (mode === "recording") {
    knowledgeShortPress.disabled = false;
    knowledgeShortPress.textContent = "停止并复述";
    knowledgeLongPress.textContent = "复述准备中";
  } else if (mode === "processing") {
    knowledgeShortPress.textContent = "处理中";
    knowledgeLongPress.textContent = "处理中";
  } else if (mode === "confirming") {
    knowledgeShortPress.disabled = false;
    knowledgeLongPress.disabled = false;
    knowledgeShortPress.textContent = "重新录入";
    knowledgeLongPress.textContent = "保存并返回";
  }
}

function renderKnowledgeState(snapshot, { captureEntry = false } = {}) {
  knowledgeSnapshot = snapshot;
  const owned = snapshot.control_state === "owned" && Boolean(knowledgeLeaseToken);
  knowledgeControlState.textContent = knowledgeControlLabels[snapshot.control_state] || "待校准";
  knowledgeModeState.textContent = knowledgeModeLabels[snapshot.mode_state] || "未知";
  knowledgeProcessingStage.textContent = knowledgeStageLabels[snapshot.processing_stage] || "--";
  knowledgeDraft.textContent = owned && snapshot.draft_text
    ? snapshot.draft_text
    : "等待录入";

  if (snapshot.last_entry_id) {
    const isNewEntry = snapshot.last_entry_id !== knowledgeLastEntryId;
    knowledgeLastEntryId = snapshot.last_entry_id;
    if (captureEntry && isNewEntry) {
      knowledgeEntryId = snapshot.last_entry_id;
      clearKnowledgeLease();
    }
  }
  updateKnowledgeControls();
  updateLocalRecordControl();
}

function renderKnowledgeEntry(entry) {
  const syncState = entry.sync_state || "local_saved";
  knowledgeSync.dataset.syncState = syncState;
  const baseLabel = knowledgeSyncLabels[syncState] || "同步状态未知";
  knowledgeSyncState.textContent = syncState === "retrying" && entry.last_error
    ? `${baseLabel}：${entry.last_error}`
    : baseLabel;
  if (entry.sync_state === "synced") knowledgeEntryId = null;
  if (entry.sync_state === "retrying") knowledgeEntryId = entry.entry_id;
}

function handleKnowledgeError(error, { showFailure = true, captureEntry = false } = {}) {
  if (error.code === "knowledge_lease_expired") clearKnowledgeLease();
  const errorState = error.payload && error.payload.knowledge_state;
  if (errorState) renderKnowledgeState(errorState, { captureEntry });
  if (error.message === "设备凭证无效") stopAllPolling();
  if (showFailure) showKnowledgeError(error);
}

function renderState(snapshot) {
  currentPhase = snapshot.phase || "idle";
  hasLastRecording = Boolean(snapshot.has_last_recording);
  phasePill.dataset.phase = currentPhase;
  phase.textContent = phaseLabels[currentPhase] || "处理中";
  if (currentPhase === "error" && snapshot.message === NO_SPEECH_MESSAGE) {
    phase.textContent = "未检测到语音";
  }
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
  updateKnowledgeControls();
}

function updateLocalRecordControl() {
  const keyReady = Boolean(deviceKey.value.trim());
  const processing = ["transcribing", "thinking", "speaking"].includes(currentPhase);
  const busy = currentPhase === "recording" || processing;
  const knowledgeModeActive = (
    knowledgeSnapshot
    && knowledgeSnapshot.mode_state !== "inactive"
  );
  const standardControlsBlocked = Boolean(knowledgeModeActive || knowledgeOperationPending);
  const knowledgeModeBusy = knowledgeSnapshot && (
    knowledgeSnapshot.mode_state === "recording"
    || knowledgeSnapshot.mode_state === "processing"
  );
  for (const button of modeButtons) {
    button.disabled = (
      operationPending
      || knowledgeOperationPending
      || currentPhase === "recording"
      || processing
      || (
        knowledgeModeBusy
        && button.dataset.mode !== "knowledge"
        && button.dataset.mode !== inputMode
      )
    );
  }
  runTest.disabled = operationPending || standardControlsBlocked;
  resetDevice.disabled = operationPending || standardControlsBlocked;
  localRecord.dataset.recording = String(currentPhase === "recording");
  localRecord.disabled = operationPending || processing || !keyReady || standardControlsBlocked;
  if (operationPending) {
    localRecordLabel.textContent = "正在处理";
  } else if (currentPhase === "recording") {
    localRecordLabel.textContent = "结束并提交";
  } else {
    localRecordLabel.textContent = "开始录音";
  }
  replayRecording.disabled = (
    operationPending
    || replayPending
    || busy
    || !keyReady
    || !hasLastRecording
    || standardControlsBlocked
  );
  if (replayPending) {
    replayRecordingLabel.textContent = "正在播放录音";
  } else {
    replayRecordingLabel.textContent = "播放刚才的录音";
  }
}

function showInputMode(mode) {
  inputMode = mode;
  microphoneInput.hidden = mode !== "microphone";
  wavInput.hidden = mode !== "wav";
  knowledgeInput.hidden = mode !== "knowledge";
  for (const button of modeButtons) {
    const selected = button.dataset.mode === mode;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = button.dataset.mode === mode ? 0 : -1;
  }
  if (mode === "knowledge" && deviceKey.value.trim()) {
    refreshKnowledgeState({ showFailure: true });
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
  const requestGeneration = deviceKeyGeneration;
  const requestKey = deviceKey.value.trim();
  if (
    !requestKey
    || (stateRequestPending && stateRequestGeneration === requestGeneration)
    || document.hidden
  ) return;
  const requestId = ++stateRequestId;
  stateRequestPending = true;
  stateRequestGeneration = requestGeneration;
  try {
    const response = await request("/api/device/state", {}, STATUS_REQUEST_TIMEOUT_MS, requestKey);
    const snapshot = await response.json();
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;
    renderState(snapshot);
  } catch (error) {
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;
    if (error.message === "设备凭证无效") stopPolling();
    if (showFailure || error.message === "设备凭证无效") showError(error);
  } finally {
    if (requestId === stateRequestId) {
      stateRequestPending = false;
      stateRequestGeneration = null;
    }
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

async function refreshKnowledgeEntry({ showFailure = false } = {}) {
  const requestGeneration = deviceKeyGeneration;
  const requestKey = deviceKey.value.trim();
  if (
    !requestKey
    || !knowledgeEntryId
    || (knowledgeEntryRequestPending && knowledgeEntryRequestGeneration === requestGeneration)
    || document.hidden
  ) return;
  const requestId = ++knowledgeEntryRequestId;
  knowledgeEntryRequestPending = true;
  knowledgeEntryRequestGeneration = requestGeneration;
  try {
    const entry = await knowledgeRequest(
      `/api/device/knowledge/entries/${encodeURIComponent(knowledgeEntryId)}`,
      {},
      STATUS_REQUEST_TIMEOUT_MS,
      requestKey,
    );
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;
    renderKnowledgeEntry(entry);
  } catch (error) {
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;
    handleKnowledgeError(error, { showFailure });
  } finally {
    if (requestId === knowledgeEntryRequestId) {
      knowledgeEntryRequestPending = false;
      knowledgeEntryRequestGeneration = null;
    }
  }
}

async function loadKnowledgeState({
  showFailure = false,
  allowDuringOperation = false,
  requestGeneration,
  requestKey,
} = {}) {
  const captureEntry = knowledgeOperationNeedsResync;
  try {
    const snapshot = await knowledgeRequest("/api/device/knowledge/state", {}, STATUS_REQUEST_TIMEOUT_MS, requestKey);
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return false;
    if (!allowDuringOperation && knowledgeOperationPending) return false;
    renderKnowledgeState(snapshot, { captureEntry });
    knowledgeOperationNeedsResync = false;
  } catch (error) {
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return false;
    if (!allowDuringOperation && knowledgeOperationPending) return false;
    handleKnowledgeError(error, { showFailure, captureEntry });
    if (error.payload && error.payload.knowledge_state) {
      knowledgeOperationNeedsResync = false;
    }
  }
  return true;
}

async function refreshKnowledgeState({ showFailure = false } = {}) {
  const requestGeneration = deviceKeyGeneration;
  const requestKey = deviceKey.value.trim();
  if (
    !requestKey
    || (
      knowledgeStateRequestPending
      && knowledgeStateRequestGeneration === requestGeneration
    )
    || knowledgeOperationPending || document.hidden
  ) return;
  const requestId = ++knowledgeStateRequestId;
  knowledgeStateRequestPending = true;
  knowledgeStateRequestGeneration = requestGeneration;
  try {
    const applied = await loadKnowledgeState({
      showFailure,
      requestGeneration,
      requestKey,
    });
    if (knowledgeOperationPending) return;
    if (applied) await refreshKnowledgeEntry({ showFailure });
  } finally {
    if (requestId === knowledgeStateRequestId) {
      knowledgeStateRequestPending = false;
      knowledgeStateRequestGeneration = null;
    }
  }
}

async function resyncKnowledgeState({ showFailure = false } = {}) {
  const requestGeneration = deviceKeyGeneration;
  const requestKey = deviceKey.value.trim();
  if (!requestKey) return;
  const requestId = ++knowledgeStateRequestId;
  knowledgeStateRequestPending = true;
  knowledgeStateRequestGeneration = requestGeneration;
  try {
    const applied = await loadKnowledgeState({
      showFailure,
      allowDuringOperation: true,
      requestGeneration,
      requestKey,
    });
    if (applied) await refreshKnowledgeEntry({ showFailure });
  } finally {
    if (requestId === knowledgeStateRequestId) {
      knowledgeStateRequestPending = false;
      knowledgeStateRequestGeneration = null;
    }
  }
}

function stopKnowledgePolling() {
  if (knowledgePollTimer !== null) {
    window.clearInterval(knowledgePollTimer);
    knowledgePollTimer = null;
  }
}

function startKnowledgePolling() {
  stopKnowledgePolling();
  if (!deviceKey.value.trim() || document.hidden) return;
  refreshKnowledgeState({ showFailure: true });
  knowledgePollTimer = window.setInterval(refreshKnowledgeState, 2000);
}

function stopAllPolling() {
  stopPolling();
  stopKnowledgePolling();
}

function startAllPolling() {
  startPolling();
  startKnowledgePolling();
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
  const requestGeneration = deviceKeyGeneration;
  const requestKey = deviceKey.value.trim();
  if (
    !requestKey
    || (metricsRequestPending && metricsRequestGeneration === requestGeneration)
  ) return;
  const requestId = ++metricsRequestId;
  metricsRequestPending = true;
  metricsRequestGeneration = requestGeneration;
  latencyRefresh.disabled = true;
  try {
    const response = await request("/api/device/metrics", {}, STATUS_REQUEST_TIMEOUT_MS, requestKey);
    const snapshot = await response.json();
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;
    renderMetrics(snapshot);
  } catch (error) {
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;
    if (error.message === "设备凭证无效") stopPolling();
    if (showFailure || error.message === "设备凭证无效") showError(error);
  } finally {
    if (requestId === metricsRequestId) {
      metricsRequestPending = false;
      metricsRequestGeneration = null;
      latencyRefresh.disabled = false;
    }
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

function setKnowledgeOperationPending(pending) {
  knowledgeOperationPending = pending;
  updateKnowledgeControls();
  updateLocalRecordControl();
}

async function acquireKnowledgeControl() {
  clearError();
  setKnowledgeOperationPending(true);
  try {
    const payload = await knowledgeRequest("/api/device/knowledge/acquire", { method: "POST" });
    persistKnowledgeLease(payload.lease_token);
    renderKnowledgeState(payload.knowledge_state);
  } catch (error) {
    handleKnowledgeError(error);
    if (error.code === "knowledge_timeout") {
      knowledgeOperationNeedsResync = true;
      await resyncKnowledgeState({ showFailure: false });
    }
  } finally {
    setKnowledgeOperationPending(false);
    startKnowledgePolling();
  }
}

async function runKnowledgeOperation(path, { captureEntry = false } = {}) {
  clearError();
  setKnowledgeOperationPending(true);
  try {
    const snapshot = await knowledgeRequest(path, { method: "POST" });
    if (snapshot.control_state !== "owned") clearKnowledgeLease();
    renderKnowledgeState(snapshot, { captureEntry });
    if (captureEntry && knowledgeEntryId) await refreshKnowledgeEntry({ showFailure: true });
  } catch (error) {
    handleKnowledgeError(error);
    if (error.code === "knowledge_timeout") {
      knowledgeOperationNeedsResync = true;
      await resyncKnowledgeState({ showFailure: false });
    }
  } finally {
    setKnowledgeOperationPending(false);
    startKnowledgePolling();
  }
}

async function switchInputMode(nextMode) {
  const leavingKnowledge = inputMode === "knowledge" && nextMode !== "knowledge";
  if (!leavingKnowledge || !knowledgeLeaseToken) {
    showInputMode(nextMode);
    return;
  }

  clearError();
  setKnowledgeOperationPending(true);
  try {
    const snapshot = await knowledgeRequest("/api/device/knowledge/release", { method: "POST" });
    clearKnowledgeLease();
    renderKnowledgeState(snapshot);
    showInputMode(nextMode);
  } catch (error) {
    const errorState = error.payload && error.payload.knowledge_state;
    handleKnowledgeError(error);
    if (error.code === "knowledge_timeout") {
      await resyncKnowledgeState({ showFailure: false });
    }
    const releaseStillOwned = (
      knowledgeLeaseToken
      && knowledgeSnapshot
      && knowledgeSnapshot.control_state === "owned"
    );
    if (
      error.code === "knowledge_lease_expired"
      || (errorState && errorState.control_state !== "owned")
      || (error.code === "knowledge_timeout" && !releaseStillOwned)
    ) {
      clearKnowledgeLease();
      showInputMode(nextMode);
    } else {
      showInputMode("knowledge");
    }
  } finally {
    setKnowledgeOperationPending(false);
    startKnowledgePolling();
  }
}

async function handleModeKeydown(event) {
  const navigationKeys = ["ArrowLeft", "ArrowRight", "Home", "End"];
  if (!navigationKeys.includes(event.key)) return;
  const enabledButtons = modeButtons.filter((button) => !button.disabled);
  if (!enabledButtons.length) return;

  event.preventDefault();
  const currentIndex = Math.max(0, enabledButtons.indexOf(event.currentTarget));
  let nextIndex = currentIndex;
  if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = enabledButtons.length - 1;
  else if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % enabledButtons.length;
  else nextIndex = (currentIndex - 1 + enabledButtons.length) % enabledButtons.length;

  const nextButton = enabledButtons[nextIndex];
  await switchInputMode(nextButton.dataset.mode);
  const selectedButton = modeButtons.find((button) => button.dataset.mode === inputMode);
  if (selectedButton) selectedButton.focus();
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
  button.addEventListener("click", async () => {
    await switchInputMode(button.dataset.mode);
    const selectedButton = modeButtons.find((item) => item.dataset.mode === inputMode);
    if (selectedButton) selectedButton.focus();
  });
  button.addEventListener("keydown", handleModeKeydown);
}

deviceKey.addEventListener("change", () => {
  startAllPolling();
  refreshMetrics({ showFailure: true });
  updateLocalRecordControl();
  updateKnowledgeControls();
});
deviceKey.addEventListener("input", () => {
  deviceKeyGeneration += 1;
  updateLocalRecordControl();
  updateKnowledgeControls();
  stopAllPolling();
  if (!deviceKey.value.trim()) {
    knowledgeSnapshot = null;
    knowledgeControlState.textContent = "待校准";
    knowledgeModeState.textContent = "未进入";
    knowledgeProcessingStage.textContent = "--";
    knowledgeDraft.textContent = "等待录入";
    statusMessage.textContent = "填写密钥后开始测试";
    renderMetrics(null);
    clearError();
    updateKnowledgeControls();
  }
});

knowledgeShortPress.addEventListener("click", async () => {
  await runKnowledgeOperation("/api/device/knowledge/short-press");
});

knowledgeLongPress.addEventListener("click", async () => {
  if (!knowledgeLeaseToken) {
    await acquireKnowledgeControl();
    return;
  }
  const captureEntry = knowledgeSnapshot && knowledgeSnapshot.mode_state === "confirming";
  await runKnowledgeOperation("/api/device/knowledge/long-press", { captureEntry });
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

replayRecording.addEventListener("click", async () => {
  clearError();
  try {
    requireKey();
    replayPending = true;
    setOperationPending(true);
    updateLocalRecordControl();
    statusMessage.textContent = "正在由树莓派扬声器播放刚才的录音";
    await request("/api/device/recording/replay", { method: "POST" });
    statusMessage.textContent = "录音播放完成";
  } catch (error) {
    showError(error);
  } finally {
    replayPending = false;
    setOperationPending(false);
    await refreshState({ showFailure: false });
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
    hasLastRecording = false;
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

audio.addEventListener("error", async () => {
  if (!deviceKey.value.trim()) return;
  try {
    await request("/api/device/playback-finished", { method: "POST" });
    audioHint.textContent = "语音加载失败，设备状态已恢复";
    await refreshState({ showFailure: false });
  } catch (error) {
    showError(error);
  }
});

latencyCurrentTab.addEventListener("click", () => showLatencyView("current"));
latencyStatsTab.addEventListener("click", () => showLatencyView("stats"));
latencyRefresh.addEventListener("click", () => refreshMetrics({ showFailure: true }));

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopAllPolling();
    return;
  }
  startAllPolling();
});

window.addEventListener("beforeunload", () => {
  stopAllPolling();
  if (audioObjectUrl) URL.revokeObjectURL(audioObjectUrl);
});

renderMetrics(null);
showInputMode("microphone");
updateLocalRecordControl();
updateKnowledgeControls();
if (deviceKey.value.trim()) {
  refreshMetrics({ showFailure: true });
  startAllPolling();
}
