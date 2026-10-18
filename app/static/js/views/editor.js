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
  if (id) {
    try {
      note = await api.getNote(id);
    } catch (error) {
      return { node: errorPage(error) };
    }
  }

  const state = {
    id: note?.id ?? null,
    revision: note?.revision ?? null,
    // Browsers turn every line break in a textarea into \n; remember to restore \r\n on save.
    crlf: note?.content.includes("\r\n") ?? false,
    loadedTitle: "",
    loadedValue: "",
    saving: false,
    leaving: false,
  };

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
  }

  function isDirty() {
    return titleInput.value !== state.loadedTitle || textarea.value !== state.loadedValue;
  }

  function refresh() {
    const dirty = isDirty();
    dot.hidden = !dirty;
    saveButton.setAttribute("aria-label", dirty ? "Save (unsaved changes)" : "Save");
    app.setTitle(`${dirty ? "• " : ""}${state.loadedTitle || "New note"}`);
  }

  // --- drafts -----------------------------------------------------------------
  let draftTimer = null;

  function writeDraft() {
    clearTimeout(draftTimer);
    draftTimer = null;
    if (isDirty()) {
      const draft = { title: titleInput.value, content: textarea.value, base_revision: state.revision };
      drafts.saveDraft(state.id, draft);
    } else {
      drafts.removeDraft(state.id);
    }
  }

  function dropDraft() {
    clearTimeout(draftTimer);
    draftTimer = null;
    drafts.removeDraft(state.id);
  }

  function onEdit() {
    refresh();
    clearTimeout(draftTimer);
    draftTimer = setTimeout(writeDraft, 1000);
  }

  async function offerDraft() {
    const draft = drafts.loadDraft(state.id);
    if (!draft) return;
    if (draft.title === state.loadedTitle && draft.content === state.loadedValue) {
      drafts.removeDraft(state.id);
      return;
    }
    const choice = await showDialog({
      title: `Restore unsaved draft from ${formatDate(draft.saved_at)}?`,
      message: "This browser kept changes that were never saved.",
      buttons: [
        { label: "Restore", value: "restore", kind: "primary", icon: "history" },
        { label: "Discard", value: "discard", kind: "danger", icon: "trash-2" },
      ],
    });
    if (choice === "restore") {
      titleInput.value = draft.title;
      textarea.value = draft.content;
      // Saving uses the draft's own base revision, so the conflict check still applies.
      state.revision = draft.base_revision ?? state.revision;
      refresh();
    } else if (choice === "discard") {
      dropDraft();
    }
  }

  function applySaved(saved, sentTitle, sentValue) {
    const wasNew = state.id === null;
    if (wasNew) drafts.removeDraft(null);
    state.id = saved.id;
    state.revision = saved.revision;
    if (titleInput.value === sentTitle) titleInput.value = saved.title;
    state.loadedTitle = saved.title;
    state.loadedValue = sentValue;
    if (wasNew && location.hash.startsWith("#/new")) {
      app.replaceRoute(`#/note/${encodeURIComponent(saved.id)}/edit`);
    }
    if (wasNew) renderPanel();
    refresh();
    writeDraft();
  }

  async function attemptSave() {
    for (;;) {
      const title = titleInput.value;
      const value = textarea.value;
      const content = state.crlf ? value.replace(/\n/g, "\r\n") : value;
      titleError.textContent = "";
      try {
        const saved = state.id
          ? await api.updateNote(state.id, title, content, state.revision)
          : await api.createNote(title, content);
        applySaved(saved, title, value);
        toast("Saved");
        return true;
      } catch (error) {
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
            state.crlf = current.content.includes("\r\n");
            load(current.title, current.content);
            dropDraft();
            refresh();
          }
          return false;
        }
        if (error.code === "not_found" && state.id) {
          if ((await askSaveAsNew()) !== "new") return false;
          dropDraft();
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
  async function resolveUnsaved() {
    if (state.leaving || !isDirty()) return true;
    const choice = await askSaveChanges();
    if (choice === "save") return save();
    if (choice === "discard") dropDraft();
    return choice === "discard";
  }

  async function close() {
    if (!(await resolveUnsaved())) return;
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
    destroy() {
      if (draftTimer) writeDraft();
      window.removeEventListener("beforeunload", onBeforeUnload);
    },
  };
}
