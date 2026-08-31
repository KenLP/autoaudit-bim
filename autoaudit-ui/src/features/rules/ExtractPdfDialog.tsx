import { useRef, useState } from "react";
import { toast } from "sonner";
import { Upload } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { MonoText } from "@/components/MonoText";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { strings } from "@/strings";
import { useExtractPdf, useSaveRuleset } from "@/api/hooks";
import { ApiError } from "@/api/client";
import type { RuleSetDict } from "@/api/types";

export interface ExtractPdfDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called after a successful save — caller navigates/refreshes. */
  onSaved?: (path: string) => void;
}

type Stage = "idle" | "review";

/** Rules library → "Extract from PDF": upload a spec PDF, review the
 *  extracted rule set + warnings, then save it as a new rules file. Never
 *  re-implements the extraction itself (B10) — one round-trip to
 *  `POST /api/extraction/pdf`, then reuses the same save path as the Rule
 *  Builder (`useSaveRuleset`, 409 → overwrite confirm). */
export function ExtractPdfDialog({ open, onOpenChange, onSaved }: ExtractPdfDialogProps) {
  const [stage, setStage] = useState<Stage>("idle");
  const [file, setFile] = useState<File | null>(null);
  const [ruleset, setRuleset] = useState<RuleSetDict | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [scenario, setScenario] = useState("");
  const [maxSections, setMaxSections] = useState("");
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const extractPdf = useExtractPdf();
  const saveRuleset = useSaveRuleset();

  function reset() {
    setStage("idle");
    setFile(null);
    setRuleset(null);
    setWarnings([]);
    setScenario("");
    setMaxSections("");
    setConfirmOverwrite(false);
    extractPdf.reset();
    saveRuleset.reset();
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
  }

  function handleChooseFile() {
    fileRef.current?.click();
  }

  function handleFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) setFile(f);
  }

  function handleExtract() {
    if (!file) return;
    const parsed = parseInt(maxSections, 10);
    const cap = maxSections.trim() !== "" && parsed >= 1 ? parsed : undefined;
    extractPdf.mutate(
      { file, maxSections: cap },
      {
        onSuccess: (res) => {
          setRuleset(res.ruleset);
          setWarnings(res.warnings);
          setStage("review");
        },
      },
    );
  }

  function doSave(overwrite: boolean) {
    if (!ruleset || !scenario.trim()) return;
    saveRuleset.mutate(
      { scenario: scenario.trim(), ruleset: { ...ruleset, scenario: scenario.trim() }, overwrite },
      {
        onSuccess: (res) => {
          const path = res.path ?? `config/rules.${scenario.trim()}.yaml`;
          toast(strings.extractPdf.savedBody(path));
          setConfirmOverwrite(false);
          onSaved?.(path);
          handleOpenChange(false);
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            setConfirmOverwrite(true);
          } else {
            toast.error(err instanceof ApiError ? err.detail : String(err));
          }
        },
      },
    );
  }

  const notInstalled =
    extractPdf.isError && extractPdf.error instanceof ApiError && extractPdf.error.status === 503;
  const parseFailed =
    extractPdf.isError && extractPdf.error instanceof ApiError && extractPdf.error.status === 422;
  const otherError =
    extractPdf.isError && !notInstalled && !parseFailed;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogTitle>{strings.extractPdf.title}</DialogTitle>
        <DialogDescription>{strings.extractPdf.description}</DialogDescription>

        {stage === "idle" && (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <Button variant="outline" onClick={handleChooseFile}>
                <Upload size={14} />
                {strings.extractPdf.chooseFile}
              </Button>
              {file && <MonoText>{strings.extractPdf.selectedFile(file.name)}</MonoText>}
              <input
                ref={fileRef}
                type="file"
                accept=".pdf"
                className="hidden"
                onChange={handleFileSelected}
              />
            </div>

            <label className="flex max-w-xs flex-col gap-1">
              <span className="text-caption">{strings.extractPdf.maxSections}</span>
              <Input
                value={maxSections}
                onChange={(e) => setMaxSections(e.target.value.replace(/[^0-9]/g, ""))}
                inputMode="numeric"
                placeholder={strings.extractPdf.maxSectionsPlaceholder}
              />
              <span className="text-caption">{strings.extractPdf.maxSectionsHint}</span>
            </label>

            {extractPdf.isPending && (
              <div className="card flex flex-col gap-1 p-3 text-[13px]">
                <span>{strings.extractPdf.extracting}</span>
                <span className="text-caption">{strings.extractPdf.extractingHint}</span>
              </div>
            )}

            {notInstalled && (
              <div
                role="alert"
                className="card flex flex-col gap-1 border-[var(--fail)] p-3 text-[var(--fail)]"
              >
                <span className="font-medium">{strings.extractPdf.notInstalledTitle}</span>
                <span>
                  {extractPdf.error instanceof ApiError ? extractPdf.error.detail : ""}
                </span>
              </div>
            )}

            {parseFailed && (
              <div
                role="alert"
                className="card flex flex-col gap-1 border-[var(--fail)] p-3 text-[var(--fail)]"
              >
                <span className="font-medium">{strings.extractPdf.parseFailedTitle}</span>
                <span>
                  {extractPdf.error instanceof ApiError ? extractPdf.error.detail : ""}
                </span>
              </div>
            )}

            {otherError && (
              <div role="alert" className="card border-[var(--fail)] p-3 text-[var(--fail)]">
                {extractPdf.error instanceof ApiError
                  ? extractPdf.error.detail
                  : String(extractPdf.error)}
              </div>
            )}

            <DialogFooter>
              <Button variant="outline" onClick={() => handleOpenChange(false)}>
                {strings.common.cancel}
              </Button>
              <Button disabled={!file || extractPdf.isPending} onClick={handleExtract}>
                {strings.extractPdf.extract}
              </Button>
            </DialogFooter>
          </div>
        )}

        {stage === "review" && ruleset && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <div className="text-section-title">
                {strings.extractPdf.reviewCount(ruleset.rules.length)}
              </div>
              <div className="max-h-64 overflow-y-auto">
                <table className="w-full text-left text-[13px]">
                  <thead>
                    <tr className="h-8 border-b border-[var(--border)] text-caption">
                      <th className="px-2">{strings.extractPdf.columnRuleId}</th>
                      <th className="px-2">{strings.extractPdf.columnCategory}</th>
                      <th className="px-2">{strings.extractPdf.columnParameter}</th>
                      <th className="px-2">{strings.extractPdf.columnRequirement}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ruleset.rules.map((r) => (
                      <tr key={r.id} className="h-[30px] border-t border-[var(--border)]">
                        <td className="px-2">
                          <MonoText>{r.id}</MonoText>
                        </td>
                        <td className="px-2">{r.category ?? "—"}</td>
                        <td className="px-2 font-mono-val">{r.parameter}</td>
                        <td className="px-2">{r.requirement}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {warnings.length > 0 && (
              <div className="flex flex-col gap-1">
                <div className="text-caption font-medium">{strings.extractPdf.warningsTitle}</div>
                <ul className="list-inside list-disc text-caption text-[var(--warn)]">
                  {warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            )}

            <label className="flex max-w-sm flex-col gap-1">
              <span className="text-caption">{strings.extractPdf.scenario}</span>
              <Input
                value={scenario}
                onChange={(e) => setScenario(e.target.value)}
                placeholder={strings.extractPdf.scenarioPlaceholder}
              />
              <span className="text-caption">{strings.extractPdf.scenarioHint(scenario)}</span>
            </label>

            <DialogFooter>
              <Button variant="outline" onClick={reset}>
                {strings.extractPdf.startOver}
              </Button>
              <Button
                disabled={!scenario.trim() || saveRuleset.isPending}
                onClick={() => doSave(false)}
              >
                {saveRuleset.isPending ? strings.extractPdf.saving : strings.extractPdf.save}
              </Button>
            </DialogFooter>
          </div>
        )}

        <ConfirmDialog
          open={confirmOverwrite}
          onOpenChange={setConfirmOverwrite}
          title={strings.extractPdf.overwriteTitle}
          description={strings.extractPdf.overwriteBody}
          loading={saveRuleset.isPending}
          onConfirm={() => doSave(true)}
        />
      </DialogContent>
    </Dialog>
  );
}
