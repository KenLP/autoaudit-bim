import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ProposalCard } from "./ProposalCard";
import type { ApprovalRecord } from "@/api/types";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function proposal(fixes: ApprovalRecord["fixes"]): ApprovalRecord {
  return {
    file: "p.json",
    display_id: "42",
    issue_id: "abc",
    project_id: "b.x",
    rule_ids: ["demo.doors.type_mark_format"],
    status: "pending",
    created_at: "2026-08-26T10:32:02Z",
    fixes,
  } as ApprovalRecord;
}

/**
 * L2-05 closed the API gap: the approval record says which values a MODEL
 * proposed and the router forwards `value_source`. This card was the last
 * link that dropped it — the approver could not tell an AI proposal from a
 * deterministic one, which is the one fact the approve gate exists for.
 */
describe("ProposalCard — AI-proposed values are marked", () => {
  it("badges the LLM-proposed fix and counts them in the header", () => {
    render(
      <ProposalCard
        proposal={proposal([
          { element_id: "705", parameter: "Type Mark", old_value: "36x84", new_value: "DT-01", value_source: "llm" },
          { element_id: "401", parameter: "Department", old_value: "", new_value: "General" },
        ])}
      />,
      { wrapper },
    );
    expect(screen.getByText("1 AI-proposed")).toBeInTheDocument();
    expect(screen.getAllByText("AI-proposed")).toHaveLength(1);
    expect(screen.getByText("DT-01")).toBeInTheDocument();
    expect(screen.getByText("General")).toBeInTheDocument();
  });

  it("shows nothing AI-related when every value was computed by a rule", () => {
    render(
      <ProposalCard
        proposal={proposal([
          { element_id: "401", parameter: "Department", old_value: "", new_value: "General" },
          { element_id: "402", parameter: "Department", old_value: null, new_value: "General", value_source: "deterministic" },
        ])}
      />,
      { wrapper },
    );
    expect(screen.queryByText(/AI-proposed/)).toBeNull();
  });
});
