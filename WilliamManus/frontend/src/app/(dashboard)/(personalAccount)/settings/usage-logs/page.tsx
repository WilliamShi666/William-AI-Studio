import { createClient } from '@/lib/supabase/server';
import UsageLogs from '@/components/billing/usage-logs';
import { isBillingUiEnabled } from '@/lib/config';

export default async function UsageLogsPage() {
  if (!isBillingUiEnabled()) {
    return (
      <div className="text-sm text-muted-foreground">
        功能暂未开放。
      </div>
    );
  }
  const supabaseClient = await createClient();
  const { data: personalAccount } = await supabaseClient.rpc(
    'get_personal_account',
  );

  if (!personalAccount) {
    return <div>Account not found</div>;
  }

  return (
    <div className="space-y-6">
      <UsageLogs accountId={(personalAccount as { account_id?: string; id: string }).account_id || (personalAccount as { id: string }).id} />
    </div>
  );
}
