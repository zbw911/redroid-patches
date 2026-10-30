import { createWriteStream } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { finished } from 'node:stream/promises';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
if (!path.isAbsolute(config.providerModule) || !path.isAbsolute(config.outputDirectory)
    || !Array.isArray(config.displays) || !config.displays.length) {
  throw new Error('Expected absolute providerModule/outputDirectory and nonempty displays');
}
const keys = new Set(), displayIds = new Set();
for (const display of config.displays) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(display.key) || keys.has(display.key)
      || !Number.isSafeInteger(display.displayId) || display.displayId < 1 || displayIds.has(display.displayId)
      || !/^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$/.test(display.packageName)) {
    throw new Error('Each display requires a unique safe key, positive displayId and packageName');
  }
  keys.add(display.key); displayIds.add(display.displayId);
}
const { AndroidStream } = await import(pathToFileURL(config.providerModule).href);
await mkdir(config.outputDirectory, { recursive: true });
const startedAt = new Date().toISOString();
const active = [];
const errors = [];
let commonAliveStartedAt = null, commonAliveEndedAt = null, closePromise;
let stopRequested = false;
let stopResolve;
const stopped = new Promise(resolve => { stopResolve = resolve; });
const emit = value => process.stdout.write(`${JSON.stringify(value)}\n`);
const errorRecord = (error, stage) => ({
  at: new Date().toISOString(), stage, code: error.code || 'STREAM_PROBE_ERROR', message: error.message || String(error),
});
const snapshot = () => ({
  at: new Date().toISOString(), pid: process.pid, startedAt,
  commonAliveStartedAt, commonAliveEndedAt,
  streams: active.map(({ record, provider }) => ({ ...record, closed: provider.closed })),
  errors,
});

async function closeAll(reason) {
  if (closePromise) return closePromise;
  stopRequested = true;
  stopResolve();
  commonAliveEndedAt = new Date().toISOString();
  closePromise = (async () => {
    for (const item of active) {
      item.record.commonWindowEnd = { packets: item.record.packets, framePackets: item.record.framePackets, bytes: item.record.bytes };
    }
    for (const { provider, record, output } of active) {
      try { await provider.close(); }
      catch (error) { record.errors.push(errorRecord(error, 'close')); }
      output.end();
      try { await finished(output); }
      catch (error) { record.errors.push(errorRecord(error, 'recording-close')); }
    }
    const result = {
      ...snapshot(), endedAt: new Date().toISOString(), closeReason: reason,
      config: {
        providerModule: config.providerModule, adb: config.adb, serial: config.serial,
        adbServerSocket: config.adbServerSocket, scrcpyServer: config.scrcpyServer,
        fps: config.fps, maxSize: config.maxSize, bitrateKbps: config.bitrateKbps,
        displays: config.displays,
      },
    };
    await writeFile(path.join(config.outputDirectory, 'streams.json'), `${JSON.stringify(result, null, 2)}\n`);
    if (errors.length || active.some(item => item.record.errors.length)) process.exitCode = 1;
    emit({ event: 'closed', ...result });
    input.close();
    process.stdin.pause();
  })();
  return closePromise;
}

const input = createInterface({ input: process.stdin });
input.on('line', line => {
  try {
    const command = JSON.parse(line);
    if (command.action === 'status') emit({ event: 'status', ...snapshot() });
    else if (command.action === 'close') void closeAll('close').catch(fatal);
    else throw new Error('Expected action status or close');
  } catch (error) { emit({ event: 'error', ...errorRecord(error, 'command') }); }
});
input.on('close', () => { void closeAll('eof').catch(fatal); });
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.once(signal, () => { void closeAll(signal).catch(fatal); });
}
function fatal(error) {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
}

try {
  for (const display of config.displays) {
    if (stopRequested) break;
    const record = {
      ...display, file: path.join(config.outputDirectory, `${display.key}.h264`),
      ready: null, packets: 0, framePackets: 0, bytes: 0,
      firstPacketAt: null, lastPacketAt: null, firstFrameAt: null, lastFrameAt: null,
      firstPts: null, lastPts: null, errors: [],
    };
    const output = createWriteStream(record.file, { flags: 'wx' });
    let firstFrameResolve;
    const firstFrame = new Promise(resolve => { firstFrameResolve = resolve; });
    const provider = new AndroidStream({
      adb: config.adb, serial: config.serial, adbServerSocket: config.adbServerSocket,
      displayId: display.displayId, launchComponent: `${display.packageName}/probe.StatusBarProbe`,
    }, {
      scrcpyServer: config.scrcpyServer, fps: config.fps, maxSize: config.maxSize, bitrateKbps: config.bitrateKbps,
    }, {
      ready: metadata => { record.ready = metadata; },
      packet: (data, flags, pts) => {
        const now = new Date().toISOString();
        record.packets++; record.bytes += data.length;
        record.firstPacketAt ??= now; record.lastPacketAt = now;
        if (pts !== undefined && pts !== null) {
          record.firstPts ??= String(pts); record.lastPts = String(pts);
        }
        if (!(flags & 2)) {
          record.framePackets++;
          record.firstFrameAt ??= now; record.lastFrameAt = now;
          if (record.ready) firstFrameResolve();
        }
        output.write(data);
      },
      error: error => { record.errors.push(errorRecord(error, 'stream')); firstFrameResolve(); },
    });
    output.on('error', error => { record.errors.push(errorRecord(error, 'recording')); firstFrameResolve(); });
    active.push({ provider, record, output, firstFrame });
    provider.startTask = provider.start();
    await provider.startTask;
    record.adbClientPid = provider.child?.pid ?? null;
  }
  if (!stopRequested) {
    let timer;
    try {
      await Promise.race([
        Promise.all(active.map(item => item.firstFrame)), stopped,
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Timed out waiting for all first video frames')), 30000); }),
      ]);
    } finally { clearTimeout(timer); }
    if (!stopRequested) {
      if (active.some(item => item.record.errors.length)) throw new Error('A stream failed before simultaneous readiness');
      commonAliveStartedAt = new Date().toISOString();
      for (const { record } of active) {
        record.commonWindowStart = { packets: record.packets, framePackets: record.framePackets, bytes: record.bytes };
      }
      emit({ event: 'ready', ...snapshot() });
    }
  }
} catch (error) {
  errors.push(errorRecord(error, 'startup'));
  emit({ event: 'error', ...errorRecord(error, 'startup') });
  await closeAll('startup-error');
}
