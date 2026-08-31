import * as React from "react";
import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

export const Checkbox = React.forwardRef<
  React.ElementRef<typeof CheckboxPrimitive.Root>,
  React.ComponentProps<typeof CheckboxPrimitive.Root>
>(({ className, ...props }, ref) => (
  <CheckboxPrimitive.Root
    ref={ref}
    className={cn(
      "h-4 w-4 shrink-0 rounded-[4px] border border-[var(--border)] bg-[var(--surface)] data-[state=checked]:bg-[var(--primary)] data-[state=checked]:border-[var(--primary)] flex items-center justify-center",
      className,
    )}
    {...props}
  >
    <CheckboxPrimitive.Indicator className="text-[var(--primary-ink)]">
      <Check size={12} />
    </CheckboxPrimitive.Indicator>
  </CheckboxPrimitive.Root>
));
Checkbox.displayName = "Checkbox";
