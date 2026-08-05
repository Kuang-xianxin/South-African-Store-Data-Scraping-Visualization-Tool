import assert from "node:assert/strict";
import test from "node:test";

import {
  hasPersistentCollectionClientPeer,
  type CollectionClientChannel,
  type CollectionClientMessage,
} from "../src/collectionClientClaim.ts";

class FakeChannel implements CollectionClientChannel {
  onmessage: ((event: MessageEvent<CollectionClientMessage>) => void) | null = null;
  probeCount = 0;
  private readonly responseCount: number;

  constructor(responseCount: number) {
    this.responseCount = responseCount;
  }

  postMessage(message: CollectionClientMessage) {
    if (message.type !== "probe") return;
    this.probeCount += 1;
    if (this.probeCount > this.responseCount) return;
    this.onmessage?.({
      data: {
        type: "occupied",
        clientId: message.clientId,
        instanceId: "existing-page",
        probeId: message.probeId,
      },
    } as MessageEvent<CollectionClientMessage>);
  }
}

function immediateWait() {
  return Promise.resolve();
}

test("keeps the same client when an exiting page only answers the first probe", async () => {
  const channel = new FakeChannel(1);
  let sequence = 0;

  const occupied = await hasPersistentCollectionClientPeer({
    channel,
    clientId: "saved-client",
    instanceId: "new-page",
    createProbeId: () => `probe-${sequence += 1}`,
    wait: immediateWait,
  });

  assert.equal(occupied, false);
  assert.equal(channel.probeCount, 2);
});

test("detects a real second page when it answers both probes", async () => {
  const channel = new FakeChannel(2);
  let sequence = 0;

  const occupied = await hasPersistentCollectionClientPeer({
    channel,
    clientId: "saved-client",
    instanceId: "new-page",
    createProbeId: () => `probe-${sequence += 1}`,
    wait: immediateWait,
  });

  assert.equal(occupied, true);
  assert.equal(channel.probeCount, 2);
});

test("does not delay for confirmation when no page answers", async () => {
  const channel = new FakeChannel(0);
  let waits = 0;

  const occupied = await hasPersistentCollectionClientPeer({
    channel,
    clientId: "saved-client",
    instanceId: "new-page",
    createProbeId: () => "probe",
    wait: async () => {
      waits += 1;
    },
  });

  assert.equal(occupied, false);
  assert.equal(channel.probeCount, 1);
  assert.equal(waits, 1);
});
