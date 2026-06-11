'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import { FcGoogle } from "react-icons/fc";
import { Loader2 } from 'lucide-react';

interface GoogleSignInProps {
  returnUrl?: string;
}

export default function GoogleSignIn({ returnUrl }: GoogleSignInProps) {
  const [isLoading, setIsLoading] = useState(false);

  const handleGoogleSignIn = async () => {
    setIsLoading(true);
    // Google登录已禁用，显示消息
    toast.info('Google sign-in is not available. Please use email and password.');
    setIsLoading(false);
  };

  return (
    <button
      onClick={handleGoogleSignIn}
      disabled={isLoading}
      className="w-full h-12 flex items-center justify-center text-sm font-medium tracking-wide rounded-full bg-background text-foreground border border-border hover:bg-accent/30 transition-all duration-200 disabled:opacity-60 disabled:cursor-not-allowed font-sans"
      type="button"
    >
      {isLoading ? (
        <Loader2 className="w-4 h-4 mr-2 animate-spin" />
      ) : (
        <FcGoogle className="w-4 h-4 mr-2" />
      )}
      <span className="font-medium">
        {isLoading ? 'Signing in...' : 'Continue with Google'}
      </span>
    </button>
  );
}