import * as React from 'react';

import { cn } from '@/lib/utils';

function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        // 基础样式
        'flex h-11 w-full min-w-0 px-4 py-2',
        'bg-input text-foreground',
        'placeholder:text-muted-foreground',
        'text-base md:text-sm',
        // 圆角 - Apple 风格
        'rounded-xl',
        // 边框
        'border border-border/80',
        // 阴影 - 轻微内陷感 (暖色调)
        'shadow-[0_1px_2px_oklch(0.4_0.015_75/4%)]',
        // 过渡
        'transition-all duration-200',
        // 焦点状态 - 靛蓝紫环
        'focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-ring',
        'focus-visible:shadow-[0_2px_8px_-2px_oklch(0.4_0.015_75/8%)]',
        // Dark mode - 柔和白边框
        'dark:bg-[oklch(0.22_0.018_250)] dark:border-[oklch(1_0_0/10%)]',
        'dark:focus-visible:border-primary dark:focus-visible:ring-ring',
        'dark:shadow-none',
        // 文件输入
        'file:text-foreground file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium',
        // 选择文本
        'selection:bg-primary selection:text-primary-foreground',
        // 禁用和错误状态
        'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
        'aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive',
        'outline-none',
        className,
      )}
      {...props}
    />
  );
}

export { Input };
