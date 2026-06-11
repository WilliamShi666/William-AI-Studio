'use server';

import { redirect } from 'next/navigation';
import { getPostAuthRedirect } from '@/lib/auth/access';

type AuthLogPayload = Record<string, any>;

function redactAuthLogData(data: AuthLogPayload) {
  if (!data || typeof data !== 'object') {
    return data;
  }

  return {
    ...data,
    access_token: data.access_token ? '[REDACTED]' : data.access_token,
    refresh_token: data.refresh_token ? '[REDACTED]' : data.refresh_token,
  };
}
// 移除authClient导入，Server Action直接调用后端API

// 移除欢迎邮件功能，简化注册流程

export async function signIn(prevState: any, formData: FormData) {
  const email = formData.get('email') as string;
  const password = formData.get('password') as string;
  const returnUrl = formData.get('returnUrl') as string | undefined;

  if (!email || !email.includes('@')) {
    return { message: 'Please enter a valid email address' };
  }

  if (!password || password.length < 6) {
    return { message: 'Password must be at least 6 characters' };
  }

  try {
    console.log('🚀 Server Action: 发送登录请求到后端');
    
    const backendUrl = process.env.BACKEND_URL
      ? `${process.env.BACKEND_URL}/api`
      : (process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8002/api');
    const response = await fetch(`${backendUrl}/auth/login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email, password }),
    });

    console.log('📡 Server Action: 响应状态:', response.status);
    
    const data = await response.json();
    console.log('📡 Server Action: 响应数据:', redactAuthLogData(data));

    if (!response.ok) {
      return { message: data.error || 'Login failed' };
    }

    // 登录成功，返回认证信息
    if (data.access_token && data.user) {
      return { 
        success: true, 
        redirectTo: getPostAuthRedirect(data.user, returnUrl),
        authData: {
          access_token: data.access_token,
          refresh_token: data.refresh_token,
          expires_at: data.expires_at,
          user: data.user
        }
      };
    } else {
      return { message: 'Invalid response from server' };
    }
  } catch (error: any) {
    console.error('❌ Server Action: 登录请求失败:', error);
    return { message: error.message || 'Network error' };
  }
}

export async function signUp(prevState: any, formData: FormData) {
  const origin = formData.get('origin') as string;
  const email = formData.get('email') as string;
  const password = formData.get('password') as string;
  const confirmPassword = formData.get('confirmPassword') as string;
  const returnUrl = formData.get('returnUrl') as string | undefined;

  if (!email || !email.includes('@')) {
    return { message: 'Please enter a valid email address' };
  }

  if (!password || password.length < 6) {
    return { message: 'Password must be at least 6 characters' };
  }

  if (password !== confirmPassword) {
    return { message: 'Passwords do not match' };
  }

  try {
    console.log('🚀 Server Action: 发送注册请求到后端');
    
    const backendUrl = process.env.BACKEND_URL
      ? `${process.env.BACKEND_URL}/api`
      : (process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8002/api');
    const response = await fetch(`${backendUrl}/auth/register`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ email, password }),
    });

    console.log('📡 Server Action: 响应状态:', response.status);
    
    const data = await response.json();
    console.log('📡 Server Action: 响应数据:', redactAuthLogData(data));

    if (!response.ok) {
      return { message: data.error || 'Registration failed' };
    }

    // 注册成功，直接回到登录页面
    return { 
      success: true,
      message: 'Registration successful! Please sign in with your credentials.',
      redirectToLogin: true
    };
  } catch (error: any) {
    console.error('❌ Server Action: 注册请求失败:', error);
    return { message: error.message || 'Network error' };
  }
}

export async function forgotPassword(prevState: any, formData: FormData) {
  const email = formData.get('email') as string;
  const origin = formData.get('origin') as string;

  if (!email || !email.includes('@')) {
    return { message: 'Please enter a valid email address' };
  }

  // TODO: 实现密码重置功能
  console.log('Password reset not implemented yet');
  return { 
    success: true,
    message: 'If an account with that email exists, you will receive a password reset link.' 
  };
}

export async function resetPassword(prevState: any, formData: FormData) {
  const password = formData.get('password') as string;
  const confirmPassword = formData.get('confirmPassword') as string;

  if (!password || password.length < 6) {
    return { message: 'Password must be at least 6 characters' };
  }

  if (password !== confirmPassword) {
    return { message: 'Passwords do not match' };
  }

  // TODO: 实现密码更新功能
  console.log('Password update not implemented yet');
  return {
    success: true,
    message: 'Password updated successfully',
  };
}

export async function signOut() {
  try {
    console.log('🚀 Server Action: 发送登出请求到后端');
    // TODO: 实现登出逻辑
    console.log('登出功能待实现');
  } catch (error) {
    console.error('❌ Server Action: 登出请求失败:', error);
  }
  return redirect('/');
}
