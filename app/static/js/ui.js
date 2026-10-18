/**
 * Dialogs (native <dialog>), toasts and copying to the clipboard.
 */

import { el } from "./dom.js";
import { icon } from "./icons.js";

let dialogCount = 0;

/** @returns {boolean} true while a modal dialog is open (it then handles all keys) */
export function isDialogOpen() {
  return document.querySelector("dialog[open]") !== null;
}

/**
 * Show a modal dialog and wait for a choice.
 * @param {Object} options
 * @param {string} options.title
 * @param {string} [options.message]
 * @param {{label: string, value: *, kind?: string, icon?: string}[]} options.buttons the first is
 *   focused; icon names an icon from icons.js
 * @param {*} [options.cancelValue] result when the dialog is dismissed with Esc
 * @returns {Promise<*>} the value of the chosen button
 */
export function showDialog({ title, message, buttons, cancelValue = null }) {
  return new Promise((resolve) => {
    dialogCount += 1;
    const titleId = `dialog-title-${dialogCount}`;
    let result = cancelValue;
    const dialog = el("dialog", { class: "dialog", "aria-labelledby": titleId });
    const actions = buttons.map((button) =>
      el(
        "button",
        {
          type: "button",
          class: `button ${button.kind ?? ""}`.trim(),
          on: {
            click: () => {
              result = button.value;
              dialog.close();
            },
          },
        },
        button.icon ? icon(button.icon) : null,
        button.label,
      ),
    );
    dialog.append(
      el("h2", { id: titleId }, title),
      message ? el("p", {}, message) : null,
      el("div", { class: "dialog-actions" }, actions),
    );
    dialog.addEventListener("cancel", () => {
      result = cancelValue;
    });
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    document.body.append(dialog);
    dialog.showModal();
    actions[0]?.focus();
  });
}

/**
 * Ask a yes/no question; the other button is Cancel.
 * @param {string} title
 * @param {string} message
 * @param {{label: string, kind?: string, icon?: string}} button the button that answers yes;
 *   kind "danger" for destructive actions
 * @returns {Promise<boolean>}
 */
export async function confirmAction(title, message, button) {
  const choice = await showDialog({
    title,
    message,
    buttons: [
      { kind: "primary", ...button, value: true },
      { label: "Cancel", value: false, icon: "x" },
    ],
    cancelValue: false,
  });
  return choice === true;
}

/**
 * Show a short message in the toast area, optionally with one action button (such as Undo).
 * @param {string} message
 * @param {"info"|"error"} [kind]
 * @param {{label: string, run: () => void, icon?: string}|null} [action] closes the toast when
 *   clicked
 */
export function toast(message, kind = "info", action = null) {
  const area = document.getElementById("toasts");
  while (area.childElementCount >= 3) area.firstElementChild.remove();
  const item = el("div", { class: `toast ${kind}` }, message);
  if (action) {
    const run = () => {
      item.remove();
      action.run();
    };
    item.append(
      el(
        "button",
        { type: "button", class: "toast-action", on: { click: run } },
        action.icon ? icon(action.icon) : null,
        action.label,
      ),
    );
  }
  area.append(item);
  // A toast with an action stays longer, so there is time to reach the button.
  const lifetime = action ? 8000 : kind === "error" ? 6000 : 2500;
  setTimeout(() => item.remove(), lifetime);
}

/**
 * Show an API error as a toast, unless it already led to the login page.
 * @param {Error & {code?: string}} error
 */
export function showError(error) {
  if (error.code !== "not_authenticated") toast(error.message, "error");
}

/**
 * Copy text exactly. Without the Clipboard API (plain HTTP), select `fallback` instead.
 * @param {string} text
 * @param {Node} [fallback] node whose contents to select
 */
export async function copyText(text, fallback) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
      return;
    } catch {
      // Permission denied or not a secure context: fall back to selecting the text.
    }
  }
  if (fallback) {
    const range = document.createRange();
    range.selectNodeContents(fallback);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
  toast("Press Ctrl+C to copy");
}

/**
 * A full-page message with a link back to the notes list.
 * @param {string} title
 * @param {string} text
 * @returns {HTMLElement}
 */
export function messagePage(title, text) {
  return el(
    "section",
    { class: "message-page" },
    el("h1", {}, title),
    el("p", {}, text),
    el("p", {}, el("a", { href: "#/" }, "Back to all notes")),
  );
}

/**
 * The page shown when loading a view failed.
 * @param {Error & {code?: string}} error
 * @returns {HTMLElement}
 */
export function errorPage(error) {
  if (error.code === "not_authenticated") return el("section");
  if (error.code === "not_found") {
    return messagePage("Not found", "This note does not exist. It may have been moved to the trash.");
  }
  if (error.code === "not_utf8") return messagePage("Cannot open this note", error.message);
  return messagePage("Something went wrong", error.message);
}
