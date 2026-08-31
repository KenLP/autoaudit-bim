import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ExtractPdfDialog } from "./ExtractPdfDialog";
import type { ExtractPdfResponse } from "@/api/types";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <ExtractPdfDialog open onOpenChange={() => {}} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function pdfFile() {
  return new File(["%PDF-1.4"], "spec.pdf", { type: "application/pdf" });
}

async function selectFile(user: ReturnType<typeof userEvent.setup>) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(input, pdfFile());
}

describe("ExtractPdfDialog", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("idle: Extract is disabled until a file is chosen", async () => {
    renderDialog();
    const extractButton = screen.getByRole("button", { name: "Extract" });
    expect(extractButton).toBeDisabled();

    const user = userEvent.setup();
    await selectFile(user);

    expect(screen.getByText("Selected: spec.pdf")).toBeInTheDocument();
    expect(extractButton).toBeEnabled();
  });

  it("loading: shows the extracting hint while the request is in flight", async () => {
    let resolveFetch: (v: Response) => void = () => {};
    globalThis.fetch = vi.fn().mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderDialog();
    const user = userEvent.setup();
    await selectFile(user);
    await user.click(screen.getByRole("button", { name: "Extract" }));

    expect(await screen.findByText("Extracting…")).toBeInTheDocument();
    expect(
      screen.getByText("This can take up to a few minutes for long documents."),
    ).toBeInTheDocument();

    // Unblock so the test doesn't leak a pending promise into the next case.
    resolveFetch(jsonResponse({ ruleset: { scenario: "", target_category: [], rules: [], geometry_rules: [] }, warnings: [] }));
  });

  it("503: shows the not-installed message from the server verbatim, no crash", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: "rules_extractor is not installed — run: uv pip install -e <path-to>/ExtractionAgents" },
        503,
      ),
    );
    renderDialog();
    const user = userEvent.setup();
    await selectFile(user);
    await user.click(screen.getByRole("button", { name: "Extract" }));

    expect(await screen.findByText("Extraction not available")).toBeInTheDocument();
    expect(
      screen.getByText(/rules_extractor is not installed/),
    ).toBeInTheDocument();
  });

  it("success: shows the review list with warnings and a scenario field", async () => {
    const body: ExtractPdfResponse = {
      ruleset: {
        scenario: "",
        target_category: ["Doors"],
        rules: [
          {
            id: "doors.fire_rating.present",
            parameter: "Fire Rating",
            requirement: "present_and_nonempty",
            category: "Doors",
            severity_tag: "high",
            description: "Doors must have a Fire Rating",
            autofill: { strategy: "none" },
            citation: { mode: "soft", on_missing: "warn" },
            fixability: "manual",
            remediation: {
              action: "create_acc_issue",
              new_value_strategy: "fixed",
              target: "instance",
              llm_safety_critical: false,
            },
            requires_human: true,
          },
        ],
        geometry_rules: [],
      },
      warnings: ["Grounding: 'Clear Width' not found in catalog"],
    };
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse(body));
    renderDialog();
    const user = userEvent.setup();
    await selectFile(user);
    await user.click(screen.getByRole("button", { name: "Extract" }));

    expect(await screen.findByText("1 rule extracted")).toBeInTheDocument();
    expect(screen.getByText("doors.fire_rating.present")).toBeInTheDocument();
    expect(screen.getByText(/Clear Width/)).toBeInTheDocument();

    const saveButton = screen.getByRole("button", { name: "Save as rules file" });
    expect(saveButton).toBeDisabled();

    await user.type(screen.getByPlaceholderText("e.g. vp_bep"), "vp_bep");
    expect(saveButton).toBeEnabled();
  });
});
