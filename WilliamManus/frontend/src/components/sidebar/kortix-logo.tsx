'use client';

import Image from 'next/image';

interface KortixLogoProps {
  size?: number;
  className?: string;
}
export function KortixLogo({ size = 24, className }: KortixLogoProps) {
  return (
    <span
      className={`relative inline-flex shrink-0 overflow-hidden rounded ${className ?? ''}`}
      style={{ width: size, height: size, minWidth: size, minHeight: size }}
    >
      <Image
        src="/666666666.png"
        alt="Roys Alpha Logo"
        fill
        sizes={`${size}px`}
        className="object-cover"
        style={{ transform: 'scale(1.15)', transformOrigin: 'center' }}
      />
    </span>
  );
}
