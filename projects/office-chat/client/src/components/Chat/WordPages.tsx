import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { atom, useAtom, useSetAtom } from 'jotai';
import { useRecoilValue, useResetRecoilState } from 'recoil';
import { activeSubagentPanel } from '@librechat/chat/react';
import { apiBaseUrl } from 'librechat-data-provider';
import { Button } from '@librechat/client';
import { useAuthContext, useLocalize } from '~/hooks';
import { useGetStartupConfig } from '~/data-provider';
import store from '~/store';

type WordFile = {
  filename: string;
  load: () => Promise<Blob>;
  onOpen?: () => void;
  fallback?: ReactNode;
};
type Selection = Pick<WordFile, 'filename' | 'load' | 'fallback'> & {
  conversationId: string | null;
  opener: HTMLElement;
};

const selectedWord = atom<Selection | null>(null);

export default function WordPages({ filename, load, onOpen, fallback }: WordFile) {
  const { data } = useGetStartupConfig();
  const t = useLocalize();
  const conversationId = useRecoilValue(store.conversationIdByIndex(0)) ?? null;
  const resetArtifact = useResetRecoilState(store.currentArtifactId);
  const clearSubagent = useSetAtom(activeSubagentPanel);
  const select = useSetAtom(selectedWord);
  if (!data?.wordPages && !fallback) return null;
  return (
    <Button variant="outline" onClick={(event) => {
      resetArtifact();
      clearSubagent(null);
      select({ filename, load, fallback: data?.wordPages ? undefined : fallback,
        conversationId, opener: event.currentTarget });
      onOpen?.();
    }}>{t('com_ui_preview_word')}</Button>
  );
}

export function useWordSidePanel(conversationId: string | null, otherPanelOpen: boolean) {
  const [file, select] = useAtom(selectedWord);
  const visible = file != null && file.conversationId === conversationId && !otherPanelOpen;
  useEffect(() => {
    if (file && !visible) select(null);
  }, [file, visible, select]);
  useEffect(() => () => { select(null); }, [select]);
  if (!visible) return null;
  return <WordReader file={file} close={() => {
    select(null);
    if (file.opener.isConnected) file.opener.focus();
  }} />;
}

function WordReader({ file, close }: { file: Selection; close: () => void }) {
  const { token } = useAuthContext();
  const t = useLocalize();
  const [result, setResult] = useState<{ file: Selection; pages?: string[]; failed?: boolean }>();
  const [attempt, retry] = useState(0);
  const [zoom, setZoom] = useState(100);
  const title = useRef<HTMLHeadingElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  useEffect(() => {
    title.current?.focus({ preventScroll: true });
    setZoom(100);
    viewport.current?.scrollTo(0, 0);
  }, [file]);
  useEffect(() => {
    if (file.fallback) return;
    const request = new AbortController();
    setResult(undefined);
    async function render() {
      try {
        const blob = await file.load();
        if (request.signal.aborted) return;
        if (!token || blob.size > 16 * 1024 * 1024) throw Error('invalid_file');
        const hash = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
        const digest = Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('');
        const response = await fetch(apiBaseUrl() + '/api/files/word-pages', {
          method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/octet-stream' },
          body: blob, signal: request.signal, redirect: 'error',
        });
        if (!response.ok) throw Error('render_failed');
        const data = await response.json();
        if (data.sha256 !== digest || !Array.isArray(data.pages) || !data.pages.length ||
          data.pages.length > 40 || !data.pages.every((p: unknown) => typeof p === 'string' && p.startsWith('/9j/'))) {
          throw Error('invalid_pages');
        }
        if (!request.signal.aborted) setResult({ file, pages: data.pages });
      } catch {
        if (!request.signal.aborted) setResult({ file, failed: true });
      }
    }
    void render();
    return () => request.abort();
  }, [file, token, attempt]);
  const pages = result?.file === file ? result.pages : undefined;
  return (
    <section aria-label={file.filename} data-testid="word-side-preview"
      className="bg-surface-primary text-text-primary flex h-full min-w-0 flex-col"
      onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); close(); } }}>
      <header className="border-border-light flex items-center gap-2 border-b px-3 py-2">
        <h2 ref={title} tabIndex={-1} title={file.filename} className="min-w-0 flex-1 truncate text-sm font-semibold">{file.filename}</h2>
        {pages && <>
          <Button size="icon" variant="ghost" aria-label={t('com_ui_zoom_out')} disabled={zoom <= 75}
            onClick={() => setZoom(z => Math.max(75, z - 25))}>−</Button>
          <button type="button" title={t('com_word_fit_width')} onClick={() => setZoom(100)} className="text-xs">{zoom}%</button>
          <Button size="icon" variant="ghost" aria-label={t('com_ui_zoom_in')} disabled={zoom >= 200}
            onClick={() => setZoom(z => Math.min(200, z + 25))}>+</Button>
        </>}
        <Button size="icon" variant="ghost" aria-label={t('com_ui_close')} onClick={close}>×</Button>
      </header>
      <div ref={viewport} className="bg-surface-secondary min-h-0 flex-1 overflow-auto p-3">
        {file.fallback ?? (pages ? (
          <div className="mx-auto" style={{ width: zoom + '%' }}>
            {pages.map((page, i) => (
              <figure key={i} className="mb-3">
                <img src={'data:image/jpeg;base64,' + page} alt={file.filename + ' · ' + (i + 1) + ' / ' + pages.length}
                  loading={i === 0 ? 'eager' : 'lazy'} className="block h-auto w-full bg-white shadow" />
                <figcaption className="text-text-secondary mt-1 text-center text-xs">{i + 1} / {pages.length}</figcaption>
              </figure>
            ))}
          </div>
        ) : result?.file === file && result.failed ? (
          <div role="alert">{t('com_ui_preview_unavailable')} <Button variant="outline" onClick={() => retry(x => x + 1)}>{t('com_ui_retry')}</Button></div>
        ) : <p role="status">{t('com_ui_loading')}</p>)}
      </div>
    </section>
  );
}
