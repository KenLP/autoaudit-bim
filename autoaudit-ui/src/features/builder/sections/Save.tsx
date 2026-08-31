import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Download } from "lucide-react";
import { toast } from "sonner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { strings } from "@/strings";
import { useIdsExport, useRulesFiles, useSaveRuleset } from "@/api/hooks";
import { ApiError } from "@/api/client";
import { basename } from "@/lib/path";
import type { RuleSetDict } from "@/api/types";

const NEW_SCENARIO = "__new__";

export interface SaveSectionProps {
  /** Scenario is filled in by this component before submit — callers pass
   *  everything else (target_category, rules, geometry_rules). */
  buildRuleset: (scenario: string) => RuleSetDict;
  disabled: boolean;
  scenario: string;
  onScenarioChange: (name: string) => void;
}

/** "5 Save" — pick/type a scenario (= config/rules.<scenario>.yaml), save
 *  (with an overwrite confirm on 409), and export the single-rule IDS. */
export function SaveSection({ buildRuleset, disabled, scenario, onScenarioChange }: SaveSectionProps) {
  const { data: filesData } = useRulesFiles();
  const saveRuleset = useSaveRuleset();
  const idsExport = useIdsExport();
  const navigate = useNavigate();
  const [creatingNew, setCreatingNew] = useState(!scenario);
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const [savedPath, setSavedPath] = useState<string | null>(null);

  const files = filesData?.files ?? [];

  function doSave(overwrite: boolean) {
    if (!scenario.trim()) return;
    saveRuleset.mutate(
      { scenario: scenario.trim(), ruleset: buildRuleset(scenario.trim()), overwrite },
      {
        onSuccess: (res) => {
          const path = res.path ?? `config/rules.${scenario.trim()}.yaml`;
          setSavedPath(path);
          toast(strings.builder.savedBody(path));
          setConfirmOverwrite(false);
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            setConfirmOverwrite(true);
          } else {
            toast.error(String(err));
          }
        },
      },
    );
  }

  function handleExportIds() {
    idsExport.mutate(buildRuleset(scenario.trim() || "rule"), {
      onSuccess: (xml) => {
        const blob = new Blob([xml], { type: "application/xml" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `rules.${scenario.trim() || "rule"}.ids`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
      },
      onError: (err) => toast.error(String(err)),
    });
  }

  return (
    <section className="card flex flex-col gap-3 p-3">
      <div className="text-section-title">{strings.builder.saveTitle}</div>
      <label className="flex max-w-sm flex-col gap-1">
        <span className="text-caption">{strings.builder.scenario}</span>
        {creatingNew ? (
          <Input
            value={scenario}
            onChange={(e) => onScenarioChange(e.target.value)}
            placeholder={strings.builder.scenarioNewPlaceholder}
          />
        ) : (
          <Select
            value={scenario || undefined}
            onValueChange={(v) => {
              if (v === NEW_SCENARIO) {
                setCreatingNew(true);
                onScenarioChange("");
                return;
              }
              onScenarioChange(v);
            }}
          >
            <SelectTrigger>
              <SelectValue placeholder={strings.builder.scenario} />
            </SelectTrigger>
            <SelectContent>
              {files.map((f) => (
                <SelectItem key={f.scenario} value={f.scenario}>
                  {f.scenario} ({basename(f.path)})
                </SelectItem>
              ))}
              <SelectItem value={NEW_SCENARIO}>{strings.builder.scenarioNewPlaceholder}</SelectItem>
            </SelectContent>
          </Select>
        )}
        <span className="text-caption">{strings.builder.scenarioHint(scenario)}</span>
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          disabled={disabled || !scenario.trim() || saveRuleset.isPending}
          onClick={() => doSave(false)}
        >
          {saveRuleset.isPending ? strings.builder.saving : strings.builder.save}
        </Button>
        <Button variant="outline" disabled={disabled || idsExport.isPending} onClick={handleExportIds}>
          <Download size={14} />
          {strings.builder.exportIds}
        </Button>
        {savedPath && (
          <Button variant="link" onClick={() => navigate("/rules")}>
            {strings.builder.openRules}
          </Button>
        )}
      </div>

      <ConfirmDialog
        open={confirmOverwrite}
        onOpenChange={setConfirmOverwrite}
        title={strings.builder.overwriteTitle}
        description={strings.builder.overwriteBody}
        loading={saveRuleset.isPending}
        onConfirm={() => doSave(true)}
      />
    </section>
  );
}
