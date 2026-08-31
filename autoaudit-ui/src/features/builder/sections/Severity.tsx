import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { strings } from "@/strings";
import type { SeverityLevel } from "@/api/types";
import type { RuleFormState } from "../lib/ruleForm";

const LEVELS: SeverityLevel[] = ["severity_low", "severity_medium", "severity_high"];

export interface SeveritySectionProps {
  state: RuleFormState;
  onChange: (patch: Partial<RuleFormState>) => void;
}

/** "3 Severity" — a plain importance level, decoupled from the check kind
 *  (v1.4-K10). */
export function SeveritySection({ state, onChange }: SeveritySectionProps) {
  return (
    <section className="card flex flex-col gap-3 p-3">
      <div className="text-section-title">{strings.builder.severityTitle}</div>
      <label className="flex max-w-xs flex-col gap-1">
        <Select
          value={state.severityLevel}
          onValueChange={(v) => onChange({ severityLevel: v as SeverityLevel })}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LEVELS.map((lvl) => (
              <SelectItem key={lvl} value={lvl}>
                {strings.builder.severityOptions[lvl]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>
    </section>
  );
}
