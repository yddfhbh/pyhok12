import { spawn } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";

export const DEFAULT_URL = "https://tetr.io/";
export const DEFAULT_PORT = 9222;
export const DEFAULT_PROFILE_DIR = "C:\\pyhok12-cdp-profile";

export function getChromeCandidatePaths(env = process.env) {
  const candidates = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
  ];
  const localAppData = `${env?.LOCALAPPDATA ?? ""}`.trim();
  if (localAppData) {
    candidates.push(path.win32.join(localAppData, "Google", "Chrome", "Application", "chrome.exe"));
  }
  return candidates;
}

export function resolveChromePath(
  chromePath = "",
  { env = process.env, existsSyncImpl = existsSync } = {}
) {
  const explicit = `${chromePath}`.trim();
  if (explicit) {
    return {
      executable: explicit,
      attemptedPaths: [explicit]
    };
  }

  const attemptedPaths = getChromeCandidatePaths(env);
  const executable = attemptedPaths.find((candidate) => existsSyncImpl(candidate)) ?? null;
  return {
    executable,
    attemptedPaths
  };
}

export function buildChromeArgs({
  port = DEFAULT_PORT,
  url = DEFAULT_URL,
  profileDir = DEFAULT_PROFILE_DIR
}) {
  return [
    `--remote-debugging-port=${port}`,
    "--remote-allow-origins=*",
    `--user-data-dir=${profileDir}`,
    url
  ];
}

export async function launchChromium({
  port = DEFAULT_PORT,
  url = DEFAULT_URL,
  chromePath = "",
  profileDir = DEFAULT_PROFILE_DIR,
  spawnImpl = spawn,
  env = process.env,
  existsSyncImpl = existsSync,
  fetchImpl = fetch,
  sleepImpl = sleep,
  timeoutMs = 10000
}) {
  const { executable, attemptedPaths } = resolveChromePath(chromePath, {
    env,
    existsSyncImpl
  });
  if (!executable) {
    throw new Error(`Chrome auto-launch failed. Tried: ${attemptedPaths.join(" | ")}`);
  }
  mkdirSync(profileDir, { recursive: true });

  let browserProcess;
  try {
    browserProcess = spawnImpl(executable, buildChromeArgs({ port, url, profileDir }), {
      detached: true,
      stdio: ["ignore", "ignore", "ignore"]
    });
  } catch (error) {
    throw new Error(
      `Chrome auto-launch failed. Executable: ${executable}. spawn error: ${error?.message ?? String(error)}`
    );
  }

  await waitForSpawn(browserProcess, executable);
  // Keep the debugging browser alive even if the helper process restarts.
  browserProcess.unref();
  try {
    await waitForCdpReady(port, {
      timeoutMs,
      fetchImpl,
      sleepImpl
    });
  } catch (error) {
    throw new Error(
      `Chrome auto-launch failed. Executable: ${executable}. ${error?.message ?? String(error)}`
    );
  }
  return browserProcess;
}

export async function isCdpOpen(port, fetchImpl = fetch) {
  try {
    const response = await fetchImpl(`http://127.0.0.1:${port}/json/version`);
    return response.ok;
  } catch {
    return false;
  }
}

export async function waitForCdpReady(
  port,
  {
    timeoutMs = 10000,
    fetchImpl = fetch,
    sleepImpl = sleep
  } = {}
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isCdpOpen(port, fetchImpl)) {
      return;
    }
    await sleepImpl(250);
  }
  throw new Error(`Chrome DevTools endpoint did not open on port ${port}`);
}

export async function shutdownChromium(browserProcess, { graceMs = 5000, sleepImpl = sleep } = {}) {
  if (!browserProcess?.pid) {
    return false;
  }
  if (browserProcess.exitCode !== null) {
    return true;
  }

  browserProcess.kill();
  const deadline = Date.now() + graceMs;
  while (Date.now() < deadline) {
    if (browserProcess.exitCode !== null) {
      return true;
    }
    await sleepImpl(100);
  }

  try {
    browserProcess.kill("SIGKILL");
  } catch {}

  const hardDeadline = Date.now() + 1000;
  while (Date.now() < hardDeadline) {
    if (browserProcess.exitCode !== null) {
      return true;
    }
    await sleepImpl(50);
  }
  return true;
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

function waitForSpawn(browserProcess, executable) {
  return new Promise((resolve, reject) => {
    if (!browserProcess) {
      reject(new Error(`Chrome auto-launch failed. Executable: ${executable}. spawn error: unknown`));
      return;
    }

    let settled = false;
    const finishResolve = () => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      resolve(browserProcess);
    };
    const finishReject = (message) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      reject(new Error(`Chrome auto-launch failed. Executable: ${executable}. spawn error: ${message}`));
    };
    const onSpawn = () => finishResolve();
    const onError = (error) => finishReject(error?.message ?? String(error));
    const onExit = (code, signal) =>
      finishReject(`process exited before CDP opened (code=${code ?? "null"} signal=${signal ?? "null"})`);
    const cleanup = () => {
      browserProcess.off?.("spawn", onSpawn);
      browserProcess.off?.("error", onError);
      browserProcess.off?.("exit", onExit);
    };

    browserProcess.once?.("spawn", onSpawn);
    browserProcess.once?.("error", onError);
    browserProcess.once?.("exit", onExit);

    if (typeof browserProcess.pid === "number" && browserProcess.pid > 0) {
      queueMicrotask(finishResolve);
    }
  });
}
