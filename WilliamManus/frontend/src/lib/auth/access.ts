export type RoysAlphaAccessUserLike =
  | {
      capabilities?: {
        can_access_roys_alpha?: boolean;
      } | null;
    }
  | null
  | undefined;

export function userCanAccessRoysAlpha(
  user: RoysAlphaAccessUserLike,
): boolean {
  return user?.capabilities?.can_access_roys_alpha === true;
}

export function userCannotAccessRoysAlpha(
  user: RoysAlphaAccessUserLike,
): boolean {
  return user?.capabilities?.can_access_roys_alpha === false;
}

export function getPostAuthRedirect(
  user: RoysAlphaAccessUserLike,
  requestedUrl?: string | null,
): string {
  if (userCannotAccessRoysAlpha(user)) {
    return '/';
  }

  return requestedUrl || '/dashboard';
}
