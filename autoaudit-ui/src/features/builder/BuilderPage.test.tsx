import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import { strings } from "@/strings";
import { BuilderPage } from "./BuilderPage";
import type { ValidationResult } from "@/api/types";

const VALIDATION: ValidationResult = {
  ok: false,
  errors: [
    { field: "parameter", message: "Parameter is required" },
    { field: "remediation", message: "`Width` is read-only — cannot be an auto-fix write target" },
  ],
  warnings: ["A threshold of 0 always passes"],
};

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function mockRoutedFetch() {
  globalThis.fetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/catalogs/categories")) {
      return Promise.resolve(
        jsonResponse({ categories: [{ key: "Doors", label: "Doors", note: null }] }),
      );
    }
    if (url.includes("/api/catalogs/lookups")) {
      return Promise.resolve(jsonResponse({ lookups: [] }));
    }
    if (url.includes("/api/catalogs/references")) {
      return Promise.resolve(jsonResponse({ references: [] }));
    }
    if (url.includes("/api/rules")) {
      return Promise.resolve(jsonResponse({ files: [] }));
    }
    if (url.includes("/api/builder/validate")) {
      return Promise.resolve(jsonResponse(VALIDATION));
    }
    return Promise.resolve(jsonResponse({}));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/rule-builder"]}>
          <BuilderPage />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("BuilderPage validation footer", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("surfaces server-side validation errors and warnings in the sticky footer", async () => {
    mockRoutedFetch();
    renderPage();

    // Wait for the SERVER-sourced error specifically (not the client-side
    // "Parameter is required" basic-field check, which would render first
    // and give a false positive before the debounced validate call lands).
    await waitFor(
      () => {
        expect(screen.getByTestId("validation-footer")).toHaveTextContent(
          "`Width` is read-only",
        );
      },
      { timeout: 3000 },
    );
    expect(screen.getByTestId("validation-footer")).toHaveTextContent(
      "A threshold of 0 always passes",
    );
  });

  it("disables Save while there are blocking basic-field issues (no ID yet)", async () => {
    mockRoutedFetch();
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(strings.builder.save)).toBeDisabled();
    });
  });
});
