import { APP_LOGO_SRC, APP_NAME } from "../../constants/branding";

type BrandLogoSize = "sm" | "md" | "lg";
type BrandLogoShape = "rounded" | "circle";

const sizeClasses: Record<BrandLogoSize, string> = {
  sm: "w-8 h-8 rounded-lg",
  md: "w-12 h-12 rounded-xl",
  lg: "w-20 h-20 rounded-2xl",
};

interface BrandLogoProps {
  size?: BrandLogoSize;
  shape?: BrandLogoShape;
  className?: string;
}

export default function BrandLogo({ size = "sm", shape = "rounded", className = "" }: BrandLogoProps) {
  const shapeClass = shape === "circle" ? "!rounded-full" : "";

  return (
    <img
      src={APP_LOGO_SRC}
      alt={`${APP_NAME} logo`}
      className={`${sizeClasses[size]} ${shapeClass} object-cover border border-[color:var(--app-logo-border)] shadow-sm ${className}`.trim()}
      draggable={false}
    />
  );
}
