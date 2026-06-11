import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';

import { cn } from '@/lib/utils';

const buttonVariants = cva(
  // 基础样式 - Apple 风格
  `cursor-pointer inline-flex items-center justify-center gap-2
   whitespace-nowrap text-sm font-medium
   transition-all duration-200 ease-out
   disabled:pointer-events-none disabled:opacity-50
   active:scale-[0.98]
   [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4 shrink-0 [&_svg]:shrink-0
   outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2
   aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive`,
  {
    variants: {
      variant: {
        // 主按钮 - 靛蓝紫
        default:
          `bg-primary text-primary-foreground
           shadow-[0_2px_8px_-2px_oklch(0.4_0.015_75/15%)]
           hover:shadow-[0_4px_12px_-2px_oklch(0.4_0.015_75/20%)]
           hover:bg-primary/90
           dark:shadow-none`,
        // 危险按钮
        destructive:
          `bg-destructive text-white
           shadow-[0_2px_8px_-2px_oklch(0.4_0.015_75/15%)]
           hover:shadow-[0_4px_12px_-2px_oklch(0.4_0.015_75/20%)]
           hover:bg-destructive/90
           focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40
           dark:bg-destructive/80 dark:shadow-none`,
        // 轮廓按钮 - 更精致的边框
        outline:
          `border border-border/80 bg-background
           shadow-[0_1px_2px_oklch(0.4_0.015_75/4%)]
           hover:shadow-[0_2px_8px_-2px_oklch(0.4_0.015_75/8%)]
           hover:bg-accent hover:text-accent-foreground hover:border-border
           dark:bg-card dark:border-[oklch(1_0_0/10%)]
           dark:hover:bg-[oklch(0.24_0.02_250)] dark:hover:border-[oklch(1_0_0/15%)]
           dark:shadow-none`,
        // 次要按钮
        secondary:
          `bg-secondary text-secondary-foreground
           shadow-[0_2px_8px_-2px_oklch(0.4_0.015_75/15%)]
           hover:shadow-[0_4px_12px_-2px_oklch(0.4_0.015_75/20%)]
           hover:bg-secondary/90
           dark:shadow-none`,
        // 幽灵按钮
        ghost:
          `hover:bg-accent/80 hover:text-accent-foreground
           dark:hover:bg-accent/50`,
        // 节点轮廓
        node_outline:
          'bg-transparent border border-primary/10',
        // 节点次要
        node_secondary:
          'px-0 bg-transparent hover:opacity-60',
        // 链接样式
        link: 'text-primary underline-offset-4 hover:underline',
      },
      size: {
        default: 'h-10 px-5 py-2 rounded-xl has-[>svg]:px-4',
        sm: 'h-8 rounded-lg gap-1.5 px-4 has-[>svg]:px-3 text-xs',
        lg: 'h-12 rounded-xl px-8 has-[>svg]:px-6 text-base',
        icon: 'size-10 rounded-xl',
        node_secondary: 'px-0',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
);

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<'button'> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot : 'button';

  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
