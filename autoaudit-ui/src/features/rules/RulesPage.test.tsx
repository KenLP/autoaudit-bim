import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RulesPage } from "./RulesPage";
import type { RuleFilesResponse } from "@/api/types";

const FILES: RuleFilesResponse = {
  files: [
    {
      name: "rules.demo.yaml",
      path: "config/rules.demo.yaml",
      scenario: "demo",
      rule_count: 3,
      categories: ["Doors", "Walls"],
      mtime: "2026-07-10T09:00:00Z",
      error: null,
    },
    {
      name: "rules.broken.yaml",
      path: "config/rules.broken.yaml",
      scenario: "broken",
      rule_count: 0,
      categories: [],
      mtime: "2026-07-09T09:00:00Z",
      error: "YAML parse error at line 4",
    },
  ],
};

function mockFetchOnce(body: unknown) {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response);
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/rules"]}>
          <RulesPage />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("RulesPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists rule files with scenario, rule count and categories", async () => {
    mockFetchOnce(FILES);
    renderPage();

    expect(await screen.findByText("rules.demo.yaml")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    expect(screen.getByText("3 rules")).toBeInTheDocument();
    expect(screen.getByText("Doors")).toBeInTheDocument();
    expect(screen.getByText("Walls")).toBeInTheDocument();
  });

  it("dims a file that failed to load and surfaces the error in a tooltip", async () => {
    mockFetchOnce(FILES);
    renderPage();

    expect(await screen.findByText("rules.broken.yaml")).toBeInTheDocument();
  });

  it("shows an empty state with a New rule action when there are no files", async () => {
    mockFetchOnce({ files: [] });
    renderPage();

    expect(await screen.findByText("No rule files yet")).toBeInTheDocument();
  });
});
