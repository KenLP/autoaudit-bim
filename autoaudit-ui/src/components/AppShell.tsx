import { Outlet } from "react-router-dom";
import { Toaster } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { useApprovals } from "@/api/hooks";

export function AppShell() {
  const { data: approvals } = useApprovals();

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex h-full">
        <Sidebar pendingApprovals={approvals?.counts.pending ?? 0} />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />
          <main className="min-h-0 flex-1 overflow-y-auto">
            <Outlet />
          </main>
        </div>
      </div>

      <Toaster position="bottom-right" />
    </TooltipProvider>
  );
}
