const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "../../src/showroom_guide/web/device-test.js"),
  "utf8",
);

test("verdict microphone and WAV failures both return the simulator to neutral", () => {
  assert.match(source, /function failVerdictSimulation\(\)/);
  const calls = source.match(/failVerdictSimulation\(\);/g) || [];
  assert.ok(calls.length >= 2);
  assert.match(
    source,
    /function failVerdictSimulation\(\)[\s\S]*servoSimulator\?\.showNeutral\(true\)/,
  );
});
