import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import {
  DEFAULT_PROFILE_DIR,
  buildChromeArgs,
  getChromeCandidatePaths,
  launchChromium,
  resolveChromePath,
  waitForCdpReady
} from "../browser-source/chromium-launch.mjs";

class FakeChildProcess extends EventEmitter {
  constructor(pid = 4321) {
    super();
    this.pid = pid;
    this.exitCode = null;
    this.killed = false;
    this.unrefCalled = false;
  }

  unref() {
    this.unrefCalled = true;
  }

  kill() {
    this.killed = true;
    this.exitCode = 0;
    this.emit("exit", 0, null);
  }
}

test("getChromeCandidatePaths returns the required Windows search order", () => {
  assert.deepEqual(getChromeCandidatePaths({
    LOCALAPPDATA: "C:\\Users\\MSI\\AppData\\Local"
  }), [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Users\\MSI\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"
  ]);
});

test("resolveChromePath picks the first existing Chrome executable", () => {
  const resolved = resolveChromePath("", {
    env: {
      LOCALAPPDATA: "C:\\Users\\MSI\\AppData\\Local"
    },
    existsSyncImpl: (candidate) =>
      candidate === "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
  });

  assert.equal(
    resolved.executable,
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
  );
  assert.equal(resolved.attemptedPaths.length, 3);
});

test("buildChromeArgs includes the required CDP arguments", () => {
  assert.deepEqual(buildChromeArgs({}), [
    "--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    `--user-data-dir=${DEFAULT_PROFILE_DIR}`,
    "https://tetr.io/"
  ]);
});

test("waitForCdpReady polls /json/version until Chrome responds", async () => {
  const calls = [];
  await waitForCdpReady(9222, {
    fetchImpl: async (url) => {
      calls.push(url);
      return {
        ok: calls.length >= 3
      };
    },
    sleepImpl: async () => undefined,
    timeoutMs: 10000
  });

  assert.equal(calls[0], "http://127.0.0.1:9222/json/version");
  assert.equal(calls.length, 3);
});

test("launchChromium starts Chrome with the required arguments and waits for CDP", async () => {
  const child = new FakeChildProcess();
  const spawnCalls = [];
  let fetchCalls = 0;

  const launched = await launchChromium({
    port: 9222,
    url: "https://tetr.io/",
    env: {
      LOCALAPPDATA: "C:\\Users\\MSI\\AppData\\Local"
    },
    existsSyncImpl: (candidate) =>
      candidate === "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    spawnImpl: (command, args, options) => {
      spawnCalls.push({ command, args, options });
      return child;
    },
    fetchImpl: async () => {
      fetchCalls += 1;
      return { ok: fetchCalls >= 2 };
    },
    sleepImpl: async () => undefined,
    timeoutMs: 10000
  });

  assert.equal(launched, child);
  assert.equal(child.unrefCalled, true);
  assert.equal(spawnCalls.length, 1);
  assert.equal(
    spawnCalls[0].command,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
  );
  assert.deepEqual(spawnCalls[0].args, [
    "--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    `--user-data-dir=${DEFAULT_PROFILE_DIR}`,
    "https://tetr.io/"
  ]);
  assert.equal(spawnCalls[0].options.detached, true);
  assert.equal(fetchCalls, 2);
});

test("launchChromium reports attempted paths when Chrome is missing", async () => {
  await assert.rejects(
    () =>
      launchChromium({
        env: {
          LOCALAPPDATA: "C:\\Users\\MSI\\AppData\\Local"
        },
        existsSyncImpl: () => false,
        fetchImpl: async () => ({ ok: false }),
        sleepImpl: async () => undefined
      }),
    /Tried: C:\\Program Files\\Google\\Chrome\\Application\\chrome\.exe \| C:\\Program Files \(x86\)\\Google\\Chrome\\Application\\chrome\.exe \| C:\\Users\\MSI\\AppData\\Local\\Google\\Chrome\\Application\\chrome\.exe/
  );
});

test("launchChromium reports the executable path and spawn error", async () => {
  await assert.rejects(
    () =>
      launchChromium({
        chromePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        spawnImpl: () => {
          throw new Error("access denied");
        },
        fetchImpl: async () => ({ ok: false }),
        sleepImpl: async () => undefined
      }),
    /Executable: C:\\Program Files\\Google\\Chrome\\Application\\chrome\.exe\. spawn error: access denied/
  );
});
