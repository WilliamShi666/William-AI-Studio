interface RoysLogoProps {
  size?: number;
  className?: string;
}

export function RoysLogo({ size = 32, className = '' }: RoysLogoProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      className={className}
    >
      {/* 蓝色背景圆形 */}
      <circle cx="50" cy="50" r="50" fill="#38BDF8" />

      {/* 外部圆环 - 底部右侧有缺口 */}
      <path
        d="M 50 6
           A 44 44 0 1 1 88 72
           L 80 65
           A 35 35 0 1 0 50 15
           Z"
        fill="#1e293b"
      />

      {/* R 字母主体 - 竖线 */}
      <path
        d="M 28 24 L 28 76 L 38 76 L 38 24 Z"
        fill="#1e293b"
      />

      {/* R 字母 - 弧形头部 */}
      <path
        d="M 38 24
           L 55 24
           A 16 16 0 0 1 55 52
           L 38 52
           L 38 43
           L 50 43
           A 7 7 0 0 0 50 33
           L 38 33
           Z"
        fill="#1e293b"
      />

      {/* R 字母 - 斜腿延伸到缺口 */}
      <path
        d="M 45 52
           L 88 88
           L 80 94
           L 80 72
           L 55 52
           Z"
        fill="#1e293b"
      />
    </svg>
  );
}
