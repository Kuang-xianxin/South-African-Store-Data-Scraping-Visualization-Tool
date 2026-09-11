import assert from "node:assert/strict";
import test from "node:test";
import { effectScope, nextTick, ref } from "vue";
import { useStablePageHeight } from "../src/useStablePageHeight.ts";

function harness() {
  const scope = effectScope();
  const date = ref("2026-09"), summaryBusy = ref(false), homeBusy = ref(false);
  let height = 3300;
  const page = scope.run(() => useStablePageHeight(
    date, () => summaryBusy.value || homeBusy.value,
  ))!;
  page.pageElement.value = {
    getBoundingClientRect: () => ({ height }),
  } as HTMLElement;
  return { scope, date, summaryBusy, homeBusy, ...page, height: (value: number) => { height = value; } };
}

for (const first of ["summaryBusy", "homeBusy"] as const) {
  test(`month reload retains height until both requests finish (${first} first)`, async () => {
    const page = harness();
    try {
      assert.equal(page.pageStyle.value.minHeight, undefined);
      page.date.value = "2026-08";
      page.summaryBusy.value = page.homeBusy.value = true;
      await nextTick();
      page.height(400); // The old charts are replaced by loading placeholders.
      assert.equal(page.pageStyle.value.minHeight, "3300px");
      assert.equal(page.pageStyle.value.overflowAnchor, "none");
      assert.equal(page.pageStyle.value.alignContent, "start");
      page[first].value = false;
      await nextTick();
      assert.equal(page.pageStyle.value.minHeight, "3300px");
      page.height(3400);
      page.summaryBusy.value = page.homeBusy.value = false;
      await nextTick();
      assert.equal(page.pageStyle.value.minHeight, undefined);
    } finally { page.scope.stop(); }
  });
}

test("rapid date changes keep the original height while previous requests are pending", async () => {
  const page = harness();
  try {
    page.date.value = "2026-08";
    page.homeBusy.value = true;
    await nextTick();
    page.height(400);
    page.date.value = "2026-07";
    await nextTick();
    assert.equal(page.pageStyle.value.minHeight, "3300px");
    page.homeBusy.value = false; // Success or failure both end the loading state.
    await nextTick();
    assert.equal(page.pageStyle.value.minHeight, undefined);
  } finally { page.scope.stop(); }
});

test("first render and disposed pages do not reserve layout space", async () => {
  const page = harness();
  page.pageElement.value = null;
  page.date.value = "2026-08";
  await nextTick();
  assert.equal(page.pageStyle.value.minHeight, undefined);
  page.scope.stop();
  page.height(4000);
  page.date.value = "2026-07";
  await nextTick();
  assert.equal(page.pageStyle.value.minHeight, undefined);
});
