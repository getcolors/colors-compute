import { expect, test } from "bun:test";
import cases from "../../test/fixtures/reauth.json";
import { commandFailure, failure } from "../src/diagnostics.ts";
for (const item of cases) test(item.name, () => {
  const error = commandFailure(item.argv, "/tmp", {}, item.result, {"provider-compute": item.provider}, {});
  const result: any = failure(error, "plan", "none");
  expect(result.error.auth_reason ?? null).toBe(item.expected);
  expect(JSON.stringify(result)).not.toContain("PRIVATE-CANARY");
  expect(JSON.stringify(result)).not.toContain("PRIVATE-STDOUT");
});
