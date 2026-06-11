import React from 'react';
import { FolderOpen } from 'lucide-react';
import { getFileIconAndColor } from '@/components/thread/tool-views/utils';
import { cn } from '@/lib/utils';

export type TaskFileEntry = {
  path: string;
  bytes?: number;
};

type TaskFilesSummaryProps = {
  files: TaskFileEntry[];
  onOpenFileViewer: (filePath?: string, filePathList?: string[]) => void;
  title?: string;
  viewAllLabel?: string;
  viewAllSubtitle?: string;
};

const MAX_FILES_TO_SHOW = 5;

const getTypeLabel = (filename: string) => {
  if (!filename.includes('.')) return 'FILE';
  const extension = filename.split('.').pop();
  return extension ? extension.toUpperCase() : 'FILE';
};

export function TaskFilesSummary({
  files,
  onOpenFileViewer,
  title = 'Files created',
  viewAllLabel = 'View all files in this task',
  viewAllSubtitle = 'Opens /workspace',
}: TaskFilesSummaryProps) {
  if (!files.length) return null;

  const visibleFiles = files.slice(0, MAX_FILES_TO_SHOW);
  const hasMore = files.length > MAX_FILES_TO_SHOW;
  const filePaths = files.map((file) => file.path);
  const cardClassName = cn(
    // 布局
    "group flex items-center gap-4 p-4 text-left",
    // 圆角 - Apple 风格更大圆角
    "rounded-xl",
    // 边框和背景
    "border transition-all duration-200",
    // Light mode - 暖色调阴影
    "bg-card border-border/50",
    "hover:border-border hover:shadow-[0_4px_12px_-2px_oklch(0.4_0.015_75/8%)]",
    // Dark mode - 极低透明度玻璃态，柔和白边框
    "dark:bg-[oklch(1_0_0/3%)] dark:border-[oklch(1_0_0/10%)]",
    "dark:hover:bg-[oklch(1_0_0/8%)] dark:hover:border-[oklch(1_0_0/20%)]"
  );

  return (
    <div className="mt-4">
      <div className="text-xs font-medium text-muted-foreground mb-2">{title}</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {visibleFiles.map((file) => {
          const filename = file.path.split('/').pop() || file.path;
          const typeLabel = getTypeLabel(filename);
          const subtitle = typeLabel;
          const { icon: Icon, color, bgColor } = getFileIconAndColor(filename);

          return (
            <button
              key={file.path}
              type="button"
              onClick={() => onOpenFileViewer(file.path, filePaths)}
              className={cardClassName}
            >
              <div className={cn(
                "flex h-10 w-10 items-center justify-center rounded-lg",
                "bg-gradient-to-br from-muted to-muted/50",
                "dark:from-[oklch(0.24_0.02_250)] dark:to-[oklch(0.20_0.02_250/50%)]",
                bgColor, "dark:opacity-80"
              )}>
                <Icon className={cn("h-4 w-4", color, "dark:opacity-80")} />
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold text-foreground truncate">
                  {filename}
                </div>
                <div className="text-xs text-muted-foreground truncate">
                  {subtitle}
                </div>
              </div>
            </button>
          );
        })}

        {hasMore && (
          <button
            type="button"
            onClick={() => onOpenFileViewer(undefined, [])}
            className={cardClassName}
          >
            <div className={cn(
              "flex h-10 w-10 items-center justify-center rounded-lg",
              "border border-dashed border-border",
              "bg-muted/30",
              "dark:border-[oklch(1_0_0/15%)] dark:bg-[oklch(1_0_0/3%)]"
            )}>
              <FolderOpen className="h-4 w-4 text-muted-foreground dark:opacity-80" />
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold text-foreground truncate">
                {viewAllLabel}
              </div>
              <div className="text-xs text-muted-foreground truncate">{viewAllSubtitle}</div>
            </div>
          </button>
        )}
      </div>
    </div>
  );
}
