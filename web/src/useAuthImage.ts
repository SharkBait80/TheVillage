// useAuthImage — load a Cognito-protected asset image into an <img>-usable URL.
//
// The /assets/{subjectId} route requires `Authorization: Bearer <idToken>` and
// returns a 302 redirect to a presigned S3 URL, so a bare <img src> (or an
// `?access_token=` query param) will NOT authenticate. Instead we fetch the URL
// with the bearer header, let fetch follow the redirect, and turn the resulting
// bytes into a blob object URL suitable for an <img src>.
//
// While loading (or on any error) the caller-supplied placeholder is returned,
// so the UI always shows the cute fallback art (Req16.7). The object URL is
// revoked on unmount / when the subject changes to avoid leaks.

import { useEffect, useState } from 'react'
import { fetchAssetObjectUrl } from './api'

export interface AuthImageState {
  /** URL to put on <img src>: the blob URL once loaded, else the placeholder. */
  src: string
  /** True until the first fetch attempt settles. */
  loading: boolean
  /** True when the fetch failed and we fell back to the placeholder. */
  failed: boolean
}

/**
 * Resolve an authenticated asset image URL for `subjectId`, falling back to
 * `placeholder` while loading or on failure. In mock mode the API returns a
 * data URL (or null), handled transparently.
 */
export function useAuthImage(subjectId: string | null | undefined, placeholder: string): AuthImageState {
  const [state, setState] = useState<AuthImageState>({ src: placeholder, loading: Boolean(subjectId), failed: false })

  useEffect(() => {
    let active = true
    const ac = new AbortController()
    let objectUrl: string | null = null

    if (!subjectId) {
      setState({ src: placeholder, loading: false, failed: false })
      return () => ac.abort()
    }

    setState({ src: placeholder, loading: true, failed: false })

    fetchAssetObjectUrl(subjectId, ac.signal)
      .then((url) => {
        if (!active) {
          if (url && url.startsWith('blob:')) URL.revokeObjectURL(url)
          return
        }
        if (url) {
          objectUrl = url.startsWith('blob:') ? url : null
          setState({ src: url, loading: false, failed: false })
        } else {
          // No asset (e.g. mock returns null) — keep the placeholder.
          setState({ src: placeholder, loading: false, failed: false })
        }
      })
      .catch((err) => {
        if ((err as Error)?.name === 'AbortError') return
        if (!active) return
        setState({ src: placeholder, loading: false, failed: true })
      })

    return () => {
      active = false
      ac.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [subjectId, placeholder])

  return state
}
