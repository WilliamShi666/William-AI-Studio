import React from 'react';
import { Info } from 'lucide-react';
import { UpgradeDialog as UnifiedUpgradeDialog } from '@/components/ui/upgrade-dialog';
import { useLanguage } from '@/contexts/LanguageContext';

interface UpgradeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDismiss: () => void;
}

export function UpgradeDialog({ open, onOpenChange, onDismiss }: UpgradeDialogProps) {
  const { t } = useLanguage();

  return (
    <UnifiedUpgradeDialog
      open={open}
      onOpenChange={onOpenChange}
      icon={Info}
      title={t('notice.unavailableTitle')}
      description={t('notice.unavailableDescription')}
      theme="primary"
      size="sm"
      preventOutsideClick={true}
      actions={[
        {
          label: t('common.gotIt'),
          onClick: onDismiss,
          variant: "outline",
        }
      ]}
    >
      <div className="py-4" />
    </UnifiedUpgradeDialog>
  );
} 
