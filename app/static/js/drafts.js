/**
 * Unsaved editor text kept in localStorage, so it survives a closed tab or a crash.
 * Keys: plainnotes.draft.<note id>, or plainnotes.draft.new for a note not saved yet.
 */

const PREFIX = "plainnotes.draft.";

const keyFor = (noteId) => PREFIX + (noteId ?? "new");

/**
 * The stored draft for a note, or null.
 * @param {string|null} noteId null for a new note
 * @returns {{title: string, content: string, base_revision: string|null, saved_at: string}|null}
 */
export function loadDraft(noteId) {
  try {
    const draft = JSON.parse(localStorage.getItem(keyFor(noteId)) ?? "null");
    const valid =
      typeof draft?.title === "string" &&
      typeof draft.content === "string" &&
      typeof draft.saved_at === "string";
    return valid ? draft : null;
  } catch {
    return null;
  }
}

/**
 * Store a draft.
 * @param {string|null} noteId
 * @param {{title: string, content: string, base_revision: string|null}} draft
 */
export function saveDraft(noteId, draft) {
  try {
    localStorage.setItem(keyFor(noteId), JSON.stringify({ ...draft, saved_at: new Date().toISOString() }));
  } catch {
    // Storage full or unavailable (private browsing): there is just no draft.
  }
}

/**
 * Remove a note's draft.
 * @param {string|null} noteId
 */
export function removeDraft(noteId) {
  try {
    localStorage.removeItem(keyFor(noteId));
  } catch {
    // Nothing to remove.
  }
}

/** Remove every draft (on logout). */
export function removeAllDrafts() {
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith(PREFIX)) localStorage.removeItem(key);
    }
  } catch {
    // Nothing to remove.
  }
}
