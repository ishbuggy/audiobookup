// src/static/js/modules/cover-cache.js
//
// Shared per-session cover cache-buster registry.
//
// When a user re-uploads a book's cover, the server reuses the same
// `/covers/<asin>_original.jpg` / `_thumb.jpg` URLs for the replacement image, so
// without a per-session buster the browser can keep serving the stale cached
// bytes anywhere those bare URLs are set fresh — the detail modal on reopen, and
// (crucially) the grid thumbnail after any `fetchUpdates` refresh replaces the
// library objects with the server's bare `cover_url`s. Both the modal and the
// library grid share this one registry so a cover updated in the modal stays
// busted in the grid across refreshes. Lives in its own module to keep the map a
// single source of truth without a modal <-> library import cycle.

// ASIN -> cache-buster token (a timestamp) for covers replaced this session.
const coverCacheBusters = {};

// Record that `asin`'s cover changed this session and return the new token so the
// caller can build an immediately-busted URL.
export function setCoverBuster(asin) {
    coverCacheBusters[asin] = Date.now();
    return coverCacheBusters[asin];
}

// Append this session's cache-buster to a cover URL when the book's cover was
// re-uploaded, so a freshly-set src shows the new art rather than the cached one.
// A URL for an untouched book is returned unchanged; an empty/missing url yields "".
export function bustCoverUrl(asin, url) {
    if (!url) return "";
    const token = coverCacheBusters[asin];
    return token ? `${url}?t=${token}` : url;
}
