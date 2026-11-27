export function getMimeTypeFromFileName(fileName: string): string {
  const extension = fileName.split('.').pop()?.toLowerCase();

  const mimeTypes: Record<string, string> = {
    // Documents
    pdf: 'application/pdf',
    doc: 'application/msword',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    xls: 'application/vnd.ms-excel',
    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ppt: 'application/vnd.ms-powerpoint',
    pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    potx: 'application/vnd.openxmlformats-officedocument.presentationml.template',
    txt: 'text/plain',
    md: 'text/markdown',
    json: 'application/json',
    tsv: 'text/tab-separated-values',
    csv: 'text/csv',

    // Images
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    png: 'image/png',
    gif: 'image/gif',
    bmp: 'image/bmp',
    svg: 'image/svg+xml',
    webp: 'image/webp',

    // Archives
    zip: 'application/zip',
    rar: 'application/x-rar-compressed',

    // Media
    mp4: 'video/mp4',
    mp3: 'audio/mpeg',
    wav: 'audio/wav',
  };

  return mimeTypes[extension || ''] || 'application/octet-stream';
}
