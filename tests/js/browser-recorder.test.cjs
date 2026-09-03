const assert = require("node:assert/strict");
const test = require("node:test");
const Recorder = require("../../src/showroom_guide/web/browser-recorder.js");

test("encodes browser samples as 16 kHz mono 16-bit PCM WAV", () => {
  const source = Float32Array.from([0, 0.5, -0.5, 1, -1, 0.25, -0.25, 0]);

  const wav = Recorder.encodeWav(source, 32000, 16000);

  assert.equal(wav.toString("ascii", 0, 4), "RIFF");
  assert.equal(wav.toString("ascii", 8, 12), "WAVE");
  assert.equal(wav.readUInt16LE(22), 1);
  assert.equal(wav.readUInt32LE(24), 16000);
  assert.equal(wav.readUInt16LE(34), 16);
  assert.equal(wav.readUInt32LE(40), 8);
});

test("empty browser recording is rejected", () => {
  assert.throws(
    () => Recorder.encodeWav(new Float32Array(), 48000, 16000),
    /没有录到声音/,
  );
});
