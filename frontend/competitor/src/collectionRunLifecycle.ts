export interface CollectionCheckpointRunningInput {
  collecting: boolean;
  manualStopRequested: boolean;
  detachRequested: boolean;
}

export function collectionCheckpointIsRunning(
  input: CollectionCheckpointRunningInput,
): boolean {
  return (
    (input.collecting || input.detachRequested)
    && !input.manualStopRequested
  );
}

export function shouldPreserveActiveCollectionRequest(
  detachRequested: boolean,
  settled: boolean,
): boolean {
  return detachRequested && !settled;
}

export function isCollectionSessionBoundaryStatus(status: number): boolean {
  return status === 401 || status === 403;
}
