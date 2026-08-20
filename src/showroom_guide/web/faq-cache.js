const adminKey = document.querySelector("#admin-key");
const accessForm = document.querySelector("#access-form");
const toggleKey = document.querySelector("#toggle-key");
const connectButton = document.querySelector("#connect-button");
const refreshButton = document.querySelector("#refresh-button");
const newEntryButton = document.querySelector("#new-entry-button");
const connectionState = document.querySelector("#connection-state");
const accessError = document.querySelector("#access-error");
const entryList = document.querySelector("#entry-list");
const listEmpty = document.querySelector("#list-empty");
const resultCount = document.querySelector("#result-count");
const listStatus = document.querySelector("#list-status");
const searchInput = document.querySelector("#entry-search");
const priorityFilter = document.querySelector("#priority-filter");
const enabledFilter = document.querySelector("#enabled-filter");
const audioFilter = document.querySelector("#audio-filter");
const playActiveButton = document.querySelector("#play-active");
const generateAudioButton = document.querySelector("#generate-audio");
const playDraftButton = document.querySelector("#play-draft");
const approveAudioButton = document.querySelector("#approve-audio");
const reviewAudio = document.querySelector("#review-audio");
const reviewMessage = document.querySelector("#review-message");
const draftStatus = document.querySelector("#draft-status");
const editEntryButton = document.querySelector("#edit-entry-button");
const deleteEntryButton = document.querySelector("#delete-entry-button");
const entryEditor = document.querySelector("#entry-editor");
const entryEditorForm = document.querySelector("#entry-editor-form");
const editorTitle = document.querySelector("#editor-title");
const editorId = document.querySelector("#editor-id");
const editorTitleInput = document.querySelector("#editor-title-input");
const editorPriority = document.querySelector("#editor-priority");
const editorEnabled = document.querySelector("#editor-enabled");
const editorAliases = document.querySelector("#editor-aliases");
const editorAnswer = document.querySelector("#editor-answer");
const editorRulesEnabled = document.querySelector("#editor-rules-enabled");
const editorRules = document.querySelector("#editor-rules");
const editorSubjects = document.querySelector("#editor-subjects");
const editorIntents = document.querySelector("#editor-intents");
const editorExcludes = document.querySelector("#editor-excludes");
const editorError = document.querySelector("#editor-error");
const saveEntryButton = document.querySelector("#save-entry");

const audioLabels = {
  valid: "有效",
  missing: "缺失",
  stale: "已过期",
  invalid: "无效",
  disabled: "已停用",
};

let snapshot = null;
let selectedId = null;
let requestPending = false;
let actionPending = false;
let reviewedDraftId = null;
let playingSource = null;
let reviewAudioUrl = null;
let editorMode = "create";
let editorEntry = null;

function setConnection(state, title, detail) {
  connectionState.dataset.state = state;
  connectionState.querySelector("strong").textContent = title;
  connectionState.querySelector("span").textContent = detail;
}

function setPending(pending) {
  requestPending = pending;
  connectButton.disabled = pending;
  refreshButton.disabled = pending || !snapshot;
  newEntryButton.disabled = pending || !snapshot;
  connectButton.querySelector("span").textContent = pending ? "正在读取" : "读取缓存";
}

async function readSnapshot() {
  const key = adminKey.value.trim();
  if (!key) throw new Error("请输入管理密钥");
  const response = await fetch("/api/faq-cache", {
    headers: { "X-FAQ-Admin-Key": key },
    cache: "no-store",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) throw new Error("管理密钥无效");
    if (response.status === 404) throw new Error("高频问答维护功能未启用");
    if (response.status === 422) throw new Error("高频问答配置校验失败");
    if (response.status === 503) throw new Error("高频问答配置暂时无法读取");
    throw new Error(payload.detail || `请求失败（${response.status}）`);
  }
  return response.json();
}

async function adminRequest(url, options = {}) {
  const key = adminKey.value.trim();
  if (!key) throw new Error("请输入管理密钥");
  const headers = new Headers(options.headers || {});
  headers.set("X-FAQ-Admin-Key", key);
  const response = await fetch(url, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) throw new Error("管理密钥无效");
    if (response.status === 404) throw new Error(payload.detail || "语音不存在");
    if (response.status === 409) throw new Error(payload.detail || "当前操作无法执行");
    if (response.status === 503) throw new Error(payload.detail || "服务暂时不可用");
    throw new Error(payload.detail || `请求失败（${response.status}）`);
  }
  return response;
}

async function refreshSnapshot() {
  if (requestPending) return;
  accessError.textContent = "";
  setPending(true);
  setConnection("loading", "正在读取", "检查配置与预生成音频");
  try {
    snapshot = await readSnapshot();
    if (!snapshot.entries.some((entry) => entry.id === selectedId)) {
      selectedId = snapshot.entries[0]?.id || null;
    }
    renderSnapshot();
    setConnection("ready", "连接成功", `${snapshot.entries.length} 个缓存条目`);
  } catch (error) {
    setConnection("error", "读取失败", "请检查服务状态与管理密钥");
    accessError.textContent = error.message;
  } finally {
    setPending(false);
  }
}

function renderSnapshot() {
  const summary = snapshot.summary;
  document.querySelector("#document-name").textContent = snapshot.source_document || "未命名文档";
  document.querySelector("#reload-policy").textContent = snapshot.reload_policy === "restart_required" ? "变更后需重启" : "运行时生效";
  document.querySelector("#summary").hidden = false;
  document.querySelector("#summary-total").textContent = summary.total;
  document.querySelector("#summary-enabled").textContent = summary.enabled;
  document.querySelector("#summary-valid").textContent = summary.audio_valid;
  document.querySelector("#summary-draft").textContent = summary.draft_ready;
  document.querySelector("#summary-attention").textContent = summary.audio_missing + summary.audio_stale + summary.audio_invalid;
  document.querySelector("#tts-profile").hidden = false;
  document.querySelector("#profile-model").textContent = snapshot.tts_profile.model;
  document.querySelector("#profile-voice").textContent = snapshot.tts_profile.voice;
  document.querySelector("#profile-speed").textContent = `${snapshot.tts_profile.speed}x`;
  listStatus.textContent = "已读取当前文件";
  renderEntries();
}

function filteredEntries() {
  if (!snapshot) return [];
  const query = searchInput.value.trim().toLocaleLowerCase("zh-CN");
  return snapshot.entries.filter((entry) => {
    const searchable = [entry.title, entry.id, ...entry.aliases].join(" ").toLocaleLowerCase("zh-CN");
    const matchesText = !query || searchable.includes(query);
    const matchesPriority = priorityFilter.value === "all" || entry.priority === priorityFilter.value;
    const matchesEnabled = enabledFilter.value === "all" || entry.enabled === (enabledFilter.value === "enabled");
    const matchesAudio = audioFilter.value === "all" || entry.audio.status === audioFilter.value;
    return matchesText && matchesPriority && matchesEnabled && matchesAudio;
  });
}

function renderEntries() {
  const entries = filteredEntries();
  entryList.replaceChildren();
  resultCount.textContent = `${entries.length} 个条目`;
  listEmpty.hidden = entries.length > 0;

  for (const entry of entries) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "entry-item";
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(entry.id === selectedId));
    button.dataset.entryId = entry.id;

    const header = document.createElement("header");
    const title = document.createElement("h3");
    title.textContent = entry.title;
    const version = document.createElement("span");
    version.textContent = `v${entry.version}`;
    header.append(title, version);

    const id = document.createElement("p");
    id.textContent = entry.id;
    const footer = document.createElement("footer");
    const priority = document.createElement("span");
    priority.textContent = entry.priority === "high" ? "高优先级" : "中优先级";
    const enabled = document.createElement("span");
    enabled.textContent = entry.enabled ? "已启用" : "已停用";
    const audio = document.createElement("span");
    audio.className = "audio-dot";
    audio.dataset.status = entry.audio.status;
    audio.title = `音频${audioLabels[entry.audio.status] || entry.audio.status}`;
    footer.append(priority, enabled, audio);
    button.append(header, id, footer);
    button.addEventListener("click", () => selectEntry(entry.id));
    entryList.append(button);
  }

  const selectedVisible = entries.some((entry) => entry.id === selectedId);
  if (!selectedVisible && entries.length) selectedId = entries[0].id;
  renderDetail(snapshot?.entries.find((entry) => entry.id === selectedId) || null);
  for (const item of entryList.querySelectorAll(".entry-item")) {
    item.setAttribute("aria-selected", String(item.dataset.entryId === selectedId));
  }
}

function selectEntry(id) {
  if (selectedId !== id) clearReviewAudio();
  selectedId = id;
  renderEntries();
}

function appendTags(container, values) {
  container.replaceChildren();
  for (const value of values) {
    const tag = document.createElement("span");
    tag.textContent = value;
    container.append(tag);
  }
  if (!values.length) {
    const tag = document.createElement("span");
    tag.textContent = "无";
    container.append(tag);
  }
}

function appendRule(container, label, values) {
  const row = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = values.length ? values.join("、") : "无";
  row.append(term, description);
  container.append(row);
}

function renderDetail(entry) {
  document.querySelector("#detail-empty").hidden = Boolean(entry);
  const detail = document.querySelector("#entry-detail");
  detail.hidden = !entry;
  if (!entry) return;

  document.querySelector("#detail-id").textContent = entry.id;
  document.querySelector("#detail-name").textContent = entry.title;
  const enabled = document.querySelector("#detail-enabled");
  enabled.textContent = entry.enabled ? "已启用" : "已停用";
  enabled.dataset.enabled = String(entry.enabled);
  document.querySelector("#detail-priority").textContent = entry.priority === "high" ? "高" : "中";
  document.querySelector("#detail-version").textContent = `v${entry.version}`;
  document.querySelector("#detail-audio-status").textContent = audioLabels[entry.audio.status] || entry.audio.status;
  document.querySelector("#detail-answer").textContent = entry.answer;
  document.querySelector("#alias-count").textContent = entry.aliases.length;
  appendTags(document.querySelector("#detail-aliases"), entry.aliases);

  const rules = document.querySelector("#detail-rules");
  rules.replaceChildren();
  const matchRules = entry.match_rules;
  appendRule(rules, "主题词", matchRules?.subjects || []);
  appendRule(rules, "意图词", matchRules?.intents || []);
  appendRule(rules, "排除词", matchRules?.excludes || []);

  document.querySelector("#audio-file").textContent = entry.audio_file || "--";
  document.querySelector("#audio-duration").textContent = Number.isFinite(entry.audio.duration_seconds) ? `${entry.audio.duration_seconds.toFixed(2)} 秒` : "--";
  document.querySelector("#audio-format").textContent = entry.audio.sample_rate ? `${entry.audio.sample_rate} Hz · ${entry.audio.channels} 声道 · ${entry.audio.sample_width * 8} bit` : "--";
  document.querySelector("#audio-size").textContent = Number.isFinite(entry.audio.size_bytes) ? `${(entry.audio.size_bytes / 1024).toFixed(1)} KiB` : "--";
  renderReviewControls(entry);
  renderCrudControls(entry);
}

function selectedEntry() {
  return snapshot?.entries.find((entry) => entry.id === selectedId) || null;
}

function clearReviewAudio() {
  reviewAudio.pause();
  reviewAudio.hidden = true;
  reviewAudio.removeAttribute("src");
  reviewAudio.load();
  if (reviewAudioUrl) URL.revokeObjectURL(reviewAudioUrl);
  reviewAudioUrl = null;
  playingSource = null;
  reviewedDraftId = null;
}

function renderReviewControls(entry) {
  const draft = entry.draft_audio || { status: "none" };
  const labels = { none: "无草稿", ready: "待审批", stale: "草稿已过期", invalid: "草稿无效" };
  draftStatus.textContent = labels[draft.status] || draft.status;
  draftStatus.dataset.status = draft.status;
  playActiveButton.disabled = actionPending || entry.audio.status !== "valid";
  generateAudioButton.disabled = actionPending || !entry.enabled;
  generateAudioButton.lastChild.textContent = draft.status === "ready" ? "重新生成草稿" : "生成语音草稿";
  playDraftButton.disabled = actionPending || draft.status !== "ready";
  approveAudioButton.disabled = actionPending || draft.status !== "ready" || reviewedDraftId !== entry.id;
}

function setActionPending(pending) {
  actionPending = pending;
  const entry = selectedEntry();
  newEntryButton.disabled = pending || !snapshot;
  if (entry) {
    renderReviewControls(entry);
    renderCrudControls(entry);
  }
}

function renderCrudControls(entry) {
  editEntryButton.disabled = actionPending || !entry;
  deleteEntryButton.disabled = actionPending || !entry;
}

function splitLines(value) {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function joinLines(values) {
  return (values || []).join("\n");
}

function setRulesEditorEnabled(enabled) {
  editorRules.dataset.enabled = String(enabled);
  for (const field of [editorSubjects, editorIntents, editorExcludes]) {
    field.disabled = !enabled;
  }
}

function openEntryEditor(mode) {
  const entry = mode === "edit" ? selectedEntry() : null;
  if (mode === "edit" && !entry) return;
  editorMode = mode;
  editorEntry = entry;
  editorError.textContent = "";
  editorTitle.textContent = mode === "create" ? "新建缓存条目" : "编辑缓存条目";
  editorId.disabled = mode === "edit";
  editorId.value = entry?.id || "";
  editorTitleInput.value = entry?.title || "";
  editorPriority.value = entry?.priority || "medium";
  editorEnabled.checked = entry?.enabled ?? false;
  editorAliases.value = joinLines(entry?.aliases);
  editorAnswer.value = entry?.answer || "";
  const rules = entry?.match_rules || null;
  editorRulesEnabled.checked = Boolean(rules);
  editorSubjects.value = joinLines(rules?.subjects);
  editorIntents.value = joinLines(rules?.intents);
  editorExcludes.value = joinLines(rules?.excludes);
  setRulesEditorEnabled(Boolean(rules));
  entryEditor.showModal();
  (mode === "create" ? editorId : editorTitleInput).focus();
}

function closeEntryEditor() {
  if (!actionPending) entryEditor.close();
}

function editorPayload() {
  const aliases = splitLines(editorAliases.value);
  if (!aliases.length) throw new Error("至少填写一个精确别名");
  let matchRules = null;
  if (editorRulesEnabled.checked) {
    const subjects = splitLines(editorSubjects.value);
    const intents = splitLines(editorIntents.value);
    if (!subjects.length || !intents.length) {
      throw new Error("启用规则匹配后，主题词和意图词都不能为空");
    }
    matchRules = {
      subjects,
      intents,
      excludes: splitLines(editorExcludes.value),
    };
  }
  return {
    title: editorTitleInput.value.trim(),
    enabled: editorEnabled.checked,
    priority: editorPriority.value,
    aliases,
    match_rules: matchRules,
    answer: editorAnswer.value.trim(),
  };
}

async function saveEntry(event) {
  event.preventDefault();
  if (!entryEditorForm.reportValidity()) return;
  editorError.textContent = "";
  try {
    const payload = editorPayload();
    let url = "/api/faq-cache/entries";
    let method = "POST";
    if (editorMode === "create") {
      payload.id = editorId.value.trim();
    } else {
      url = `/api/faq-cache/${encodeURIComponent(editorEntry.id)}`;
      method = "PUT";
      payload.expected_edit_token = editorEntry.edit_token;
    }
    setActionPending(true);
    saveEntryButton.disabled = true;
    saveEntryButton.textContent = "正在保存";
    const response = await adminRequest(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    selectedId = result.entry_id;
    await refreshSnapshot();
    entryEditor.close();
    listStatus.textContent = result.status === "created" ? "条目已创建" : "条目已更新";
  } catch (error) {
    editorError.textContent = error.message;
  } finally {
    saveEntryButton.disabled = false;
    saveEntryButton.textContent = "保存条目";
    setActionPending(false);
  }
}

async function deleteSelectedEntry() {
  const entry = selectedEntry();
  if (!entry) return;
  const confirmed = window.confirm(
    `确认删除“${entry.title}”的缓存配置？正式音频和待审批草稿会保留。`,
  );
  if (!confirmed) return;
  setActionPending(true);
  try {
    const response = await adminRequest(
      `/api/faq-cache/${encodeURIComponent(entry.id)}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_edit_token: entry.edit_token }),
      },
    );
    const result = await response.json();
    selectedId = null;
    clearReviewAudio();
    await refreshSnapshot();
    listStatus.textContent = "条目已删除，音频资产已保留";
    connectionState.querySelector("span").textContent = result.message;
  } catch (error) {
    reviewMessage.textContent = error.message;
  } finally {
    setActionPending(false);
  }
}

async function playEntryAudio(source) {
  const entry = selectedEntry();
  if (!entry) return;
  setActionPending(true);
  reviewMessage.textContent = source === "draft" ? "正在加载待审批草稿…" : "正在加载正式音频…";
  try {
    const response = await adminRequest(`/api/faq-cache/${encodeURIComponent(entry.id)}/audio?source=${source}`);
    const blob = await response.blob();
    clearReviewAudio();
    reviewAudioUrl = URL.createObjectURL(blob);
    reviewAudio.src = reviewAudioUrl;
    reviewAudio.hidden = false;
    playingSource = source;
    reviewMessage.textContent = source === "draft" ? "请完整试听草稿；播放结束后可审批。" : "正在试听当前正式音频。";
    reviewAudio.load();
    await reviewAudio.play().catch(() => {
      reviewMessage.textContent = "浏览器未允许自动播放，请点击播放器开始试听。";
    });
  } catch (error) {
    reviewMessage.textContent = error.message;
  } finally {
    setActionPending(false);
  }
}

async function generateDraft() {
  const entry = selectedEntry();
  if (!entry) return;
  clearReviewAudio();
  setActionPending(true);
  reviewMessage.textContent = "正在调用语音合成模型生成草稿…";
  try {
    const response = await adminRequest(
      `/api/faq-cache/${encodeURIComponent(entry.id)}/audio/generate`,
      { method: "POST" },
    );
    const payload = await response.json();
    reviewMessage.textContent = payload.message;
    await refreshSnapshot();
    reviewMessage.textContent = "语音草稿已生成，请试听后审批。";
  } catch (error) {
    reviewMessage.textContent = error.message;
  } finally {
    setActionPending(false);
  }
}

async function approveDraft() {
  const entry = selectedEntry();
  if (!entry || reviewedDraftId !== entry.id) return;
  if (!window.confirm(`确认将“${entry.title}”的试听草稿安装为正式音频？`)) return;
  setActionPending(true);
  reviewMessage.textContent = "正在校验并安装正式音频…";
  try {
    const response = await adminRequest(
      `/api/faq-cache/${encodeURIComponent(entry.id)}/audio/approve`,
      { method: "POST" },
    );
    const payload = await response.json();
    clearReviewAudio();
    await refreshSnapshot();
    draftStatus.textContent = "已安装";
    draftStatus.dataset.status = "approved";
    reviewMessage.textContent = payload.message;
  } catch (error) {
    reviewMessage.textContent = error.message;
  } finally {
    setActionPending(false);
  }
}

toggleKey.addEventListener("click", () => {
  const showing = adminKey.type === "text";
  adminKey.type = showing ? "password" : "text";
  toggleKey.textContent = showing ? "显示" : "隐藏";
  toggleKey.setAttribute("aria-pressed", String(!showing));
  toggleKey.setAttribute("aria-label", showing ? "显示管理密钥" : "隐藏管理密钥");
  adminKey.focus();
});

accessForm.addEventListener("submit", (event) => {
  event.preventDefault();
  refreshSnapshot();
});
refreshButton.addEventListener("click", refreshSnapshot);
newEntryButton.addEventListener("click", () => openEntryEditor("create"));
editEntryButton.addEventListener("click", () => openEntryEditor("edit"));
deleteEntryButton.addEventListener("click", deleteSelectedEntry);
entryEditorForm.addEventListener("submit", saveEntry);
document.querySelector("#close-editor").addEventListener("click", closeEntryEditor);
document.querySelector("#cancel-editor").addEventListener("click", closeEntryEditor);
editorRulesEnabled.addEventListener("change", () => {
  setRulesEditorEnabled(editorRulesEnabled.checked);
});
playActiveButton.addEventListener("click", () => playEntryAudio("active"));
playDraftButton.addEventListener("click", () => playEntryAudio("draft"));
generateAudioButton.addEventListener("click", generateDraft);
approveAudioButton.addEventListener("click", approveDraft);
reviewAudio.addEventListener("ended", () => {
  const entry = selectedEntry();
  if (playingSource === "draft" && entry) {
    reviewedDraftId = entry.id;
    reviewMessage.textContent = "草稿试听完成，可以审批安装。";
    renderReviewControls(entry);
  }
});
for (const control of [searchInput, priorityFilter, enabledFilter, audioFilter]) {
  control.addEventListener(control === searchInput ? "input" : "change", renderEntries);
}
adminKey.addEventListener("input", () => {
  accessError.textContent = "";
  if (!adminKey.value.trim()) setConnection("idle", "等待连接", "密钥仅保留在当前页面");
});

window.addEventListener("beforeunload", () => {
  if (reviewAudioUrl) URL.revokeObjectURL(reviewAudioUrl);
});
