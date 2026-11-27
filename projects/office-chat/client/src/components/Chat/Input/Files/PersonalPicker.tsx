import { useMemo, useRef } from 'react';
import { atom, useAtom } from 'jotai';
import { useMutation, useQuery } from '@tanstack/react-query';
import { personalFileRequest } from 'librechat-data-provider';
import {
  Button,
  OGDialog,
  OGDialogContent,
  OGDialogTitle,
  OGDialogDescription,
} from '@librechat/client';
import { useAuthContext, useLocalize } from '~/hooks';
import PersonalBrowser from './PersonalBrowser';
import WordPages from '../../WordPages';
import { usePersonalHistory } from './Versions';

function PersonalWordFallback({ path, version }: { path: string; version: number }) {
  const t = useLocalize();
  const { token, user } = useAuthContext();
  const preview = useQuery({
    queryKey: ['personalWordPreview', user?.id, path, version],
    queryFn: async (): Promise<{ html: string }> => {
      const response = await personalFileRequest(
        token!, version ? 'revisions' : 'file', 'GET', path, undefined, { version, preview: 'word' },
      );
      return response.json();
    },
    enabled: !!token,
    retry: false,
    cacheTime: 0,
  });
  return <div className="flex h-full flex-col">
          {preview.isError && <p role="alert">{t('com_ui_preview_unavailable')}</p>}
          {preview.isLoading && <p role="status">{t('com_ui_loading')}</p>}
          {preview.data && (
            <iframe
              title={t('com_office_word_preview')}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              className="min-h-0 w-full flex-1 border-0"
              srcDoc={preview.data.html}
            />
          )}
  </div>;
}

function SelectedWordPreview({ path, version, onOpen }: { path: string; version: number; onOpen: () => void }) {
  const { token } = useAuthContext();
  const name = path.split('/').pop() ?? path;
  return <WordPages filename={version ? name + ' · v' + version : name} onOpen={onOpen}
    fallback={<PersonalWordFallback path={path} version={version} />}
    load={async () => (await personalFileRequest(token!, version ? 'revisions' : 'file', 'GET', path, undefined, { version })).blob()} />;
}

export default function PersonalPicker({
  open,
  onClose,
  onSelect,
}: {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string, name: string, version?: number) => Promise<boolean>;
}) {
  const t = useLocalize();
  const returnFocus = useRef<HTMLElement | null>(null);
  return (
    <OGDialog
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
      triggerRef={returnFocus}
    >
      {open && <PickerBody onClose={onClose} onSelect={onSelect} returnFocus={returnFocus} t={t} />}
    </OGDialog>
  );
}

function PickerBody({
  onClose,
  onSelect,
  returnFocus,
  t,
}: {
  onClose: () => void;
  onSelect: (path: string, name: string, version?: number) => Promise<boolean>;
  returnFocus: React.MutableRefObject<HTMLElement | null>;
  t: ReturnType<typeof useLocalize>;
}) {
  const [view, setView] = useAtom(
    useMemo(() => atom({ path: '', selected: '', name: '', version: 0 }), []),
  );
  const history = usePersonalHistory(view.selected);
  const attach = useMutation({
    mutationFn: async () => {
      if (view.version && !history.data?.revisions.some((v) => v.version === view.version))
        throw new Error('revision_not_available');
      const dot = view.name.lastIndexOf('.');
      let name = view.name;
      if (view.version)
        name =
          dot > 0
            ? `${view.name.slice(0, dot)}-v${view.version}${view.name.slice(dot)}`
            : `${view.name}-v${view.version}`;
      if (!(await onSelect(view.selected, name, view.version || undefined)))
        throw new Error('attachment_not_accepted');
    },
    onSuccess: onClose,
    retry: false,
  });
  return (
    <OGDialogContent
      className="max-h-[90dvh] w-[calc(100%-2rem)] max-w-md overflow-y-auto"
      onOpenAutoFocus={() => {
        returnFocus.current =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
      }}
    >
      <OGDialogTitle>{t('com_starbridge_files_pick')}</OGDialogTitle>
      <OGDialogDescription>{t('com_starbridge_files_attach_help')}</OGDialogDescription>
      <PersonalBrowser
        path={view.path}
        selected={view.selected}
        disabled={attach.isLoading}
        onNavigate={(path) => {
          attach.reset();
          setView({ path, selected: '', name: '', version: 0 });
        }}
        onFile={(selected, name) => {
          attach.reset();
          setView({ ...view, selected, name, version: 0 });
        }}
      />
      {view.selected && (
        <label className="flex items-center gap-3 text-sm">
          <span>{t('com_starbridge_versions')}</span>
          <select
            className="border-border-light bg-surface-primary min-w-0 flex-1 rounded-lg border px-3 py-2"
            value={view.version}
            disabled={attach.isLoading || history.isFetching}
            onChange={(e) => {
              attach.reset();
              setView({ ...view, version: Number(e.target.value) });
            }}
          >
            <option value={0}>{t('com_starbridge_files_original')}</option>
            {history.data?.revisions.map((v) => (
              <option key={v.version} value={v.version}>{`v${v.version}`}</option>
            ))}
          </select>
        </label>
      )}
      {view.selected && (history.isError || history.isPaused) && (
        <p role="alert" className="text-sm">
          {t('com_starbridge_files_error')}
        </p>
      )}
      {attach.isError && (
        <p role="alert" className="text-sm">
          {t('com_starbridge_files_attach_error')}
        </p>
      )}
      <div className="flex justify-end gap-2">
        {/\.docx$/i.test(view.name) && (
          <SelectedWordPreview key={JSON.stringify([view.selected, view.version])} path={view.selected} version={view.version} onOpen={onClose} />
        )}
        <Button variant="outline" onClick={onClose}>
          {t('com_ui_cancel')}
        </Button>
        <Button
          disabled={
            !view.selected ||
            attach.isLoading ||
            (view.version > 0 && (history.isFetching || history.isError || history.isPaused))
          }
          onClick={() => attach.mutate()}
        >
          {t(attach.isLoading ? 'com_starbridge_loading' : 'com_starbridge_files_attach')}
        </Button>
      </div>
    </OGDialogContent>
  );
}
