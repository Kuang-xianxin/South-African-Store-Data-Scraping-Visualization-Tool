import assert from "node:assert/strict";
import test from "node:test";

import {
  rootExpansionCheckIsPhrase,
  rootExpansionCheckLabel,
  rootExpansionCheckValue,
} from "../src/searchRankingRootExpansion.ts";

test("uses the current root field when it is present", () => {
  const check = { root: "  lazy sofa  ", seed: "lazy" };

  assert.equal(rootExpansionCheckValue(check), "lazy sofa");
  assert.equal(rootExpansionCheckLabel(check), "lazy sofa");
  assert.equal(rootExpansionCheckIsPhrase(check), true);
});

test("falls back to a legacy seed without calling trim on an absent root", () => {
  const check = { root: undefined, seed: "  corduroy  " };

  assert.equal(rootExpansionCheckValue(check), "corduroy");
  assert.equal(rootExpansionCheckLabel(check), "corduroy");
  assert.equal(rootExpansionCheckIsPhrase(check), false);
});

test("falls back through input state and shopper root for older evidence", () => {
  assert.equal(rootExpansionCheckValue({ input_state: "floor chair" }), "floor chair");
  assert.equal(rootExpansionCheckValue({ shopper_root: "sofa chair" }), "sofa chair");
});

test("renders a safe historical label when every root identity field is absent", () => {
  const check = { root: undefined, seed: null, input_state: undefined };

  assert.equal(rootExpansionCheckValue(check), "");
  assert.equal(rootExpansionCheckLabel(check), "历史词根（原记录未保存）");
  assert.equal(rootExpansionCheckIsPhrase(check), false);
});
