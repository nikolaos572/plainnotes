/**
 * The editor (#/note/<id>/edit and #/new): a plain textarea that never changes what you type.
 */

import * as api from "../api.js";
import { el, formatDate, formatSize } from "../dom.js";
import * as drafts from "../drafts.js";
import { icon } from "../icons.js";
import { confirmAction, errorPage, showDialog, showError, toast } from "../ui.js";
import { attachmentLink, wrapToggle } from "./note.js";

/**
 * Insert text at the cursor, replacing the selection, keeping undo history where possible.
 * @param {HTMLTextAreaElement} field
 * @param {string} text
 */
export function insertAtCursor(field, text) {
  field.focus();
  if (!document.execCommand("insertText", false, text)) {
    field.setRangeText(text, field.selectionStart, field.selectionEnd, "end");
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

/**
 * Markdown that links to an attachment: ![name](…) for images, [name](…) otherwise.
 * @param {string} name
 * @returns {string}
 */
export function attachmentMarkdown(name) {
  const label = name.replace(/[\\[\]]/g, "\\$&");
  const target = encodeURIComponent(name).replace(/\(/g, "%28").replace(/\)/g, "%29");
  return `${api.isImage(name) ? "!" : ""}[${label}](attachments/${target})`;
}

function pastedName(file) {
  const now = new Date();
  const pad = (number) => String(number).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-` +
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const byType = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" };
  const extension = byType[file.type] ?? /\.([A-Za-z0-9]{1,10})$/.exec(file.name)?.[1] ?? "bin";
  return `pasted-${stamp}.${extension}`;
}

function askSaveChanges() {
  return showDialog({
    title: "Save changes?",
    message: "This note has unsaved changes.",
    buttons: [
      { label: "Save", value: "save", kind: "primary", icon: "save" },
      { label: "Discard", value: "discard", kind: "danger", icon: "trash-2" },
      { label: "Cancel", value: "cancel", icon: "x" },
    ],
    cancelValue: "cancel",
  });
}

function askAboutConflict() {
  return showDialog({
    title: "This note was changed somewhere else since you opened it",
    message:
      "Overwrite the other version with yours, or load the latest version into the editor. " +
      "Cancel to keep editing (you can copy your text first).",
    buttons: [
      { label: "Overwrite", value: "overwrite", kind: "danger", icon: "save" },
      { label: "Load latest", value: "load", icon: "refresh-cw" },
      { label: "Cancel", value: "cancel", icon: "x" },
    ],
    cancelValue: "cancel",
  });
}

function askSaveAsNew() {
  return showDialog({
    title: "This note no longer exists",
    message: "It was deleted or moved to the trash while you were editing.",
    buttons: [
      { label: "Save as new note", value: "new", kind: "primary", icon: "file-plus" },
      { label: "Cancel", value: "cancel", icon: "x" },
    ],
    cancelValue: "cancel",
  });
}

/**
 * Render the editor.
 * @param {{id?: string, query?: URLSearchParams}} params id is missing for a new note
 * @param {Object} app
 */
export async function editorView({ id, query }, app) {
  if (app.session?.read_only) {
    app.navigate(id ? `#/note/${encodeURIComponent(id)}` : "#/", { replace: true });
    return { node: el("section") };
  }
  let note = null;
  let missing = false;
  if (id) {
    try {
      note = await api.getNote(id);
    } catch (error) {
      if (error.code !== "not_found") return { node: errorPage(error) };
      try {
        if (!(await drafts.loadDrafts(id)).length) return { node: errorPage(error) };
      } catch {
        return { node: errorPage(error) };
      }
      missing = true;
    }
  }

  const state = {
    id: note?.id ?? id ?? null,
    revision: note?.revision ?? null,
    // Retain raw bytes when only the title changes, and the convention for text edits.
    rawContent: "",
    rawValue: "",
    loadedContent: "",
    lineEnding: "\n",
    loadedTitle: "",
    loadedValue: "",
    saving: false,
    leaving: false,
  };

  let destroyed = false;

  const titleInput = el("input", {
    id: "note-title",
    class: "title-input",
    type: "text",
    autocomplete: "off",
    spellcheck: "false",
    placeholder: "Title",
    "aria-describedby": "title-error",
  });
  const titleError = el("p", { id: "title-error", class: "field-error", "aria-live": "polite" });
  const textarea = el("textarea", {
    id: "note-content",
    class: "editor-text",
    spellcheck: "false",
    autocomplete: "off",
    autocorrect: "off",
    autocapitalize: "off",
    wrap: "soft",
    translate: "no",
    "aria-label": "Note text",
  });
  // "off" keeps long lines (such as commands) on one line, with horizontal scrolling.
  const wrapControl = wrapToggle((wrap) => textarea.setAttribute("wrap", wrap ? "soft" : "off"));
  const dot = el("span", { class: "dirty-dot", "aria-hidden": "true", hidden: true }, "●");
  const saveButton = el("button", { type: "button", class: "button primary" }, icon("save"), "Save", dot);
  const closeButton = el("button", { type: "button", class: "button" }, icon("x"), "Close");

  function load(title, content) {
    titleInput.value = title;
    textarea.value = content;
    // Compare against the textarea's own value, so line endings it normalized don't count.
    state.loadedTitle = titleInput.value;
    state.loadedValue = textarea.value;
    state.loadedContent = content;
    rememberRaw(content, textarea.value);
  }

  function rememberRaw(content, value) {
    state.rawContent = content;
    state.rawValue = value;
    state.lineEnding = content.includes("\r\n") ? "\r\n"
      : content.includes("\r") && !content.includes("\n") ? "\r" : "\n";
  }

  function contentForSave() {
    const value = textarea.value;
    return value === state.rawValue ? state.rawContent : value.replace(/\n/g, state.lineEnding);
  }

  function isDirty() {
    return titleInput.value !== state.loadedTitle || textarea.value !== state.loadedValue
      || contentForSave() !== state.loadedContent;
  }

  function refresh() {
    if (destroyed) return;
    const dirty = isDirty();
    dot.hidden = !dirty;
    saveButton.setAttribute("aria-label", dirty ? "Save (unsaved changes)" : "Save");
    app.setTitle(`${dirty ? "• " : ""}${state.loadedTitle || "New note"}`);
  }

  // --- drafts -----------------------------------------------------------------
  let draftTimer = null;
  let owner = null;
  const ownerReady = drafts.beginEditor().catch(() => null);
  const priorDraftIds = new Set();
  let draftWork = Promise.resolve(true);
  let restoredDraft = null;
  const draftWarning = el("p", {
    class: "field-error", role: "alert", hidden: true, id: "draft-warning",
  });

  function queueDraft(operation) {
    draftWork = draftWork.then(async () => {
      owner ??= await ownerReady;
      if (!owner) throw new Error("Draft storage is unavailable.");
      await operation(owner);
      draftWarning.hidden = true;
      return true;
    }).catch(() => {
      draftWarning.textContent =
        "Your recovery draft could not be stored. Save this note or copy your text before closing.";
      draftWarning.hidden = false;
      return false;
    });
    return draftWork;
  }

  function writeDraft() {
    clearTimeout(draftTimer);
    draftTimer = null;
    const noteId = state.id;
    const draft = {
      title: titleInput.value, content: textarea.value, base_revision: state.revision,
      rawContent: contentForSave(),
    };
    const dirty = isDirty();
    return queueDraft((editor) => dirty
      ? drafts.saveDraft(noteId, draft, editor)
      : drafts.removeDraft(noteId, editor));
  }

  function dropDraft() {
    clearTimeout(draftTimer);
    draftTimer = null;
    const noteId = state.id;
    restoredDraft = null;
    return queueDraft(async (editor) => {
      await drafts.removeDraft(noteId, editor);
    });
  }

  function onEdit() {
    refresh();
    clearTimeout(draftTimer);
    draftTimer = setTimeout(writeDraft, 250);
  }

  async function offerDraft() {
    try {
      const candidates = await drafts.loadDrafts(state.id);
      for (const draft of candidates) {
        if (destroyed) return;
        if (draft.title === state.loadedTitle && draft.content === state.loadedValue
            && (draft.rawContent ?? state.loadedContent) === state.loadedContent) {
          await drafts.discardDraft(draft);
          continue;
        }
        const choice = await showDialog({
          title: `Restore unsaved draft from ${formatDate(draft.saved_at)}?`,
          message: missing
            ? "The original note no longer exists. Restore this draft, then save it as a new note."
            : `This browser kept a draft of “${draft.title}”. Other drafts are kept independently.`,
          buttons: [
            { label: "Restore", value: "restore", kind: "primary", icon: "history" },
            { label: "Discard", value: "discard", kind: "danger", icon: "trash-2" },
            { label: "Keep for later", value: "keep", icon: "x" },
          ],
        });
        if (destroyed) return;
        if (choice === "restore") {
          titleInput.value = draft.title;
          textarea.value = draft.content;
          const raw = draft.rawContent ?? draft.content.replace(/\n/g, state.lineEnding);
          rememberRaw(raw, textarea.value);
          state.revision = draft.base_revision ?? state.revision;
          restoredDraft = draft;
          refresh();
          await writeDraft();
          return;
        }
        if (choice !== "discard") return;
        await drafts.discardDraft(draft);
      }
    } catch {
      draftWarning.textContent =
        "Recovery drafts are unavailable. Save this note or copy your text before closing.";
      draftWarning.hidden = false;
    }
  }

  async function applySaved(saved, sentTitle, sentValue) {
    const oldId = state.id;
    const wasNew = oldId === null;
    if (wasNew) priorDraftIds.add(oldId);
    state.id = saved.id;
    state.revision = saved.revision;
    if (titleInput.value === sentTitle) titleInput.value = saved.title;
    state.loadedTitle = saved.title;
    state.loadedValue = sentValue;
    state.loadedContent = saved.content;
    rememberRaw(saved.content, sentValue);
    if (wasNew && !destroyed) {
      app.replaceRoute(`#/note/${encodeURIComponent(saved.id)}/edit`);
    }
    if (wasNew) renderPanel();
    refresh();
    // Persist edits typed during the request before deleting their previous recovery key.
    if (!(await writeDraft())) return;
    await queueDraft(async (editor) => {
      for (const priorId of priorDraftIds) {
        await drafts.removeDraft(priorId, editor);
        await drafts.removeMatchingDrafts(priorId, saved.title, sentValue, saved.content);
        priorDraftIds.delete(priorId);
      }
      await drafts.removeMatchingDrafts(saved.id, saved.title, sentValue, saved.content);
      if (restoredDraft?.content === sentValue && restoredDraft?.title === saved.title
          && (restoredDraft.rawContent === undefined || restoredDraft.rawContent === saved.content)) {
        await drafts.discardDraft(restoredDraft);
        restoredDraft = null;
      }
    });
  }

  async function attemptSave() {
    for (;;) {
      if (destroyed) return false;
      const title = titleInput.value;
      const value = textarea.value;
      const content = contentForSave();
      titleError.textContent = "";
      try {
        const saved = state.id
          ? await api.updateNote(state.id, title, content, state.revision)
          : await api.createNote(title, content);
        await applySaved(saved, title, value);
        if (!destroyed) toast("Saved");
        return !isDirty();
      } catch (error) {
        if (destroyed) return false;
        if (error.code === "revision_conflict") {
          const current = error.body.current;
          const choice = await askAboutConflict();
          if (choice === "overwrite") {
            state.revision = current.revision;
            continue;
          }
          if (
            choice === "load" &&
            (await confirmAction(
              "Load the latest version?",
              "Your unsaved changes in this editor will be replaced.",
              { label: "Load latest", kind: "danger", icon: "refresh-cw" },
            ))
          ) {
            state.revision = current.revision;
            load(current.title, current.content);
            dropDraft();
            refresh();
          }
          return false;
        }
        if (error.code === "not_found" && state.id) {
          if ((await askSaveAsNew()) !== "new") return false;
          await writeDraft();
          priorDraftIds.add(state.id);
          state.id = null;
          state.revision = null;
          continue;
        }
        if (error.code === "invalid_title" || error.code === "title_exists") {
          titleError.textContent = error.message;
          titleInput.focus();
          return false;
        }
        showError(error);
        return false;
      }
    }
  }

  /** Save if anything changed; resolves to true when nothing is left unsaved. */
  async function save() {
    if (state.saving) return false;
    if (!isDirty()) return true;
    state.saving = true;
    saveButton.disabled = true;
    try {
      return await attemptSave();
    } finally {
      state.saving = false;
      saveButton.disabled = false;
    }
  }

  /** Ask what to do with unsaved changes; resolves to true if it is fine to leave. */
  async function resolveUnsaved({ deferDiscard = false } = {}) {
    if (state.leaving || !isDirty()) return true;
    const choice = await askSaveChanges();
    if (choice === "save") return save();
    if (choice === "discard" && !deferDiscard) await dropDraft();
    return choice === "discard";
  }

  async function close() {
    if (destroyed) return;
    if (!(await resolveUnsaved()) || destroyed) return;
    state.leaving = true;
    app.navigate(state.id ? `#/note/${encodeURIComponent(state.id)}` : "#/");
  }

  function onBeforeUnload(event) {
    if (draftTimer) writeDraft();
    if ((!state.leaving && isDirty()) || uploading) {
      event.preventDefault();
      event.returnValue = "";
    }
  }


  // --- attachments ------------------------------------------------------------
  let files = note?.attachments ?? [];
  const uploads = [];
  let uploading = false;
  const panel = el("section", { class: "attachments-panel", "aria-labelledby": "attachments-title" });
  const fileRows = el("ul", { class: "attachment-rows" });
  const uploadRows = el("ul", { class: "attachment-rows" });
  const picker = el("input", { type: "file", multiple: true, hidden: true, "aria-label": "Files to upload" });

  function renderFiles() {
    fileRows.replaceChildren(
      ...files.map((file) => {
        const inline = api.opensInBrowser(file.name);
        const open = attachmentLink(state.id, file, inline ? "Open" : "Download");
        open.classList.add("button", "small");
        open.prepend(icon(inline ? "external-link" : "download"));
        return el(
          "li",
          {},
          el("span", { class: "name" }, file.name),
          el("span", { class: "muted" }, formatSize(file.size)),
          el(
            "button",
            {
              type: "button",
              class: "button small",
              on: { click: () => insertAtCursor(textarea, attachmentMarkdown(file.name)) },
            },
            icon("link"),
            "Insert link",
          ),
          open,
          el(
            "button",
            { type: "button", class: "button small danger", on: { click: () => removeFile(file) } },
            icon("trash-2"),
            "Delete",
          ),
        );
      }),
    );
  }

  function renderPanel() {
    const heading = el("h2", { id: "attachments-title" }, "Attachments");
    if (!state.id) {
      panel.replaceChildren(heading, el("p", { class: "muted" }, "Save the note to add attachments."));
      return;
    }
    const upload = el(
      "button",
      { type: "button", class: "button", on: { click: () => picker.click() } },
      icon("upload"),
      "Upload files",
    );
    panel.replaceChildren(heading, fileRows, uploadRows, upload, picker);
    renderFiles();
  }

  async function refreshFiles() {
    try {
      files = await api.listAttachments(state.id);
      renderFiles();
    } catch (error) {
      showError(error);
    }
  }

  async function removeFile(file) {
    const message = `“${file.name}” will be deleted permanently.`;
    const button = { label: "Delete", kind: "danger", icon: "trash-2" };
    if (!(await confirmAction("Delete attachment?", message, button))) return;
    try {
      await api.deleteAttachment(state.id, file.name);
      toast("Attachment deleted");
    } catch (error) {
      showError(error);
    }
    await refreshFiles();
  }

  async function uploadOne({ file, name }) {
    const progress = el("progress", { max: "1", value: "0", "aria-label": `Uploading ${name}` });
    const transfer = api.uploadAttachment(state.id, file, name, (fraction) => {
      progress.value = fraction;
    });
    const cancel = el(
      "button",
      { type: "button", class: "button small", on: { click: () => transfer.abort() } },
      icon("x"),
      "Cancel",
    );
    const row = el("li", { class: "upload-row" }, el("span", { class: "name" }, name), progress, cancel);
    uploadRows.append(row);
    try {
      const saved = await transfer.promise;
      toast(`Uploaded ${saved.name}`);
    } catch (error) {
      if (error.code !== "aborted") showError(error);
    } finally {
      row.remove();
    }
  }

  /** Upload files one at a time, in order. */
  async function enqueue(items) {
    if (!state.id) {
      toast("Save the note to add attachments", "error");
      return;
    }
    uploads.push(...items);
    if (uploading) return;
    uploading = true;
    while (uploads.length) await uploadOne(uploads.shift());
    uploading = false;
    await refreshFiles();
  }

  const asItems = (list) => [...list].map((file) => ({ file, name: file.name }));
  const hasFiles = (event) => [...(event.dataTransfer?.types ?? [])].includes("Files");
  picker.addEventListener("change", () => {
    enqueue(asItems(picker.files));
    picker.value = "";
  });
  textarea.addEventListener("paste", (event) => {
    const pasted = [...(event.clipboardData?.files ?? [])];
    if (!pasted.length) return;
    event.preventDefault();
    enqueue(pasted.map((file) => ({ file, name: pastedName(file) })));
  });

  load(note?.title ?? query?.get("title") ?? "", note?.content ?? "");
  if (!note) state.loadedTitle = "";

  titleInput.addEventListener("input", onEdit);
  textarea.addEventListener("input", onEdit);
  textarea.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.isComposing) return;
    event.preventDefault();
    insertAtCursor(textarea, "\t");
  });
  saveButton.addEventListener("click", save);
  closeButton.addEventListener("click", close);
  window.addEventListener("beforeunload", onBeforeUnload);

  const node = el(
    "section",
    { class: "editor" },
    el(
      "div",
      { class: "editor-bar" },
      el("label", { for: "note-title", class: "visually-hidden" }, "Title"),
      titleInput,
      el("div", { class: "editor-actions" }, wrapControl, saveButton, closeButton),
    ),
    titleError,
    draftWarning,
    textarea,
    panel,
  );
  renderPanel();
  node.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    node.classList.add("dragging");
  });
  node.addEventListener("dragleave", (event) => {
    if (!node.contains(event.relatedTarget)) node.classList.remove("dragging");
  });
  node.addEventListener("drop", (event) => {
    node.classList.remove("dragging");
    if (!event.dataTransfer?.files.length) return;
    event.preventDefault();
    enqueue(asItems(event.dataTransfer.files));
  });

  return {
    node,
    title: state.loadedTitle || "New note",
    mounted() {
      refresh();
      (state.id ? textarea : titleInput).focus();
      if (state.id) {
        // Start at the top. Setting the value left the cursor at the end, and focusing scrolled
        // there; moving the cursor back does not scroll back by itself.
        textarea.setSelectionRange(0, 0);
        textarea.scrollTop = 0;
        textarea.scrollLeft = 0;
      }
      offerDraft();
    },
    keydown(event) {
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && !event.altKey && (event.key.toLowerCase() === "s" || event.key === "Enter")) {
        event.preventDefault();
        save();
        return true;
      }
      if (event.key === "Escape" && !modifier && !event.altKey && !event.shiftKey) {
        event.preventDefault();
        close();
        return true;
      }
      return false;
    },
    canLeave: resolveUnsaved,
    prepareLogout: () => resolveUnsaved({ deferDiscard: true }),
    destroy() {
      destroyed = true;
      if (draftTimer) writeDraft();
      window.removeEventListener("beforeunload", onBeforeUnload);
    },
  };
}
