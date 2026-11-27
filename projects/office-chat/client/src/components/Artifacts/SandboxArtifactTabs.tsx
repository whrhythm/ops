import { useMemo, useRef } from 'react';
import * as Tabs from '@radix-ui/react-tabs';
import type { SandpackPreviewRef } from '@codesandbox/sandpack-react/unstyled';
import type { editor } from 'monaco-editor';
import type { Artifact } from '~/common';
import { useFilePreview, useGetSharedStartupConfig, useGetStartupConfig } from '~/data-provider';
import useArtifactProps from '~/hooks/Artifacts/useArtifactProps';
import { useArtifactCode } from '~/Providers/EditorContext';
import { normalizeArtifactType, TOOL_ARTIFACT_TYPES } from '~/utils/artifacts';
import { ArtifactCodeEditor } from './ArtifactCodeEditor';
import { ArtifactPreview } from './ArtifactPreview';
import { DocumentReader } from './DocumentReader';
import '~/document-reader.css';
import { useShareContext } from '~/Providers';

export default function SandboxArtifactTabs({
  artifact,
  previewRef,
  isSharedConvo,
}: {
  artifact: Artifact;
  previewRef: React.MutableRefObject<SandpackPreviewRef>;
  isSharedConvo?: boolean;
}) {
  const { shareId } = useShareContext();
  const shouldUseSharedConfig =
    isSharedConvo === true && typeof shareId === 'string' && shareId.length > 0;
  const { data: startupConfig } = useGetStartupConfig({ enabled: !shouldUseSharedConfig });
  const { data: sharedStartupConfig } = useGetSharedStartupConfig(shareId, {
    enabled: shouldUseSharedConfig,
  });
  const resolvedStartupConfig = shouldUseSharedConfig ? sharedStartupConfig : startupConfig;
  const monacoRef = useRef<editor.IStandaloneCodeEditor | null>(null);

  /* The buffer belongs to whichever artifact last wrote it, so a pane that
   * remounted for another host keeps this artifact's unsaved text; a copy
   * another artifact displaced is just as much this artifact's text. */
  const editedCode = useArtifactCode(artifact.id);

  const isLegacyWord =
    isSharedConvo !== true &&
    artifact.type === TOOL_ARTIFACT_TYPES.DOCX &&
    artifact.content?.includes('docx.renderAsync') === true &&
    !artifact.content.includes('<meta name="lc-word-page-preview" content="1">');
  const { data: refreshedPreview } = useFilePreview(
    isLegacyWord ? artifact.download?.file_id : undefined,
    { refetchInterval: false },
  );
  const resolvedArtifact =
    isLegacyWord &&
    refreshedPreview?.textFormat === 'html' &&
    (refreshedPreview.text?.includes('<ruby>') ||
      refreshedPreview.text?.includes('<meta name="lc-word-page-preview" content="1">'))
      ? { ...artifact, content: refreshedPreview.text }
      : artifact;
  const { files, fileKey, template, sharedProps, deriveFiles } = useArtifactProps({
    artifact: resolvedArtifact,
  });

  /* An artifact whose preview entry is derived from its source needs the whole
   * set rebuilt from the editor text; `ArtifactPreview` can only swap the file
   * the editor owns. Empty text counts as no edit there, so it does here too. */
  const previewFiles = useMemo(
    () => (deriveFiles != null && editedCode ? deriveFiles(editedCode) : files),
    [deriveFiles, editedCode, files],
  );

  return (
    <div className="flex h-full w-full flex-col">
      <Tabs.Content
        value="code"
        id="artifacts-code"
        className="h-full w-full grow overflow-auto"
        tabIndex={-1}
      >
        <ArtifactCodeEditor
          artifact={artifact}
          monacoRef={monacoRef}
          readOnly={isSharedConvo === true}
        />
      </Tabs.Content>

      <Tabs.Content value="preview" className="h-full w-full grow overflow-hidden" tabIndex={-1}>
        {fileKey === 'content.md' ? <DocumentReader source={editedCode ?? artifact.content ?? ''}
          markdown={normalizeArtifactType(artifact.type ?? '') === TOOL_ARTIFACT_TYPES.MARKDOWN}
          title={artifact.title ?? undefined} /> : <ArtifactPreview
          files={previewFiles}
          fileKey={fileKey}
          template={template}
          previewRef={previewRef}
          sharedProps={sharedProps}
          currentCode={editedCode}
          startupConfig={resolvedStartupConfig}
        />}
      </Tabs.Content>
    </div>
  );
}
