import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";
import * as permissions from "../src/permissions.ts";
import { ERP_PUBLIC_LOGIN_URL, formatUserLogin, copyUserLogin } from "../src/userLoginCopy.ts";

const source = readFileSync(new URL("../src/pages/UsersPage.vue", import.meta.url), "utf8");
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: "users-copy" }).content, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const account = (id = 1) => ({
  id, username: `staff${id}`, display_name: `员工${id}`, role: "viewer", active: true,
  permissions: [], all_stores: false, assigned_store_ids: [], accessible_stores: [],
  updated_at: "2026-09-07T01:00:00Z", created_at: "2026-09-07T01:00:00Z", last_login_at: null,
});

async function harness(overrides: Record<string, unknown> = {}) {
  const writes: unknown[] = [];
  const copied: string[] = [];
  const lifecycle: (() => void)[] = [];
  const api = {
    fetchUsers: async () => [account(), account(2)], fetchStores: async () => [],
    createUser: async (input: any) => { writes.push(input); return { ...account(3), username: input.username.toLowerCase() }; },
    updateUser: async (id: number, input: any) => { writes.push(input); return { ...account(id), updated_at: "2026-09-07T02:00:00Z" }; },
    ...overrides,
  };
  const exports: any = {};
  const ctx = vm.createContext({
    exports, setTimeout: () => 1, clearTimeout: () => {}, Intl, Date,
    window: { prompt: () => "New-Test-Only-Password" },
    require: (name: string) => {
      if (name === "vue") return { ...vue, onBeforeUnmount: (fn: () => void) => lifecycle.push(fn), onDeactivated: () => {} };
      if (name === "../api") return api;
      if (name === "../components/CliUsageDialog.vue") return { default: {} };
      if (name === "../permissions") return permissions;
      if (name === "../liveUpdates") return { useLiveUpdates: () => {} };
      if (name === "../userLoginCopy") return { ERP_PUBLIC_LOGIN_URL, formatUserLogin, copyUserLogin: async (text: string) => { copied.push(text); if (overrides.clipboardFails) throw Error("denied"); } };
      throw Error(`Unexpected import: ${name}`);
    },
  });
  vm.runInContext(compiled, ctx);
  const page = exports.default.setup({}, { expose: () => {} });
  page.copyDialog.value = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  await page.load();
  return { page, writes, copied, api, lifecycle };
}

test("copy format uses the public entry and preserves exact password characters", () => {
  assert.equal(formatUserLogin("staff1", "  Test<&密>123  "),
    "昂古古科技有限公司ERP\n系统地址：https://119.91.117.232/\n账号：staff1\n密码：  Test<&密>123  ");
  assert.throws(() => formatUserLogin("staff1", ""));
});

test("existing users open a password dialog without any password mutation; cancel clears it", async () => {
  const { page, writes, copied } = await harness();
  await page.copyLogin(account());
  assert.equal(page.copyDialog.value.open, true);
  assert.equal(page.copyTarget.value.id, 1);
  assert.equal(page.copyPassword.value, "");
  assert.equal(writes.length, 0);
  assert.equal(copied.length, 0);
  page.closeCopyDialog();
  assert.equal(page.copyTarget.value, null);
  assert.equal(page.copyPassword.value, "");
});

test("entered passwords copy directly and remain isolated per account", async () => {
  const { page, writes, copied } = await harness();
  await page.copyLogin(account());
  page.copyPassword.value = "Test-Staff-One";
  await page.submitLoginCopy();
  await page.copyLogin(account());
  assert.equal(copied.length, 2);
  assert.equal(copied[1], formatUserLogin("staff1", "Test-Staff-One"));
  await page.copyLogin(account(2));
  assert.equal(page.copyPassword.value, "");
  assert.equal(copied.length, 2);
  assert.equal(writes.length, 0);
});

test("creation remembers the submitted password even if the input changes while saving", async () => {
  let finish!: (value: unknown) => void;
  const { page, copied } = await harness({ createUser: () => new Promise((resolve) => { finish = resolve; }) });
  page.username.value = "STAFF3";
  page.password.value = "Submitted-Test-Password";
  const pending = page.submit();
  page.password.value = "Later-Unsubmitted-Password";
  finish(account(3));
  await pending;
  assert.equal(page.password.value, "");
  await page.copyLogin(page.users.value.find((u: any) => u.id === 3));
  assert.equal(copied[0], formatUserLogin("staff3", "Submitted-Test-Password"));
});

test("reset-and-copy saves once and clipboard retries never reset the password again", async () => {
  const { page, writes } = await harness({ clipboardFails: true });
  await page.copyLogin(account());
  page.copyMode.value = "reset";
  page.copyPassword.value = "New-Test-Password";
  await page.submitLoginCopy();
  assert.equal(writes.length, 1);
  assert.equal(page.copyMode.value, "current");
  assert.match(page.manualCopyText.value, /密码：New-Test-Password$/);
  assert.match(page.copyError.value, /复制失败/);
  await page.submitLoginCopy();
  assert.equal(writes.length, 1);
});

test("a rejected reset cannot copy or retain an unconfirmed password", async () => {
  const { page, copied } = await harness({ updateUser: async () => { throw Error("账号更新失败"); } });
  await page.copyLogin(account());
  page.copyMode.value = "reset";
  page.copyPassword.value = "Rejected-Test-Password";
  await page.submitLoginCopy();
  assert.equal(copied.length, 0);
  assert.equal(page.loginPasswords.size, 0);
  assert.match(page.copyError.value, /账号更新失败/);
});

test("account updates from another page invalidate a remembered password", async () => {
  const { page, api } = await harness();
  await page.copyLogin(account(), "Old-Test-Password");
  api.fetchUsers = async () => [{ ...account(), updated_at: "2026-09-07T05:00:00Z" }];
  await page.load(true);
  assert.equal(page.loginPasswords.size, 0);
  await page.copyLogin(page.users.value[0]);
  assert.equal(page.copyPassword.value, "");
});

test("disabled accounts cannot copy and leaving clears all password material", async () => {
  const { page, copied, lifecycle } = await harness();
  await page.copyLogin({ ...account(), active: false }, "Inactive-Test-Password");
  assert.equal(copied.length, 0);
  await page.copyLogin(account(), "Active-Test-Password");
  lifecycle.forEach((fn) => fn());
  assert.equal(page.loginPasswords.size, 0);
  assert.equal(page.copyPassword.value, "");
  assert.equal(page.manualCopyText.value, "");
});

test("a pending creation cannot retain a password after the page is left", async () => {
  let finish!: (value: unknown) => void;
  const { page, lifecycle } = await harness({ createUser: () => new Promise((resolve) => { finish = resolve; }) });
  page.password.value = "Pending-Test-Password";
  const pending = page.submit();
  lifecycle.forEach((fn) => fn());
  finish(account(3));
  await pending;
  assert.equal(page.loginPasswords.size, 0);
});

test("clipboard supports native success, denied permission fallback, and truthful failure", async () => {
  const oldNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const oldDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const oldHTMLElement = Object.getOwnPropertyDescriptor(globalThis, "HTMLElement");
  const values: string[] = [];
  let fallbackWorks = true, appended = 0, removed = 0, focused = 0;
  class Element { focus() { focused++; } }
  const textarea = { value: "", style: {}, focus() {}, select() {}, setSelectionRange() {}, remove() { removed++; } };
  try {
    Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: Element });
    Object.defineProperty(globalThis, "document", { configurable: true, value: {
      activeElement: new Element(), createElement: () => textarea,
      querySelector: () => ({ appendChild: () => { appended++; } }),
      execCommand: () => fallbackWorks,
    } });
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (text: string) => { values.push(text); } } } });
    await copyUserLogin("native-test");
    assert.deepEqual(values, ["native-test"]);
    assert.equal(appended, 0);
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async () => { throw Error("denied"); } } } });
    await copyUserLogin("fallback-test");
    assert.equal(textarea.value, "fallback-test");
    assert.equal(appended, 1);
    assert.equal(removed, 1);
    assert.equal(focused, 1);
    fallbackWorks = false;
    await assert.rejects(copyUserLogin("failure-test"), /复制失败/);
    assert.equal(removed, 2);
  } finally {
    for (const [key, descriptor] of [["navigator", oldNavigator], ["document", oldDocument], ["HTMLElement", oldHTMLElement]] as const) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
