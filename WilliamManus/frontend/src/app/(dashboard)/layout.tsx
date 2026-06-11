import DashboardLayoutContent from '@/components/dashboard/layout-content';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: {
    absolute: 'Roys Alpha',
  },
};

interface DashboardLayoutProps {
  children: React.ReactNode;
}

export default function DashboardLayout({
  children,
}: DashboardLayoutProps) {
  return <DashboardLayoutContent>{children}</DashboardLayoutContent>;
}
