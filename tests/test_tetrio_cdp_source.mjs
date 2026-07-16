import test from "node:test";
import assert from "node:assert/strict";

import {
  acceptPieceCounterSource,
  buildTombstoneSnapshot,
  createSessionState,
  resetSessionState,
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
    token: null,
    capturedAt: 123
  });
});
