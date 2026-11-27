import { useCallback, useRef, useMemo } from 'react';
import { atom, useAtom } from 'jotai';
import { FileSources, FileContext } from 'librechat-data-provider';
import {
  Button,
  OGDialog,
  OGDialogContent,
  OGDialogHeader,
  OGDialogTitle,
} from '@librechat/client';
import type { TFile } from 'librechat-data-provider';
import { useGetFiles, useGetStartupConfig } from '~/data-provider';
import PersonalFiles from './PersonalFiles';
import { DataTable, columns } from './Table';
import { useLocalize } from '~/hooks';

export function MyFilesModal({
  open,
  onOpenChange,
  triggerRef,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  triggerRef?: React.RefObject<HTMLElement | null>;
}) {
  const localize = useLocalize();
  const { data: config } = useGetStartupConfig();
  const [section, setSection] = useAtom(useMemo(() => atom<'personal' | 'chat'>('personal'), []));
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const captureReturnFocus = useCallback(() => {
    const activeElement = document.activeElement;
    const canRestoreShortcutFocus =
      activeElement instanceof HTMLElement &&
      activeElement !== document.body &&
      activeElement.isConnected &&
      activeElement.closest('[inert]') == null &&
      activeElement.closest('[role="menu"]') == null;
    returnFocusRef.current = canRestoreShortcutFocus
      ? activeElement
      : (triggerRef?.current ?? null);
  }, [triggerRef]);

  const { data: files = [] } = useGetFiles<TFile[]>({
    select: (files) =>
      files.map((file) => {
        file.context = file.context ?? FileContext.unknown;
        file.filterSource = file.source === FileSources.firebase ? FileSources.local : file.source;
        return file;
      }),
  });

  return (
    <OGDialog open={open} onOpenChange={onOpenChange} triggerRef={returnFocusRef}>
      <OGDialogContent
        title={localize('com_nav_my_files')}
        onOpenAutoFocus={captureReturnFocus}
        className="text-text-primary max-h-[90dvh] w-11/12 max-w-3xl overflow-y-auto shadow-2xl"
      >
        <OGDialogHeader>
          <OGDialogTitle>{localize('com_nav_my_files')}</OGDialogTitle>
        </OGDialogHeader>
        {config?.personalFiles && (
          <div className="flex gap-2" role="group" aria-label={localize('com_nav_my_files')}>
            <Button
              size="sm"
              variant={section === 'personal' ? 'default' : 'ghost'}
              aria-pressed={section === 'personal'}
              onClick={() => setSection('personal')}
            >
              {localize('com_starbridge_files_personal')}
            </Button>
            <Button
              size="sm"
              variant={section === 'chat' ? 'default' : 'ghost'}
              aria-pressed={section === 'chat'}
              onClick={() => setSection('chat')}
            >
              {localize('com_starbridge_files_chat')}
            </Button>
          </div>
        )}
        {config?.personalFiles && section === 'personal' ? (
          <PersonalFiles />
        ) : (
          <DataTable columns={columns} data={files} />
        )}
      </OGDialogContent>
    </OGDialog>
  );
}
