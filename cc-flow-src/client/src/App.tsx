import { useEffect } from "react";
import AppLayout from "./components/layout/AppLayout";
import ErrorBoundary from "./components/shared/ErrorBoundary";
import { useWebSocket } from "./hooks/useWebSocket";
import { useThemeStore } from "./stores/themeStore";

export default function App() {
  // Connect WebSocket (always, regardless of projectPath)
  useWebSocket();
  const initializeTheme = useThemeStore((state) => state.initializeTheme);

  useEffect(() => {
    initializeTheme();
  }, [initializeTheme]);

  return (
    <ErrorBoundary scope="App">
      <AppLayout />
    </ErrorBoundary>
  );
}
