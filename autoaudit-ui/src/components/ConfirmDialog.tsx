import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { strings } from "@/strings";

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  confirmLabel?: string;
  destructive?: boolean;
  loading?: boolean;
  onConfirm: () => void;
}

/** Visual language #4: confirm dialog for EVERY write action. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  destructive,
  loading,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        {description && <DialogDescription>{description}</DialogDescription>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {strings.common.cancel}
          </Button>
          <Button
            variant={destructive ? "destructive" : "primary"}
            disabled={loading}
            onClick={onConfirm}
          >
            {confirmLabel ?? strings.common.confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
