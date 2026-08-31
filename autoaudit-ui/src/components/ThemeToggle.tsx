import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { strings } from "@/strings";

type Theme = "light" | "dark";

const STORAGE_KEY = "autoaudit-theme";

function systemPrefersDark(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem(STORAGE_KEY) as Theme | null;
    return stored ?? (systemPrefersDark() ? "dark" : "light");
  });

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  function toggle() {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      localStorage.setItem(STORAGE_KEY, next);
      return next;
    });
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={strings.topbar.theme}
      onClick={toggle}
    >
      {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </Button>
  );
}
