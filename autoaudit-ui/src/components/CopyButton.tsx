import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { strings } from "@/strings";

export function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label={strings.common.copy}
      onClick={handleCopy}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </Button>
  );
}
