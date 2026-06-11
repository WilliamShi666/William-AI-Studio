import type { PreparedAttachment } from '@/lib/api';

type UploadedFileLike = {
  name?: string;
  path?: string;
  type?: string;
  preparedAttachment?: PreparedAttachment;
};

export type ImageMediaRef = {
  kind: 'image';
  path: string;
  mime_type: string;
  filename: string;
  sha256?: string;
};

const IMAGE_FILE_EXTENSIONS = /\.(png|jpe?g|gif|bmp|webp)$/i;

function isWorkspacePath(path: string): boolean {
  return path.startsWith('/workspace/');
}

function isImageLike(mimeType: string | undefined, filename: string | undefined): boolean {
  const normalizedMime = (mimeType || '').trim().toLowerCase();
  if (normalizedMime.startsWith('image/')) {
    return true;
  }
  return IMAGE_FILE_EXTENSIONS.test((filename || '').trim());
}

function filenameFromPath(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts[parts.length - 1] || 'uploaded_image';
}

function normalizeImageMediaRef(file: UploadedFileLike): ImageMediaRef | null {
  const preparedAttachment = file.preparedAttachment;
  const rawPath = (preparedAttachment?.path || file.path || '').trim();
  if (!rawPath || !isWorkspacePath(rawPath)) {
    return null;
  }

  const rawFilename =
    preparedAttachment?.filename ||
    preparedAttachment?.name ||
    file.name ||
    filenameFromPath(rawPath);
  const rawMime =
    preparedAttachment?.mime_type ||
    preparedAttachment?.content_type ||
    file.type ||
    '';

  if (!isImageLike(rawMime, rawFilename)) {
    return null;
  }

  const mimeType = rawMime.trim().toLowerCase().startsWith('image/')
    ? rawMime.trim().toLowerCase()
    : 'image/png';
  const mediaRef: ImageMediaRef = {
    kind: 'image',
    path: rawPath,
    mime_type: mimeType,
    filename: rawFilename || filenameFromPath(rawPath),
  };

  const sha256 = (preparedAttachment?.sha256 || '').trim();
  return sha256 ? { ...mediaRef, sha256 } : mediaRef;
}

export function buildUploadedImageMediaRefs(
  uploadedFiles: UploadedFileLike[],
): ImageMediaRef[] {
  const seenPaths = new Set<string>();
  const refs: ImageMediaRef[] = [];

  for (const file of uploadedFiles) {
    const mediaRef = normalizeImageMediaRef(file);
    if (!mediaRef || seenPaths.has(mediaRef.path)) {
      continue;
    }
    seenPaths.add(mediaRef.path);
    refs.push(mediaRef);
  }

  return refs;
}
