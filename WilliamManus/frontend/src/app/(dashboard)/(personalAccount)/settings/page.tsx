import EditPersonalAccountName from '@/components/basejump/edit-personal-account-name';
import { createClient } from '@/lib/supabase/server';

export default async function PersonalAccountSettingsPage() {
  const supabaseClient = await createClient();
  const { data: personalAccount } = await supabaseClient.rpc(
    'get_personal_account',
  );

  if (!personalAccount) {
    return <div>Account not found</div>;
  }

  return (
    <div>
      <EditPersonalAccountName account={personalAccount} />
    </div>
  );
}
