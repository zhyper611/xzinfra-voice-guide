(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else if (root) root.ShowroomServoSimulator = api;
}(typeof globalThis === "undefined" ? this : globalThis, () => {
  const ANGLES = Object.freeze({ yes: 20, neutral: 75, no: 130 });
  const LABELS = Object.freeze({ yes: "是", neutral: "中立", no: "否" });
  const PHASES = new Set([
    "idle", "thinking", "windup", "decisive", "holding", "neutral", "failed",
  ]);
  const safeVerdict = (value) => Object.hasOwn(ANGLES, value) ? value : "neutral";
  const safePhase = (value) => PHASES.has(value) ? value : "neutral";
  const defaultSleep = (milliseconds) => new Promise(
    (resolve) => setTimeout(resolve, milliseconds),
  );

  function create({
    render = () => {},
    sleep = defaultSleep,
    windupMilliseconds = 360,
    decisiveMilliseconds = 140,
    yesAngle = ANGLES.yes,
    neutralAngle = ANGLES.neutral,
    noAngle = ANGLES.no,
  } = {}) {
    const angles = { yes: yesAngle, neutral: neutralAngle, no: noAngle };
    let mode = "follow";
    let phase = "idle";
    let verdict = "neutral";
    let angle = angles.neutral;
    let destroyed = false;
    let operation = 0;
    let sessionState = { verdict_phase: "idle", verdict: "neutral" };
    const history = [];

    const snapshot = () => ({ mode, phase, verdict, angle });
    const emit = (event = null) => {
      if (event) history.push(event);
      if (!destroyed) render(snapshot());
    };
    const setDirect = (nextPhase, nextVerdict) => {
      phase = safePhase(nextPhase);
      verdict = safeVerdict(nextVerdict);
      if (verdict === "neutral" || phase === "neutral" || phase === "failed") {
        verdict = "neutral";
      }
      angle = angles[verdict];
      emit();
    };
    const startThinking = () => {
      if (destroyed || mode !== "follow") return;
      operation += 1;
      phase = "thinking";
      verdict = "neutral";
      angle = angles.neutral;
      emit("thinking");
    };
    const showNeutral = (failed = false) => {
      if (destroyed) return;
      operation += 1;
      phase = failed ? "failed" : "neutral";
      verdict = "neutral";
      angle = angles.neutral;
      emit(failed ? "failed-neutral" : "neutral");
    };
    const showVerdict = async (value) => {
      const target = safeVerdict(value);
      if (destroyed) return;
      if (target === "neutral") {
        showNeutral();
        return;
      }
      const current = ++operation;
      const opposite = target === "yes" ? "no" : "yes";
      phase = "windup";
      verdict = opposite;
      angle = angles[opposite];
      emit(`windup-${opposite}`);
      await sleep(windupMilliseconds);
      if (destroyed || current !== operation) return;
      phase = "decisive";
      verdict = target;
      angle = angles[target];
      emit();
      await sleep(decisiveMilliseconds);
      if (destroyed || current !== operation) return;
      phase = "holding";
      emit(`holding-${target}`);
    };
    const applySessionState = async (value = {}) => {
      sessionState = {
        verdict_phase: safePhase(value.verdict_phase),
        verdict: safeVerdict(value.verdict),
      };
      if (mode !== "follow") return;
      if (sessionState.verdict_phase === "thinking") startThinking();
      else if (sessionState.verdict_phase === "holding") {
        await showVerdict(sessionState.verdict);
      } else if (sessionState.verdict_phase === "failed") showNeutral(true);
      else if (sessionState.verdict_phase === "neutral") showNeutral();
      else setDirect(sessionState.verdict_phase, sessionState.verdict);
    };
    const setMode = (value) => {
      if (!new Set(["follow", "preview"]).has(value) || value === mode) return;
      operation += 1;
      mode = value;
      if (mode === "follow") {
        setDirect(sessionState.verdict_phase, sessionState.verdict);
      } else emit();
    };
    const previewVerdict = async (value) => {
      if (mode !== "preview") return;
      await showVerdict(value);
    };
    emit();
    return {
      history,
      startThinking,
      showVerdict,
      showNeutral,
      applySessionState,
      setMode,
      previewVerdict,
      getSnapshot: snapshot,
      destroy: () => { destroyed = true; operation += 1; },
    };
  }

  function bind(root, { createOptions = {} } = {}) {
    if (!root) throw new Error("servo simulator root is required");
    const find = (selector) => {
      const node = root.querySelector(selector);
      if (!node) throw new Error(`servo simulator element missing: ${selector}`);
      return node;
    };
    const stage = find(".servo-stage");
    const follow = find("#servo-mode-follow");
    const preview = find("#servo-mode-preview");
    const output = find("#servo-verdict-output");
    const phaseOutput = find("#servo-phase-output");
    const buttons = [...root.querySelectorAll("[data-servo-verdict]")];
    const listeners = [];
    const listen = (node, type, callback) => {
      node.addEventListener(type, callback);
      listeners.push(() => node.removeEventListener(type, callback));
    };
    const controller = create({
      ...createOptions,
      render(snapshot) {
        stage.style.setProperty("--servo-angle", String(snapshot.angle));
        stage.dataset.phase = snapshot.phase;
        output.textContent = LABELS[snapshot.verdict];
        phaseOutput.textContent = snapshot.phase;
        follow.setAttribute("aria-pressed", String(snapshot.mode === "follow"));
        preview.setAttribute("aria-pressed", String(snapshot.mode === "preview"));
        for (const button of buttons) {
          button.disabled = snapshot.mode !== "preview";
          button.setAttribute("aria-pressed", String(
            snapshot.mode === "preview"
            && button.dataset.servoVerdict === snapshot.verdict
          ));
        }
        createOptions.render?.({ ...snapshot });
      },
    });
    listen(follow, "click", () => controller.setMode("follow"));
    listen(preview, "click", () => controller.setMode("preview"));
    for (const button of buttons) {
      listen(button, "click", () => controller.previewVerdict(
        button.dataset.servoVerdict,
      ));
    }
    const destroy = controller.destroy;
    controller.destroy = () => {
      for (const remove of listeners.splice(0)) remove();
      destroy();
    };
    return controller;
  }

  return { create, bind };
}));
