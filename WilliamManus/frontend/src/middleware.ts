import { NextResponse, type NextRequest } from 'next/server'

const PUBLIC_PATHS = new Set(['/', '/legal', '/invitation', '/claude-code-ui'])
const PUBLIC_PREFIXES = ['/auth', '/share', '/api', '/_next', '/downloads']
const PUBLIC_FILE_REGEX = /\.(?:svg|png|jpg|jpeg|gif|webp|ico|css|js|map|txt|xml)$/i

function isPublicPath(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) {
    return true
  }

  if (PUBLIC_FILE_REGEX.test(pathname)) {
    return true
  }

  return PUBLIC_PREFIXES.some((prefix) =>
    pathname === prefix || pathname.startsWith(`${prefix}/`),
  )
}

function readJsonCookie<T>(rawValue?: string): T | null {
  if (!rawValue) {
    return null
  }

  try {
    return JSON.parse(decodeURIComponent(rawValue)) as T
  } catch {
    return null
  }
}

function readJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const [, payload] = token.split('.')
    if (!payload) {
      return null
    }

    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padding = '='.repeat((4 - (normalized.length % 4)) % 4)
    const decoded = atob(`${normalized}${padding}`)
    return JSON.parse(decoded) as Record<string, unknown>
  } catch {
    return null
  }
}

function resolveRoysAlphaAccess(request: NextRequest): boolean | null {
  const authToken = request.cookies.get('auth_token')?.value
  if (authToken) {
    const tokenPayload = readJwtPayload(authToken)
    if (typeof tokenPayload?.can_access_roys_alpha === 'boolean') {
      return tokenPayload.can_access_roys_alpha
    }
  }

  const sessionPayload = readJsonCookie<{
    user?: {
      capabilities?: {
        can_access_roys_alpha?: boolean
      }
    }
  }>(request.cookies.get('auth_session')?.value)

  if (
    typeof sessionPayload?.user?.capabilities?.can_access_roys_alpha === 'boolean'
  ) {
    return sessionPayload.user.capabilities.can_access_roys_alpha
  }

  return null
}

export async function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl

  if (isPublicPath(pathname)) {
    return NextResponse.next({
      request,
    })
  }

  const authToken = request.cookies.get('auth_token')?.value
  if (authToken) {
    const canAccessRoysAlpha = resolveRoysAlphaAccess(request)

    if (canAccessRoysAlpha === false) {
      return NextResponse.redirect(new URL('/', request.url))
    }

    return NextResponse.next({
      request,
    })
  }

  const redirectUrl = new URL('/auth', request.url)
  redirectUrl.searchParams.set('returnUrl', `${pathname}${search}`)

  return NextResponse.redirect(redirectUrl)
}

export const config = {
  matcher: [
    /*
     * Match all request paths except for the ones starting with:
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     * Feel free to modify this pattern to include more paths.
     */
    '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
} 
