import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Button } from '@librechat/client';
import { getAllContentText } from '@librechat/chat';
import { dataService } from 'librechat-data-provider';
import type { TMessage } from 'librechat-data-provider';
import type { Artifact } from '~/common';
import SavePersonal from '~/components/Chat/Input/Files/SavePersonal';
import { useCachedMessages } from '~/data-provider';
import { useAuthContext, useLocalize } from '~/hooks';
import {
  designPageSize,
  readDesignPhoto,
  renderOfficeDesign,
  resolveDesignPhotos,
  resolveOfficeDesign,
} from '~/utils/officeDesign';

const PX_PER_MM = 96 / 25.4;

export default function OfficeDesign({
  artifact,
  shared,
}: {
  artifact: Artifact;
  shared: boolean;
}) {
  const localize = useLocalize();
  const { user } = useAuthContext();
  const { pathname } = useLocation();
  const conversationId = artifact.conversationId ?? /^\/c\/([^/]+)\/?$/.exec(pathname)?.[1] ?? '';
  const selectDraft = useCallback(
    (messages: TMessage[]) => {
      const design = resolveOfficeDesign(
        artifact.content ?? '',
        artifact.identifier,
        messages,
        artifact.messageId,
        getAllContentText,
      );
      return {
        design,
        photos:
          design && !shared ? resolveDesignPhotos(design, messages, artifact.messageId) : null,
      };
    },
    [artifact.content, artifact.identifier, artifact.messageId, shared],
  );
  const { data: draft } = useCachedMessages(conversationId, selectDraft);
  const { design, photos } = draft ?? {};
  const frame = useRef<HTMLIFrameElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const [html, setHtml] = useState('');
  const [loaded, setLoaded] = useState('');
  const [failed, setFailed] = useState(false);
  const [fits, setFits] = useState(true);

  useEffect(() => {
    setHtml('');
    setLoaded('');
    setFailed(false);
    setFits(true);
    if (!photos || !design || !user?.id) {
      return;
    }
    let active = true;
    const userId = user.id;
    Promise.all(
      photos.map(async (file) => {
        if (!file.file_id || (file.bytes ?? 0) > 6 * 1024 * 1024) {
          throw new Error('Photo unavailable');
        }
        const { data } = await dataService.getFileDownload(userId, file.file_id);
        if (!(data instanceof Blob)) {
          throw new Error('Photo unavailable');
        }
        return readDesignPhoto(data);
      }),
    )
      .then((images) => {
        if (active) {
          setHtml(renderOfficeDesign(design, images));
        }
      })
      .catch(() => {
        if (active) {
          setFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [design, photos, user?.id]);

  const paper = design && designPageSize(design);
  useEffect(() => {
    const container = viewport.current;
    if (!container || !paper || !loaded || loaded !== html) {
      return;
    }
    const resize = () => {
      const iframe = frame.current;
      const root = iframe?.contentDocument?.documentElement;
      if (!iframe || !root) {
        return;
      }
      const scale = Math.min(1, container.clientWidth / (paper.width * PX_PER_MM));
      root.style.setProperty('--preview-scale', String(scale));
      iframe.style.height = `${Math.ceil(paper.height * PX_PER_MM * scale)}px`;
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    return () => observer.disconnect();
  }, [html, loaded, paper?.width, paper?.height]);

  async function checkLayout(iframe: HTMLIFrameElement) {
    const document = iframe.contentDocument;
    const page = document?.querySelector('main');
    if (!document || !page) {
      return;
    }
    await document.fonts.ready;
    if (frame.current !== iframe || iframe.contentDocument !== document) {
      return;
    }
    const bounds = page.getBoundingClientRect();
    const elements = Array.from(
      document.querySelectorAll('header, .content, figure, aside, [data-fit], .handwriting'),
    );
    const outside = elements.some((element) => {
      const rect = element.getBoundingClientRect();
      return (
        rect.bottom > bounds.bottom ||
        rect.right > bounds.right ||
        element.scrollHeight > element.clientHeight + 1 ||
        element.scrollWidth > element.clientWidth + 1
      );
    });
    setFits(!outside && Array.from(document.images).every((image) => image.naturalWidth > 0));
    setLoaded(html);
  }

  if (!shared && (!draft || !user?.id)) {
    return (
      <span className="p-4 text-sm" role="status">
        {localize('com_ui_loading')}
      </span>
    );
  }
  if (shared || !design || !photos || failed) {
    return (
      <p className="text-text-secondary p-4 text-sm" role="status">
        {localize(
          shared
            ? 'com_office_design_private'
            : !design
              ? 'com_office_design_invalid'
              : 'com_office_design_photos_unavailable',
        )}
      </p>
    );
  }
  return (
    <div className="bg-surface-secondary flex h-full flex-col gap-2 overflow-auto p-3">
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={!html || loaded !== html || !fits}
          onClick={() => frame.current?.contentWindow?.print()}
        >
          {localize('com_office_design_print')}
        </Button>
        {html && loaded === html && fits && (
          <SavePersonal
            files={[
              {
                filename: `${design.title.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')}.html`,
                file_id: artifact.id,
                load: async () => new Blob([html], { type: 'text/html;charset=utf-8' }),
              },
            ]}
          />
        )}
        {!fits && (
          <span role="alert" className="text-text-secondary text-sm">
            {localize('com_office_design_overflow')}
          </span>
        )}
      </div>
      {!html && <span role="status">{localize('com_ui_loading')}</span>}
      <div ref={viewport} className="w-full">
        {html && (
          <iframe
            ref={frame}
            title={artifact.title ?? design.title}
            srcDoc={html}
            sandbox="allow-same-origin allow-modals"
            onLoad={(event) => void checkLayout(event.currentTarget)}
            className="block w-full border-0"
          />
        )}
      </div>
    </div>
  );
}
