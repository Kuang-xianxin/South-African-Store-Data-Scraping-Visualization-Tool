import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";

const source = readFileSync(new URL("../src/components/CliUsageDialog.vue", import.meta.url), "utf8");
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: "cli-usage" }).content, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

function harness() {
  const requests: { userId?: number; resolve: (value: unknown) => void; reject: (error: Error) => void }[] = [];
  const hooks: Record<string, () => void> = {};
  const events: string[] = [];
  const exports: any = {};
  const props = vue.reactive({ userId: 1, allUsers: false });
  vm.runInNewContext(compiled, {
    exports, Intl, Date, Error,
    require: (name: string) => {
      if (name === "vue") return { ...vue,
        onMounted: (fn: () => void) => { hooks.mounted = fn; },
        onDeactivated: (fn: () => void) => { hooks.deactivated = fn; },
        onBeforeUnmount: (fn: () => void) => { hooks.unmount = fn; },
      };
      if (name === "../api") return { fetchCliUsage: (_period: string, userId?: number) =>
        new Promise((resolve, reject) => requests.push({ userId, resolve, reject })) };
      throw Error(`Unexpected import: ${name}`);
    },
  });
  const scope = vue.effectScope();
  const page = scope.run(() => exports.default.setup(props, { expose: () => {}, emit: (name: string) => events.push(name) }));
  return { requests, props, page, hooks, events, scope };
}

test("changing accounts hides old usage immediately and ignores a late response", async () => {
  const h = harness();
  try {
    h.props.userId = 2;
    await vue.nextTick();
    assert.deepEqual(h.requests.map(r => r.userId), [1, 2]);
    assert.equal(h.page.data.value, null);
    h.requests[1].resolve({ owner: 2 });
    await vue.nextTick();
    h.requests[0].resolve({ owner: 1 });
    await vue.nextTick();
    assert.equal(h.page.data.value.owner, 2);
    const refresh = h.page.load();
    assert.equal(h.page.data.value, null);
    h.requests[2].reject(Error("read failed"));
    await refresh;
    assert.equal(h.page.data.value, null);
    assert.equal(h.page.error.value, "read failed");
  } finally { h.scope.stop(); }
});

test("leaving a cached page closes usage and invalidates the pending response", async () => {
  const h = harness();
  let closed = false;
  try {
    h.page.dialog.value = { close() { closed = true; } };
    h.hooks.deactivated();
    h.requests[0].resolve({ owner: 1 });
    await vue.nextTick();
    assert.equal(h.page.data.value, null);
    assert.equal(closed, true);
    assert.deepEqual(h.events, ["close"]);
  } finally { h.scope.stop(); }
});
