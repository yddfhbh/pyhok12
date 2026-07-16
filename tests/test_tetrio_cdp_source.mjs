import test from "node:test";
import assert from "node:assert/strict";

import {
  acceptPieceCounterSource,
  buildTombstoneSnapshot,
  buildSoloSpawnSignature,
  createSessionState,
  normalizeSoloPieceValue,
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
