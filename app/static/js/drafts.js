/**
 * Independent editor drafts in IndexedDB. Scope includes the installation path.
 * Logout advances an epoch atomically, so stale tabs cannot recreate erased drafts.
 */
const SCOPE = location.pathname;
const EPOCH_KEY = JSON.stringify([SCOPE, "epoch"]);
let database = null;
let sessionSerial = 0;
const channel = typeof BroadcastChannel === "function"
  ? new BroadcastChannel(`plainnotes:logout:${SCOPE}`) : null;
channel?.addEventListener("message", () => {
  sessionSerial += 1;
  window.dispatchEvent(new Event("plainnotes:logout"));
});

export function notifyLogout() {
  sessionSerial += 1;
  channel?.postMessage("logout");
}

// Exceptions in IDB callbacks must abort the transaction, not escape as page errors.
function onSuccess(request, callback) {
  request.addEventListener("success", () => {
    try { callback(); }
    catch (error) {
      if (request.transaction) request.transaction.abort();
      else throw error;
    }
  });
}

function openDatabase() {
  if (!database) {
    database = new Promise((resolve, reject) => {
      const request = indexedDB.open("plainnotes-drafts", 1);
      request.addEventListener("upgradeneeded", () => request.result.createObjectStore("drafts", { keyPath: "key" }));
      request.addEventListener("success", () => {
        const db = request.result;
        db.addEventListener("versionchange", () => { db.close(); database = null; });
        resolve(db);
      });
      request.addEventListener("error", () => reject(request.error));
      request.addEventListener("blocked", () => reject(new Error("Draft storage is blocked by another tab.")));
    }).catch((error) => { database = null; throw error; });
  }
  return database;
}

async function transaction(mode, run) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("drafts", mode);
    let result;
    tx.addEventListener("complete", () => resolve(result));
    tx.addEventListener("abort", () => reject(tx.error ?? new Error("Draft storage failed.")));
    run(tx.objectStore("drafts"), (value) => { result = value; }, tx);
  });
}

function keyFor(noteId, owner) {
  return JSON.stringify([SCOPE, noteId, owner.id]);
}

/** Start a distinct editor, including when a browser duplicates a tab. */
export async function beginEditor() {
  const serial = sessionSerial;
  const bytes = crypto.getRandomValues(new Uint32Array(4));
  const id = [...bytes].map((value) => value.toString(16)).join("-");
  const epoch = await transaction("readonly", (store, done) => {
    const request = store.get(EPOCH_KEY);
    onSuccess(request, () => done(request.result?.value ?? 0));
  });
  return { id, epoch, serial };
}

/** List all recovery candidates for a note (null: new notes), newest first. */
export async function loadDrafts(noteId) {
  const records = await transaction("readonly", (store, done) => {
    const request = store.getAll();
    onSuccess(request, () => done(request.result.filter(
      (item) => item.scope === SCOPE && item.kind === "draft" && item.noteId === noteId,
    )));
  });
  return records.sort((a, b) => (b.saved_at ?? "").localeCompare(a.saved_at ?? ""));
}

export async function saveDraft(noteId, draft, owner) {
  const saved = await transaction("readwrite", (store, done) => {
    const request = store.get(EPOCH_KEY);
    onSuccess(request, () => {
      if (owner.serial !== sessionSerial || (request.result?.value ?? 0) !== owner.epoch) { done(false); return; }
      store.put({
        ...draft, key: keyFor(noteId, owner), scope: SCOPE, kind: "draft", noteId,
        saved_at: new Date().toISOString(),
      });
      done(true);
    });
  });
  if (!saved) {
    window.dispatchEvent(new Event("plainnotes:logout"));
    throw new Error("This editor's session ended. Log in again before editing.");
  }
}

/** Remove only this editor's draft. */
export async function removeDraft(noteId, owner) {
  if (!owner) return;
  await transaction("readwrite", (store) => store.delete(keyFor(noteId, owner)));
}

/** Discard a candidate only if another editor has not updated it since it was shown. */
export async function discardDraft(draft) {
  await transaction("readwrite", (store) => {
    const request = store.get(draft.key);
    onSuccess(request, () => {
      const current = request.result;
      if (current?.saved_at === draft.saved_at && current?.content === draft.content
          && current?.title === draft.title && current?.rawContent === draft.rawContent) store.delete(draft.key);
    });
  });
}

/** Identical text is safely on the server; independent different drafts must remain. */
export async function removeMatchingDrafts(noteId, title, content, rawContent) {
  const records = await loadDrafts(noteId);
  for (const record of records) {
    if (record.title === title && record.content === content && record.rawContent === rawContent) {
      await discardDraft(record);
    }
  }
}

/** Explicit logout clears this installation and invalidates every currently open editor. */
export async function removeAllDrafts() {
  await transaction("readwrite", (store) => {
    const epoch = store.get(EPOCH_KEY);
    onSuccess(epoch, () => {
      const request = store.getAll();
      onSuccess(request, () => {
        for (const record of request.result) {
          if (record.scope === SCOPE) store.delete(record.key);
        }
        store.put({ key: EPOCH_KEY, scope: SCOPE, kind: "epoch", value: (epoch.result?.value ?? 0) + 1 });
      });
    });
  });
  notifyLogout();
}
