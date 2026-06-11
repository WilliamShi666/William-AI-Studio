import { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'API Keys | Roys Alpha',
  description: 'Manage your API keys for programmatic access to Roys Alpha',
  openGraph: {
    title: 'API Keys | Roys Alpha',
    description: 'Manage your API keys for programmatic access to Roys Alpha',
    type: 'website',
  },
};

export default async function APIKeysLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
