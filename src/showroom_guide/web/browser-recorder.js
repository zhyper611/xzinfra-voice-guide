(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else if (root) root.ShowroomBrowserRecorder = api;
}(typeof globalThis === "undefined" ? this : globalThis, () => {
  function resample(samples, sourceRate, targetRate) {
    if (!Number.isFinite(sourceRate) || sourceRate <= 0) {
      throw new Error("录音采样率无效");
    }
    if (samples.length === 0) throw new Error("没有录到声音");
    if (sourceRate === targetRate) return Float32Array.from(samples);
    const length = Math.max(1, Math.round(samples.length * targetRate / sourceRate));
    const output = new Float32Array(length);
    const ratio = sourceRate / targetRate;
    for (let index = 0; index < length; index += 1) {
      const position = index * ratio;
      const left = Math.min(samples.length - 1, Math.floor(position));
      const right = Math.min(samples.length - 1, left + 1);
      const weight = position - left;
      output[index] = samples[left] * (1 - weight) + samples[right] * weight;
    }
    return output;
  }

  function encodeWav(samples, sourceRate, targetRate = 16000) {
    const pcm = resample(samples, sourceRate, targetRate);
    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const writeText = (offset, value) => {
      for (let index = 0; index < value.length; index += 1) {
        view.setUint8(offset + index, value.charCodeAt(index));
      }
    };
    writeText(0, "RIFF");
    view.setUint32(4, 36 + pcm.length * 2, true);
    writeText(8, "WAVE");
    writeText(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, targetRate, true);
    view.setUint32(28, targetRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeText(36, "data");
    view.setUint32(40, pcm.length * 2, true);
    for (let index = 0; index < pcm.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, pcm[index]));
      view.setInt16(
        44 + index * 2,
        sample < 0 ? sample * 0x8000 : sample * 0x7fff,
        true,
      );
    }
    if (typeof Buffer !== "undefined") return Buffer.from(buffer);
    return new Uint8Array(buffer);
  }

  function create({
    mediaDevices = typeof navigator !== "undefined" ? navigator.mediaDevices : null,
    AudioContextClass = typeof window !== "undefined"
      ? (window.AudioContext || window.webkitAudioContext)
      : null,
  } = {}) {
    let context = null;
    let stream = null;
    let source = null;
    let processor = null;
    let chunks = [];

    const cleanup = async () => {
      processor?.disconnect();
      source?.disconnect();
      for (const track of stream?.getTracks?.() || []) track.stop();
      if (context && context.state !== "closed") await context.close();
      context = null;
      stream = null;
      source = null;
      processor = null;
    };
    const start = async () => {
      if (context) throw new Error("浏览器麦克风正在录音");
      if (!mediaDevices?.getUserMedia || !AudioContextClass) {
        throw new Error("当前浏览器不支持麦克风录音");
      }
      stream = await mediaDevices.getUserMedia({ audio: true });
      context = new AudioContextClass();
      chunks = [];
      source = context.createMediaStreamSource(stream);
      processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (event) => {
        chunks.push(Float32Array.from(event.inputBuffer.getChannelData(0)));
      };
      source.connect(processor);
      processor.connect(context.destination);
    };
    const stop = async () => {
      if (!context) throw new Error("浏览器麦克风尚未开始录音");
      const sourceRate = context.sampleRate;
      const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
      const samples = new Float32Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        samples.set(chunk, offset);
        offset += chunk.length;
      }
      await cleanup();
      chunks = [];
      return new Blob([encodeWav(samples, sourceRate)], { type: "audio/wav" });
    };
    const cancel = async () => {
      await cleanup();
      chunks = [];
    };
    return {
      start,
      stop,
      cancel,
      get isRecording() { return context !== null; },
    };
  }

  return { encodeWav, create };
}));
