import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SetupPage } from "./SetupPage";
import type { HealthResponse, ProfilesResponse, SettingsResponse } from "@/api/types";

const HEALTH: HealthResponse = {
  ok: true,
  version: "1.6.0",
  // Booleans — the service's actual HealthAxes contract. This fixture used
  // to use HealthStatus strings, i.e. it pinned a contract the backend
  // never spoke; the pills rendered colorless in production while this
  // test stayed green.
  axes: { revit: true, forma: false, lod: true, spatial: true },
};

const PROFILES: ProfilesResponse = { profiles: [] };

const SETTINGS: SettingsResponse = {
  env: [
    { key: "FORMA_PROJECT_ID", set: true, masked: "••••ab12" },
    { key: "APS_CLIENT_ID", set: false, masked: null },
    { key: "ANTHROPIC_API_KEY", set: true, masked: "••••99zz" },
    { key: "BIM_RUNS_ROOT", set: true, masked: "••••runs" },
  ],
  services: { lod: true, spatial: true, revitcontrol: false },
  llm: { provider: "anthropic" },
};

function mockRoutedFetch() {
  globalThis.fetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/api/settings")
      ? SETTINGS
      : url.includes("/api/health")
        ? HEALTH
        : url.includes("/api/profiles")
          ? PROFILES
          : {};
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as Response);
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <SetupPage />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("SetupPage — editable connections", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("groups env keys by Forma/ACC, Anthropic, and Other, masking set values", async () => {
    mockRoutedFetch();
    renderPage();

    expect(await screen.findByText("Forma / ACC")).toBeInTheDocument();
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
    expect(screen.getByText("Other")).toBeInTheDocument();

    expect(screen.getByText("FORMA_PROJECT_ID")).toBeInTheDocument();
    expect(screen.getByText("••••ab12")).toBeInTheDocument();
    expect(screen.getAllByText("Not set").length).toBeGreaterThan(0);
  });

  it("Edit reveals a password input with a reveal toggle, gated by a confirm dialog on Save", async () => {
    mockRoutedFetch();
    renderPage();
    await screen.findByText("FORMA_PROJECT_ID");

    const user = userEvent.setup();
    const editButtons = screen.getAllByRole("button", { name: "Edit" });
    await user.click(editButtons[0]);

    const input = screen.getByPlaceholderText("Enter value…");
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, "new-project-id");

    await user.click(screen.getByRole("button", { name: "Save" }));

    // ConfirmDialog gates the actual write (visual language #4).
    expect(await screen.findByText("Update connection setting")).toBeInTheDocument();
  });

  it("runs diagnostics on demand and renders the PASS/WARN/FAIL table", async () => {
    mockRoutedFetch();
    renderPage();
    await screen.findByText("Diagnostics");

    const doctorBody = {
      checks: [
        { name: "Revit addin", status: "pass", detail: "Responded in 12ms" },
        { name: "Forma token", status: "warn", detail: "Expires in 2 days" },
      ],
    };
    globalThis.fetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/doctor")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers({ "content-type": "application/json" }),
          json: async () => doctorBody,
          text: async () => JSON.stringify(doctorBody),
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => ({}),
        text: async () => "{}",
      } as Response);
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Run diagnostics" }));

    await waitFor(() => expect(screen.getByText("Revit addin")).toBeInTheDocument());
    expect(screen.getByText("Pass")).toBeInTheDocument();
    expect(screen.getByText("Warn")).toBeInTheDocument();
  });
});
