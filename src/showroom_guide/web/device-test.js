const deviceForm = document.querySelector("#device-form");
const deviceKey = document.querySelector("#device-key");
const toggleKey = document.querySelector("#toggle-key");
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
  return new Error(messages[response.status] || detail || `请求失败（${response.status}）`);
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
  const currentPhase = snapshot.phase || "idle";
  phasePill.dataset.phase = currentPhase;
  phase.textContent = phaseLabels[currentPhase] || "处理中";
  statusMessage.textContent = snapshot.message || "设备状态已更新";
  if (snapshot.transcript) transcript.textContent = snapshot.transcript;
  if (snapshot.answer) answer.textContent = snapshot.answer;
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
  runTest.disabled = pending;
  resetDevice.disabled = pending;
  runTestLabel.textContent = pending ? "正在处理" : "开始测试";
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

deviceKey.addEventListener("change", startPolling);
deviceKey.addEventListener("input", () => {
  if (!deviceKey.value.trim()) {
    stopPolling();
    statusMessage.textContent = "填写密钥后开始测试";
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

deviceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  try {
    requireKey();
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

    transcript.textContent = payload.transcript || "未识别到有效内容";
    answer.textContent = payload.answer || "知识库未返回回答";
    if (payload.warning) deviceError.textContent = payload.warning;
    if (payload.audio_url) {
      await loadProtectedAudio(payload);
    } else {
      audioHint.textContent = "本次仅返回文字内容";
    }
  } catch (error) {
    showError(error);
    audioHint.textContent = "语音尚未生成";
  } finally {
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
    phasePill.dataset.phase = "idle";
    phase.textContent = "待机";
    statusMessage.textContent = "设备已重置，可以开始新一轮测试";
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

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else startPolling();
});

window.addEventListener("beforeunload", () => {
  stopPolling();
  if (audioObjectUrl) URL.revokeObjectURL(audioObjectUrl);
});
