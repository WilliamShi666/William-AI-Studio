'use client';

import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useLanguage } from '@/contexts/LanguageContext';

const messagesByLocale = {
  zh: [
    { id: 'thinking', content: '智能体正在思考或工作，请稍后' },
    { id: 'notice', content: '写文件、写代码或执行某些命令可能会花费较长时间，请稍后' },
  ],
  en: [
    { id: 'thinking', content: 'The agent is working, please wait' },
    { id: 'notice', content: 'Writing files or running commands may take a while, please wait' },
  ],
};

export const FileWritingIndicator = () => {
  const { language } = useLanguage();
  const [messageIndex, setMessageIndex] = useState(0);
  const messages = messagesByLocale[language] ?? messagesByLocale.zh;

  useEffect(() => {
    const interval = setInterval(() => {
      setMessageIndex((prev) => (prev + 1) % messages.length);
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="flex items-center gap-3 py-3 px-4 rounded-lg bg-gradient-to-r from-blue-500/5 to-cyan-500/5 border border-blue-500/20">
      {/* 动画小球 */}
      <div className="flex items-center">
        <div className="file-writing-dot shrink-0" aria-hidden="true" />
      </div>

      {/* 提示文字 */}
      <AnimatePresence mode="wait">
        <motion.div
          key={messages[messageIndex].id}
          initial={{ opacity: 0, x: -10 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 10 }}
          transition={{ duration: 0.3 }}
          className="relative overflow-hidden"
        >
          <span className="text-sm font-medium animate-shimmer-text bg-gradient-to-r from-slate-400 via-slate-200 to-slate-400 bg-[length:200%_100%] bg-clip-text text-transparent">
            {messages[messageIndex].content}
          </span>
        </motion.div>
      </AnimatePresence>
    </div>
  );
};
