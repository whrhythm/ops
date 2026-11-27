import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  Button,
  OGDialog,
  OGDialogContent,
  OGDialogDescription,
  OGDialogTitle,
} from '@librechat/client';
import { apiBaseUrl } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';
import { useGetStartupConfig } from '~/data-provider';
import PersonalBrowser from './Input/Files/PersonalBrowser';
import SavePersonal from './Input/Files/SavePersonal';
import { correctScanWord } from './OfficeWordPreview';

type Region = { polygon: [number, number][] };
type Cell = Region & { text: string; review_required: boolean };
type Block = Region & {
  block_id: string;
  label: string;
  text?: string;
  rows?: string[][];
  cells?: Cell[][];
  review_reasons: string[];
  review_marks?: (Region & { kind: string; text: string })[];
};
type Scan = {
  status: 'running' | 'complete' | 'failed';
  digest: string;
  result?: { editable_text_bookmarks?: boolean; pages: { page: number; blocks: Block[] }[] };
};

function scanRoute(operation: string, conversation: string, query: Record<string, string> = {}) {
  return `${apiBaseUrl()}/api/files/personal/${operation}?${new URLSearchParams({ conversation, ...query })}`;
}

export default function OfficeScan({ conversationId }: { conversationId?: string | null }) {
  const { data: config } = useGetStartupConfig();
  const { token, user } = useAuthContext();
  if (
    !config?.structuredOcr ||
    !config.personalFiles ||
    !token ||
    !user ||
    !conversationId ||
    !/^[a-f0-9-]{36}$/.test(conversationId)
  )
    return null;
  return <ScanDialog conversation={conversationId} token={token} userId={user.id} />;
}

function ScanDialog({
  conversation,
  token,
  userId,
}: {
  conversation: string;
  token: string;
  userId: string;
}) {
  const t = useLocalize();
  const [open, setOpen] = useState(false);
  const [folder, setFolder] = useState('');
  const [path, setPath] = useState('');
  const [pageRange, setPageRange] = useState('');
  const [selecting, setSelecting] = useState(false);
  const [page, setPage] = useState(1);
  const [focus, setFocus] = useState<Region>();
  const [image, setImage] = useState('');
  const originalPage = useRef<HTMLDivElement>(null);
  const [previewError, setPreviewError] = useState(false);
  const [editor, setEditor] = useState<{ key: string; original: string }>();
  const [revisions, setRevisions] = useState<{
    digest?: string;
    edits: Record<string, string>;
    revision?: string;
  }>({
    edits: {},
  });
  const headers = { Authorization: `Bearer ${token}` };
  const scan = useQuery<Scan | null>({
    queryKey: ['structured-ocr', userId, conversation],
    enabled: open,
    retry: false,
    queryFn: async () => {
      const response = await fetch(scanRoute('scan', conversation), { headers });
      if (response.status === 404 || response.status === 410) return null;
      if (!response.ok) throw new Error('scan_unavailable');
      return response.json();
    },
    refetchInterval: (value) => (value?.status === 'running' ? 2000 : false),
  });
  const start = useMutation({
    mutationFn: async () => {
      const pages = /\.pdf$/i.test(path) ? pageRange.replace(/\s+/g, '').replaceAll('，', ',') : '';
      const response = await fetch(
        scanRoute('scan', conversation, { path, ...(pages ? { pages } : {}) }),
        {
          headers,
          method: 'POST',
        },
      );
      if (!response.ok) throw new Error('scan_unavailable');
    },
    onSuccess: () => {
      setSelecting(false);
      setPage(1);
      setFocus(undefined);
      void scan.refetch();
    },
  });
  const digest = scan.data?.status === 'complete' ? scan.data.digest : undefined;
  const activePage =
    scan.data?.result?.pages.find((entry) => entry.page === page) ?? scan.data?.result?.pages[0];
  const shownPage = activePage?.page ?? page;
  const word = digest ? scanRoute('scan-word', conversation, { digest }) : undefined;
  const edits = revisions.digest === digest ? revisions.edits : {};
  const editable = scan.data?.result?.editable_text_bookmarks === true;
  function openEditor(region: Region, key: string, original: string) {
    locate(region);
    setEditor({ key, original });
  }
  function revise(key: string, value: string, original: string) {
    const revision = crypto.randomUUID();
    setRevisions((previous) => {
      const next = previous.digest === digest ? { ...previous.edits } : {};
      if (value === original) delete next[key];
      else next[key] = value;
      return { digest, edits: next, revision };
    });
  }
  useEffect(() => {
    setEditor(undefined);
  }, [digest, shownPage, selecting]);
  function locate(region: Region) {
    setFocus(region);
    originalPage.current?.scrollIntoView({ block: 'nearest' });
  }
  useEffect(() => {
    setImage('');
    setPreviewError(false);
    if (!open || !digest || selecting) return;
    const controller = new AbortController();
    let url: string | undefined;
    fetch(scanRoute('scan-page', conversation, { digest, page: String(shownPage) }), {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error('preview_unavailable');
        const body = await response.blob();
        if (controller.signal.aborted) return;
        url = URL.createObjectURL(body);
        setImage(url);
      })
      .catch(() => {
        if (!controller.signal.aborted) setPreviewError(true);
      });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [open, selecting, digest, shownPage, conversation, token]);

  return (
    <div className="text-text-primary relative mt-14 mr-auto ml-3 shrink-0 [details+&]:mt-2">
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
        {t('com_scan_title')}
      </Button>
      <OGDialog open={open} onOpenChange={setOpen}>
        <OGDialogContent
          className="flex flex-col overflow-hidden"
          style={{ maxHeight: '92dvh', width: 'min(96vw, 64rem)', maxWidth: '96vw' }}
        >
          <OGDialogTitle>{t('com_scan_title')}</OGDialogTitle>
          <OGDialogDescription>{t('com_scan_description')}</OGDialogDescription>
          {(scan.isLoading || scan.data?.status === 'running') && (
            <p role="status">{t('com_scan_running')}</p>
          )}
          {(scan.isError || scan.data?.status === 'failed' || start.isError) && (
            <p role="alert">{t('com_scan_failed')}</p>
          )}
          {!scan.isLoading && scan.data?.status !== 'running' && (!digest || selecting) && (
            <>
              <PersonalBrowser
                path={folder}
                onNavigate={setFolder}
                onFile={(file) => {
                  setPath(file);
                  setPageRange('');
                }}
                selected={path}
                disabled={start.isLoading}
                extension={['pdf', 'png', 'jpg', 'jpeg']}
              />
              {/\.pdf$/i.test(path) && (
                <label className="flex items-center gap-2 text-sm">
                  {t('com_scan_page')}
                  <input
                    className="rounded border p-1"
                    value={pageRange}
                    placeholder={t('com_scan_range_hint')}
                    maxLength={100}
                    onChange={(event) => setPageRange(event.target.value)}
                  />
                </label>
              )}
              <div className="flex justify-end gap-2">
                {digest && (
                  <Button
                    variant="ghost"
                    disabled={start.isLoading}
                    onClick={() => {
                      start.reset();
                      setSelecting(false);
                    }}
                  >
                    {t('com_ui_back')}
                  </Button>
                )}
                <Button disabled={!path || start.isLoading} onClick={() => start.mutate()}>
                  {t('com_scan_start')}
                </Button>
              </div>
            </>
          )}
          {digest && !selecting && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  aria-label={t('com_scan_page')}
                  value={shownPage}
                  onChange={(event) => {
                    setPage(Number(event.target.value));
                    setFocus(undefined);
                  }}
                >
                  {scan.data?.result?.pages.map((entry) => (
                    <option key={entry.page} value={entry.page}>
                      {t('com_scan_page')} {entry.page}
                    </option>
                  ))}
                </select>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setPath('');
                    setSelecting(true);
                  }}
                >
                  {t('com_scan_choose')}
                </Button>
                {word && (
                  <SavePersonal
                    files={[
                      {
                        filename: 'scan.docx',
                        file_id: Object.keys(edits).length ? revisions.revision : undefined,
                        filepath: word,
                        load: async () => {
                          const response = await fetch(word, { headers });
                          if (!response.ok) throw new Error('word_unavailable');
                          return correctScanWord(await response.blob(), edits);
                        },
                      },
                    ]}
                  />
                )}
              </div>
              <div className="grid min-h-0 gap-4 overflow-auto md:grid-cols-2">
                <div>
                  {previewError && <p role="alert">{t('com_ui_preview_unavailable')}</p>}
                  {image && (
                    <div className="relative" ref={originalPage}>
                      <img
                        src={image}
                        alt={`${t('com_scan_page')} ${shownPage}`}
                        className="block w-full"
                      />
                      {focus && (
                        <span
                          className="pointer-events-none absolute inset-0 bg-amber-500/45"
                          style={{
                            clipPath: `polygon(${focus.polygon.map(([x, y]) => `${x * 100}% ${y * 100}%`).join(',')})`,
                          }}
                        />
                      )}
                    </div>
                  )}
                </div>
                <div className="space-y-3 overflow-auto md:max-h-[55dvh]">
                  {editor && editable && (
                    <label className="block text-sm">
                      {t('com_scan_correction')}
                      <textarea
                        className="w-full rounded border p-2"
                        maxLength={4000}
                        value={edits[editor.key] ?? editor.original}
                        onChange={(event) =>
                          revise(editor.key, event.target.value, editor.original)
                        }
                      />
                    </label>
                  )}
                  {activePage?.blocks
                    .filter(
                      (block) =>
                        block.text ||
                        block.rows?.length ||
                        (block.review_reasons.length && !['image', 'chart'].includes(block.label)),
                    )
                    .map((block) => {
                      const key = 'scan_' + block.block_id.replace('-', '_');
                      const canCorrect =
                        editable &&
                        !block.label.includes('formula') &&
                        ![
                          'table',
                          'image',
                          'chart',
                          'header',
                          'footer',
                          'number',
                          'page_number',
                        ].includes(block.label);
                      return (
                        <div
                          key={block.block_id}
                          className="border-border-light space-y-1 border-b pb-2 text-sm"
                        >
                          <button
                            className="text-left break-words underline"
                            onClick={() =>
                              canCorrect ? openEditor(block, key, block.text ?? '') : locate(block)
                            }
                          >
                            {block.review_reasons.some((reason) => reason !== 'degraded_scan') && (
                              <strong className="mr-2">{t('com_scan_review')}</strong>
                            )}
                            {(edits[key] ?? block.text) ||
                              t(block.rows?.length ? 'com_scan_table' : 'com_scan_unreadable')}
                          </button>
                          {block.rows && (
                            <div className="overflow-x-auto">
                              <table>
                                <tbody>
                                  {block.rows.map((row, rowIndex) => (
                                    <tr key={rowIndex}>
                                      {row.map((value, columnIndex) => {
                                        const cell = block.cells?.[rowIndex]?.[columnIndex];
                                        const cellKey = key + '_r' + rowIndex + '_c' + columnIndex;
                                        return (
                                          <td
                                            key={columnIndex}
                                            className="border-border-light border p-1"
                                          >
                                            <button
                                              onClick={() =>
                                                editable
                                                  ? openEditor(cell ?? block, cellKey, value)
                                                  : locate(cell ?? block)
                                              }
                                            >
                                              {edits[cellKey] ?? value}
                                              {cell?.review_required ? ' ＊' : ''}
                                            </button>
                                          </td>
                                        );
                                      })}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                          {block.review_marks
                            ?.filter((mark) => mark.kind === 'number')
                            .map((mark, index) => (
                              <Button
                                key={index}
                                size="sm"
                                variant="ghost"
                                onClick={() => locate(mark)}
                              >
                                {mark.text}
                              </Button>
                            ))}
                        </div>
                      );
                    })}
                </div>
              </div>
            </>
          )}
        </OGDialogContent>
      </OGDialog>
    </div>
  );
}
