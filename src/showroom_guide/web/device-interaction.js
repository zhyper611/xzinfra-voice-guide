(function exposeDeviceInteraction(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  } else if (root) {
    root.ShowroomDeviceInteraction = api;
  }
}(typeof globalThis === "undefined" ? this : globalThis, () => {
  function bindUnifiedPress({ button, releaseTarget, gesture }) {
    let activeInput = null;

    function isGestureKey(key) {
      return key === " " || key === "Enter";
    }

    function releasePointer(input) {
      if (!input || input.kind !== "pointer") return;
      try {
        if (button.hasPointerCapture?.(input.value)) {
          button.releasePointerCapture(input.value);
        }
      } catch {
        return;
      }
    }

    function cancel() {
      const input = activeInput;
      activeInput = null;
      releasePointer(input);
      gesture.cancel();
    }

    function finish(input, event) {
      if (!activeInput || activeInput.kind !== input.kind || activeInput.value !== input.value) {
        return;
      }
      event?.preventDefault();
      const finishedInput = activeInput;
      activeInput = null;
      gesture.end();
      releasePointer(finishedInput);
    }

    function handlePointerDown(event) {
      if (button.disabled || activeInput !== null) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      activeInput = { kind: "pointer", value: event.pointerId };
      try {
        button.setPointerCapture?.(event.pointerId);
        gesture.start();
      } catch (error) {
        cancel();
        throw error;
      }
    }

    function handlePointerUp(event) {
      finish({ kind: "pointer", value: event.pointerId }, event);
    }

    function handleKeyDown(event) {
      if (!isGestureKey(event.key) || button.disabled) return;
      event.preventDefault();
      if (event.repeat || activeInput !== null) return;
      activeInput = { kind: "key", value: event.key };
      try {
        gesture.start();
      } catch (error) {
        cancel();
        throw error;
      }
    }

    function handleKeyUp(event) {
      if (!isGestureKey(event.key)) return;
      finish({ kind: "key", value: event.key }, event);
    }

    function handleClick(event) {
      event.preventDefault();
    }

    function handleLostPointerCapture(event) {
      if (
        activeInput?.kind === "pointer"
        && activeInput.value === event.pointerId
      ) cancel();
    }

    button.addEventListener("pointerdown", handlePointerDown);
    button.addEventListener("keydown", handleKeyDown);
    button.addEventListener("click", handleClick);
    button.addEventListener("lostpointercapture", handleLostPointerCapture);
    releaseTarget.addEventListener("pointerup", handlePointerUp);
    releaseTarget.addEventListener("pointercancel", cancel);
    releaseTarget.addEventListener("keyup", handleKeyUp);
    releaseTarget.addEventListener("blur", cancel);

    function setDisabled(value) {
      const disabled = Boolean(value);
      if (disabled) cancel();
      button.disabled = disabled;
      gesture.setDisabled(disabled);
    }

    function destroy() {
      cancel();
      button.removeEventListener("pointerdown", handlePointerDown);
      button.removeEventListener("keydown", handleKeyDown);
      button.removeEventListener("click", handleClick);
      button.removeEventListener("lostpointercapture", handleLostPointerCapture);
      releaseTarget.removeEventListener("pointerup", handlePointerUp);
      releaseTarget.removeEventListener("pointercancel", cancel);
      releaseTarget.removeEventListener("keyup", handleKeyUp);
      releaseTarget.removeEventListener("blur", cancel);
    }

    return { cancel, destroy, setDisabled };
  }

  function createWavReviewGate({ revokeObjectUrl }) {
    let state = "none";
    let objectUrl = null;
    let generation = 0;

    function revokeCurrentUrl() {
      if (!objectUrl) return;
      revokeObjectUrl(objectUrl);
      objectUrl = null;
    }

    function beginDraft() {
      revokeCurrentUrl();
      generation += 1;
      state = "loading";
      return generation;
    }

    function markFailed(token = null) {
      if (token !== null && token !== generation) return false;
      generation += 1;
      state = "failed";
      return true;
    }

    function markLoaded(nextObjectUrl) {
      if (!nextObjectUrl) {
        markFailed();
        return null;
      }
      if (objectUrl && objectUrl !== nextObjectUrl) revokeCurrentUrl();
      objectUrl = nextObjectUrl;
      generation += 1;
      state = "loaded";
      return generation;
    }

    function markReady(token) {
      if (token !== generation || state !== "loaded" || !objectUrl) return false;
      state = "ready";
      return true;
    }

    function clear() {
      revokeCurrentUrl();
      generation += 1;
      state = "none";
    }

    return {
      beginDraft,
      markFailed,
      markLoaded,
      markReady,
      clear,
      get state() { return state; },
      get objectUrl() { return objectUrl; },
      get hasDraft() { return state !== "none"; },
      get canSave() { return state === "ready" && Boolean(objectUrl); },
    };
  }

  function createKnowledgeDraftSourceStore({ storage, key }) {
    const validSources = new Set(["wav", "microphone", "unknown"]);

    function read(leaseToken) {
      if (!leaseToken) return null;
      let record;
      try {
        record = JSON.parse(storage.getItem(key) || "null");
      } catch {
        return "unknown";
      }
      if (
        !record
        || record.lease_token !== leaseToken
        || !validSources.has(record.source)
      ) return "unknown";
      return record.source;
    }

    function write(leaseToken, source) {
      if (!leaseToken) return false;
      const normalizedSource = validSources.has(source) ? source : "unknown";
      storage.setItem(key, JSON.stringify({
        lease_token: leaseToken,
        source: normalizedSource,
      }));
      return true;
    }

    function clear() {
      storage.removeItem(key);
    }

    return { read, write, clear };
  }

  function createRequestEpoch() {
    let value = 0;

    function bump() {
      value += 1;
      return value;
    }

    function capture() {
      return value;
    }

    function isCurrent(requestEpoch) {
      return requestEpoch === value;
    }

    return { bump, capture, isCurrent };
  }

  function rehydrateKnowledgeReview({ source, owned, modeState, gate }) {
    if (!owned || modeState !== "confirming" || source === "microphone") {
      return false;
    }
    if (!gate.hasDraft) {
      gate.beginDraft();
      gate.markFailed();
    }
    return true;
  }

  function resolveKnowledgeDraftTransition({
    source,
    previousMode,
    nextMode,
    shortPress,
  }) {
    if (previousMode === "ready" && nextMode === "recording") {
      return { source: "microphone", invalidateReview: false };
    }
    if (
      previousMode === "confirming"
      && (nextMode === "recording" || nextMode === "processing")
    ) {
      return { source, invalidateReview: true };
    }
    if (shortPress && previousMode === "recording" && nextMode === "confirming") {
      return { source: "microphone", invalidateReview: true };
    }
    if (
      nextMode === "inactive"
      || (nextMode === "ready" && previousMode !== null && previousMode !== "ready")
    ) {
      return { source: null, invalidateReview: true };
    }
    return { source, invalidateReview: false };
  }

  function captureKnowledgeEntry({ captureEntry, lastEntryId, previousLastEntryId }) {
    const hasLastEntry = Boolean(lastEntryId);
    const isNewEntry = hasLastEntry && lastEntryId !== previousLastEntryId;
    return {
      nextLastEntryId: hasLastEntry ? lastEntryId : previousLastEntryId,
      capturedEntryId: captureEntry && isNewEntry ? lastEntryId : null,
    };
  }

  return {
    bindUnifiedPress,
    captureKnowledgeEntry,
    createKnowledgeDraftSourceStore,
    createRequestEpoch,
    createWavReviewGate,
    rehydrateKnowledgeReview,
    resolveKnowledgeDraftTransition,
  };
}));
