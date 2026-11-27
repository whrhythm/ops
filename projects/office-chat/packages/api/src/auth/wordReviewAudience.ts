/** Office editing pilot scoped to the authenticated account on this deployment. */
export function wordSectionReviewEnabled(user?: { email?: string } | null): boolean {
  const audience = process.env.STARBRIDGE_WORD_SECTION_AUDIENCE;
  return !!user?.email && (audience === 'all' || audience === user.email);
}
