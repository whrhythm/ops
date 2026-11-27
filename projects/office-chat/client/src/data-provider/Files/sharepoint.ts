import { getMimeTypeFromFileName } from '~/utils/fileMime';
import { useMutation } from '@tanstack/react-query';
import { getClientLimit } from 'librechat-data-provider';
import type { UseMutationResult } from '@tanstack/react-query';

export interface SharePointFile {
  id: string;
  name: string;
  size: number;
  webUrl: string;
  downloadUrl: string;
  driveId: string;
  itemId: string;
  sharePointItem: any;
}

export interface SharePointBatchProgress {
  completed: number;
  total: number;
  currentFile?: string;
  failed: string[];
}

export const useSharePointBatchDownload = (): UseMutationResult<
  File[],
  unknown,
  {
    files: SharePointFile[];
    accessToken: string;
    onProgress?: (progress: SharePointBatchProgress) => void;
  },
  unknown
> => {
  return useMutation({
    mutationFn: async ({ files, accessToken, onProgress }) => {
      const downloadedFiles: File[] = [];
      const failed: string[] = [];
      let completed = 0;

      const concurrencyLimit = 3;
      const chunks: SharePointFile[][] = [];
      for (let i = 0; i < files.length; i += concurrencyLimit) {
        chunks.push(files.slice(i, i + concurrencyLimit));
      }

      for (const chunk of chunks) {
        const chunkPromises = chunk.map(async (file) => {
          try {
            const downloadUrl =
              file.downloadUrl ||
              `https://graph.microsoft.com/v1.0/drives/${file.driveId}/items/${file.itemId}/content`;

            const controller = new AbortController();
            const abortTimer = setTimeout(() => controller.abort(), getClientLimit('sharepointMs'));
            let response: Response;
            let blob: Blob;
            try {
              response = await fetch(downloadUrl, {
                headers: {
                  Authorization: `Bearer ${accessToken}`,
                },
                signal: controller.signal,
              });

              if (!response.ok) {
                throw new Error(`${response.status} ${response.statusText}`);
              }

              blob = await response.blob();
            } finally {
              clearTimeout(abortTimer);
            }
            const contentType =
              response.headers.get('content-type') || getMimeTypeFromFileName(file.name);

            const downloadedFile = new File([blob], file.name, {
              type: contentType,
              lastModified: Date.now(),
            });

            completed++;
            onProgress?.({
              completed,
              total: files.length,
              currentFile: file.name,
              failed,
            });

            return downloadedFile;
          } catch (error) {
            console.error('SharePoint file download failed');
            failed.push(file.name);
            completed++;
            onProgress?.({
              completed,
              total: files.length,
              currentFile: `Error: ${file.name}`,
              failed,
            });
            throw error;
          }
        });

        const chunkResults = await Promise.allSettled(chunkPromises);

        chunkResults.forEach((result) => {
          if (result.status === 'fulfilled') {
            downloadedFiles.push(result.value);
          }
        });
      }

      if (failed.length > 0) {
        console.warn(`Failed to download ${failed.length} files`);
      }

      return downloadedFiles;
    },
    retry: 1,
  });
};
