export interface CollectionClientMessage {
  type: "probe" | "occupied";
  clientId: string;
  instanceId: string;
  probeId: string;
}

export interface CollectionClientChannel {
  onmessage: ((event: MessageEvent<CollectionClientMessage>) => void) | null;
  postMessage(message: CollectionClientMessage): void;
}

interface PersistentPeerOptions {
  channel: CollectionClientChannel;
  clientId: string;
  instanceId: string;
  createProbeId: () => string;
  wait: (milliseconds: number) => Promise<void>;
  firstProbeWaitMs?: number;
  confirmationGapMs?: number;
  confirmationProbeWaitMs?: number;
}

export async function hasPersistentCollectionClientPeer({
  channel,
  clientId,
  instanceId,
  createProbeId,
  wait,
  firstProbeWaitMs = 160,
  confirmationGapMs = 320,
  confirmationProbeWaitMs = 220,
}: PersistentPeerOptions): Promise<boolean> {
  const occupiedProbeIds = new Set<string>();
  channel.onmessage = (event: MessageEvent<CollectionClientMessage>) => {
    const message = event.data;
    if (!message || message.clientId !== clientId) return;
    if (message.type === "probe" && message.instanceId !== instanceId) {
      channel.postMessage({
        type: "occupied",
        clientId,
        instanceId,
        probeId: message.probeId,
      });
    } else if (
      message.type === "occupied"
      && message.instanceId !== instanceId
    ) {
      occupiedProbeIds.add(message.probeId);
    }
  };

  const probe = async (waitMilliseconds: number) => {
    const probeId = createProbeId();
    channel.postMessage({
      type: "probe",
      clientId,
      instanceId,
      probeId,
    });
    await wait(waitMilliseconds);
    return occupiedProbeIds.has(probeId);
  };

  if (!(await probe(firstProbeWaitMs))) return false;
  await wait(confirmationGapMs);
  return probe(confirmationProbeWaitMs);
}
