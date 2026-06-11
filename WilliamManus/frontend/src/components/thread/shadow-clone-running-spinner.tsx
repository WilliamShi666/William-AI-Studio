'use client';

import React from 'react';
import { cn } from '@/lib/utils';

type ShadowCloneRunningSpinnerSize = 'xs' | 'sm' | 'md' | 'lg';

interface ShadowCloneRunningSpinnerProps {
  size?: ShadowCloneRunningSpinnerSize;
  className?: string;
  ringClassName?: string;
}

const WRAPPER_SIZE_CLASS: Record<ShadowCloneRunningSpinnerSize, string> = {
  xs: 'h-3.5 w-3.5',
  sm: 'h-4 w-4',
  md: 'h-5 w-5',
  lg: 'h-6 w-6',
};

const RING_SIZE_CLASS: Record<ShadowCloneRunningSpinnerSize, string> = {
  xs: 'h-3.5 w-3.5 border-[1.5px]',
  sm: 'h-4 w-4 border-[1.75px]',
  md: 'h-5 w-5 border-2',
  lg: 'h-6 w-6 border-2',
};

const SPINNER_ACCELERATION_STYLE = {
  backfaceVisibility: 'hidden' as const,
};

export const ShadowCloneRunningSpinner = React.memo(function ShadowCloneRunningSpinner({
  size = 'sm',
  className,
  ringClassName,
}: ShadowCloneRunningSpinnerProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center justify-center text-blue-600',
        WRAPPER_SIZE_CLASS[size],
        className,
      )}
      aria-hidden="true"
    >
      <span
        className={cn(
          'block animate-shadow-clone-spin rounded-full border-current border-r-transparent will-change-transform [contain:paint]',
          RING_SIZE_CLASS[size],
          ringClassName,
        )}
        style={SPINNER_ACCELERATION_STYLE}
      />
    </span>
  );
});
