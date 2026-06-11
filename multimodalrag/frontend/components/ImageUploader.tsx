import { useRef, useState, type DragEvent } from 'react';
import { FileText, Image as ImageIcon, UploadCloud, X } from 'lucide-react';
import { motion } from 'motion/react';

export type UploadKind = 'image' | 'pdf';

export interface UploadItem {
  id: string;
  name: string;
  size: number;
  type: string;
  kind: UploadKind;
  dataUrl: string;
  previewUrl?: string;
}

interface ImageUploaderProps {
  files: UploadItem[];
  onChange: (files: UploadItem[]) => void;
  allowPdf?: boolean;
  disabled?: boolean;
}

const readFileAsDataUrl = (file: File): Promise<string> => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
};

export function ImageUploader({ files, onChange, allowPdf = true, disabled = false }: ImageUploaderProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [isDragging, setIsDragging] = useState(false);

  const handleFiles = async (list: FileList | null) => {
    if (!list) return;

    const selections = Array.from(list).filter((file) => {
      if (file.type.startsWith('image/')) return true;
      if (allowPdf && file.type === 'application/pdf') return true;
      return false;
    });
    const items = await Promise.all(
      selections.map(async (file) => {
        const dataUrl = await readFileAsDataUrl(file);
        const isImage = file.type.startsWith('image/');
        return {
          id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          name: file.name,
          size: file.size,
          type: file.type,
          kind: isImage ? 'image' : 'pdf',
          dataUrl,
          previewUrl: isImage ? dataUrl : undefined,
        } as UploadItem;
      }),
    );

    onChange([...files, ...items]);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (disabled) return;
    setIsDragging(false);
    void handleFiles(event.dataTransfer.files);
  };

  const handleBrowse = () => {
    if (disabled) return;
    inputRef.current?.click();
  };

  const handleRemove = (id: string) => {
    onChange(files.filter((file) => file.id !== id));
  };

  const accept = allowPdf ? 'image/*,application/pdf' : 'image/*';

  return (
    <div className="space-y-3">
      <div
        className={`border border-dashed rounded-xl p-4 transition-all ${
          isDragging ? 'border-[#00d4ff] bg-[rgba(0,212,255,0.08)]' : 'border-[rgba(0,212,255,0.2)]'
        } ${disabled ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer hover:border-[rgba(0,212,255,0.5)]'}`}
        onDragEnter={() => setIsDragging(true)}
        onDragLeave={() => setIsDragging(false)}
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
        onClick={handleBrowse}
      >
        <div className="flex items-center gap-3 text-sm text-[#94a3b8]">
          <UploadCloud size={18} className="text-[#00d4ff]" />
          <span>{allowPdf ? '拖拽图片/PDF到这里，或点击选择文件' : '拖拽图片到这里，或点击选择文件'}</span>
        </div>
      </div>

      <input
        ref={inputRef}
        type="file"
        multiple
        accept={accept}
        className="hidden"
        onChange={(event) => void handleFiles(event.target.files)}
      />

      {files.length > 0 && (
        <div className="flex flex-wrap gap-3">
          {files.map((file) => (
            <motion.div
              key={file.id}
              className="relative flex items-center gap-2 rounded-lg bg-[rgba(15,23,42,0.6)] border border-[rgba(0,212,255,0.2)] px-3 py-2"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
            >
              {file.kind === 'image' ? (
                <div className="w-10 h-10 rounded-md overflow-hidden border border-[rgba(0,212,255,0.2)]">
                  {file.previewUrl ? (
                    <img src={file.previewUrl} alt={file.name} className="w-full h-full object-cover" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center text-[#00d4ff]">
                      <ImageIcon size={16} />
                    </div>
                  )}
                </div>
              ) : (
                <div className="w-10 h-10 rounded-md border border-[rgba(0,212,255,0.2)] flex items-center justify-center text-[#00d4ff]">
                  <FileText size={18} />
                </div>
              )}
              <div className="text-xs text-[#e2e8f0] max-w-[160px] truncate">
                {file.name}
              </div>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  handleRemove(file.id);
                }}
                className="absolute -top-2 -right-2 w-6 h-6 rounded-full bg-[#0f172a] border border-[rgba(0,212,255,0.3)] flex items-center justify-center"
              >
                <X size={12} className="text-[#94a3b8]" />
              </button>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}
