/** Auxiliary titles must not reserve a full document-generation response. */
export function boundTitleOutput<T extends Record<string, unknown>>(options: T): T & { maxTokens: number } {
  return { ...options, maxTokens: 128 };
}
