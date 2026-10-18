/**
 * The only module that talks to the server. All URLs are relative to the page (<BASE>/).
 */

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** An error response from the API, with its machine code and message. */
export class ApiError extends Error {
  /**
   * @param {number} status HTTP status (0 for network errors)
   * @param {string} code machine-readable code
   * @param {string} detail human-readable message
   * @param {Object} [body] the full error body
   * @param {Headers|null} [headers]
   */
  constructor(status, code, detail, body = {}, headers = null) {
    super(detail);
    this.status = status;
    this.code = code;
    this.body = body;
    this.headers = headers;
  }
}

function goToLogin() {
  const current = location.hash || "#/";
  if (!current.startsWith("#/login")) {
    location.hash = `#/login?next=${encodeURIComponent(current)}`;
  }
}

async function request(method, path, { json, query } = {}) {
  const url = query ? `${path}?${new URLSearchParams(query)}` : path;
  const headers = {};
  if (MUTATING.has(method)) headers["X-Requested-With"] = "plainnotes";
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  let response;
  try {
    response = await fetch(url, { method, headers, body, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the server.");
  }
  if (response.ok) return response.status === 204 ? null : response.json();
  let errorBody = {};
  try {
    errorBody = await response.json();
  } catch {
    // Not JSON (for example an error page from a proxy); keep the status text.
  }
  if (response.status === 401 && path !== "api/login") goToLogin();
  throw new ApiError(
    response.status,
    errorBody.code ?? "error",
    errorBody.detail ?? `${response.status} ${response.statusText}`,
    errorBody,
    response.headers,
  );
}

// Attachment types the server sends inline (app/attachments.py); everything else downloads.
const INLINE_EXTENSION =
  /\.(png|jpe?g|gif|webp|avif|bmp|ico|mp3|m4a|ogg|oga|opus|wav|flac|mp4|m4v|webm|ogv|mov|txt|log|md|csv)$/i;
const IMAGE_EXTENSION = /\.(png|jpe?g|gif|webp|avif|bmp|ico)$/i;

/** @returns {boolean} true if the browser shows this attachment itself */
export const opensInBrowser = (name) => INLINE_EXTENSION.test(name);
/** @returns {boolean} true for image attachments */
export const isImage = (name) => IMAGE_EXTENSION.test(name);

const notePath = (id) => `api/notes/${encodeURIComponent(id)}`;

/** @returns {string} the URL of one attachment */
export const attachmentUrl = (noteId, name) =>
  `${notePath(noteId)}/attachments/${encodeURIComponent(name)}`;

/** Log in and receive the session cookie. */
export const login = (username, password) =>
  request("POST", "api/login", { json: { username, password } });
/** End the session. */
export const logout = () => request("POST", "api/logout");
/** @returns {Promise<{username: string, read_only: boolean}>} */
export const getSession = () => request("GET", "api/session");
/** @returns {Promise<Object[]>} all notes, newest first */
export const listNotes = () => request("GET", "api/notes");
/** @returns {Promise<Object>} one note with content and revision */
export const getNote = (id) => request("GET", notePath(id));
/** Create a note. */
export const createNote = (title, content) =>
  request("POST", "api/notes", { json: { title, content } });
/** Save and/or rename a note; fails with revision_conflict if it changed. */
export const updateNote = (id, title, content, baseRevision) =>
  request("PUT", notePath(id), { json: { title, content, base_revision: baseRevision } });
/** Move a note to the trash. */
export const deleteNote = (id) => request("DELETE", notePath(id));
/** Search; params may hold q, sort and order. */
export const searchNotes = (params) => request("GET", "api/search", { query: params });
/** @returns {Promise<{tag: string, count: number}[]>} */
export const listTags = () => request("GET", "api/tags");
/** @returns {Promise<Object[]>} the attachments of a note */
export const listAttachments = (noteId) => request("GET", `${notePath(noteId)}/attachments`);
/** Delete one attachment permanently. */
export const deleteAttachment = (noteId, name) => request("DELETE", attachmentUrl(noteId, name));
/** @returns {Promise<Object[]>} trash entries, newest first */
export const listTrash = () => request("GET", "api/trash");
/** Restore a note from the trash. */
export const restoreTrash = (trashId) =>
  request("POST", `api/trash/${encodeURIComponent(trashId)}/restore`);
/** Delete one trash entry permanently. */
export const deleteTrash = (trashId) => request("DELETE", `api/trash/${encodeURIComponent(trashId)}`);
/** Delete everything in the trash. */
export const emptyTrash = () => request("DELETE", "api/trash");

/**
 * Upload one file as the raw request body, reporting progress.
 * @param {string} noteId
 * @param {Blob} file
 * @param {string} name requested file name (the server may change it)
 * @param {(fraction: number) => void} onProgress
 * @returns {{promise: Promise<Object>, abort: () => void}}
 */
export function uploadAttachment(noteId, file, name, onProgress) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open("PUT", attachmentUrl(noteId, name));
    xhr.setRequestHeader("X-Requested-With", "plainnotes");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let body = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // Keep the empty body.
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body);
        return;
      }
      if (xhr.status === 401) goToLogin();
      reject(new ApiError(xhr.status, body.code ?? "error", body.detail ?? "Upload failed.", body));
    });
    xhr.addEventListener("error", () =>
      reject(new ApiError(0, "network", "Upload failed: cannot reach the server.")),
    );
    xhr.addEventListener("abort", () => reject(new ApiError(0, "aborted", "Upload cancelled.")));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}
