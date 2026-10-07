import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { strings } from "@/strings";
import {
  useFormaElementGroups,
  useFormaProjects,
  useProjectSelection,
  useSaveProjectSelection,
} from "@/api/hooks";
import { ApiError } from "@/api/client";
import type { ProjectSelection } from "@/api/types";

/**
 * Pick the ACC/Forma project this pilot audits (v1.7-R25).
 *
 * Before this card the project came from DEMO_* env vars only, and the Setup
 * tab's own caption promised you could change it here — a promise the page
 * could not keep because the env allowlist hid those keys.
 *
 * Two things are worth knowing when editing this file:
 *
 *  * A `SelectItem`'s `value` is the AECDM id, never the display name. Radix
 *    matches values EXACTLY, so a select whose stored state is a label and
 *    whose items carry ids renders blank — the bug that bit GeometryForm.
 *  * Switching project clears the model here as well as on the server. The
 *    server guarantee is what protects a run; clearing in the client is what
 *    stops the dropdown from showing project A's model while project B is
 *    selected.
 */
export function ProjectCard() {
  const projectsQuery = useFormaProjects();
  const selectionQuery = useProjectSelection();
  const save = useSaveProjectSelection();

  const [aecdmId, setAecdmId] = useState("");
  const [groupId, setGroupId] = useState("");
  // Manual entry keeps its own DM id: a browsed project brings its own.
  const [manualOpen, setManualOpen] = useState(false);
  const [manualDm, setManualDm] = useState("");
  const [manualAecdm, setManualAecdm] = useState("");
  const [manualGroup, setManualGroup] = useState("");

  const groupsQuery = useFormaElementGroups(aecdmId || undefined);

  const saved = selectionQuery.data;

  // Seed from the server once it answers, so a reload still shows the
  // selection (and so the manual inputs start from whatever is stored).
  useEffect(() => {
    if (!saved) return;
    setAecdmId(saved.aecdm_project_id);
    setGroupId(saved.element_group_id);
    setManualDm(saved.project_id);
    setManualAecdm(saved.aecdm_project_id);
    setManualGroup(saved.element_group_id);
  }, [saved]);

  const projects = projectsQuery.data?.projects ?? [];
  const browseError = projectsQuery.data?.error ?? null;
  const groups = groupsQuery.data?.groups ?? [];
  const hubName = projectsQuery.data?.hub?.name ?? "";

  function selectProject(nextAecdmId: string) {
    if (nextAecdmId === aecdmId) return;
    setAecdmId(nextAecdmId);
    // Never carry the previous project's model over — it would audit the
    // wrong model with nothing in the output saying so.
    setGroupId("");
  }

  function submit(body: ProjectSelection) {
    save.mutate(body, {
      onSuccess: (data) => {
        toast(
          `${strings.setup.projectSaved} — ${strings.setup.projectSelected(
            data.project_name || data.project_id,
            data.element_group_name || data.element_group_id,
          )}`,
        );
      },
      onError: (err) => {
        toast.error(err instanceof ApiError ? err.detail : String(err));
      },
    });
  }

  function saveBrowsed() {
    const project = projects.find((p) => p.aecdm_id === aecdmId);
    const group = groups.find((g) => g.id === groupId);
    submit({
      hub_id: saved?.hub_id ?? "",
      // A project with no linked DM container reports an empty dm_id; keep
      // whatever DM id is already stored rather than blanking issues access.
      project_id: project?.dm_id || saved?.project_id || "",
      aecdm_project_id: aecdmId,
      element_group_id: groupId,
      project_name: project?.name ?? "",
      element_group_name: group?.name ?? "",
    });
  }

  function saveManual() {
    submit({
      hub_id: saved?.hub_id ?? "",
      project_id: manualDm,
      aecdm_project_id: manualAecdm,
      element_group_id: manualGroup,
      // Typed-in ids carry no names — say so rather than keeping a stale
      // name from a different project.
      project_name: "",
      element_group_name: "",
    });
  }

  return (
    <div className="card flex flex-col gap-3 p-4">
      <div className="text-section-title">{strings.setup.projectTitle}</div>
      <p className="text-caption">{strings.setup.projectNote}</p>
      <span className="text-caption">
        {hubName ? strings.setup.projectHub(hubName) : strings.setup.projectHubUnknown}
      </span>

      {browseError && (
        <div className="flex flex-wrap items-center gap-3">
          <span style={{ color: "var(--fail)" }}>{browseError}</span>
          <Button
            variant="outline"
            size="sm"
            disabled={projectsQuery.isFetching}
            onClick={() => projectsQuery.refetch()}
          >
            {strings.setup.projectRetry}
          </Button>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.setup.projectLabel}</span>
          <Select value={aecdmId} onValueChange={selectProject} disabled={projects.length === 0}>
            <SelectTrigger aria-label={strings.setup.projectLabel}>
              <SelectValue
                placeholder={
                  projectsQuery.isLoading
                    ? strings.setup.projectLoading
                    : strings.setup.projectPlaceholder
                }
              />
            </SelectTrigger>
            <SelectContent>
              {projects.map((p) => (
                // value = the AECDM id, NOT the name (see the doc comment).
                <SelectItem key={p.aecdm_id} value={p.aecdm_id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.setup.projectModelLabel}</span>
          <Select value={groupId} onValueChange={setGroupId} disabled={groups.length === 0}>
            <SelectTrigger aria-label={strings.setup.projectModelLabel}>
              <SelectValue
                placeholder={
                  !aecdmId
                    ? strings.setup.projectModelNeedsProject
                    : groupsQuery.isLoading
                      ? strings.setup.projectModelLoading
                      : groups.length === 0
                        ? strings.setup.projectModelNone
                        : strings.setup.projectModelPlaceholder
                }
              />
            </SelectTrigger>
            <SelectContent>
              {groups.map((g) => (
                <SelectItem key={g.id} value={g.id}>
                  {g.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button disabled={!aecdmId || save.isPending} onClick={saveBrowsed}>
          {save.isPending ? strings.setup.projectSaving : strings.setup.projectSave}
        </Button>
        <span className="text-caption">
          {strings.setup.projectSelected(
            saved?.project_name || saved?.project_id || "",
            saved?.element_group_name || saved?.element_group_id || "",
          )}
        </span>
      </div>

      <Collapsible open={manualOpen} onOpenChange={setManualOpen}>
        <CollapsibleTrigger asChild>
          <button className="flex items-center gap-1 text-[13px] text-[var(--ink-muted)]">
            <ChevronDown
              size={14}
              className={manualOpen ? "rotate-180 transition-transform" : "transition-transform"}
            />
            {strings.setup.projectManualTitle}
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-2 flex flex-col gap-2">
          <p className="text-caption">{strings.setup.projectManualNote}</p>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.setup.projectManualDm}</span>
            <Input value={manualDm} onChange={(e) => setManualDm(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.setup.projectManualAecdm}</span>
            <Input value={manualAecdm} onChange={(e) => setManualAecdm(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.setup.projectManualGroup}</span>
            <Input value={manualGroup} onChange={(e) => setManualGroup(e.target.value)} />
          </label>
          <div>
            <Button variant="outline" disabled={save.isPending} onClick={saveManual}>
              {save.isPending ? strings.setup.projectSaving : strings.setup.projectSave}
            </Button>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
