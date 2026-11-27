import React from 'react';
import userEvent from '@testing-library/user-event';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { personalFileRequest } from 'librechat-data-provider';
import PersonalPicker from './PersonalPicker';

jest.mock('~/hooks', () => ({
  useLocalize: () => (key: string) => key,
  useAuthContext: () => ({ user: { id: 'owner' }, token: 'token' }),
}));
jest.mock('librechat-data-provider', () => ({
  ...jest.requireActual('librechat-data-provider'),
  personalFileRequest: jest.fn(),
}));

beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(personalFileRequest).mockImplementation(
    async (_token, operation) =>
      ({
        json: async () =>
          operation === 'file' ? { html: '<p>preview</p>' } :
          operation === 'directories'
            ? {
                entries: [
                  { name: 'report.xlsx', kind: 'file' },
                  { name: 'other.xlsx', kind: 'file' },
                  { name: 'card.docx', kind: 'file' },
                ],
              }
            : { revisions: [{ version: 2 }, { version: 1 }] },
      }) as Response,
  );
});

function setup() {
  const onSelect = jest.fn(async () => true);
  const onClose = jest.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, cacheTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <PersonalPicker open onClose={onClose} onSelect={onSelect} />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), onSelect, onClose };
}

it('lets the user explicitly select a revision and labels the attachment', async () => {
  const { user, onSelect } = setup();
  await user.click(await screen.findByRole('button', { name: 'report.xlsx' }));
  await screen.findByRole('option', { name: 'v2' });
  await user.selectOptions(screen.getByRole('combobox'), '2');
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_attach' }));
  expect(onSelect).toHaveBeenCalledWith('report.xlsx', 'report-v2.xlsx', 2);
});

it('resets the revision when changing files and keeps the original explicit', async () => {
  const { user, onSelect } = setup();
  await user.click(await screen.findByRole('button', { name: 'report.xlsx' }));
  await screen.findByRole('option', { name: 'v2' });
  await user.selectOptions(screen.getByRole('combobox'), '2');
  await user.click(screen.getByRole('button', { name: 'other.xlsx' }));
  expect(screen.getByRole('combobox')).toHaveValue('0');
  await user.click(screen.getByRole('button', { name: 'com_starbridge_files_attach' }));
  expect(onSelect).toHaveBeenCalledWith('other.xlsx', 'other.xlsx', undefined);
});

it('does not attach when cancelled', async () => {
  const { user, onSelect, onClose } = setup();
  await user.click(await screen.findByRole('button', { name: 'report.xlsx' }));
  await user.click(screen.getByRole('button', { name: 'com_ui_cancel' }));
  expect(onSelect).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalled();
});

it('opens the selected Word original without sending it to chat', async () => {
  const { user, onSelect } = setup();
  await user.click(await screen.findByRole('button', { name: 'card.docx' }));
  await user.click(screen.getByRole('button', { name: 'com_office_word_preview' }));
  const frame = await screen.findByTitle('com_office_word_preview');
  expect(frame).toHaveAttribute('srcdoc', '<p>preview</p>');
  expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
  expect(onSelect).not.toHaveBeenCalled();
});
