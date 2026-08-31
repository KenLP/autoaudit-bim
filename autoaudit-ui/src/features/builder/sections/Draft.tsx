import { useRef, useState } from "react";
import { Sparkles, Upload } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { strings } from "@/strings";
import { useDraftRule, useIdsImport } from "@/api/hooks";
import { ApiError } from "@/api/client";
import type { RuleDict } from "@/api/types";

export interface DraftSectionProps {
  onApplyDraft: (rule: RuleDict, warnings: string[]) => void;
}

/** "Draft from text" + "Import IDS" — the only two ways to seed the form
 *  besides starting from scratch or loading an existing rule. Neither
 *  reimplements any grounding/parsing logic client-side (B10): both are a
 *  single round-trip to the server. */
export function DraftSection({ onApplyDraft }: DraftSectionProps) {
  const [text, setText] = useState("");
  const draft = useDraftRule();
  const idsImport = useIdsImport();
  const fileRef = useRef<HTMLInputElement>(null);

  const missingKey =
    draft.isError && draft.error instanceof ApiError && draft.error.status === 503;

  function handleGenerate() {
    if (!text.trim()) return;
    draft.mutate(
      { text: text.trim() },
      {
        onSuccess: (res) => onApplyDraft(res.rule, res.warnings),
        onError: (err) => {
          if (!(err instanceof ApiError && err.status === 503)) toast.error(String(err));
        },
      },
    );
  }

  function handleImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    idsImport.mutate(file, {
      onSuccess: (res) => {
        const [first, ...rest] = res.ruleset.rules;
        if (!first) {
          toast.error("IDS file had no parameter rules to import");
          return;
        }
        onApplyDraft(
          first,
          rest.length > 0
            ? [`Imported ${res.rule_count} rules — showing the first; save this one, then import again for the rest.`]
            : [],
        );
      },
      onError: (err) => toast.error(String(err)),
    });
  }

  return (
    <section className="card flex flex-col gap-2 p-3">
      <div className="text-section-title">{strings.builder.draftTitle}</div>
      <Textarea
        rows={3}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={strings.builder.draftPlaceholder}
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={handleGenerate} disabled={!text.trim() || draft.isPending || missingKey}>
          <Sparkles size={14} />
          {draft.isPending ? strings.builder.generating : strings.builder.generate}
        </Button>
        <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={idsImport.isPending}>
          <Upload size={14} />
          {strings.builder.importIds}
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept=".ids,.xml"
          className="hidden"
          onChange={handleImport}
        />
      </div>
      {missingKey && <p className="text-caption">{strings.builder.generateMissingKey}</p>}
    </section>
  );
}
