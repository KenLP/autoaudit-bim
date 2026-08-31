import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

function mockFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => ({}),
    text: async () => "",
    ...response,
  } as Response);
}

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns parsed JSON on success", async () => {
    mockFetch({ json: async () => ({ ok: true, value: 42 }) });
    const result = await api.get<{ ok: boolean; value: number }>("/health");
    expect(result).toEqual({ ok: true, value: 42 });
  });

  it("returns raw text for non-JSON responses (markdown reports)", async () => {
    mockFetch({
      headers: new Headers({ "content-type": "text/markdown" }),
      text: async () => "# Report",
    });
    const result = await api.get<string>("/runs/abc/report");
    expect(result).toBe("# Report");
  });

  it("throws ApiError with the server detail on non-2xx", async () => {
    mockFetch({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Run not found" }),
    });
    await expect(api.get("/runs/missing")).rejects.toMatchObject(
      new ApiError(404, "Run not found"),
    );
  });

  it("falls back to statusText when the error body has no detail", async () => {
    mockFetch({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => {
        throw new Error("not json");
      },
    });
    await expect(api.get("/boom")).rejects.toMatchObject({
      status: 500,
      detail: "Internal Server Error",
    });
  });

  it("serializes 409 conflicts as ApiError so callers can branch on status", async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({ detail: "Another audit is running" }),
    });
    await expect(api.post("/audits", { profile_path: "x" })).rejects.toBeInstanceOf(
      ApiError,
    );
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({ detail: "Another audit is running" }),
    });
    const err = await api.post("/audits", { profile_path: "x" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
  });
});
