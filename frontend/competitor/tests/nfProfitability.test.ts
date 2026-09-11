import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";
import * as server from "vue/server-renderer";

const source = readFileSync(new URL("../src/components/NfProfitabilityPanel.vue", import.meta.url), "utf8");
const compiled = compileScript(parse(source).descriptor, {
  id: "nf-profit", inlineTemplate: true, templateOptions: { ssr: true },
}).content;
const code = ts.transpileModule(compiled, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const exports: any = {};
runInNewContext(code, { exports, Intl, require: (name: string) => {
  if (name === "vue") return vue;
  if (name === "vue/server-renderer") return server;
  throw Error(name);
} });

test("renders workbook cost and loss with its actual source and fee breakdown", async () => {
  const html = await server.renderToString(vue.createSSRApp(exports.default, { model: {
    status: "available", source: { file: "NF毛利计算.xlsx" }, source_rows: [2], message: "",
    calculation: { cost_rmb: 162, total_cost_rmb: 204, profit_rmb: -12.34, margin_percentage: -4.2,
      cells: { D: 499, F: .009391, G: 1.878, I: .85, J: 24.16, K: 57.385,
        L: 60, M: 9, N: 7.8, P: 20.7, Q: 2.6, R: .97, S: .03, T: 149.66 } },
  } }));
  assert.match(html, /-¥12.34/);
  assert.match(html, /loss/);
  assert.match(html, /-4.20%/);
  assert.match(html, /采购及头程/);
  assert.match(html, /利润计算表/);
  assert.match(html, /送仓费/);
});

test("missing source shows a reason without profit cards", async () => {
  const html = await server.renderToString(vue.createSSRApp(exports.default, { model: {
    status: "unavailable", source: {}, source_rows: [661], calculation: null,
    message: "公司 SKU 对应冲突",
  } }));
  assert.match(html, /公司 SKU 对应冲突/);
  assert.match(html, /661/);
  assert.doesNotMatch(html, /当前售价利润（按表计算）/);
});
