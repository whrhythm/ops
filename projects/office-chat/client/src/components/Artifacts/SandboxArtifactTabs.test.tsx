import React from 'react';
import * as Tabs from '@radix-ui/react-tabs';
import { render, screen } from '@testing-library/react';
import type { SandpackPreviewRef } from '@codesandbox/sandpack-react/unstyled';
import type { Artifact } from '~/common';
import SandboxArtifactTabs from './SandboxArtifactTabs';

interface PreviewProps {
  files: Record<string, string>;
  fileKey: string;
  currentCode?: string;
}

const mockPreview = jest.fn((_props: PreviewProps) => null);
const mockFilePreview = jest.fn();
let mockCurrentCode: string | undefined;
let mockCodeArtifactId: string | undefined;

jest.mock('./ArtifactCodeEditor', () => ({
  ArtifactCodeEditor: () => null,
}));

jest.mock('./ArtifactPreview', () => ({
  ArtifactPreview: (props: PreviewProps) => mockPreview(props),
}));

jest.mock('~/Providers/EditorContext', () => ({
  useArtifactCode: (artifactId: string) =>
    mockCodeArtifactId === artifactId ? mockCurrentCode : undefined,
}));

jest.mock('~/Providers', () => ({
  useShareContext: () => ({ shareId: undefined }),
}));

jest.mock('~/data-provider', () => ({
  useFilePreview: (fileId: string | undefined) => mockFilePreview(fileId),
  useGetStartupConfig: () => ({ data: {} }),
  useGetSharedStartupConfig: () => ({ data: {} }),
}));

const previewRef = {
  current: Object.create(null) as SandpackPreviewRef,
} as React.MutableRefObject<SandpackPreviewRef>;

const original =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>';
const edited =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>';

const svgArtifact: Artifact = {
  id: 'svg-artifact-1',
  type: 'image/svg+xml',
  title: 'Chart',
  content: original,
  lastUpdateTime: 1,
};

function renderTabs(artifact: Artifact = svgArtifact) {
  return render(
    <Tabs.Root value="preview">
      <SandboxArtifactTabs artifact={artifact} previewRef={previewRef} />
    </Tabs.Root>,
  );
}

function lastFiles(): Record<string, string> {
  return mockPreview.mock.calls.at(-1)?.[0].files ?? {};
}

beforeEach(() => {
  mockFilePreview.mockReset().mockReturnValue({});
});

it('renders the refreshed phonetic annotation preview for a saved Word artifact', () => {
  const artifact: Artifact = {
    id: 'word-01',
    type: 'application/vnd.librechat.docx-preview',
    content: '<script>docx.renderAsync</script><p>☆</p>',
    download: { file_id: 'saved-word' },
    lastUpdateTime: 1,
  };
  const refreshed = '<p><ruby>爷<rt>yé</rt></ruby></p>';
  mockFilePreview.mockReturnValue({
    data: { status: 'ready', textFormat: 'html', text: refreshed },
  });

  renderTabs(artifact);

  expect(mockFilePreview).toHaveBeenCalledWith('saved-word');
  expect(lastFiles()['index.html']).toContain(refreshed);
  expect(lastFiles()['index.html']).toContain('color-scheme:light');
});

it('reads Markdown headings, lists and GFM tables without starting the app preview', () => {
  renderTabs({ ...svgArtifact, type: 'text/markdown', content: '# 清单\n\n- 一项\n\n| 名称 | 数量 |\n| --- | --- |\n| 样本 | 2 |' });
  expect(screen.getByRole('heading', {name:'清单'})).toBeVisible();
  expect(screen.getByRole('listitem')).toHaveTextContent('一项');
  expect(screen.getByRole('cell', {name:'样本'})).toBeVisible();
});

it('preserves plain text syntax and newlines when reading a text file', () => {
  const source = '# 标记\n  缩进';
  renderTabs({ ...svgArtifact, type: 'text/plain', content: source });
  expect(screen.getByRole('region').querySelector('pre')?.textContent).toBe(source);
});

describe('SandboxArtifactTabs SVG preview', () => {
  beforeEach(() => {
    mockCurrentCode = undefined;
    mockCodeArtifactId = undefined;
    mockPreview.mockClear();
  });

  /** An SVG preview renders a derived `index.html`, so an edit that only
   * replaced `index.svg` left the preview showing the original drawing. */
  it('rebuilds the preview shell from the edited source', () => {
    const { rerender } = renderTabs();
    expect(lastFiles()['index.html']).toContain(original);

    /* Editor text belongs to the preview only once it was typed against the
     * artifact on screen, so it lands on a later render, not on mount. */
    mockCurrentCode = edited;
    mockCodeArtifactId = svgArtifact.id;
    rerender(
      <Tabs.Root value="preview">
        <SandboxArtifactTabs artifact={svgArtifact} previewRef={previewRef} />
      </Tabs.Root>,
    );

    const files = lastFiles();
    expect(files['index.svg']).toBe(edited);
    expect(files['index.html']).toContain(edited);
    expect(files['index.html']).not.toContain('<rect');
  });

  it('keeps the artifact source when the editor is emptied', () => {
    const { rerender } = renderTabs();

    mockCurrentCode = '';
    mockCodeArtifactId = svgArtifact.id;
    rerender(
      <Tabs.Root value="preview">
        <SandboxArtifactTabs artifact={svgArtifact} previewRef={previewRef} />
      </Tabs.Root>,
    );

    expect(lastFiles()['index.html']).toContain(original);
  });

  it('leaves artifacts whose preview entry is the edited file untouched', () => {
    const htmlArtifact: Artifact = {
      id: 'html-artifact-1',
      type: 'text/html',
      title: 'Page',
      content: '<p>original</p>',
      lastUpdateTime: 1,
    };

    const { rerender } = renderTabs(htmlArtifact);

    mockCurrentCode = '<p>edited</p>';
    mockCodeArtifactId = htmlArtifact.id;
    rerender(
      <Tabs.Root value="preview">
        <SandboxArtifactTabs artifact={htmlArtifact} previewRef={previewRef} />
      </Tabs.Root>,
    );

    /* `ArtifactPreview` owns this swap; the tabs must not pre-empt it. */
    const call = mockPreview.mock.calls.at(-1)?.[0];
    expect(call?.files['index.html']).toBe('<p>original</p>');
    expect(call?.currentCode).toBe('<p>edited</p>');
  });
});
