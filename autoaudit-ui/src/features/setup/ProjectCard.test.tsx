import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ProjectCard } from "./ProjectCard";
import type {
  FormaElementGroupsResponse,
  FormaProjectsResponse,
  ProjectSelection,
} from "@/api/types";

const PROJECTS: FormaProjectsResponse = {
  hub: { id: "urn:adsk.ace:prod.scope:aa", name: "Ken's Hub" },
  projects: [
    { name: "Sample ACC Project", aecdm_id: "urn:proj:1", dm_id: "b.1" },
    { name: "Some Office", aecdm_id: "urn:proj:2", dm_id: "b.2" },
  ],
  error: null,
};

const GROUPS: FormaElementGroupsResponse = {
  groups: [{ id: "urn:eg:1", name: "Snowdon Towers" }],
  error: null,
};

const EMPTY_SELECTION: ProjectSelection = {
  hub_id: "b.hub",
  project_id: "",
  aecdm_project_id: "",
  element_group_id: "",
  project_name: "",
  element_group_name: "",
};

function json(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response);
}

/** Routes the three GETs the card makes; PUT echoes its own body back, the
 *  way the server does. `overrides` swaps in a failing browse etc. */
function mockFetch(overrides: Partial<{
  projects: FormaProjectsResponse;
  groups: FormaElementGroupsResponse;
  selection: ProjectSelection;
}> = {}) {
  const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "PUT") return json(JSON.parse(String(init.body)));
    if (url.startsWith("/api/forma/projects")) return json(overrides.projects ?? PROJECTS);
    if (url.startsWith("/api/forma/element-groups")) return json(overrides.groups ?? GROUPS);
    if (url.startsWith("/api/settings/project")) return json(overrides.selection ?? EMPTY_SELECTION);
    return json({});
  });
  globalThis.fetch = fetchMock;
  return fetchMock;
}

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return render(<ProjectCard />, { wrapper: Wrapper });
}

function putBodies(fetchMock: ReturnType<typeof vi.fn>): ProjectSelection[] {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)) as ProjectSelection);
}

describe("ProjectCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists projects by NAME and reports which hub answered", async () => {
    mockFetch();
    renderCard();
    await waitFor(() => expect(screen.getByText("Hub: Ken's Hub")).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText("Project"));
    expect(await screen.findByText("Sample ACC Project")).toBeInTheDocument();
    expect(screen.getByText("Some Office")).toBeInTheDocument();
  });

  it("saves the picked project with BOTH ids and the model", async () => {
    const fetchMock = mockFetch();
    renderCard();
    await waitFor(() => expect(screen.getByText("Hub: Ken's Hub")).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText("Project"));
    await userEvent.click(await screen.findByText("Some Office"));
    await userEvent.click(screen.getByLabelText("Model"));
    await userEvent.click(await screen.findByText("Snowdon Towers"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBodies(fetchMock)).toHaveLength(1));
    expect(putBodies(fetchMock)[0]).toEqual({
      hub_id: "b.hub",
      project_id: "b.2",
      aecdm_project_id: "urn:proj:2",
      element_group_id: "urn:eg:1",
      project_name: "Some Office",
      element_group_name: "Snowdon Towers",
    });
  });

  it("clears the model when the project changes", async () => {
    // The wrong-model bug: carrying project A's element group into a run
    // against project B produces results with nothing saying they are wrong.
    const fetchMock = mockFetch();
    renderCard();
    await waitFor(() => expect(screen.getByText("Hub: Ken's Hub")).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText("Project"));
    await userEvent.click(await screen.findByText("Sample ACC Project"));
    await userEvent.click(screen.getByLabelText("Model"));
    await userEvent.click(await screen.findByText("Snowdon Towers"));

    await userEvent.click(screen.getByLabelText("Project"));
    await userEvent.click(await screen.findByText("Some Office"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBodies(fetchMock)).toHaveLength(1));
    expect(putBodies(fetchMock)[0].element_group_id).toBe("");
    expect(putBodies(fetchMock)[0].element_group_name).toBe("");
  });

  it("shows the saved selection on mount so a reload still reflects it", async () => {
    mockFetch({
      selection: {
        hub_id: "b.hub",
        project_id: "b.1",
        aecdm_project_id: "urn:proj:1",
        element_group_id: "urn:eg:1",
        project_name: "Sample ACC Project",
        element_group_name: "Snowdon Towers",
      },
    });
    renderCard();
    expect(
      await screen.findByText("Selected: Sample ACC Project · Snowdon Towers"),
    ).toBeInTheDocument();
  });

  it("offers Retry and manual entry when the browse fails", async () => {
    // A 403 from a rotated APS client answers 200 with `error` — the card
    // must stay usable, because typing the ids in is then the only way
    // through.
    const fetchMock = mockFetch({
      projects: { hub: null, projects: [], error: "HTTP 403 forbidden" },
    });
    renderCard();
    expect(await screen.findByText("HTTP 403 forbidden")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByText("Hub: not loaded")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Enter IDs manually"));
    await userEvent.type(
      screen.getByLabelText("Project ID (DM / Issues — b.<uuid>)"),
      "b.typed",
    );
    await userEvent.type(
      screen.getByLabelText("AECDM Project ID (urn:adsk.workspace:prod.project:<uuid>)"),
      "urn:typed",
    );
    await userEvent.click(
      screen.getAllByRole("button", { name: "Save" }).slice(-1)[0],
    );

    await waitFor(() => expect(putBodies(fetchMock)).toHaveLength(1));
    expect(putBodies(fetchMock)[0]).toEqual({
      hub_id: "b.hub",
      project_id: "b.typed",
      aecdm_project_id: "urn:typed",
      element_group_id: "",
      project_name: "",
      element_group_name: "",
    });
  });

  it("does not ask for element groups before a project is chosen", async () => {
    const fetchMock = mockFetch();
    renderCard();
    await waitFor(() => expect(screen.getByText("Hub: Ken's Hub")).toBeInTheDocument());
    const urls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(urls.some((u) => u.startsWith("/api/forma/element-groups"))).toBe(false);
  });
});
