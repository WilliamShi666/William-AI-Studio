import { Moon, Sun } from "lucide-react";
import { useThemeStore } from "../../stores/themeStore";

export default function ThemeToggle() {
  const { theme, toggleTheme } = useThemeStore();
  const isDark = theme === "dark";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="flex items-center gap-2 w-full px-3 py-2 rounded-lg text-sm text-[color:var(--app-text-muted)] hover:text-[color:var(--app-text)] hover:bg-[color:var(--app-hover)] transition-colors"
      title={isDark ? "切换浅色模式" : "切换深色模式"}
      aria-label={isDark ? "切换浅色模式" : "切换深色模式"}
    >
      {isDark ? <Sun size={15} /> : <Moon size={15} />}
      <span>{isDark ? "Light mode" : "Dark mode"}</span>
    </button>
  );
}
