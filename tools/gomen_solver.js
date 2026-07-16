import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function loadRuntime() {
  const scriptPath = path.join(__dirname, "gomen.js");
  const wasmPath = path.join(__dirname, "gomen_bg.wasm");
  const legalBoardsPath = path.join(__dirname, "legal-boards.leb128");

  const source = fs.readFileSync(scriptPath, "utf8") + "\n;globalThis.__wb = wasm_bindgen;";
  const context = {
    console,
    TextEncoder,
    TextDecoder,
    URL,
    Uint8Array,
    ArrayBuffer,
    WebAssembly,
    Response,
    Request,
    Headers,
    fetch,
    setTimeout,
    clearTimeout,
    globalThis: null,
    progress: () => {},
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context);

  context.__wb.initSync(fs.readFileSync(wasmPath));
  const legalBoards = fs.readFileSync(legalBoardsPath);
  const solver = new context.__wb.Solver(new Uint8Array(legalBoards));

  return {
    wasmBindgen: context.__wb,
    solver,
  };
}

function buildQueue(wasmBindgen, queueText) {
  const queue = new wasmBindgen.Queue();
  const bag = /[ILJOSTZ]|\[([ILJOSTZ]+)\](\d*)|(\*)(\d*)/g;

  for (const match of queueText.matchAll(bag)) {
    if (match[1]) {
      const count = parseInt(match[2], 10) || 1;
      queue.add_bag(match[1], count);
    } else if (match[3]) {
      const count = parseInt(match[4], 10) || 1;
      queue.add_bag("IJLOSTZ", count);
    } else {
      queue.add_shape(match[0]);
    }
  }

  return queue;
}

function solve(runtime, request) {
  const queueText = String(request.queue || "").trim().toUpperCase();
  const targetQueue = String(request.target_queue || "").trim().toUpperCase();
  const garbage = BigInt(String(request.garbage || "0"));
  const hold = request.hold !== false;
  const physics = String(request.physics || "SRS");
  const limit = Math.max(1, Math.min(12, Number(request.limit || 6)));

  if (!queueText) {
    throw new Error("queue is required");
  }

  const queue = buildQueue(runtime.wasmBindgen, queueText);
  const startedAt = Date.now();
  let raw = runtime.solver.solve(queue, garbage, hold, physics).split(",");
  if (raw.length === 1 && raw[0] === "") {
    raw = [];
  }

  const matchedSolutions = [];
  const fallbackSolutions = [];
  for (const entry of raw) {
    const [cells, id] = entry.split("|");
    let orderGroups = [];
    let matchedGroup = "";

    if (id) {
      const info = runtime.wasmBindgen.solution_info(id);
      const parts = String(info || "").split("|");
      orderGroups = parts
        .slice(1)
        .map((group) => String(group || ""))
        .map((group) => group.split(",").map((item) => item.trim()).filter(Boolean))
        .filter((group) => group.length > 0);

      if (targetQueue) {
        if (orderGroups[0] && orderGroups[0].includes(targetQueue)) {
          matchedGroup = "without_hold";
        } else if (orderGroups[1] && orderGroups[1].includes(targetQueue)) {
          matchedGroup = "with_hold";
        }
      }
    }

    const solution = {
      cells,
      id: id || "",
      order_groups: orderGroups,
      matched_group: matchedGroup,
    };

    if (fallbackSolutions.length < limit) {
      fallbackSolutions.push(solution);
    }

    if (matchedGroup) {
      matchedSolutions.push(solution);
    }

    if (fallbackSolutions.length >= limit && (!targetQueue || matchedSolutions.length >= limit)) {
      break;
    }
  }

  const solutions = targetQueue
    ? (matchedSolutions.length ? matchedSolutions.slice(0, limit) : fallbackSolutions.slice(0, limit))
    : fallbackSolutions.slice(0, limit);

  return {
    ok: true,
    fast: !!runtime.solver.is_fast(garbage),
    duration_ms: Date.now() - startedAt,
    total: raw.length,
    matched_total: matchedSolutions.length,
    shown_total: solutions.length,
    exact_match_used: !targetQueue || matchedSolutions.length > 0,
    solutions,
  };
}

function main() {
  const runtime = loadRuntime();
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  process.stdout.write(`${JSON.stringify({ kind: "ready" })}\n`);

  rl.on("line", (line) => {
    const text = String(line || "").trim();
    if (!text) {
      return;
    }

    try {
      const request = JSON.parse(text);
      const result = solve(runtime, request);
      process.stdout.write(`${JSON.stringify(result)}\n`);
    } catch (error) {
      process.stdout.write(
        `${JSON.stringify({
          ok: false,
          error: error && error.message ? error.message : String(error),
        })}\n`
      );
    }
  });
}

main();
