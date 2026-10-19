import { describe, expect, it } from "vitest";
import { curlFor, qs } from "../src/islands/lib";

const call = { method: "POST", path: "/v1/context/build", caller: "planning-agent", status: 200, response: {}, request: { task: "x", max_tokens: 600 } };

describe("curlFor", () => {
  it("builds a runnable curl for a POST without an idempotency key on the read-like context build", () => {
    const c = curlFor(call);
    expect(c).toContain('curl -s -X POST "http://localhost:8000/v1/context/build"');
    expect(c).toContain("Authorization: Bearer $KG_API_KEY");
    expect(c).not.toContain("Idempotency-Key");
    expect(c).toContain(`-d '${JSON.stringify(call.request)}'`);
  });

  it("adds an idempotency key to other writes", () => {
    expect(curlFor({ ...call, path: "/v1/entities" })).toContain("Idempotency-Key: $(uuidgen)");
  });

  it("keeps a GET plain and carries its query", () => {
    const c = curlFor({ method: "GET", path: "/v1/entities/sarah/relationships", query: { as_of: "2027-03-01T00:00:00Z" }, caller: "admin", status: 200, response: [], request: null });
    expect(c).toContain('curl -s "http://localhost:8000/v1/entities/sarah/relationships?as_of=2027-03-01T00%3A00%3A00Z"');
    expect(c).not.toContain("-X");
  });
});

describe("qs", () => {
  it("is empty without a query and encodes values", () => {
    expect(qs()).toBe("");
    expect(qs({ a: "b c" })).toBe("?a=b%20c");
  });
});
