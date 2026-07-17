import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";

import {
  acceptPieceCounterSource,
  buildSoloSpawnSignature,
  buildTombstoneSnapshot,
  captureTetrioGame,
  countOccupiedFieldCells,
  createSessionState,
  exposeTetrioGameFromPausedCallFrames,
  markCurrentGameAsEnded,
  normalizeSoloPieceValue,
  pausedFrameExposureExpression,
  readTetrioState,
  resolveSoloDerivedPlacedPieces,
  resetSessionState,
  resolveSoloStateRevision,
  selectSoloLinesClearedCandidate,
  selectSoloPieceCounterCandidate,
  shouldAdvanceGameEpoch,
  shouldHandleEndedGame,
  updateSessionState
} from "../browser-source/tetrio-cdp-source.mjs";

test("updateSessionState keeps one session until identity changes", () => {
  const sessionState = createSessionState();

  const first = updateSessionState(sessionState, {
    playing: true,
    roundId: "round-1",
    gameId: "game-1",
    gameObjectId: "object-1"
  });
  const second = updateSessionState(sessionState, {
    playing: true,
    roundId: "round-1",
    gameId: "game-1",
    gameObjectId: "object-1"
  });
  const third = updateSessionState(sessionState, {
    playing: true,
    roundId: "round-2",
    gameId: "game-1",
    gameObjectId: "object-1"
  });

  assert.equal(first, "session-1");
  assert.equal(second, "session-1");
  assert.equal(third, "session-2");
});

test("Retry after ended reset gets a new session id", () => {
  const sessionState = createSessionState();

  const first = updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });
  resetSessionState(sessionState);
  const second = updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });

  assert.equal(first, "session-1");
  assert.equal(second, "session-2");
});

test("leaving lobby and re-entering gets a new session id", () => {
  const sessionState = createSessionState();

  const first = updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });
  const inactive = updateSessionState(sessionState, { playing: false });
  const second = updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-2",
    gameObjectId: "object-2"
  });

  assert.equal(first, "session-1");
  assert.equal(inactive, null);
  assert.equal(second, "session-2");
});

test("acceptPieceCounterSource rejects source changes inside one session", () => {
  const sessionState = createSessionState();
  updateSessionState(sessionState, {
    playing: true,
    roundId: "round-1",
    gameId: "game-1",
    gameObjectId: "object-1"
  });

  assert.equal(acceptPieceCounterSource(sessionState, "stats.piecesPlaced"), true);
  assert.equal(acceptPieceCounterSource(sessionState, "stats.piecesPlaced"), true);
  assert.equal(acceptPieceCounterSource(sessionState, "pieces"), false);
});

test("buildTombstoneSnapshot marks the snapshot inactive", () => {
  const snapshot = buildTombstoneSnapshot({
    source: "vs_object",
    mode: "VS",
    reason: "VS round inactive",
    capturedAt: 123
  });

  assert.deepEqual(snapshot, {
    ok: false,
    source: "vs_object",
    mode: "VS",
    reason: "VS round inactive",
    ready: false,
    playing: false,
    sessionId: null,
    gameId: null,
    roundId: null,
    board: null,
    current: null,
    hold: null,
    queue: [],
    pieceCounter: null,
    pieceCounterSource: null,
    stateRevision: null,
    token: null,
    capturedAt: 123
  });
});

test("normalizeSoloPieceValue handles string, object, and nested piece shapes", () => {
  assert.equal(normalizeSoloPieceValue("T"), "T");
  assert.equal(normalizeSoloPieceValue({ type: "t" }), "T");
  assert.equal(normalizeSoloPieceValue({ active: { piece: { name: "tetromino_l" } } }), "L");
});

test("selectSoloPieceCounterCandidate keeps zero and derived revision fallback", () => {
  assert.deepEqual(
    selectSoloPieceCounterCandidate({
      state: {
        stats: {
          pieces: 0
        }
      }
    }),
    {
      value: 0,
      source: "state.stats.pieces"
    }
  );
  assert.deepEqual(selectSoloPieceCounterCandidate({}), {
    value: null,
    source: "derived-revision"
  });
});

test("selectSoloLinesClearedCandidate keeps zero and checks solo sources", () => {
  assert.deepEqual(
    selectSoloLinesClearedCandidate({
      state: {
        lines: 0
      }
    }),
    {
      value: 0,
      source: "state.lines"
    }
  );
  assert.equal(countOccupiedFieldCells([[true, false], [true, true]]), 3);
});

test("resolveSoloStateRevision does not increase on repeated polls but increments on spawn changes", () => {
  const sessionState = createSessionState();
  updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });

  const baseState = {
    field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
    current: "T",
    hold: "I",
    queue: ["L", "S", "O", "Z", "J"],
    pieceCounter: null,
    pieceCounterSource: "derived-revision"
  };

  const first = resolveSoloStateRevision(sessionState, baseState);
  const second = resolveSoloStateRevision(sessionState, {
    ...baseState,
    activeX: 4,
    activeRotation: "east"
  });
  const third = resolveSoloStateRevision(sessionState, {
    ...baseState,
    current: "L"
  });

  assert.equal(first, 0);
  assert.equal(second, 0);
  assert.equal(third, 1);
});

test("resolveSoloDerivedPlacedPieces sets zero only for first empty new-session snapshot", () => {
  const sessionState = createSessionState();
  updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });
  const state = {
    field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
    current: "I",
    hold: null,
    queue: ["T", "L", "S", "O", "Z"],
    pieceCounter: null,
    pieceCounterSource: "derived-revision",
    linesCleared: undefined
  };

  assert.equal(resolveSoloDerivedPlacedPieces(sessionState, state, { stateRevision: 0 }), 0);

  const midSession = createSessionState();
  updateSessionState(midSession, {
    playing: true,
    gameId: "solo-2",
    gameObjectId: "object-2"
  });
  assert.equal(resolveSoloDerivedPlacedPieces(midSession, state, { stateRevision: 2 }), null);
});

test("resolveSoloDerivedPlacedPieces does not advance on hold-only changes", () => {
  const sessionState = createSessionState();
  updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });
  const firstState = {
    field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
    current: "I",
    hold: null,
    queue: ["T", "L", "S", "O", "Z"],
    pieceCounter: null,
    pieceCounterSource: "derived-revision",
    linesCleared: undefined
  };
  resolveSoloDerivedPlacedPieces(sessionState, firstState, { stateRevision: 0 });

  const holdSwapState = {
    ...firstState,
    current: "O",
    hold: "I"
  };
  assert.equal(resolveSoloDerivedPlacedPieces(sessionState, holdSwapState, { stateRevision: 1 }), 0);
});

test("resolveSoloDerivedPlacedPieces advances once on queue shift and does not double-count repeats", () => {
  const sessionState = createSessionState();
  updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });
  const emptyField = Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false));
  resolveSoloDerivedPlacedPieces(
    sessionState,
    {
      field: emptyField,
      current: "I",
      hold: null,
      queue: ["T", "L", "S", "O", "Z"],
      pieceCounter: null,
      pieceCounterSource: "derived-revision",
      linesCleared: undefined
    },
    { stateRevision: 0 }
  );

  const secondField = emptyField.map((row) => [...row]);
  secondField[0][0] = true;
  secondField[0][1] = true;
  secondField[0][2] = true;
  secondField[0][3] = true;
  const advanced = {
    field: secondField,
    current: "T",
    hold: null,
    queue: ["L", "S", "O", "Z", "J"],
    pieceCounter: null,
    pieceCounterSource: "derived-revision",
    linesCleared: undefined
  };
  assert.equal(resolveSoloDerivedPlacedPieces(sessionState, advanced, { stateRevision: 1 }), 1);
  assert.equal(resolveSoloDerivedPlacedPieces(sessionState, advanced, { stateRevision: 1 }), 1);
});

test("resolveSoloDerivedPlacedPieces resets after new session", () => {
  const sessionState = createSessionState();
  updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-1",
    gameObjectId: "object-1"
  });
  resolveSoloDerivedPlacedPieces(
    sessionState,
    {
      field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
      current: "I",
      hold: null,
      queue: ["T", "L", "S", "O", "Z"],
      pieceCounter: null,
      pieceCounterSource: "derived-revision",
      linesCleared: undefined
    },
    { stateRevision: 0 }
  );
  resetSessionState(sessionState);
  updateSessionState(sessionState, {
    playing: true,
    gameId: "solo-2",
    gameObjectId: "object-2"
  });
  assert.equal(
    resolveSoloDerivedPlacedPieces(
      sessionState,
      {
        field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
        current: "J",
        hold: null,
        queue: ["I", "T", "L", "S", "O"],
        pieceCounter: null,
        pieceCounterSource: "derived-revision",
        linesCleared: undefined
      },
      { stateRevision: 0 }
    ),
    0
  );
});

test("buildSoloSpawnSignature is stable across movement-only changes", () => {
  const signatureA = buildSoloSpawnSignature({
    field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
    current: "T",
    hold: "I",
    queue: ["L", "S", "O", "Z", "J"]
  });
  const signatureB = buildSoloSpawnSignature({
    field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
    current: { type: "t" },
    hold: "I",
    queue: ["L", "S", "O", "Z", "J"],
    activeX: 7,
    activeY: 18
  });

  assert.equal(signatureA, signatureB);
});

test("ended-game helpers use botbot-style transition predicates", () => {
  const endedState = {
    ok: true,
    ready: false,
    reason: "TETR.IO game ended"
  };
  const activeState = {
    ok: true,
    ready: true,
    playing: true,
    countdown: false
  };

  assert.equal(shouldHandleEndedGame(endedState, false), true);
  assert.equal(shouldHandleEndedGame(endedState, true), false);
  assert.equal(shouldAdvanceGameEpoch(activeState, true), true);
  assert.equal(shouldAdvanceGameEpoch(activeState, false), false);
});

test("exposeTetrioGameFromPausedCallFrames skips Function Ai and accepts later valid frame", async () => {
  const calls = [];
  const cdp = {
    async send(method, params) {
      calls.push({ method, params });
      if (method === "Debugger.evaluateOnCallFrame") {
        if (params.callFrameId === "frame-1") {
          return { result: { value: { ok: false } } };
        }
        if (params.callFrameId === "frame-2") {
          return { result: { value: { ok: true, source: "closure:Ai" } } };
        }
      }
      if (method === "Runtime.queryObjects") {
        throw new Error("queryObjects should never be called in Solo path");
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const result = await exposeTetrioGameFromPausedCallFrames(cdp, {
    callFrames: [{ callFrameId: "frame-1" }, { callFrameId: "frame-2" }]
  });

  assert.equal(result.ok, true);
  assert.equal(result.source, "closure:Ai");
  assert.deepEqual(
    calls.filter((call) => call.method === "Debugger.evaluateOnCallFrame").map((call) => call.params.callFrameId),
    ["frame-1", "frame-2"]
  );
});

test("exposeTetrioGameFromPausedCallFrames returns false when no valid frame exists", async () => {
  const cdp = {
    async send(method) {
      if (method === "Debugger.evaluateOnCallFrame") {
        return { result: { value: { ok: false } } };
      }
      if (method === "Runtime.queryObjects") {
        throw new Error("queryObjects should never be called in Solo path");
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const result = await exposeTetrioGameFromPausedCallFrames(cdp, {
    callFrames: [{ callFrameId: "frame-1" }]
  });

  assert.equal(result.ok, false);
});

test("captureTetrioGame resumes every paused event and still runs finally cleanup on success", async () => {
  const calls = [];
  const pausedEvents = [
    { callFrames: [{ callFrameId: "frame-1" }] },
    { callFrames: [{ callFrameId: "frame-2" }] }
  ];
  const cdp = {
    async send(method, params) {
      calls.push({ method, params });
      if (method === "Runtime.evaluate") {
        return { result: { objectId: `${params.expression}-id` } };
      }
      if (method === "Debugger.setBreakpointOnFunctionCall") {
        return { breakpointId: `bp-${params.objectId}` };
      }
      if (method === "Debugger.evaluateOnCallFrame") {
        if (params.callFrameId === "frame-1") {
          return { result: { value: { ok: false } } };
        }
        return { result: { value: { ok: true, source: "closure:Ai" } } };
      }
      if (
        method === "Debugger.enable" ||
        method === "Debugger.resume" ||
        method === "Debugger.removeBreakpoint" ||
        method === "Runtime.releaseObjectGroup" ||
        method === "Debugger.disable"
      ) {
        return {};
      }
      if (method === "Runtime.queryObjects") {
        throw new Error("queryObjects should never be called in Solo path");
      }
      throw new Error(`unexpected ${method}`);
    },
    async waitForEvent(method) {
      assert.equal(method, "Debugger.paused");
      const next = pausedEvents.shift();
      if (!next) {
        throw new Error("Timed out waiting for CDP event Debugger.paused");
      }
      return next;
    }
  };

  const result = await captureTetrioGame(cdp);

  assert.equal(result.ok, true);
  assert.equal(result.source, "closure:Ai");
  assert.equal(calls.filter((call) => call.method === "Debugger.resume").length, 2);
  assert.equal(calls.filter((call) => call.method === "Debugger.removeBreakpoint").length, 2);
  assert.equal(
    calls.some(
      (call) =>
        call.method === "Runtime.releaseObjectGroup" &&
        call.params?.objectGroup === "fusion-tetrio-probe"
    ),
    true
  );
  assert.equal(calls.some((call) => call.method === "Debugger.disable"), true);
});

test("captureTetrioGame cleans up breakpoints and debugger on failure", async () => {
  const calls = [];
  const cdp = {
    async send(method, params) {
      calls.push({ method, params });
      if (method === "Runtime.evaluate") {
        return { result: { objectId: `${params.expression}-id` } };
      }
      if (method === "Debugger.setBreakpointOnFunctionCall") {
        return { breakpointId: `bp-${params.objectId}` };
      }
      if (
        method === "Debugger.enable" ||
        method === "Debugger.removeBreakpoint" ||
        method === "Runtime.releaseObjectGroup" ||
        method === "Debugger.disable"
      ) {
        return {};
      }
      if (method === "Runtime.queryObjects") {
        throw new Error("queryObjects should never be called in Solo path");
      }
      throw new Error(`unexpected ${method}`);
    },
    async waitForEvent() {
      throw new Error("Timed out waiting for CDP event Debugger.paused");
    }
  };

  const result = await captureTetrioGame(cdp);

  assert.equal(result.ok, false);
  assert.equal(calls.filter((call) => call.method === "Debugger.removeBreakpoint").length, 2);
  assert.equal(calls.some((call) => call.method === "Debugger.disable"), true);
});

test("readTetrioState does not probe with debugger after a valid cached game state exists", async () => {
  let captureCalled = false;
  const cdp = {
    async send(method) {
      if (method === "Runtime.evaluate") {
        return {
          result: {
            value: {
              ok: true,
              ready: true,
              playing: true,
              countdown: false,
              field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
              current: "t",
              hold: "i",
              queue: ["l", "s", "o"],
              pieceCounter: 0,
              pieceCounterSource: "state.stats.pieces",
              gameId: "solo-1",
              roundId: "round-1",
              gameObjectId: "object-1"
            }
          }
        };
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const state = await readTetrioState(cdp, {
    probePageState: true,
    useSeedSimulationFallback: false,
    network: { lastPageProbeAt: 0, seed: "" },
    probeState: { lastCaptureAt: 0 },
    captureGameFn: async () => {
      captureCalled = true;
      return { ok: true, source: "closure:Ai" };
    }
  });

  assert.equal(state.ok, true);
  assert.equal(state.pieceCounter, 0);
  assert.equal(captureCalled, false);
});

test("readTetrioState preserves linesCleared zero from direct solo paths", async () => {
  const cdp = {
    async send(method) {
      if (method === "Runtime.evaluate") {
        return {
          result: {
            value: {
              ok: true,
              ready: true,
              playing: true,
              countdown: false,
              field: Array.from({ length: 40 }, () => Array.from({ length: 10 }, () => false)),
              current: "t",
              hold: null,
              queue: ["l", "s", "o"],
              pieceCounter: null,
              pieceCounterSource: "derived-revision",
              linesCleared: 0,
              gameId: "solo-1",
              gameObjectId: "object-1"
            }
          }
        };
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const state = await readTetrioState(cdp, {
    probePageState: false,
    useSeedSimulationFallback: false,
    network: { lastPageProbeAt: 0, seed: "" },
    probeState: { lastCaptureAt: 0 }
  });

  assert.equal(state.linesCleared, 0);
});

test("markCurrentGameAsEnded keeps ended cache and removes live cache only", async () => {
  let expression = "";
  const cdp = {
    async send(method, params) {
      if (method === "Runtime.evaluate") {
        expression = params.expression;
        return { result: { value: true } };
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  await markCurrentGameAsEnded(cdp);

  assert.match(expression, /__fusionEndedTetrioGame/);
  assert.match(expression, /delete window.__fusionTetrioGame/);
  assert.doesNotMatch(expression, /__fusionSoloRootCandidate/);
});

test("pausedFrameExposureExpression rejects stale ended object reuse", () => {
  const staleGame = {
    ejectState() {
      return { game: { gameover: true } };
    },
    ejectBoardState() {
      return { b: [] };
    }
  };
  const context = {
    Ai: staleGame,
    window: {
      __fusionEndedTetrioGame: staleGame
    },
    location: {
      href: "https://tetr.io/"
    },
    Date
  };

  const result = vm.runInNewContext(pausedFrameExposureExpression(), context);

  assert.equal(result.ok, false);
  assert.equal("__fusionTetrioGame" in context.window, false);
});
