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

  function buildMotionAngles({
    yesAngle = ANGLES.yes,
    neutralAngle = ANGLES.neutral,
    noAngle = ANGLES.no,
    windupOffset = 20,
  } = {}) {
    const toward = target => neutralAngle + Math.sign(target - neutralAngle)
      * Math.min(Math.abs(target - neutralAngle), windupOffset);
    return {
      thinking: [yesAngle, noAngle],
      windup: { yes: toward(noAngle), no: toward(yesAngle) },
    };
  }

  function buildMotionTimings({
    yesAngle = ANGLES.yes,
    neutralAngle = ANGLES.neutral,
    noAngle = ANGLES.no,
    windupOffset = 20,
    thinkingMilliseconds = 1600,
  } = {}) {
    const motionAngles = buildMotionAngles({
      yesAngle,
      neutralAngle,
      noAngle,
      windupOffset,
    });
    const thinkingDistance = Math.abs(noAngle - yesAngle);
    const windupDuration = angle => thinkingDistance === 0
      ? 0
      : thinkingMilliseconds * Math.abs(angle - neutralAngle) / thinkingDistance;
    return {
      thinking: thinkingMilliseconds,
      windup: {
        yes: windupDuration(motionAngles.windup.yes),
        no: windupDuration(motionAngles.windup.no),
      },
    };
  }

  function angleToRotation(angle) {
    return -135 + ((angle - ANGLES.yes) * 90 / (ANGLES.no - ANGLES.yes));
  }

  function create({
    render = () => {},
    sleep = defaultSleep,
    windupMilliseconds = null,
    thinkingMilliseconds = 1600,
    decisiveMilliseconds = 140,
    yesAngle = ANGLES.yes,
    neutralAngle = ANGLES.neutral,
    noAngle = ANGLES.no,
  } = {}) {
    const angles = { yes: yesAngle, neutral: neutralAngle, no: noAngle };
    const motionAngles = buildMotionAngles({
      yesAngle,
      neutralAngle,
      noAngle,
    });
    const motionTimings = buildMotionTimings({
      yesAngle,
      neutralAngle,
      noAngle,
      thinkingMilliseconds,
    });
    const windupTimings = windupMilliseconds === null
      ? motionTimings.windup
      : { yes: windupMilliseconds, no: windupMilliseconds };
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
      phase = "windup";
      verdict = "neutral";
      angle = motionAngles.windup[target];
      emit("windup");
      await sleep(windupTimings[target]);
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
      const nextSessionState = {
        verdict_phase: safePhase(value.verdict_phase),
        verdict: safeVerdict(value.verdict),
      };
      const unchanged = nextSessionState.verdict_phase === sessionState.verdict_phase
        && nextSessionState.verdict === sessionState.verdict;
      sessionState = nextSessionState;
      if (unchanged) return;
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
    const motionAngles = buildMotionAngles(createOptions);
    const motionTimings = buildMotionTimings(createOptions);
    const windupTimings = createOptions.windupMilliseconds == null
      ? motionTimings.windup
      : {
          yes: createOptions.windupMilliseconds,
          no: createOptions.windupMilliseconds,
        };
    stage.style.setProperty(
      "--servo-thinking-yes-rotation",
      `${angleToRotation(motionAngles.thinking[0])}deg`,
    );
    stage.style.setProperty(
      "--servo-thinking-no-rotation",
      `${angleToRotation(motionAngles.thinking[1])}deg`,
    );
    stage.style.setProperty(
      "--servo-thinking-duration",
      `${motionTimings.thinking}ms`,
    );
    const listeners = [];
    const listen = (node, type, callback) => {
      node.addEventListener(type, callback);
      listeners.push(() => node.removeEventListener(type, callback));
    };
    const controller = create({
      ...createOptions,
      render(snapshot) {
        if (snapshot.phase === "windup") {
          const target = snapshot.angle === motionAngles.windup.yes ? "yes" : "no";
          stage.style.setProperty(
            "--servo-windup-duration",
            `${windupTimings[target]}ms`,
          );
        }
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

  return { create, bind, buildMotionAngles, buildMotionTimings };
}));
