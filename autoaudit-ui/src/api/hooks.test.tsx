import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useExtractPdf, useSettings } from "./hooks";
import type { ExtractPdfResponse, SettingsResponse } from "./types";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useSettings", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("maps GET /api/settings into env/services/llm shape", async () => {
    const body: SettingsResponse = {
      env: [
        { key: "FORMA_PROJECT_ID", set: true, masked: "••••ab12" },
        { key: "ANTHROPIC_API_KEY", set: false, masked: null },
      ],
      services: { lod: true, spatial: false, revitcontrol: true },
      llm: { provider: "anthropic" },
    };
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(body));

    const { result } = renderHook(() => useSettings(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(body);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/settings",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("useExtractPdf", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("posts a multipart file to /api/extraction/pdf and returns ruleset + warnings", async () => {
    const body: ExtractPdfResponse = {
      ruleset: {
        scenario: "",
        target_category: [],
        rules: [],
        geometry_rules: [],
      },
      warnings: ["Grounding: 'Clear Width' not found in catalog"],
    };
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(body));

    const { result } = renderHook(() => useExtractPdf(), { wrapper });
    const file = new File(["%PDF-1.4"], "spec.pdf", { type: "application/pdf" });

    result.current.mutate({ file, maxSections: 12 });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(body);

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/extraction/pdf");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("max_sections")).toBe("12");
    // FormData bodies must NOT get a Content-Type header (client.ts leaves
    // it to fetch so the multipart boundary is set correctly).
    expect(init.headers["Content-Type"]).toBeUndefined();
  });

  it("surfaces a 503 (rules_extractor not installed) as an ApiError the dialog can branch on", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: "rules_extractor is not installed — run: uv pip install -e ..." },
        503,
      ),
    );

    const { result } = renderHook(() => useExtractPdf(), { wrapper });
    result.current.mutate({ file: new File(["x"], "spec.pdf") });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as { status?: number }).status).toBe(503);
  });
});
