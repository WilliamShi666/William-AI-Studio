'use client';

import posthog from 'posthog-js';
import { useEffect } from 'react';

export const PostHogIdentify = () => {
  useEffect(() => {
    // 移除认证追踪，使用匿名分析
    console.log('PostHog tracking without authentication');
    // 可选：设置匿名用户ID
    // posthog.identify('anonymous-user', { mode: 'local' });
  }, []);

  return null;
};
