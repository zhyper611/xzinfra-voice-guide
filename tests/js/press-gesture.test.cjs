const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const modulePath = path.resolve(
  __dirname,
  "../../src/showroom_guide/web/press-gesture.js",
);
const deviceTestHtmlPath = path.resolve(
  __dirname,
  "../../src/showroom_guide/web/device-test.html",
);
const { createPressGesture } = require(modulePath);

class FakeTimers {
  constructor() {
    this.now = 0;
    this.nextId = 1;
    this.timers = new Map();
    this.setTimer = (callback, delay) => {
      const id = this.nextId++;
      this.timers.set(id, { callback, dueAt: this.now + delay });
      return id;
    };
    this.clearTimer = (id) => {
      this.timers.delete(id);
    };
  }

  advance(milliseconds) {
    const target = this.now + milliseconds;
    while (true) {
      const next = [...this.timers.entries()]
        .filter(([, timer]) => timer.dueAt <= target)
        .sort((left, right) => left[1].dueAt - right[1].dueAt)[0];
      if (!next) break;
      const [id, timer] = next;
      this.now = timer.dueAt;
      this.timers.delete(id);
      timer.callback();
    }
    this.now = target;
  }

  get pendingCount() {
    return this.timers.size;
  }
}

function createHarness() {
  const timers = new FakeTimers();
  const events = [];
  const gesture = createPressGesture({
    thresholdMs: 500,
    onShortPress: () => events.push("short"),
    onLongPress: () => events.push("long"),
    onHoldStart: () => events.push("hold-start"),
    onHoldEnd: () => events.push("hold-end"),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  return { events, gesture, timers };
}

test("ending before the threshold emits one short press and clears the timer", () => {
  const { events, gesture, timers } = createHarness();

  gesture.start();
  timers.advance(499);
  gesture.end();
  gesture.end();
  timers.advance(1);

  assert.deepEqual(events, ["hold-start", "hold-end", "short"]);
  assert.equal(timers.pendingCount, 0);
});

test("reaching the threshold emits one long press and ending does not emit short", () => {
  const { events, gesture, timers } = createHarness();

  gesture.start();
  timers.advance(500);
  timers.advance(500);
  gesture.end();

  assert.deepEqual(events, ["hold-start", "long", "hold-end"]);
  assert.equal(timers.pendingCount, 0);
});

test("cancel stops an active press without emitting short or long", () => {
  const { events, gesture, timers } = createHarness();

  gesture.start();
  gesture.cancel();
  gesture.cancel();
  timers.advance(500);

  assert.deepEqual(events, ["hold-start", "hold-end"]);
  assert.equal(timers.pendingCount, 0);
});

test("duplicate start and end calls do not duplicate events or timers", () => {
  const { events, gesture, timers } = createHarness();

  gesture.start();
  gesture.start();
  assert.equal(timers.pendingCount, 1);
  gesture.end();
  gesture.end();

  gesture.start();
  gesture.end();

  assert.deepEqual(events, [
    "hold-start",
    "hold-end",
    "short",
    "hold-start",
    "hold-end",
    "short",
  ]);
  assert.equal(timers.pendingCount, 0);
});

test("disabling during a press cancels once and blocks presses until re-enabled", () => {
  const { events, gesture, timers } = createHarness();

  gesture.start();
  gesture.setDisabled(true);
  gesture.setDisabled(true);
  gesture.end();
  gesture.start();
  timers.advance(500);

  assert.deepEqual(events, ["hold-start", "hold-end"]);
  assert.equal(timers.pendingCount, 0);

  gesture.setDisabled(false);
  gesture.start();
  gesture.end();

  assert.deepEqual(events, ["hold-start", "hold-end", "hold-start", "hold-end", "short"]);
});

test("hold feedback callbacks are optional", () => {
  const timers = new FakeTimers();
  let shortPresses = 0;
  const gesture = createPressGesture({
    thresholdMs: 500,
    onShortPress: () => { shortPresses += 1; },
    onLongPress: () => {},
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  gesture.start();
  gesture.end();

  assert.equal(shortPresses, 1);
  assert.equal(timers.pendingCount, 0);
});

test("start rolls back when setTimer throws and can be retried", () => {
  const timers = new FakeTimers();
  const timerError = new Error("timer unavailable");
  let timerAttempts = 0;
  let shortPresses = 0;
  const gesture = createPressGesture({
    thresholdMs: 500,
    onShortPress: () => { shortPresses += 1; },
    onLongPress: () => {},
    setTimer: (callback, delay) => {
      timerAttempts += 1;
      if (timerAttempts === 1) throw timerError;
      return timers.setTimer(callback, delay);
    },
    clearTimer: timers.clearTimer,
  });

  assert.throws(() => gesture.start(), (error) => error === timerError);
  assert.equal(timers.pendingCount, 0);

  gesture.start();
  assert.equal(timerAttempts, 2);
  assert.equal(timers.pendingCount, 1);
  gesture.end();

  assert.equal(shortPresses, 1);
  assert.equal(timers.pendingCount, 0);
});

test("start clears its timer when onHoldStart throws and can be retried", () => {
  const timers = new FakeTimers();
  const feedbackError = new Error("feedback unavailable");
  let feedbackAttempts = 0;
  let shortPresses = 0;
  const gesture = createPressGesture({
    thresholdMs: 500,
    onShortPress: () => { shortPresses += 1; },
    onLongPress: () => {},
    onHoldStart: () => {
      feedbackAttempts += 1;
      if (feedbackAttempts === 1) throw feedbackError;
    },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  assert.throws(() => gesture.start(), (error) => error === feedbackError);
  assert.equal(timers.pendingCount, 0);

  gesture.start();
  gesture.end();

  assert.equal(shortPresses, 1);
  assert.equal(timers.pendingCount, 0);
});

test("CommonJS require exports the factory without writing to globalThis", () => {
  delete globalThis.ShowroomPressGesture;
  delete require.cache[modulePath];

  const commonJsApi = require(modulePath);

  assert.equal(typeof commonJsApi.createPressGesture, "function");
  assert.equal(globalThis.ShowroomPressGesture, undefined);
});

test("browser script exposes the same factory through ShowroomPressGesture", () => {
  const context = {};
  vm.runInNewContext(readFileSync(modulePath, "utf8"), context);

  assert.equal(typeof context.ShowroomPressGesture.createPressGesture, "function");
});

test("browser script exports when module is null", () => {
  const context = { module: null };

  vm.runInNewContext(readFileSync(modulePath, "utf8"), context);

  assert.equal(typeof context.ShowroomPressGesture.createPressGesture, "function");
});

test("device test page defers press gesture before its application script", () => {
  const html = readFileSync(deviceTestHtmlPath, "utf8");
  const pressGestureIndex = html.indexOf('src="/static/press-gesture.js');
  const deviceTestIndex = html.indexOf('src="/static/device-test.js');
  const pressGestureTag = html.match(
    /<script\s+[^>]*src="\/static\/press-gesture\.js[^>]*><\/script>/,
  );

  assert.notEqual(pressGestureIndex, -1);
  assert.notEqual(deviceTestIndex, -1);
  assert.ok(pressGestureIndex < deviceTestIndex);
  assert.match(pressGestureTag[0], /\bdefer\b/);
});
