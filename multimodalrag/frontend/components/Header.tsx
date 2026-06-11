import { Bell, User, Activity } from 'lucide-react';
import { motion } from 'motion/react';

interface HeaderProps {
  title: string;
}

export function Header({ title }: HeaderProps) {
  return (
    <header className="h-16 glass absolute top-0 left-0 right-0 z-40 border-b border-[rgba(56,189,248,0.15)] transition-all duration-300">
      <div className="h-full px-6 flex items-center justify-between">
        <motion.h2 
          className="text-[#E2E8F0]"
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3 }}
        >
          {title}
        </motion.h2>
        
        <div className="flex items-center gap-3">
          {/* Activity Indicator */}
          <motion.div
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg glass border border-[rgba(56,189,248,0.2)]"
            whileHover={{ scale: 1.05 }}
          >
            <Activity size={14} className="text-[#34D399]" />
            <span className="text-xs text-[#94A3B8]">运行中</span>
          </motion.div>

          <motion.button 
            className="w-10 h-10 rounded-xl glass-strong flex items-center justify-center transition-all duration-300 hover:bg-[rgba(56,189,248,0.1)] hover:shadow-[0_0_15px_rgba(56,189,248,0.3)] relative group"
            whileHover={{ scale: 1.1 }}
            whileTap={{ scale: 0.95 }}
          >
            <Bell size={18} className="text-[#94A3B8] group-hover:text-[#38BDF8] transition-colors" />
            <div className="absolute top-2 right-2 w-2 h-2 bg-[#ff3b5c] rounded-full animate-pulse" />
          </motion.button>
          
          <motion.button 
            className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center shadow-[0_0_20px_rgba(56,189,248,0.4)] hover:shadow-[0_0_30px_rgba(56,189,248,0.6)] transition-all duration-300"
            whileHover={{ scale: 1.1 }}
            whileTap={{ scale: 0.95 }}
          >
            <User size={18} className="text-[#0F172A]" />
          </motion.button>
        </div>
      </div>
    </header>
  );
}
