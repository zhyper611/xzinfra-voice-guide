const interactionCard = document.querySelector(".interaction-card");
const connection = document.querySelector("#connection");
const connectionText = document.querySelector("#connection-text");
const phase = document.querySelector("#phase");
const message = document.querySelector("#message");
const answer = document.querySelector("#answer");
const transcript = document.querySelector("#transcript");
const form = document.querySelector("#question-form");
const question = document.querySelector("#question");
const submit = document.querySelector("#submit");
const submitLabel = submit.querySelector("span:first-child");
const characterCount = document.querySelector("#character-count");
const formError = document.querySelector("#form-error");
const audio = document.querySelector("#audio");
const audioHint = document.querySelector("#audio-hint");
const newConversation = document.querySelector("#new-conversation");

const phaseLabels = {
  idle: "待机",
  recording: "录音",
  transcribing: "识别",
  thinking: "查询资料",
  speaking: "正在讲解",
  degraded: "服务降级",
  error: "出现错误",
};

let retryDelay = 1000;
const REQUEST_TIMEOUT_MS = 180000;

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  try {
    return await fetch(path, { ...options, signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("请求超时，请检查网络后重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function renderState(snapshot) {
  interactionCard.dataset.phase = snapshot.phase;
  phase.textContent = phaseLabels[snapshot.phase] || "处理中";
  message.textContent = snapshot.message || "";
  if (snapshot.answer) answer.textContent = snapshot.answer;
  if (snapshot.transcript) transcript.textContent = snapshot.transcript;
}

function connectStateStream() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

  socket.addEventListener("open", () => {
    connection.dataset.online = "true";
    connectionText.textContent = "服务在线";
    retryDelay = 1000;
  });

  socket.addEventListener("message", (event) => {
    renderState(JSON.parse(event.data));
  });

  socket.addEventListener("close", (event) => {
    connection.dataset.online = "false";
    if (event.code === 1008) {
      window.location.reload();
      return;
    }
    connectionText.textContent = "正在重连";
    window.setTimeout(connectStateStream, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 10000);
  });
}

async function notifyPlaybackFinished() {
  try {
    const response = await request("/api/playback-finished", { method: "POST" });
    if (response.status === 401) window.location.reload();
  } catch {
    formError.textContent = "播放已结束，但状态同步失败。";
  }
}

async function playAnswer(audioUrl) {
  audio.src = audioUrl;
  audio.hidden = false;
  audioHint.textContent = "语音讲解已准备好。";
  audio.load();
  try {
    await audio.play();
  } catch {
    formError.textContent = "浏览器阻止了自动播放，请点击播放讲解。";
  }
}

function resetInterface() {
  question.value = "";
  characterCount.textContent = "0 / 500";
  transcript.textContent = "尚未提问";
  answer.textContent = "讲解内容会在这里实时出现，并在语音准备好后自动播放。";
  formError.textContent = "";
  audio.pause();
  audio.hidden = true;
  audio.removeAttribute("src");
  audioHint.textContent = "语音准备好后可在这里控制播放。";
}

newConversation.addEventListener("click", async () => {
  if (!window.confirm("确定开始新对话吗？当前对话内容将被清空。")) return;
  newConversation.disabled = true;
  try {
    const response = await request("/api/session/reset", { method: "POST" });
    if (response.status === 401) {
      window.location.reload();
      return;
    }
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || "暂时不能开始新对话");
    }
    resetInterface();
    message.textContent = "已开始新的讲解会话";
    question.focus();
  } catch (error) {
    formError.textContent = error.message || "重置失败，请稍后重试。";
  } finally {
    newConversation.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = question.value.trim();
  if (!value) {
    formError.textContent = "请输入需要讲解的问题。";
    question.focus();
    return;
  }

  submit.disabled = true;
  submitLabel.textContent = "正在处理";
  formError.textContent = "";
  audio.hidden = true;
  audio.removeAttribute("src");
  audioHint.textContent = "正在准备语音讲解。";

  try {
    const response = await request("/api/questions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: value }),
    });
    if (response.status === 401) {
      window.location.reload();
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "讲解服务暂时不可用");
    if (payload.warning) {
      formError.textContent = payload.warning;
      audioHint.textContent = "本次讲解仅提供文字内容。";
    }
    if (payload.audio_url) await playAnswer(payload.audio_url);
  } catch (error) {
    formError.textContent = error.message || "请求失败，请稍后重试。";
    audioHint.textContent = "语音尚未生成。";
  } finally {
    submit.disabled = false;
    submitLabel.textContent = "开始讲解";
  }
});

question.addEventListener("input", () => {
  characterCount.textContent = `${question.value.length} / 500`;
});

question.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    form.requestSubmit();
  }
});

audio.addEventListener("ended", notifyPlaybackFinished);
audio.addEventListener("error", async () => {
  audioHint.textContent = "语音加载失败，本次讲解已结束。";
  await notifyPlaybackFinished();
});
connectStateStream();
