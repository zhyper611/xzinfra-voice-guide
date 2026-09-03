const assert = require("node:assert/strict");
const test = require("node:test");
const ServoSimulator = require("../../src/showroom_guide/web/servo-simulator.js");

test("yes result winds up right, snaps left, and stays left", async () => {
  const simulator = ServoSimulator.create({ sleep: async () => {} });

  simulator.startThinking();
  await simulator.showVerdict("yes");

  assert.deepEqual(simulator.history.slice(-2), ["windup-no", "holding-yes"]);
  assert.deepEqual(simulator.getSnapshot(), {
    mode: "follow",
    phase: "holding",
    verdict: "yes",
    angle: 20,
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
