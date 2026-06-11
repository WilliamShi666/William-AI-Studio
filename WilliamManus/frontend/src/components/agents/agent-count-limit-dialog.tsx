'use client';

import React from 'react';
import { AlertTriangle } from 'lucide-react';
import { UpgradeDialog } from '@/components/ui/upgrade-dialog';
import { PricingSection } from '@/components/home/sections/pricing-section';
import { isBillingUiEnabled } from '@/lib/config';
import { useLanguage } from '@/contexts/LanguageContext';

interface AgentCountLimitDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentCount: number;
  limit: number;
  tierName: string;
}

export const AgentCountLimitDialog: React.FC<AgentCountLimitDialogProps> = ({
  open,
  onOpenChange,
  currentCount,
  limit,
  tierName,
}) => {
  const billingUiEnabled = isBillingUiEnabled();
  const { language } = useLanguage();
  const returnUrl = typeof window !== 'undefined' ? window.location.href : '/';

  return (
    <UpgradeDialog
      open={open}
      onOpenChange={onOpenChange}
      icon={AlertTriangle}
      title={language === 'zh' ? '代理数量已达上限' : 'Agent limit reached'}
      description={language === 'zh'
        ? '当前代理数量已达到上限。'
        : "You've reached the maximum number of agents allowed."}
      theme="warning"
      size={billingUiEnabled ? "xl" : "sm"}
      className="[&_.grid]:!grid-cols-4 [&_.grid]:gap-3 mt-8"
    >
      {billingUiEnabled ? (
        <PricingSection 
          returnUrl={returnUrl} 
          showTitleAndTabs={false} 
          insideDialog={true} 
          showInfo={false}
          noPadding={true}
        />
      ) : (
        <div className="text-sm text-muted-foreground">
          {language === 'zh'
            ? '请删除或归档不需要的代理后再继续创建。'
            : 'Remove or archive unused agents before creating new ones.'}
        </div>
      )}
    </UpgradeDialog>
  );
}; 
