import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { strings } from "@/strings";
import { useReferences, useSaveReference } from "@/api/hooks";
import type { ReferenceEntryDict } from "@/api/types";
import { toast } from "sonner";

/** `canonical = alias1, alias2` per line — mirrors app.py `_parse_reference_lines`. */
function parseEntries(raw: string): ReferenceEntryDict[] {
  const out: ReferenceEntryDict[] = [];
  for (const rawLine of raw.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const [canonicalRaw, aliasesRaw] = line.split("=");
    const canonical = canonicalRaw?.trim();
    if (!canonical) continue;
    const aliases = (aliasesRaw ?? "")
      .split(",")
      .map((a) => a.trim())
      .filter(Boolean);
    out.push({ canonical, aliases });
  }
  return out;
}

export interface ReferenceEditorProps {
  onSaved: (name: string) => void;
}

/** Inline "create a new reference set" editor (v1.4-K21 membership-in-a-
 *  set). Same text-line editing convention as LookupEditor. */
export function ReferenceEditor({ onSaved }: ReferenceEditorProps) {
  const { data } = useReferences();
  const save = useSaveReference();
  const [name, setName] = useState("");
  const [entriesRaw, setEntriesRaw] = useState(
    "Oak = white oak, wood-oak\nSteel-Brushed = brushed steel",
  );
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);

  const entries = parseEntries(entriesRaw);
  const existingNames = new Set((data?.references ?? []).map((r) => r.name));
  const errors: string[] = [];
  if (!name.trim()) errors.push("Set name required");
  if (entries.length === 0) errors.push("Need at least 1 canonical value");

  function doSave(overwrite: boolean) {
    save.mutate(
      { name: name.trim(), body: { entries, overwrite } },
      {
        onSuccess: () => {
          toast(`Saved reference.${name.trim()}.yaml`);
          onSaved(name.trim());
          setConfirmOverwrite(false);
        },
        onError: (err) => toast.error(String(err)),
      },
    );
  }

  function handleSave() {
    if (existingNames.has(name.trim())) {
      setConfirmOverwrite(true);
      return;
    }
    doSave(false);
  }

  return (
    <div className="flex flex-col gap-2 rounded-[var(--radius)] border border-[var(--border)] p-2">
      <label className="flex flex-col gap-1">
        <span className="text-caption">Set name (slug)</span>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="approved_materials" />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-caption">Canonical values — one `canonical = alias1, alias2` per line</span>
        <Textarea rows={4} value={entriesRaw} onChange={(e) => setEntriesRaw(e.target.value)} className="font-mono-val" />
      </label>
      {errors.length > 0 && (
        <ul className="text-caption text-[var(--warn)]">
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      <Button
        size="sm"
        className="w-fit"
        disabled={errors.length > 0 || save.isPending}
        onClick={handleSave}
      >
        {strings.common.save}
      </Button>

      <ConfirmDialog
        open={confirmOverwrite}
        onOpenChange={setConfirmOverwrite}
        title={strings.builder.overwriteTitle}
        description={`config/reference.${name.trim()}.yaml already exists with different content. Overwrite it?`}
        loading={save.isPending}
        onConfirm={() => doSave(true)}
      />
    </div>
  );
}
