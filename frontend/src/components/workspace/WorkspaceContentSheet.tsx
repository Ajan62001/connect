"use client";

import { ContentStudio } from "@/components/content/ContentStudio";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

/** A wide slide-over hosting the full content pipeline scoped to one workspace:
 * generate grounded posts, then review / edit / schedule / publish — without
 * leaving the workspace. */
export function WorkspaceContentSheet({
  workspaceId,
  name,
  open,
  onOpenChange,
}: {
  workspaceId: number;
  name: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full data-[side=right]:sm:max-w-3xl">
        <SheetHeader>
          <SheetTitle>Content studio</SheetTitle>
          <SheetDescription>
            Generate, review and publish posts grounded in “{name}”.
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {open ? <ContentStudio workspaceId={workspaceId} /> : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
