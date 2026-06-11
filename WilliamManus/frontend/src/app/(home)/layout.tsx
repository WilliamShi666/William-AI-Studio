import { Navbar } from '@/components/home/sections/navbar';
import { ParticleField } from '@/components/home/ui/particle-field';

export default function HomeLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <div className="w-full min-h-screen relative landing-page-text bg-[#050b1a] text-white overflow-hidden">
      <ParticleField />
      <div className="block w-px h-full border-l border-border fixed top-0 left-6 z-10"></div>
      <div className="block w-px h-full border-r border-border fixed top-0 right-6 z-10"></div>
      <Navbar />
      {children}
    </div>
  );
}
