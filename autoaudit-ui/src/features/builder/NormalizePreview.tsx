import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { MonoText } from "@/components/MonoText";
import { strings } from "@/strings";
import { usePreviewNormalize } from "@/api/hooks";
import type { NormalizeKindOption } from "./lib/ruleForm";

const SAMPLE_DEFAULT: Partial<Record<NormalizeKindOption, string>> = {
  duration: "180 MIN",
  fire_rating: "180 MIN",
  length: "2.4 m",
  area: "120 sf",
  family_name: "ADSK-Fur-Chair",
  template: "ADSK Fur Chair Viper",
};

export interface NormalizePreviewProps {
  normalizeKind: NormalizeKindOption;
  normalizeFormat: string;
  normalizeMap: Record<string, string>;
  normalizeSource: string;
  normalizeReference: string;
  pattern: string | null;
}

/** Debounced (400ms) live preview — every normalize change debounces
 *  400ms then POSTs preview. Never re-implements the normalizer in TS (B10):
 *  every render is a round-trip to `POST /api/builder/preview`, which
 *  calls the real `policies.normalize` / `policies.reference`. */
export function NormalizePreview({
  normalizeKind,
  normalizeFormat,
  normalizeMap,
  normalizeSource,
  normalizeReference,
  pattern,
}: NormalizePreviewProps) {
  const [sample, setSample] = useState(SAMPLE_DEFAULT[normalizeKind] ?? "");
  const preview = usePreviewNormalize();

  useEffect(() => {
    setSample(SAMPLE_DEFAULT[normalizeKind] ?? "");
  }, [normalizeKind]);

  // S-08: `normalizeMap` is a fresh object each render, so the dep list
  // compares it BY VALUE — deliberate, and it works. It was written inline as
  // `JSON.stringify(normalizeMap)`, which the linter cannot check statically:
  // it has to give up on the whole array, so a genuinely missing dependency
  // here would also go unreported. Hoisting the expression restores the check
  // and changes nothing about when the effect fires.
  const normalizeMapKey = JSON.stringify(normalizeMap);
  useEffect(() => {
    if (!sample.trim() || (normalizeKind === "auto" && !pattern)) return;
    const handle = setTimeout(() => {
      preview.mutate({
        normalize_kind: normalizeKind,
        normalize_format: normalizeFormat || null,
        normalize_map: normalizeKind === "map" ? normalizeMap : null,
        normalize_source: normalizeKind === "template" ? normalizeSource : null,
        reference: normalizeKind === "reference" ? normalizeReference : null,
        sample,
        pattern,
      });
    }, 400);
    return () => clearTimeout(handle);
    // `preview` (the mutation handle) and `normalizeMap` (compared via
    // `normalizeMapKey`) are intentionally out of the list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    sample,
    normalizeKind,
    normalizeFormat,
    normalizeMapKey,
    normalizeSource,
    normalizeReference,
    pattern,
  ]);

  const data = preview.data;

  return (
    <div className="flex flex-col gap-1.5 rounded-[var(--radius)] border border-[var(--border)] p-2">
      <div className="flex items-center gap-2">
        <span className="text-caption">{strings.builder.previewTitle}</span>
        <Input
          value={sample}
          onChange={(e) => setSample(e.target.value)}
          className="h-7 w-44"
          aria-label={strings.builder.previewSample}
        />
      </div>
      {data && (
        <div className="flex items-center gap-1.5 text-[13px]">
          {data.error ? (
            <span className="text-[var(--fail)]">
              {strings.builder.previewError}: {data.error}
            </span>
          ) : data.output === null ? (
            <span className="text-[var(--warn)]">{strings.builder.previewNone}</span>
          ) : (
            <>
              <MonoText>
                {sample} → {data.output}
              </MonoText>
              {data.matches === true && <Check size={14} className="text-[var(--ok)]" />}
              {data.matches === false && <X size={14} className="text-[var(--fail)]" />}
            </>
          )}
        </div>
      )}
    </div>
  );
}
