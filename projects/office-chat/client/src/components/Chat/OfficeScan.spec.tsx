import React from 'react';
import userEvent from '@testing-library/user-event';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import OfficeScan from './OfficeScan';

jest.mock('~/hooks', () => ({
  useLocalize: () => (key: string) => key,
  useAuthContext: () => ({ user: { id: 'owner' }, token: 'token' }),
}));
jest.mock('~/data-provider', () => ({
  useGetStartupConfig: () => ({ data: { structuredOcr: true, personalFiles: true } }),
}));
jest.mock('./Input/Files/SavePersonal', () => () => <button>Save result</button>);

afterEach(() => {
  cleanup();
  jest.restoreAllMocks();
});

it('restores a completed scan and highlights the selected cell on the original page', async () => {
  const polygon = [
    [0.1, 0.2],
    [0.2, 0.2],
    [0.2, 0.3],
    [0.1, 0.3],
  ];
  const record = {
    status: 'complete',
    digest: 'a'.repeat(64),
    result: {
      editable_text_bookmarks: true,
      pages: [
        {
          page: 3,
          blocks: [
            {
              block_id: 'p3-b1',
              label: 'table',
              polygon,
              rows: [['-40.00']],
              cells: [[{ text: '-40.00', review_required: true, polygon }]],
              review_reasons: ['verify_key_cells'],
            },
          ],
        },
      ],
    },
  };
  const previousFetch = global.fetch;
  const request = jest.fn(
    async (input: RequestInfo | URL, _init?: RequestInit) =>
      ({
        ok: true,
        status: 200,
        json: async () => record,
        blob: async () => new Blob(['page']),
        url: String(input),
      }) as Response,
  );
  global.fetch = request;
  const create = URL.createObjectURL;
  const revoke = URL.revokeObjectURL;
  URL.createObjectURL = jest.fn(() => 'blob:original-page');
  URL.revokeObjectURL = jest.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, cacheTime: 0 } } });
  const user = userEvent.setup();
  try {
    render(
      <QueryClientProvider client={client}>
        <OfficeScan conversationId="11111111-1111-4111-8111-111111111111" />
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole('button', { name: 'com_scan_title' }));
    const page = await screen.findByRole('img', { name: 'com_scan_page 3' });
    expect(request.mock.calls.some(([url]) => String(url).includes('page=3'))).toBe(true);
    page.parentElement!.scrollIntoView = jest.fn();
    await user.click(screen.getByRole('button', { name: '-40.00 ＊' }));
    expect(page.nextElementSibling).toHaveStyle(
      'clip-path: polygon(10% 20%,20% 20%,20% 30%,10% 30%)',
    );
    const correction = screen.getByRole('textbox', { name: 'com_scan_correction' });
    expect(correction).toHaveValue('-40.00');
    await user.clear(correction);
    await user.type(correction, '-42.00');
    expect(screen.getByRole('button', { name: '-42.00 ＊' })).toBeInTheDocument();
    expect(request.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
  } finally {
    cleanup();
    global.fetch = previousFetch;
    URL.createObjectURL = create;
    URL.revokeObjectURL = revoke;
  }
});
