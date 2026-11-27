import { boundTitleOutput } from './titleBudget';
it('bounds auxiliary titles while preserving routing and credentials by reference', () => {
  const configuration={baseURL:'https://gateway.test'};
  const options={model:'title-model',maxTokens:4096,configuration};
  const result=boundTitleOutput(options);
  expect(result.maxTokens).toBe(128);
  expect(result.configuration).toBe(configuration);
  expect(options.maxTokens).toBe(4096);
});
