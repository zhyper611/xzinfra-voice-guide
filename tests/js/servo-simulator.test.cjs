const assert = require("node:assert/strict");
const test = require("node:test");
const ServoSimulator = require("../../src/showroom_guide/web/servo-simulator.js");

test("yes result uses a small opposite windup then snaps left", async () => {
  const snapshots = [];
  const simulator = ServoSimulator.create({
    sleep: async () => {},
    render: value => snapshots.push(value),
  });

  simulator.startThinking();
  await simulator.showVerdict("yes");

  const windup = snapshots.findLast(value => value.phase === "windup");
  assert.equal(windup.angle, 95);
  assert.equal(windup.verdict, "neutral");
  assert.deepEqual(simulator.getSnapshot(), {
    mode: "follow",
    phase: "holding",
    verdict: "yes",
    angle: 20,
  });
});

test("motion angles use full thinking range and clamp custom windup", () => {
  assert.deepEqual(ServoSimulator.buildMotionAngles({
    yesAngle: 40,
    neutralAngle: 50,
    noAngle: 60,
  }), {
    thinking: [40, 60],
    windup: { yes: 60, no: 40 },
  });
});

test("windup timing preserves the thinking angular speed", () => {
  const defaults = ServoSimulator.buildMotionTimings();
  assert.ok(Math.abs(defaults.windup.yes - (1600 * 20 / 110)) < 0.001);
  assert.ok(Math.abs(defaults.windup.no - (1600 * 20 / 110)) < 0.001);

  const custom = ServoSimulator.buildMotionTimings({
    yesAngle: 40,
    neutralAngle: 50,
    noAngle: 80,
    thinkingMilliseconds: 1600,
  });
  assert.equal(custom.windup.yes, 800);
  assert.equal(custom.windup.no, 400);
});

test("verdict keeps the decisive timing while using dynamic windup timing", async () => {
  const sleeps = [];
  const simulator = ServoSimulator.create({
    sleep: async milliseconds => sleeps.push(milliseconds),
  });

  await simulator.showVerdict("yes");

  assert.ok(Math.abs(sleeps[0] - (1600 * 20 / 110)) < 0.001);
  assert.equal(sleeps[1], 140);
});

test("binding shares motion timing with CSS", async () => {
  const properties = new Map();
  const makeNode = () => ({
    style: { setProperty: (name, value) => properties.set(name, value) },
    dataset: {},
    setAttribute: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
  });
  const nodes = {
    ".servo-stage": makeNode(),
    "#servo-mode-follow": makeNode(),
    "#servo-mode-preview": makeNode(),
    "#servo-verdict-output": makeNode(),
    "#servo-phase-output": makeNode(),
  };
  const root = {
    querySelector: selector => nodes[selector] ?? null,
    querySelectorAll: () => [],
  };
  const controller = ServoSimulator.bind(root, {
    createOptions: { sleep: async () => {} },
  });

  assert.equal(properties.get("--servo-thinking-duration"), "1600ms");
  await controller.showVerdict("yes");
  const windup = Number.parseFloat(properties.get("--servo-windup-duration"));
  assert.ok(Math.abs(windup - (1600 * 20 / 110)) < 0.001);
});

test("preview mode ignores session updates until follow resumes", async () => {
  const simulator = ServoSimulator.create({ sleep: async () => {} });
  simulator.setMode("preview");
  await simulator.previewVerdict("yes");
  await simulator.applySessionState({ verdict_phase: "holding", verdict: "no" });
  assert.equal(simulator.getSnapshot().verdict, "yes");
  simulator.setMode("follow");
  assert.equal(simulator.getSnapshot().verdict, "no");
});

test("repeated device polling does not replay the same verdict motion", async () => {
  const simulator = ServoSimulator.create({ sleep: async () => {} });
  const state = { verdict_phase: "holding", verdict: "no" };

  await simulator.applySessionState(state);
  const historyLength = simulator.history.length;
  await simulator.applySessionState(state);

  assert.equal(simulator.history.length, historyLength);
  assert.equal(simulator.getSnapshot().verdict, "no");
});

test("unknown session result fails closed to neutral", async () => {
  const simulator = ServoSimulator.create({ sleep: async () => {} });
  await simulator.applySessionState({ verdict_phase: "holding", verdict: "maybe" });
  assert.equal(simulator.getSnapshot().phase, "neutral");
  assert.equal(simulator.getSnapshot().angle, 75);
});

test("destroy stops further rendering", () => {
  const snapshots = [];
  const simulator = ServoSimulator.create({ render: value => snapshots.push(value) });
  simulator.destroy(); simulator.startThinking();
  assert.equal(snapshots.length, 1);
});
