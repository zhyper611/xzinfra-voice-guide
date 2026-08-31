const deviceForm = document.querySelector("#device-form");
const deviceKey = document.querySelector("#device-key");
const toggleKey = document.querySelector("#toggle-key");
const unifiedInteraction = document.querySelector(".unified-interaction");
const interactionMode = document.querySelector("#interaction-mode");
const interactionStage = document.querySelector("#interaction-stage");
const unifiedAction = document.querySelector("#unified-action");
const unifiedActionLabel = document.querySelector("#unified-action-label");
const replayRecording = document.querySelector("#replay-recording");
const replayRecordingLabel = document.querySelector("#replay-recording-label");
const advancedWav = document.querySelector("#advanced-wav");
const wavPurposeDialogue = document.querySelector("#wav-purpose-dialogue");
const wavPurposeKnowledge = document.querySelector("#wav-purpose-knowledge");
const wavFile = document.querySelector("#wav-file");
const dropZone = document.querySelector("#drop-zone");
const fileName = document.querySelector("#file-name");
const runTest = document.querySelector("#run-test");
const runTestLabel = runTest.querySelector("span");
const wavReview = document.querySelector("#wav-review");
const wavKnowledgeReview = document.querySelector("#wav-knowledge-review");
const wavReviewHint = document.querySelector("#wav-review-hint");
const wavKnowledgeActions = document.querySelector("#wav-knowledge-actions");
const wavKnowledgeDiscard = document.querySelector("#wav-knowledge-discard");
const wavKnowledgeRetry = document.querySelector("#wav-knowledge-retry");
const wavKnowledgeSave = document.querySelector("#wav-knowledge-save");
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
const knowledgeContext = document.querySelector("#knowledge-context");
const audioDeviceStatus = document.querySelector("#audio-device-status");

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
const TURN_REQUEST_TIMEOUT_MS = REQUEST_TIMEOUT_MS;
const STATUS_REQUEST_TIMEOUT_MS = 15000;
const HOLD_THRESHOLD_MS = 1500;
const KNOWLEDGE_LEASE_KEY = "showroom-knowledge-lease";
const KNOWLEDGE_DRAFT_SOURCE_KEY = "showroom-knowledge-draft-source";
const KNOWLEDGE_REVIEW_AUDIO_PATH = "/api/device/knowledge/review-audio";

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
let dialogueSource = "microphone";
let wavPurpose = "dialogue";
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
let knowledgeDraftSource = null;
let knowledgeOperationNeedsResync = false;
let deviceKeyGeneration = 0;
let knowledgeReviewAudioPath = null;
let knowledgeReviewPlayback = null;
let unifiedGesture = null;
let unifiedInput = null;
let audioAvailability = { ready: false, message: "正在检测麦克风和扬声器" };

const wavReviewGate = ShowroomDeviceInteraction.createWavReviewGate({
  revokeObjectUrl: (objectUrl) => URL.revokeObjectURL(objectUrl),
});
const knowledgeDraftSourceStore = ShowroomDeviceInteraction.createKnowledgeDraftSourceStore({
  storage: {
    getItem: (key) => sessionStorage.getItem(key),
    setItem: (key, value) => sessionStorage.setItem(key, value),
    removeItem: (key) => sessionStorage.removeItem(key),
  },
  key: KNOWLEDGE_DRAFT_SOURCE_KEY,
});
const knowledgeStateEpoch = ShowroomDeviceInteraction.createRequestEpoch();

try {
  knowledgeLeaseToken = sessionStorage.getItem(KNOWLEDGE_LEASE_KEY);
} catch {
  knowledgeLeaseToken = null;
}
knowledgeDraftSource = knowledgeDraftSourceStore.read(knowledgeLeaseToken);

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

async function knowledgeRawRequest(
  path,
  options = {},
  timeoutMs = REQUEST_TIMEOUT_MS,
  requestKey = null,
) {
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
    return response;
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
  if (knowledgeLeaseToken && knowledgeLeaseToken !== token) {
    clearKnowledgeDraftSource();
  }
  knowledgeLeaseToken = token;
  try {
    sessionStorage.setItem(KNOWLEDGE_LEASE_KEY, knowledgeLeaseToken);
  } catch {
    knowledgeLeaseToken = token;
  }
}

function persistKnowledgeDraftSource(source) {
  knowledgeDraftSource = source === "wav" || source === "microphone"
    ? source
    : "unknown";
  try {
    knowledgeDraftSourceStore.write(knowledgeLeaseToken, knowledgeDraftSource);
  } catch {
    knowledgeDraftSource = source === "microphone" ? "microphone" : "unknown";
  }
}

function clearKnowledgeDraftSource() {
  knowledgeDraftSource = null;
  try {
    knowledgeDraftSourceStore.clear();
  } catch {
    knowledgeDraftSource = null;
  }
}

function clearKnowledgeLease() {
  knowledgeLeaseToken = null;
  clearKnowledgeDraftSource();
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

function getUnifiedAction() {
  const mode = knowledgeSnapshot?.mode_state || "inactive";
  const owned = knowledgeSnapshot?.control_state === "owned" && Boolean(knowledgeLeaseToken);
  const observed = knowledgeSnapshot?.control_state === "observed";
  const knowledgeShort = () => runKnowledgeOperation("/api/device/knowledge/short-press");
  const knowledgeLong = () => runKnowledgeOperation(
    "/api/device/knowledge/long-press",
    { captureEntry: mode === "confirming" },
  );

  if (operationPending || knowledgeOperationPending) {
    return { short: null, long: null, label: "正在处理" };
  }
  if (mode === "processing" || (observed && mode !== "inactive")) {
    return {
      short: null,
      long: null,
      label: observed ? "知识补充已被占用" : "正在处理",
    };
  }
  if (mode !== "inactive" && !owned) {
    return { short: null, long: null, label: "正在校准知识模式" };
  }
  if (owned && mode === "ready") {
    return { short: knowledgeShort, long: knowledgeLong, label: "短按录入知识" };
  }
  if (owned && mode === "recording") {
    return { short: knowledgeShort, long: null, label: "短按停止并复述" };
  }
  if (owned && mode === "confirming") {
    if (knowledgeDraftSource !== "microphone" && !wavReviewGate.canSave) {
      return {
        short: knowledgeShort,
        long: null,
        label: wavReviewGate.state === "loading"
          ? "正在获取复述"
          : (wavReviewGate.state === "loaded" ? "请完整试听复述" : "复述未就绪，短按重录"),
      };
    }
    return { short: knowledgeShort, long: knowledgeLong, label: "长按保存并返回" };
  }
  if (currentPhase === "recording") {
    return { short: runDialogueShortPress, long: null, label: "短按结束录音" };
  }
  if (["transcribing", "thinking", "speaking"].includes(currentPhase)) {
    return { short: null, long: null, label: "正在处理" };
  }
  return {
    short: runDialogueShortPress,
    long: knowledgeSnapshot?.enabled === false ? null : acquireKnowledgeControl,
    label: "短按开始对话",
  };
}

function updateUnifiedAction() {
  const action = getUnifiedAction();
  const mode = knowledgeSnapshot?.mode_state || "inactive";
  const knowledgeActive = mode !== "inactive";
  const keyReady = Boolean(deviceKey.value.trim());
  const requiresAudio = (
    (mode === "inactive" && currentPhase !== "recording")
    || mode === "ready"
  );
  const disabled = (
    !keyReady
    || (!action.short && !action.long)
    || (requiresAudio && !audioAvailability.ready)
  );

  unifiedActionLabel.textContent = action.label;
  unifiedAction.dataset.state = knowledgeActive ? mode : currentPhase;
  unifiedInteraction.dataset.mode = knowledgeActive ? "knowledge" : "dialogue";
  interactionMode.textContent = knowledgeActive ? "知识补充" : "正常对话";
  interactionStage.textContent = knowledgeActive
    ? (knowledgeStageLabels[knowledgeSnapshot?.processing_stage]
      || knowledgeModeLabels[mode]
      || "待机")
    : (phaseLabels[currentPhase] || "处理中");
  knowledgeContext.hidden = !(knowledgeActive || knowledgeEntryId);
  unifiedInput.setDisabled(disabled);
}

function renderKnowledgeState(
  snapshot,
  { captureEntry = false, shortPress = false } = {},
) {
  const previousMode = knowledgeSnapshot?.mode_state || null;
  const previousSource = knowledgeDraftSource;
  knowledgeSnapshot = snapshot;
  const owned = snapshot.control_state === "owned" && Boolean(knowledgeLeaseToken);
  const draftTransition = ShowroomDeviceInteraction.resolveKnowledgeDraftTransition({
    source: previousSource,
    previousMode,
    nextMode: snapshot.mode_state,
    shortPress,
  });
  if (draftTransition.invalidateReview) clearKnowledgeReviewAudio();
  if (draftTransition.source !== previousSource) {
    if (owned && draftTransition.source) {
      persistKnowledgeDraftSource(draftTransition.source);
    } else {
      clearKnowledgeDraftSource();
    }
  }
  const hadReviewDraft = wavReviewGate.hasDraft;
  const reviewRequired = ShowroomDeviceInteraction.rehydrateKnowledgeReview({
    source: knowledgeDraftSource,
    owned,
    modeState: snapshot.mode_state,
    gate: wavReviewGate,
  });
  if (reviewRequired && !hadReviewDraft) {
    persistKnowledgeDraftSource(knowledgeDraftSource || "unknown");
    knowledgeReviewAudioPath = KNOWLEDGE_REVIEW_AUDIO_PATH;
    wavPurpose = "knowledge";
    wavPurposeDialogue.setAttribute("aria-pressed", "false");
    wavPurposeKnowledge.setAttribute("aria-pressed", "true");
    advancedWav.open = true;
    resetKnowledgeReviewPlayer();
    wavReview.hidden = false;
    wavKnowledgeActions.hidden = false;
    wavKnowledgeRetry.hidden = false;
  }
  knowledgeControlState.textContent = knowledgeControlLabels[snapshot.control_state] || "待校准";
  knowledgeModeState.textContent = knowledgeModeLabels[snapshot.mode_state] || "未知";
  knowledgeProcessingStage.textContent = knowledgeStageLabels[snapshot.processing_stage] || "--";
  knowledgeDraft.textContent = owned && snapshot.draft_text
    ? snapshot.draft_text
    : "等待录入";

  const entryCapture = ShowroomDeviceInteraction.captureKnowledgeEntry({
    captureEntry,
    lastEntryId: snapshot.last_entry_id,
    previousLastEntryId: knowledgeLastEntryId,
  });
  knowledgeLastEntryId = entryCapture.nextLastEntryId;
  if (entryCapture.capturedEntryId) {
    knowledgeEntryId = entryCapture.capturedEntryId;
    clearKnowledgeLease();
  }
  updateControls();
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
  if (error.code === "knowledge_lease_expired") {
    clearKnowledgeLease();
    clearKnowledgeReviewAudio();
  }
  const errorState = error.payload && error.payload.knowledge_state;
  if (errorState) renderKnowledgeState(errorState, { captureEntry });
  if (error.message === "设备凭证无效") stopAllPolling();
  if (showFailure) showKnowledgeError(error);
}

function renderState(snapshot) {
  audioAvailability = ShowroomDeviceInteraction.resolveAudioAvailability(snapshot);
  audioDeviceStatus.textContent = audioAvailability.message;
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
  if (dialogueSource === "microphone" && currentPhase === "speaking") {
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
  updateControls();
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

function beginKnowledgeMutation() {
  knowledgeStateEpoch.bump();
}

function renderAuthoritativeKnowledgeState(snapshot, options = {}) {
  knowledgeStateEpoch.bump();
  renderKnowledgeState(snapshot, options);
}

function resetKnowledgeReviewPlayer() {
  knowledgeReviewPlayback = null;
  wavKnowledgeReview.pause();
  wavKnowledgeReview.hidden = true;
  wavKnowledgeReview.removeAttribute("src");
  wavKnowledgeReview.load();
}

function clearKnowledgeReviewAudio() {
  resetKnowledgeReviewPlayer();
  wavKnowledgeActions.hidden = true;
  wavKnowledgeRetry.hidden = true;
  wavReview.hidden = true;
  knowledgeReviewAudioPath = null;
  wavReviewGate.clear();
}

function mountKnowledgeReviewAudio(objectUrl, token) {
  resetKnowledgeReviewPlayer();
  knowledgeReviewPlayback = { objectUrl, token };
  wavKnowledgeReview.src = objectUrl;
  wavKnowledgeReview.hidden = false;
  wavKnowledgeActions.hidden = false;
  wavKnowledgeRetry.hidden = true;
  wavReview.hidden = false;
  wavReviewHint.textContent = "请完整试听复述，播放结束后才能保存。";
  wavKnowledgeReview.load();
}

function handleKnowledgeReviewEnded() {
  const playback = knowledgeReviewPlayback;
  if (
    !playback
    || wavKnowledgeReview.getAttribute("src") !== playback.objectUrl
    || !wavKnowledgeReview.ended
    || !wavReviewGate.markReady(playback.token)
  ) return;
  wavReviewHint.textContent = "复述已完整试听，可以保存到知识库。";
  clearError();
  updateControls();
}

function handleKnowledgeReviewError() {
  const playback = knowledgeReviewPlayback;
  if (
    !playback
    || wavKnowledgeReview.getAttribute("src") !== playback.objectUrl
    || !wavKnowledgeReview.error
    || !wavReviewGate.markFailed(playback.token)
  ) return;
  resetKnowledgeReviewPlayer();
  wavKnowledgeRetry.hidden = false;
  wavReviewHint.textContent = "复述播放失败，请重新获取后再试听。";
  showKnowledgePlaybackWarning("复述音频播放失败，请重新获取后再试。");
  updateControls();
}

function showKnowledgePlaybackWarning(message, { audioReady = false } = {}) {
  deviceError.textContent = message;
  wavKnowledgeReview.hidden = !audioReady;
  wavKnowledgeActions.hidden = false;
  wavReview.hidden = false;
}

function setWavPurpose(purpose) {
  wavPurpose = purpose;
  const isDialogue = purpose === "dialogue";
  wavPurposeDialogue.setAttribute("aria-pressed", String(isDialogue));
  wavPurposeKnowledge.setAttribute("aria-pressed", String(!isDialogue));
  wavReview.hidden = isDialogue || !wavReviewGate.hasDraft;
  updateControls();
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
  updateControls();
}

function updateControls() {
  const keyReady = Boolean(deviceKey.value.trim());
  const processing = ["transcribing", "thinking", "speaking"].includes(currentPhase);
  const busy = currentPhase === "recording" || processing;
  const knowledgeMode = knowledgeSnapshot?.mode_state || "inactive";
  const knowledgeModeActive = knowledgeMode !== "inactive";
  const ownedKnowledgeReady = (
    knowledgeSnapshot?.control_state === "owned"
    && Boolean(knowledgeLeaseToken)
    && knowledgeMode === "ready"
  );
  const controlsPending = operationPending || knowledgeOperationPending;
  const wavAllowed = wavPurpose === "knowledge"
    ? (!knowledgeModeActive || ownedKnowledgeReady)
    : !knowledgeModeActive;

  deviceKey.disabled = controlsPending;
  toggleKey.disabled = controlsPending;
  runTest.disabled = controlsPending || busy || !keyReady || !wavAllowed;
  resetDevice.disabled = controlsPending || knowledgeModeActive;
  runTestLabel.textContent = operationPending
    ? "正在处理"
    : (wavPurpose === "knowledge" ? "生成知识复述" : "开始对话测试");
  wavPurposeDialogue.disabled = controlsPending || busy;
  wavPurposeKnowledge.disabled = controlsPending || busy;
  replayRecording.disabled = (
    controlsPending
    || replayPending
    || busy
    || !keyReady
    || !hasLastRecording
    || knowledgeModeActive
  );
  if (replayPending) {
    replayRecordingLabel.textContent = "正在播放录音";
  } else {
    replayRecordingLabel.textContent = "播放刚才的录音";
  }
  const wavConfirming = (
    knowledgeMode === "confirming"
    && Boolean(knowledgeLeaseToken)
    && wavReviewGate.hasDraft
  );
  wavKnowledgeDiscard.disabled = controlsPending || !wavConfirming;
  wavKnowledgeRetry.hidden = !(wavConfirming && wavReviewGate.state === "failed");
  wavKnowledgeRetry.disabled = controlsPending || wavReviewGate.state !== "failed";
  wavKnowledgeSave.disabled = controlsPending || !wavConfirming || !wavReviewGate.canSave;
  updateUnifiedAction();
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
  requestEpoch = knowledgeStateEpoch.capture(),
} = {}) {
  const captureEntry = knowledgeOperationNeedsResync;
  try {
    const snapshot = await knowledgeRequest("/api/device/knowledge/state", {}, STATUS_REQUEST_TIMEOUT_MS, requestKey);
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return false;
    if (!knowledgeStateEpoch.isCurrent(requestEpoch)) return false;
    if (!allowDuringOperation && (knowledgeOperationPending || operationPending)) return false;
    renderKnowledgeState(snapshot, { captureEntry });
    knowledgeOperationNeedsResync = false;
  } catch (error) {
    if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return false;
    if (!knowledgeStateEpoch.isCurrent(requestEpoch)) return false;
    if (!allowDuringOperation && (knowledgeOperationPending || operationPending)) return false;
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
    || knowledgeOperationPending || operationPending || document.hidden
  ) return;
  const requestId = ++knowledgeStateRequestId;
  const requestEpoch = knowledgeStateEpoch.capture();
  knowledgeStateRequestPending = true;
  knowledgeStateRequestGeneration = requestGeneration;
  try {
    const applied = await loadKnowledgeState({
      showFailure,
      requestGeneration,
      requestKey,
      requestEpoch,
    });
    if (knowledgeOperationPending || operationPending) return;
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
  const requestEpoch = knowledgeStateEpoch.capture();
  knowledgeStateRequestPending = true;
  knowledgeStateRequestGeneration = requestGeneration;
  try {
    const applied = await loadKnowledgeState({
      showFailure,
      allowDuringOperation: true,
      requestGeneration,
      requestKey,
      requestEpoch,
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
  updateControls();
}

async function acquireKnowledgeControl() {
  clearError();
  setKnowledgeOperationPending(true);
  beginKnowledgeMutation();
  let acquired = false;
  try {
    const payload = await knowledgeRequest("/api/device/knowledge/acquire", { method: "POST" });
    persistKnowledgeLease(payload.lease_token);
    renderAuthoritativeKnowledgeState(payload.knowledge_state);
    acquired = true;
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
  return acquired;
}

async function runKnowledgeOperation(path, { captureEntry = false } = {}) {
  const shortPress = path === "/api/device/knowledge/short-press";
  clearError();
  setKnowledgeOperationPending(true);
  beginKnowledgeMutation();
  try {
    const snapshot = await knowledgeRequest(path, { method: "POST" });
    if (snapshot.control_state !== "owned") clearKnowledgeLease();
    renderAuthoritativeKnowledgeState(snapshot, { captureEntry, shortPress });
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

async function releaseKnowledgeControl() {
  if (!knowledgeLeaseToken) {
    clearKnowledgeReviewAudio();
    await refreshKnowledgeState({ showFailure: false });
    return;
  }
  clearError();
  setKnowledgeOperationPending(true);
  beginKnowledgeMutation();
  try {
    const snapshot = await knowledgeRequest("/api/device/knowledge/release", { method: "POST" });
    clearKnowledgeLease();
    clearKnowledgeReviewAudio();
    renderAuthoritativeKnowledgeState(snapshot);
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
      clearKnowledgeReviewAudio();
    }
  } finally {
    setKnowledgeOperationPending(false);
    startKnowledgePolling();
  }
}

async function submitKnowledgeWav(file) {
  if (!knowledgeLeaseToken) {
    const acquired = await acquireKnowledgeControl();
    if (!acquired) return;
  }
  beginKnowledgeMutation();
  clearKnowledgeReviewAudio();
  persistKnowledgeDraftSource("wav");
  const form = new FormData();
  form.append("file", file, file.name);
  let payload;
  try {
    payload = await knowledgeRequest(
      "/api/device/knowledge/upload",
      { method: "POST", body: form },
      TURN_REQUEST_TIMEOUT_MS,
    );
  } catch (error) {
    clearKnowledgeDraftSource();
    throw error;
  }
  persistKnowledgeDraftSource("wav");
  knowledgeReviewAudioPath = payload.review_audio_url || KNOWLEDGE_REVIEW_AUDIO_PATH;
  wavReviewGate.beginDraft();
  wavReview.hidden = false;
  wavKnowledgeActions.hidden = false;
  wavKnowledgeRetry.hidden = true;
  renderAuthoritativeKnowledgeState(payload.knowledge_state);
  updateControls();
  await loadKnowledgeReviewAudio();
}

async function loadKnowledgeReviewAudio() {
  try {
    if (!knowledgeReviewAudioPath) throw new Error("复述音频地址不存在");
    const response = await knowledgeRawRequest(
      knowledgeReviewAudioPath,
      {},
      TURN_REQUEST_TIMEOUT_MS,
    );
    const reviewBlob = await response.blob();
    if (reviewBlob.size === 0) throw new Error("复述音频为空");
    const objectUrl = URL.createObjectURL(reviewBlob);
    const playbackToken = wavReviewGate.markLoaded(objectUrl);
    if (!playbackToken) throw new Error("复述音频为空");
    mountKnowledgeReviewAudio(objectUrl, playbackToken);
    updateControls();
    try {
      await wavKnowledgeReview.play();
    } catch {
      showKnowledgePlaybackWarning(
        "浏览器未能自动播放复述，请点击播放器并完整试听后再确认。",
        { audioReady: true },
      );
    }
    return true;
  } catch (error) {
    if (error.code === "knowledge_lease_expired") {
      handleKnowledgeError(error);
      return false;
    }
    wavReviewGate.markFailed();
    resetKnowledgeReviewPlayer();
    wavKnowledgeRetry.hidden = false;
    showKnowledgePlaybackWarning(
      "复述音频获取失败，请重新获取后再确认。",
    );
    updateControls();
    return false;
  }
}

async function retryKnowledgeReviewAudio() {
  clearError();
  setKnowledgeOperationPending(true);
  wavReviewGate.beginDraft();
  wavKnowledgeRetry.hidden = true;
  updateControls();
  try {
    await loadKnowledgeReviewAudio();
  } finally {
    setKnowledgeOperationPending(false);
  }
}

async function submitDialogueWav(file) {
  dialogueSource = "wav";
  clearAudio();
  audioHint.textContent = "正在生成语音讲解";
  statusMessage.textContent = "正在上传音频并执行完整链路";
  const body = new FormData();
  body.append("file", file, file.name);
  const response = await request("/api/device/turn", { method: "POST", body });
  const payload = await response.json();
  await renderTurnResult(payload);
}

async function runDialogueShortPress() {
  clearError();
  try {
    requireKey();
    dialogueSource = "microphone";
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
}

async function executeUnifiedAction(kind) {
  const action = getUnifiedAction()[kind];
  if (!action) {
    updateControls();
    return;
  }
  await action();
}

unifiedAction.style.setProperty("--hold-duration", `${HOLD_THRESHOLD_MS}ms`);
unifiedGesture = ShowroomPressGesture.createPressGesture({
  thresholdMs: HOLD_THRESHOLD_MS,
  onShortPress: () => { void executeUnifiedAction("short"); },
  onLongPress: () => { void executeUnifiedAction("long"); },
  onHoldStart: () => { unifiedAction.dataset.holding = "true"; },
  onHoldEnd: () => { unifiedAction.dataset.holding = "false"; },
});
unifiedInput = ShowroomDeviceInteraction.bindUnifiedPress({
  button: unifiedAction,
  releaseTarget: window,
  gesture: unifiedGesture,
});

toggleKey.addEventListener("click", () => {
  const showing = deviceKey.type === "text";
  deviceKey.type = showing ? "password" : "text";
  toggleKey.textContent = showing ? "显示" : "隐藏";
  toggleKey.setAttribute("aria-pressed", String(!showing));
  toggleKey.setAttribute("aria-label", showing ? "显示设备密钥" : "隐藏设备密钥");
  deviceKey.focus();
});

deviceKey.addEventListener("change", () => {
  startAllPolling();
  refreshMetrics({ showFailure: true });
  updateControls();
});
deviceKey.addEventListener("input", () => {
  deviceKeyGeneration += 1;
  updateControls();
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
    updateControls();
  }
});

wavPurposeDialogue.addEventListener("click", () => setWavPurpose("dialogue"));
wavPurposeKnowledge.addEventListener("click", () => setWavPurpose("knowledge"));
wavKnowledgeReview.addEventListener("ended", handleKnowledgeReviewEnded);
wavKnowledgeReview.addEventListener("error", handleKnowledgeReviewError);
wavKnowledgeDiscard.addEventListener("click", releaseKnowledgeControl);
wavKnowledgeRetry.addEventListener("click", retryKnowledgeReviewAudio);
wavKnowledgeSave.addEventListener("click", () => runKnowledgeOperation(
  "/api/device/knowledge/long-press",
  { captureEntry: true },
));

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

replayRecording.addEventListener("click", async () => {
  clearError();
  try {
    requireKey();
    replayPending = true;
    setOperationPending(true);
    updateControls();
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
    const selected = wavFile.files[0];
    if (!selected) throw new Error("请选择用于测试的 WAV 文件");
    if (!selected.name.toLowerCase().endsWith(".wav")) throw new Error("请选择 WAV 文件");

    setOperationPending(true);
    if (wavPurpose === "knowledge") {
      statusMessage.textContent = "正在识别知识并生成复述";
      await submitKnowledgeWav(selected);
    } else {
      await submitDialogueWav(selected);
    }
  } catch (error) {
    if (wavPurpose === "knowledge") {
      handleKnowledgeError(error);
    } else {
      showError(error);
      audioHint.textContent = "语音尚未生成";
    }
  } finally {
    if (wavPurpose === "dialogue") await refreshMetrics();
    setOperationPending(false);
    startAllPolling();
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
    clearKnowledgeDraftSource();
    clearKnowledgeReviewAudio();
    updateControls();
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

window.addEventListener("pagehide", () => {
  stopAllPolling();
  unifiedInput.cancel();
  audio.pause();
  clearKnowledgeReviewAudio();
  if (audioObjectUrl) {
    URL.revokeObjectURL(audioObjectUrl);
    audioObjectUrl = null;
  }
  const key = deviceKey.value.trim();
  if (key && knowledgeLeaseToken) {
    const headers = new Headers();
    headers.set("X-Device-Key", key);
    headers.set("X-Knowledge-Lease", knowledgeLeaseToken);
    void fetch("/api/device/knowledge/release", {
      method: "POST",
      headers,
      keepalive: true,
    });
  }
});

renderMetrics(null);
setWavPurpose("dialogue");
updateControls();
if (deviceKey.value.trim()) {
  refreshMetrics({ showFailure: true });
  startAllPolling();
}
