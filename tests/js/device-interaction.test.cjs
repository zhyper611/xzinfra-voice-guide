const assert = require("node:assert/strict");
const test = require("node:test");

const { createPressGesture } = require(
  "../../src/showroom_guide/web/press-gesture.js",
);
const {
  bindUnifiedPress,
  captureKnowledgeEntry,
  createKnowledgeDraftSourceStore,
  createRequestEpoch,
  createWavReviewGate,
  rehydrateKnowledgeReview,
  resolveKnowledgeDraftTransition,
} = require("../../src/showroom_guide/web/device-interaction.js");

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  dispatch(type, fields = {}) {
    const event = {
      type,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      ...fields,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }
}

class FakeButton extends FakeEventTarget {
  constructor() {
    super();
    this.disabled = false;
    this.capturedPointers = new Set();
  }

  setPointerCapture(pointerId) {
    this.capturedPointers.add(pointerId);
  }

  hasPointerCapture(pointerId) {
    return this.capturedPointers.has(pointerId);
  }

  releasePointerCapture(pointerId) {
    this.capturedPointers.delete(pointerId);
  }
}

function createFakeTimers() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimer(callback) {
      const id = nextId++;
      timers.set(id, callback);
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    fire() {
      const pending = [...timers.values()];
      timers.clear();
      for (const callback of pending) callback();
    },
  };
}

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("async keyboard long press cannot lock the next pointer or keyboard press", async () => {
  const button = new FakeButton();
  const releaseTarget = new FakeEventTarget();
  const timers = createFakeTimers();
  const actions = [];
  let input;
  const gesture = createPressGesture({
    thresholdMs: 1500,
    onShortPress: () => actions.push("short"),
    onLongPress: () => {
      actions.push("long");
      Promise.resolve().then(() => input.setDisabled(true));
    },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  input = bindUnifiedPress({ button, releaseTarget, gesture });

  button.dispatch("keydown", { key: "Enter", repeat: false });
  timers.fire();
  await Promise.resolve();
  assert.equal(button.disabled, true);

  releaseTarget.dispatch("keyup", { key: "Enter" });
  input.setDisabled(false);
  button.dispatch("pointerdown", { pointerId: 7, pointerType: "mouse", button: 0 });
  releaseTarget.dispatch("pointerup", { pointerId: 7 });
  button.dispatch("keydown", { key: " ", repeat: false });
  releaseTarget.dispatch("keyup", { key: " " });

  assert.deepEqual(actions, ["long", "short", "short"]);
  assert.equal(button.capturedPointers.size, 0);
  assert.equal(button.dispatch("click").defaultPrevented, true);
});

test("WAV review stays locked after loading and unlocks only after ended", () => {
  const revoked = [];
  const gate = createWavReviewGate({ revokeObjectUrl: (url) => revoked.push(url) });

  gate.beginDraft();
  gate.markFailed();
  assert.equal(gate.hasDraft, true);
  assert.equal(gate.canSave, false);

  assert.equal(gate.markLoaded(""), null);
  assert.equal(gate.canSave, false);
  gate.beginDraft();
  assert.equal(gate.state, "loading");
  assert.equal(gate.canSave, false);
  const firstToken = gate.markLoaded("blob:review-one");
  assert.equal(gate.state, "loaded");
  assert.equal(gate.canSave, false);
  assert.equal(gate.markReady(firstToken), true);
  assert.equal(gate.canSave, true);

  gate.beginDraft();
  assert.deepEqual(revoked, ["blob:review-one"]);
  const secondToken = gate.markLoaded("blob:review-two");
  assert.equal(gate.markReady(firstToken), false);
  assert.equal(gate.canSave, false);
  assert.equal(gate.markReady(secondToken), true);
  gate.clear();
  assert.deepEqual(revoked, ["blob:review-one", "blob:review-two"]);
  assert.equal(gate.hasDraft, false);
  assert.equal(gate.canSave, false);
});

test("autoplay rejection leaves a loaded review locked for manual listening", async () => {
  const gate = createWavReviewGate({ revokeObjectUrl: () => {} });
  gate.beginDraft();
  const playbackToken = gate.markLoaded("blob:manual-review");

  await Promise.reject(new Error("autoplay blocked")).catch(() => false);

  assert.equal(gate.state, "loaded");
  assert.equal(gate.canSave, false);
  assert.equal(gate.markReady(playbackToken), true);
  assert.equal(gate.canSave, true);
});

test("WAV and unknown draft sources fail closed after page refresh", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const firstPage = createKnowledgeDraftSourceStore({
    storage,
    key: "draft-source",
  });
  firstPage.write("lease-one", "wav");

  const refreshedPage = createKnowledgeDraftSourceStore({
    storage,
    key: "draft-source",
  });
  const restoredSource = refreshedPage.read("lease-one");
  const wavGate = createWavReviewGate({ revokeObjectUrl: () => {} });

  assert.equal(restoredSource, "wav");
  assert.equal(rehydrateKnowledgeReview({
    source: restoredSource,
    owned: true,
    modeState: "confirming",
    gate: wavGate,
  }), true);
  assert.equal(wavGate.state, "failed");
  assert.equal(wavGate.canSave, false);
  wavGate.beginDraft();
  const restoredToken = wavGate.markLoaded("blob:restored-review");
  assert.equal(wavGate.canSave, false);
  wavGate.markReady(restoredToken);
  assert.equal(wavGate.canSave, true);

  const unknownGate = createWavReviewGate({ revokeObjectUrl: () => {} });
  assert.equal(refreshedPage.read("different-lease"), "unknown");
  assert.equal(rehydrateKnowledgeReview({
    source: "unknown",
    owned: true,
    modeState: "confirming",
    gate: unknownGate,
  }), true);
  assert.equal(unknownGate.canSave, false);
  const unknownToken = unknownGate.markLoaded("blob:unknown-review");
  unknownGate.markReady(unknownToken);
  assert.equal(unknownGate.canSave, true);
});

test("known microphone drafts do not require browser review after refresh", () => {
  const values = new Map();
  const store = createKnowledgeDraftSourceStore({
    storage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    key: "draft-source",
  });
  store.write("lease-one", "microphone");
  const gate = createWavReviewGate({ revokeObjectUrl: () => {} });

  assert.equal(store.read("lease-one"), "microphone");
  assert.equal(rehydrateKnowledgeReview({
    source: "microphone",
    owned: true,
    modeState: "confirming",
    gate,
  }), false);
  assert.equal(gate.hasDraft, false);
});

test("ready microphone recording remains directly saveable after automatic confirming", () => {
  const started = resolveKnowledgeDraftTransition({
    source: null,
    previousMode: "ready",
    nextMode: "recording",
    shortPress: false,
  });
  assert.deepEqual(started, {
    source: "microphone",
    invalidateReview: false,
  });

  const confirmedByPoll = resolveKnowledgeDraftTransition({
    source: started.source,
    previousMode: "processing",
    nextMode: "confirming",
    shortPress: false,
  });
  const gate = createWavReviewGate({ revokeObjectUrl: () => {} });
  assert.equal(confirmedByPoll.source, "microphone");
  assert.equal(rehydrateKnowledgeReview({
    source: confirmedByPoll.source,
    owned: true,
    modeState: "confirming",
    gate,
  }), false);
  assert.equal(gate.canSave, false);
});

test("failed microphone start keeps the previous draft source", () => {
  assert.deepEqual(resolveKnowledgeDraftTransition({
    source: "unknown",
    previousMode: "ready",
    nextMode: "ready",
    shortPress: true,
  }), {
    source: "unknown",
    invalidateReview: false,
  });
});

test("rerecord invalidates ready WAV and unknown reviews until the current draft is fetched", () => {
  for (const source of ["wav", "unknown"]) {
    const revoked = [];
    const gate = createWavReviewGate({ revokeObjectUrl: (url) => revoked.push(url) });
    gate.beginDraft();
    const oldToken = gate.markLoaded(`blob:old-${source}`);
    gate.markReady(oldToken);
    assert.equal(gate.canSave, true);
    const started = resolveKnowledgeDraftTransition({
      source,
      previousMode: "confirming",
      nextMode: "recording",
      shortPress: true,
    });
    assert.deepEqual(started, {
      source,
      invalidateReview: true,
    });
    if (started.invalidateReview) gate.clear();
    assert.equal(gate.canSave, false);
    assert.equal(gate.objectUrl, null);
    assert.deepEqual(revoked, [`blob:old-${source}`]);

    const failedBackToConfirming = resolveKnowledgeDraftTransition({
      source: started.source,
      previousMode: "recording",
      nextMode: "confirming",
      shortPress: false,
    });
    assert.equal(failedBackToConfirming.source, source);
    assert.equal(rehydrateKnowledgeReview({
      source: failedBackToConfirming.source,
      owned: true,
      modeState: "confirming",
      gate,
    }), true);
    assert.equal(gate.canSave, false);

    const processingObservedByPoll = resolveKnowledgeDraftTransition({
      source,
      previousMode: "confirming",
      nextMode: "processing",
      shortPress: false,
    });
    assert.equal(processingObservedByPoll.source, source);
    assert.equal(processingObservedByPoll.invalidateReview, true);

    gate.beginDraft();
    const currentToken = gate.markLoaded(`blob:current-${source}`);
    assert.equal(gate.canSave, false);
    assert.equal(gate.markReady(currentToken), true);
    assert.equal(gate.canSave, true);
  }
});

test("explicit recording stop replaces an old review with a microphone draft", () => {
  assert.deepEqual(resolveKnowledgeDraftTransition({
    source: "wav",
    previousMode: "recording",
    nextMode: "confirming",
    shortPress: true,
  }), {
    source: "microphone",
    invalidateReview: true,
  });
});

test("upload snapshots never capture historical knowledge entries", () => {
  assert.deepEqual(captureKnowledgeEntry({
    captureEntry: false,
    lastEntryId: "historical-entry",
    previousLastEntryId: null,
  }), {
    nextLastEntryId: "historical-entry",
    capturedEntryId: null,
  });
  assert.deepEqual(captureKnowledgeEntry({
    captureEntry: true,
    lastEntryId: "saved-entry",
    previousLastEntryId: "historical-entry",
  }), {
    nextLastEntryId: "saved-entry",
    capturedEntryId: "saved-entry",
  });
});

test("stale knowledge poll cannot replace an authoritative WAV draft", async () => {
  const epoch = createRequestEpoch();
  const staleState = createDeferred();
  const revoked = [];
  const gate = createWavReviewGate({ revokeObjectUrl: (url) => revoked.push(url) });
  let snapshot = { mode_state: "ready" };
  let reviewVisible = false;

  const requestEpoch = epoch.capture();
  const poll = staleState.promise.then((nextSnapshot) => {
    if (!epoch.isCurrent(requestEpoch)) return false;
    snapshot = nextSnapshot;
    gate.clear();
    reviewVisible = false;
    return true;
  });

  epoch.bump();
  epoch.bump();
  snapshot = { mode_state: "confirming" };
  gate.beginDraft();
  const reviewToken = gate.markLoaded("blob:current-review");
  gate.markReady(reviewToken);
  reviewVisible = true;

  staleState.resolve({ mode_state: "processing" });
  assert.equal(await poll, false);
  assert.equal(snapshot.mode_state, "confirming");
  assert.equal(gate.canSave, true);
  assert.equal(gate.objectUrl, "blob:current-review");
  assert.equal(reviewVisible, true);
  assert.deepEqual(revoked, []);

  const freshEpoch = epoch.capture();
  const freshApplied = await Promise.resolve({ mode_state: "confirming" }).then(
    (nextSnapshot) => {
      if (!epoch.isCurrent(freshEpoch)) return false;
      snapshot = nextSnapshot;
      return true;
    },
  );
  assert.equal(freshApplied, true);
  assert.equal(snapshot.mode_state, "confirming");
});

test("stale knowledge poll error cannot surface after a mutation", async () => {
  const epoch = createRequestEpoch();
  const staleError = createDeferred();
  const shownErrors = [];
  const requestEpoch = epoch.capture();
  const poll = staleError.promise.catch((error) => {
    if (!epoch.isCurrent(requestEpoch)) return false;
    shownErrors.push(error.message);
    return true;
  });

  epoch.bump();
  staleError.reject(new Error("旧轮询失败"));
  assert.equal(await poll, false);
  assert.deepEqual(shownErrors, []);

  const currentEpoch = epoch.capture();
  assert.equal(epoch.isCurrent(currentEpoch), true);
});
