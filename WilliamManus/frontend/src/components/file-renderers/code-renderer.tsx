'use client';

import React, { useEffect, useState } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { vscodeDark } from '@uiw/codemirror-theme-vscode';
import { langs } from '@uiw/codemirror-extensions-langs';
import { cn } from '@/lib/utils';
import { ScrollArea } from '@/components/ui/scroll-area';
import { xcodeLight } from '@uiw/codemirror-theme-xcode';
import { useTheme } from 'next-themes';
import { EditorView } from '@codemirror/view';

interface CodeRendererProps {
  content: string;
  language?: string;
  className?: string;
}

// Map of language aliases to CodeMirror language support
const langsAny = langs as any;
const languageMap: Record<string, any> = {
  js: langsAny.javascript,
  jsx: langsAny.jsx,
  ts: langsAny.typescript,
  tsx: langsAny.tsx,
  html: langsAny.html,
  css: langsAny.css,
  json: langsAny.json,
  md: langsAny.markdown,
  python: langsAny.python,
  py: langsAny.python,
  rust: langsAny.rust,
  go: langsAny.go,
  java: langsAny.java,
  c: langsAny.c,
  cpp: langsAny.cpp,
  cs: langsAny.csharp,
  php: langsAny.php,
  ruby: langsAny.ruby,
  sh: langsAny.shell,
  bash: langsAny.shell,
  sql: langsAny.sql,
  yaml: langsAny.yaml,
  yml: langsAny.yaml,
  // Add more languages as needed
};

export function CodeRenderer({
  content,
  language = '',
  className,
}: CodeRendererProps) {
  // Get current theme
  const { resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  // Set mounted state to true after component mounts
  useEffect(() => {
    setMounted(true);
  }, []);

  // Determine the language extension to use
  const langExtension =
    language && languageMap[language] ? [languageMap[language]()] : [];

  // Add line wrapping extension
  const extensions = [...langExtension, EditorView.lineWrapping];

  // Select the theme based on the current theme
  const theme = mounted && resolvedTheme === 'dark' ? vscodeDark : xcodeLight;

  return (
    <ScrollArea className={cn('w-full h-full', className)}>
      <div className="w-full">
        <CodeMirror
          value={content}
          theme={theme}
          extensions={extensions}
          basicSetup={{
            lineNumbers: false,
            highlightActiveLine: false,
            highlightActiveLineGutter: false,
            foldGutter: false,
          }}
          editable={false}
          className="text-sm w-full min-h-full"
          style={{ maxWidth: '100%' }}
          height="auto"
        />
      </div>
    </ScrollArea>
  );
}
