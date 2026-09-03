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

test("preview mode ignores session updates until follow resumes", async () => {
  const simulator = ServoSimulator.create({ sleep: async () => {} });
  simulator.setMode("preview");
  await simulator.previewVerdict("yes");
  await simulator.applySessionState({ verdict_phase: "holding", verdict: "no" });
  assert.equal(simulator.getSnapshot().verdict, "yes");
  simulator.setMode("follow");
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
