import { motion, AnimatePresence } from 'motion/react';
import { AlertTriangle, CheckCircle, Info, XCircle } from 'lucide-react';

interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  type?: 'warning' | 'success' | 'error' | 'info';
  confirmText?: string;
  cancelText?: string;
}

export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  type = 'warning',
  confirmText = '确定',
  cancelText = '取消',
}: ConfirmDialogProps) {
  if (!isOpen) return null;

  const getIcon = () => {
    switch (type) {
      case 'warning':
        return <AlertTriangle size={48} className="text-[#ffb800]" />;
      case 'success':
        return <CheckCircle size={48} className="text-[#34D399]" />;
      case 'error':
        return <XCircle size={48} className="text-[#ff3b5c]" />;
      case 'info':
        return <Info size={48} className="text-[#38BDF8]" />;
    }
  };

  const getGradient = () => {
    switch (type) {
      case 'warning':
        return 'from-[#ffb800] to-[#ff8c00]';
      case 'success':
        return 'from-[#34D399] to-[#10B981]';
      case 'error':
        return 'from-[#ff3b5c] to-[#ff1744]';
      case 'info':
        return 'from-[#38BDF8] to-[#0EA5E9]';
    }
  };

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
        <motion.div
          initial={{ opacity: 0, scale: 0.9, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.9, y: 20 }}
          transition={{ duration: 0.3, ease: 'easeOut' }}
          className="glass gradient-border rounded-2xl p-8 w-full max-w-md mx-4 relative overflow-hidden"
        >
          {/* Background shimmer effect */}
          <div className="absolute inset-0 opacity-5">
            <div className={`absolute inset-0 bg-gradient-to-br ${getGradient()} blur-3xl`} />
          </div>

          {/* Content */}
          <div className="relative z-10">
            {/* Icon */}
            <div className="flex justify-center mb-6">
              <motion.div
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                transition={{ delay: 0.1, type: 'spring', stiffness: 200 }}
              >
                {getIcon()}
              </motion.div>
            </div>

            {/* Title */}
            <h3 className="text-2xl text-[#E2E8F0] text-center mb-4">{title}</h3>

            {/* Message */}
            <p className="text-[#94A3B8] text-center mb-8 whitespace-pre-line">{message}</p>

            {/* Buttons */}
            <div className="flex gap-3">
              <motion.button
                onClick={onClose}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                className="flex-1 px-6 py-3 border-2 border-[#38BDF8] text-[#38BDF8] rounded-xl hover:bg-[rgba(56,189,248,0.1)] transition-all relative overflow-hidden group"
              >
                <span className="relative z-10">{cancelText}</span>
              </motion.button>
              <motion.button
                onClick={() => {
                  onConfirm();
                  onClose();
                }}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                className={`flex-1 px-6 py-3 bg-gradient-to-r ${getGradient()} text-[#0F172A] rounded-xl hover:shadow-[0_0_20px_rgba(56,189,248,0.5)] transition-all relative overflow-hidden group`}
              >
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
                <span className="relative z-10">{confirmText}</span>
              </motion.button>
            </div>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
