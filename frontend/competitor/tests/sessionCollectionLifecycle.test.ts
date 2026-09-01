import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { AuthSessionRevision } from "../src/authSessionRevision.ts";
import {
  collectionCheckpointIsRunning,
  isCollectionSessionBoundaryStatus,
  shouldPreserveActiveCollectionRequest,
} from "../src/collectionRunLifecycle.ts";

const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

test("a detached collection stays resumable without clearing its active request", () => {
  assert.equal(
    collectionCheckpointIsRunning({
      collecting: false,
      manualStopRequested: false,
      detachRequested: true,
    }),
    true,
  );
  assert.equal(shouldPreserveActiveCollectionRequest(true, false), true);
  assert.equal(shouldPreserveActiveCollectionRequest(true, true), false);
  assert.equal(isCollectionSessionBoundaryStatus(401), true);
  assert.equal(isCollectionSessionBoundaryStatus(403), true);
  assert.equal(isCollectionSessionBoundaryStatus(423), false);
  assert.equal(
    collectionCheckpointIsRunning({
      collecting: true,
      manualStopRequested: true,
      detachRequested: true,
    }),
    false,
  );
});

test("a delayed 401 revision cannot expire a newer login", () => {
  const revision = new AuthSessionRevision();
  const oldRequestRevision = revision.snapshot();
  revision.advance();

  assert.equal(revision.isCurrent(oldRequestRevision), false);
  assert.equal(revision.isCurrent(revision.snapshot()), true);
  assert.match(
    apiSource,
    /const requestAuthSessionRevision = authSessionRevision\.snapshot\(\)/,
  );
  assert.match(
    apiSource,
    /authSessionRevision\.isCurrent\(requestAuthSessionRevision\)/,
  );
});

test("logout detaches the old collection loop before replacing the session", () => {
  const signOutBlock = appSource.slice(
    appSource.indexOf("async function signOut"),
    appSource.indexOf("async function loadFreshness"),
  );
  assert.ok(
    signOutBlock.indexOf("AUTH_SESSION_ENDING_EVENT")
      < signOutBlock.indexOf("await logout()"),
  );
  assert.match(
    pageSource,
    /onBeforeUnmount\(\(\) => \{[\s\S]{0,160}detachCollectionForSessionChange\(\)/,
  );
  assert.match(pageSource, /abortController\.value\?\.abort\(\)/);
  assert.match(
    pageSource,
    /error instanceof ApiRequestError\s+&& isCollectionSessionBoundaryStatus\(error\.status\)/,
  );
  assert.match(
    pageSource,
    /shouldPreserveActiveCollectionRequest\(\s+collectionDetachRequested\.value,\s+settled,/,
  );
  assert.match(
    pageSource,
    /if \(!collectionDetachRequested\.value\) \{\s+await Promise\.all\(\[loadOverview\(\), loadTargets\(\)\]\)/,
  );
});
