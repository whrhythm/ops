import React from 'react';
import { webcrypto } from 'node:crypto';
import userEvent from '@testing-library/user-event';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { dataService, personalFileRequest, PersonalFileError } from 'librechat-data-provider';
import SavePersonal from './SavePersonal';

jest.mock('~/hooks', () => ({
  useLocalize: () => (key: string) => key,
  useAuthContext: () => ({ user: { id: 'owner' }, token: 'token' }),
}));
jest.mock('~/Providers', () => ({ useShareContext: () => ({}) }));
jest.mock('~/data-provider', () => ({
  useGetStartupConfig: () => ({ data: { personalFiles: true } }),
}));
jest.mock('librechat-data-provider', () => ({
  ...jest.requireActual('librechat-data-provider'),
  personalFileRequest: jest.fn(),
  dataService: { getCodeOutputDownload: jest.fn() },
}));

function setup(files: React.ComponentProps<typeof SavePersonal>['files'] = [
  { filename: 'result.xlsx', filepath: '/api/files/code/result' },
]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, cacheTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <SavePersonal files={files} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

it('saves scanned Word separately using the authenticated result loader', async () => {
  const docx = new Blob(['ocr-word']);
  const load = jest.fn().mockResolvedValue(docx);
  const user = setup([{ filename: 'scan.docx', filepath: '/api/files/personal/scan-word', load }]);
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_save' }));
  await user.click(await screen.findByRole('button', { name: '财务' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'com_ui_save' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: 'com_ui_save' }));
  await screen.findByText('com_starbridge_files_saved');
  expect(load).toHaveBeenCalledTimes(1);
  expect(personalFileRequest).toHaveBeenCalledWith('token', 'file', 'PUT', '财务/scan.docx', docx);
  expect(dataService.getCodeOutputDownload).not.toHaveBeenCalled();
});

beforeEach(() => {
  jest.clearAllMocks();
  Object.defineProperty(global.crypto, 'subtle', { configurable: true, value: webcrypto.subtle });
  Object.defineProperty(global.crypto, 'randomUUID', {
    configurable: true,
    value: () => '11111111-1111-4111-8111-111111111111',
  });
  jest
    .mocked(dataService.getCodeOutputDownload)
    .mockResolvedValue({ data: new Blob(['workbook']) } as never);
  jest.mocked(personalFileRequest).mockImplementation(async (_token, operation, method, path) => {
    if (operation === 'directories')
      return {
        json: async () => ({
          entries: path
            ? []
            : [
                { name: '财务', kind: 'directory' },
                { name: 'original.csv', kind: 'file' },
                { name: 'original.xlsx', kind: 'file' },
              ],
        }),
      } as Response;
    return { ok: true } as Response;
  });
});

it('saves a revision of the selected existing workbook without writing the original path', async () => {
  const original = jest.mocked(personalFileRequest).getMockImplementation()!;
  jest.mocked(personalFileRequest).mockImplementation(async (...args) => {
    if (args[1] === 'revisions')
      return {
        json: async () => (args[2] === 'POST' ? { version: 2 } : { revisions: [] }),
      } as Response;
    return original(...args);
  });
  const user = setup();
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_save' }));
  await user.click(screen.getByRole('checkbox', { name: 'com_starbridge_versions_save' }));
  expect(screen.queryByRole('button', { name: 'original.csv' })).not.toBeInTheDocument();
  await user.click(await screen.findByRole('button', { name: 'original.xlsx' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'com_ui_save' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: 'com_ui_save' }));
  await screen.findByText('com_starbridge_files_saved');
  expect(personalFileRequest).toHaveBeenCalledWith(
    'token',
    'revisions',
    'POST',
    'original.xlsx',
    expect.any(Blob),
    { version: 2, requestId: expect.any(String) },
  );
  expect(jest.mocked(personalFileRequest).mock.calls.some((c) => c[2] === 'PUT')).toBe(false);
});

it('saves into the chosen folder, hides files from the folder picker, and preserves the result name', async () => {
  const user = setup();
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_save' }));
  await user.click(await screen.findByRole('button', { name: '财务' }));
  expect(screen.queryByText('original.csv')).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole('button', { name: 'com_ui_save' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: 'com_ui_save' }));
  await screen.findByText('com_starbridge_files_saved');
  expect(personalFileRequest).toHaveBeenCalledWith(
    'token',
    'file',
    'PUT',
    '财务/result.xlsx',
    expect.any(Blob),
  );
});

it('reports a conflict without overwriting or retrying', async () => {
  const original = jest.mocked(personalFileRequest).getMockImplementation()!;
  jest.mocked(personalFileRequest).mockImplementation(async (...args) => {
    if (args[2] === 'PUT') throw new PersonalFileError(409);
    return original(...args);
  });
  const user = setup();
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_save' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'com_ui_save' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: 'com_ui_save' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('com_starbridge_files_conflict');
  expect(screen.queryByText('com_starbridge_files_saved')).not.toBeInTheDocument();
  expect(
    jest.mocked(personalFileRequest).mock.calls.filter((call) => call[2] === 'PUT'),
  ).toHaveLength(1);
});

it('does not save when the selected folder cannot be read', async () => {
  jest.mocked(personalFileRequest).mockRejectedValue(new Error('offline'));
  const user = setup();
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_save' }));
  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: 'com_ui_save' })).toBeDisabled();
  expect(dataService.getCodeOutputDownload).not.toHaveBeenCalled();
});

it('reuses the committed version after a page remount instead of allocating another version', async () => {
  const original = jest.mocked(personalFileRequest).getMockImplementation()!;
  let receipt: { version: number; requestId: string } | undefined;
  jest.mocked(personalFileRequest).mockImplementation(async (...args) => {
    if (args[1] !== 'revisions') return original(...args);
    if (args[2] === 'POST') receipt = args[5] as typeof receipt;
    return {
      json: async () => (args[2] === 'POST' ? receipt : { revisions: receipt ? [receipt] : [] }),
    } as Response;
  });
  for (let i = 0; i < 2; i++) {
    const user = setup();
    await user.click(screen.getByRole('button', { name: 'com_starbridge_files_save' }));
    await user.click(screen.getByRole('checkbox'));
    await user.click(await screen.findByRole('button', { name: 'original.xlsx' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'com_ui_save' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'com_ui_save' }));
    await screen.findByText('com_starbridge_files_saved');
    cleanup();
  }
  const saves = jest.mocked(personalFileRequest).mock.calls.filter((call) => call[2] === 'POST');
  expect(saves).toHaveLength(2);
  expect(saves[0][5]).toEqual(saves[1][5]);
  expect(saves[1][5]).toEqual({ version: 2, requestId: expect.any(String) });
});
