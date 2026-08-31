import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-8 w-full rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface)] px-2 text-[13px] text-[var(--ink)] placeholder:text-[var(--ink-muted)] focus-visible:border-[var(--primary)]",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
