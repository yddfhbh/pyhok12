import test from "node:test";
import assert from "node:assert/strict";

import {
  acceptPieceCounterSource,
  buildTombstoneSnapshot,
  buildSoloSpawnSignature,
  createSessionState,
  exposeTetrioGameFromPausedCallFrames,
  normalizeSoloPieceValue,
  pickBetterSoloCandidate,
  resetSessionState,
  resolveSoloStateRevision,
  selectSoloPieceCounterCandidate,
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

  resetSessionState(sessionState);
  updateSessionState(sessionState, {
    playing: true,
    roundId: "round-2",
    gameId: "game-2",
    gameObjectId: "object-2"
  });
  assert.equal(acceptPieceCounterSource(sessionState, "pieces"), true);
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

test("selectSoloPieceCounterCandidate falls back to stats.pieces and derived revision", () => {
  assert.deepEqual(
    selectSoloPieceCounterCandidate({
      state: {
        stats: {
          pieces: 14
        }
      }
    }),
    {
      value: 14,
      source: "state.stats.pieces"
    }
  );
  assert.deepEqual(selectSoloPieceCounterCandidate({}), {
    value: null,
    source: "derived-revision"
  });
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

test("pickBetterSoloCandidate does not select invalid score-zero candidates", () => {
  assert.equal(
    pickBetterSoloCandidate(null, {
      path: "closure:Ai",
      score: 0,
      valid: false
    }),
    null
  );
});

test("exposeTetrioGameFromPausedCallFrames prefers direct object bindings over queryObjects", async () => {
  const calls = [];
  const cdp = {
    async send(method, params) {
      calls.push({ method, params });
      if (method === "Runtime.evaluate") {
        return { result: { value: true } };
      }
      if (method === "Runtime.getProperties") {
        if (params.objectId === "scope-1") {
          return {
            result: [
              {
                name: "gameInstance",
                value: { type: "object", objectId: "obj-game" }
              },
              {
                name: "Ai",
                value: { type: "function", objectId: "fn-ai" }
              }
            ]
          };
        }
        return { result: [] };
      }
      if (method === "Runtime.callFunctionOn") {
        if (params.objectId === "obj-game" && params.returnByValue) {
          return {
            result: {
              value: {
                ok: true,
                path: "gameInstance",
                constructorName: "Ai",
                ownKeys: [],
                protoKeys: [],
                hasEjectState: true,
                hasEjectBoardState: true,
                boardPath: "gameInstance.board",
                currentPath: "gameInstance.current",
                holdPath: "gameInstance.hold",
                queuePath: "gameInstance.queue",
                pieceCounterPath: "gameInstance.stats.pieces",
                score: 14,
                valid: true
              }
            }
          };
        }
        return { result: { value: true } };
      }
      if (method === "Runtime.queryObjects") {
        throw new Error("queryObjects should not run when a direct object binding is valid");
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const result = await exposeTetrioGameFromPausedCallFrames(
    cdp,
    {
      callFrames: [
        {
          scopeChain: [
            {
              type: "closure",
              object: { objectId: "scope-1" }
            }
          ]
        }
      ]
    },
    { lastSoloQueryAt: 0 }
  );

  assert.equal(result.ok, true);
  assert.equal(result.source, "gameInstance");
  assert.equal(calls.some((call) => call.method === "Runtime.queryObjects"), false);
});

test("exposeTetrioGameFromPausedCallFrames uses queryObjects for constructor bindings and pins the best instance", async () => {
  const calls = [];
  const cdp = {
    async send(method, params) {
      calls.push({ method, params });
      if (method === "Runtime.evaluate") {
        return { result: { value: true } };
      }
      if (method === "Runtime.getProperties") {
        if (params.objectId === "scope-2") {
          return {
            result: [
              {
                name: "Ai",
                value: { type: "function", objectId: "fn-ai" }
              }
            ]
          };
        }
        if (params.objectId === "fn-ai") {
          return {
            result: [
              {
                name: "prototype",
                value: { type: "object", objectId: "proto-ai" }
              }
            ]
          };
        }
        if (params.objectId === "query-array") {
          return {
            result: [
              {
                name: "0",
                value: { type: "object", objectId: "inst-0" }
              },
              {
                name: "1",
                value: { type: "object", objectId: "inst-1" }
              }
            ]
          };
        }
        return { result: [] };
      }
      if (method === "Runtime.queryObjects") {
        return {
          objects: { objectId: "query-array" }
        };
      }
      if (method === "Runtime.callFunctionOn") {
        if (params.objectId === "inst-0" && params.returnByValue) {
          return {
            result: {
              value: {
                ok: true,
                path: "queryObjects(Ai.prototype)[0]",
                constructorName: "Ai",
                ownKeys: [],
                protoKeys: [],
                hasEjectState: false,
                hasEjectBoardState: false,
                boardPath: "",
                currentPath: "",
                holdPath: "",
                queuePath: "",
                pieceCounterPath: "",
                score: 0,
                valid: false
              }
            }
          };
        }
        if (params.objectId === "inst-1" && params.returnByValue) {
          return {
            result: {
              value: {
                ok: true,
                path: "queryObjects(Ai.prototype)[1]",
                constructorName: "Ai",
                ownKeys: [],
                protoKeys: [],
                hasEjectState: true,
                hasEjectBoardState: true,
                boardPath: "ejectBoardState().b",
                currentPath: "ejectState().falling.type",
                holdPath: "ejectState().hold",
                queuePath: "ejectState().bag",
                pieceCounterPath: "ejectState().stats.pieces",
                score: 15,
                valid: true
              }
            }
          };
        }
        return { result: { value: true } };
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const result = await exposeTetrioGameFromPausedCallFrames(
    cdp,
    {
      callFrames: [
        {
          scopeChain: [
            {
              type: "closure",
              object: { objectId: "scope-2" }
            }
          ]
        }
      ]
    },
    { lastSoloQueryAt: 0 }
  );

  assert.equal(result.ok, true);
  assert.equal(result.source, "queryObjects(Ai.prototype)[1]");
  assert.equal(calls.some((call) => call.method === "Runtime.queryObjects"), true);
  assert.equal(
    calls.some(
      (call) =>
        call.method === "Runtime.callFunctionOn" &&
        call.params.objectId === "inst-1" &&
        Array.isArray(call.params.arguments)
    ),
    true
  );
});

test("exposeTetrioGameFromPausedCallFrames survives empty queryObjects results", async () => {
  const cdp = {
    async send(method, params) {
      if (method === "Runtime.evaluate") {
        return { result: { value: true } };
      }
      if (method === "Runtime.getProperties") {
        if (params.objectId === "scope-3") {
          return {
            result: [
              {
                name: "Ai",
                value: { type: "function", objectId: "fn-ai" }
              }
            ]
          };
        }
        if (params.objectId === "fn-ai") {
          return {
            result: [
              {
                name: "prototype",
                value: { type: "object", objectId: "proto-ai" }
              }
            ]
          };
        }
        if (params.objectId === "query-empty") {
          return { result: [] };
        }
        return { result: [] };
      }
      if (method === "Runtime.queryObjects") {
        return { objects: { objectId: "query-empty" } };
      }
      if (method === "Runtime.callFunctionOn") {
        return { result: { value: true } };
      }
      throw new Error(`unexpected ${method}`);
    }
  };

  const result = await exposeTetrioGameFromPausedCallFrames(
    cdp,
    {
      callFrames: [
        {
          scopeChain: [
            {
              type: "closure",
              object: { objectId: "scope-3" }
            }
          ]
        }
      ]
    },
    { lastSoloQueryAt: 0 }
  );

  assert.equal(result.ok, false);
});
