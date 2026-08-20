import assert from "node:assert/strict";
import test from "node:test";

import {
  companySkuOwnLinks,
  filterReturnsForCompanySku,
  summarizeCompanySkuReturns,
} from "../src/returnCompanySku.ts";
import type { SellerReturnItem } from "../src/types.ts";

function returnItem(
  sellerReturnId: string,
  companySku: string | null,
  options: Partial<SellerReturnItem> = {},
): SellerReturnItem {
  return {
    seller_return_id: sellerReturnId,
    order_id: null,
    order_item_id: null,
    offer_id: `offer-${sellerReturnId}`,
    tsin_id: null,
    sku: `platform-${sellerReturnId}`,
    return_reference_number: null,
    quantity: 1,
    return_date: "2026-08-18",
    return_region: null,
    return_reason: null,
    return_reason_label: "未提供原因",
    customer_comment: null,
    outcome_statuses: [],
    outcome_labels: [],
    outcomes: [],
    transactions: [],
    transaction_total_incl_vat: 0,
    captured_at: null,
    productline_id: null,
    product_title: null,
    image_url: null,
    offer_quantity_returned_30_days: null,
    company_sku: companySku,
    company_product_name: null,
    store_code: "current",
    store_name: "Alpha Store",
    store_scope_key: `current:${sellerReturnId}`,
    ...options,
  };
}

test("groups every exact company SKU return without merging prefix matches", () => {
  const first = returnItem("return-1", "COMP-001", {
    quantity: 2,
    productline_id: "101",
    product_title: "First own link",
  });
  const second = returnItem("return-2", "comp-001", {
    quantity: 3,
    productline_id: "102",
    store_code: "store-02",
    store_name: "Beta Store",
    store_scope_key: "store-02:return-2",
  });
  const prefixOnly = returnItem("return-3", "COMP-001-B");
  const duplicate = { ...first };

  const grouped = filterReturnsForCompanySku(
    [first, second, prefixOnly, duplicate],
    "  Comp-001 ",
  );

  assert.deepEqual(grouped.map((item) => item.seller_return_id), ["return-1", "return-2"]);
  assert.deepEqual(summarizeCompanySkuReturns(grouped), {
    recordCount: 2,
    returnUnits: 5,
    storeCount: 2,
  });
});

test("lists each linked PLID once and never groups unlinked company SKUs", () => {
  const rows = [
    returnItem("return-1", "COMP-001", {
      productline_id: "101",
      product_title: "First own link",
      image_url: null,
    }),
    returnItem("return-2", "COMP-001", {
      productline_id: "101",
      image_url: "https://media.takealot.com/covers_images/101.jpg",
      store_code: "store-02",
      store_scope_key: "store-02:return-2",
    }),
    returnItem("return-3", "COMP-001", {
      productline_id: "102",
      company_product_name: "Second own link",
    }),
  ];

  const links = companySkuOwnLinks(rows);
  assert.deepEqual(links.map((item) => item.plid), ["101", "102"]);
  assert.equal(links[0]?.imageUrl, "https://media.takealot.com/covers_images/101.jpg");
  assert.equal(links[0]?.storeCode, "store-02");
  assert.equal(links[1]?.imageUrl, null);
  assert.deepEqual(filterReturnsForCompanySku(rows, ""), []);
  assert.deepEqual(
    filterReturnsForCompanySku([returnItem("return-4", null)], "未关联"),
    [],
  );
});
