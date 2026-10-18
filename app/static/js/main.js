/**
 * Entry module: header, hash router, session and global keyboard shortcuts.
 *
 * A view is an async function (params, app) → {node, title?, mounted?, keydown?, singleKey?,
 * canLeave?, destroy?}. keydown sees every key press (outside dialogs); singleKey only plain
 * keys outside text fields; canLeave may veto navigating away.
 */

import * as api from "./api.js";
import { el } from "./dom.js";
import { removeAllDrafts } from "./drafts.js";
import { icon } from "./icons.js";
import { isDialogOpen, messagePage, showError } from "./ui.js";
import { editorView } from "./views/editor.js";
import { listHash, listView } from "./views/list.js";
import { loginView } from "./views/login.js";
import { noteView } from "./views/note.js";
import { tagsView } from "./views/tags.js";
import { trashView } from "./views/trash.js";

const THEME_KEY = "plainnotes.theme";
const THEMES = [
  ["auto", "Auto", "sun-moon"],
  ["light", "Light", "sun"],
  ["dark", "Dark", "moon"],
];

// A malformed %-escape in a hand-typed URL must not break routing.
const decode = (value) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

const ROUTES = [
  [/^#\/login(?:\?(.*))?$/, loginView, (m) => ({ query: new URLSearchParams(m[1] ?? "") })],
  [/^#\/(?:\?(.*))?$/, listView, (m) => ({ query: new URLSearchParams(m[1] ?? "") })],
  [/^#\/note\/([^/?]+)$/, noteView, (m) => ({ id: decode(m[1]) })],
  [/^#\/note\/([^/?]+)\/edit$/, editorView, (m) => ({ id: decode(m[1]) })],
  [/^#\/new(?:\?(.*))?$/, editorView, (m) => ({ query: new URLSearchParams(m[1] ?? "") })],
  [/^#\/tags$/, tagsView, () => ({})],
  [/^#\/trash$/, trashView, () => ({})],
];

const root = document.getElementById("app");
const main = el("main", { id: "main", class: "main" });
let current = null;
let currentHash = null;
let navigation = 0;
let guarding = false;

const searchInput = el("input", {
  id: "search",
  type: "search",
  name: "q",
  placeholder: "Search",
  autocomplete: "off",
  spellcheck: "false",
  enterkeyhint: "search",
});

const app = {
  session: null,
  async refreshSession() {
    app.session = await api.getSession();
    updateHeader();
  },
  /** Go to a route; replace: true leaves no history entry. */
  navigate(hash, { replace = false } = {}) {
    if (replace) {
      history.replaceState(null, "", hash);
      show(hash);
    } else {
      location.hash = hash;
    }
  },
  /** Change the URL of the current view without re-rendering it. */
  replaceRoute(hash) {
    history.replaceState(null, "", hash);
    currentHash = hash;
  },
  setTitle(title) {
    document.title = `${title} – plainnotes`;
  },
  setSearch(value) {
    searchInput.value = value;
  },
};

function readTheme() {
  try {
    return localStorage.getItem(THEME_KEY) ?? "auto";
  } catch {
    return "auto";
  }
}

function setTheme(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Not remembered in private browsing; theme.js falls back to auto.
  }
  document.dispatchEvent(new Event("plainnotes:theme"));
  updateHeader();
}

async function logout() {
  if (current?.canLeave && !(await current.canLeave())) return;
  try {
    await api.logout();
  } catch (error) {
    if (error.code !== "not_authenticated") {
      showError(error);
      return;
    }
  }
  removeAllDrafts();
  app.session = null;
  location.hash = "#/login";
}

const menu = el("details", { class: "menu" });
const themeButtons = THEMES.map(([value, label, iconName]) =>
  el(
    "button",
    { type: "button", class: "menu-item", "data-theme-value": value, on: { click: () => setTheme(value) } },
    icon(iconName),
    label,
  ),
);
const newNoteLink = el("a", { class: "button primary", href: "#/new" }, icon("plus"), "New note");
const trashLink = el("a", { class: "menu-item", href: "#/trash" }, icon("trash-2"), "Trash");
menu.append(
  el("summary", { class: "button" }, icon("menu"), "Menu"),
  el(
    "div",
    { class: "menu-panel" },
    el("a", { class: "menu-item", href: "#/tags" }, icon("tag"), "Tags"),
    trashLink,
    el("div", { class: "menu-group", role: "group", "aria-label": "Theme" }, el("span", { class: "menu-label" }, "Theme"), themeButtons),
    el("button", { type: "button", class: "menu-item", on: { click: logout } }, icon("log-out"), "Log out"),
  ),
);

const header = el(
  "header",
  { class: "app-header" },
  el(
    "a",
    { class: "brand", href: "#/" },
    el("img", { class: "brand-logo", src: "static/favicon.svg", alt: "" }),
    "plainnotes",
  ),
  el(
    "form",
    {
      class: "search",
      role: "search",
      on: {
        submit: (event) => {
          event.preventDefault();
          searchInput.blur();
          app.navigate(listHash(searchInput.value.trim()));
        },
      },
    },
    el("label", { for: "search", class: "visually-hidden" }, "Search notes"),
    searchInput,
  ),
  newNoteLink,
  menu,
);

function updateHeader() {
  const readOnly = app.session?.read_only === true;
  newNoteLink.hidden = readOnly;
  trashLink.hidden = readOnly;
  const theme = readTheme();
  for (const button of themeButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.themeValue === theme));
  }
}

function resolve(hash) {
  for (const [pattern, view, params] of ROUTES) {
    const match = pattern.exec(hash);
    if (match) return [view, params(match)];
  }
  return [async () => ({ node: messagePage("Not found", "There is no page at this address.") }), {}];
}

function normalized(hash) {
  return hash && hash !== "#" ? hash : "#/";
}

async function show(hash) {
  const token = ++navigation;
  current?.destroy?.();
  current = null;
  currentHash = hash;
  menu.open = false;
  const [view, params] = resolve(hash);
  header.hidden = view === loginView;
  document.title = "plainnotes";
  let instance;
  try {
    instance = await view(params, app);
  } catch (error) {
    instance = { node: messagePage("Something went wrong", error.message) };
  }
  if (token !== navigation) {
    instance.destroy?.();
    return;
  }
  current = instance;
  main.replaceChildren(instance.node);
  if (instance.title) app.setTitle(instance.title);
  window.scrollTo(0, 0);
  instance.mounted?.();
}

async function onHashChange() {
  if (guarding) return;
  let hash = normalized(location.hash);
  if (hash === currentHash) return;
  if (current?.canLeave && !hash.startsWith("#/login")) {
    guarding = true;
    try {
      if (!(await current.canLeave())) {
        history.replaceState(null, "", currentHash);
        return;
      }
    } finally {
      guarding = false;
    }
    hash = normalized(location.hash);
  }
  await show(hash);
}

function inTextField(target) {
  return target instanceof Element && target.closest("input, textarea, select") !== null;
}

function onKeydown(event) {
  if (event.isComposing || isDialogOpen()) return;
  if (current?.keydown?.(event)) return;
  if (event.ctrlKey || event.metaKey || event.altKey || inTextField(event.target)) return;
  if (event.key === "/" && !header.hidden) {
    event.preventDefault();
    searchInput.focus();
    searchInput.select();
  } else if (event.key === "n" && !header.hidden && !app.session?.read_only) {
    event.preventDefault();
    app.navigate("#/new");
  } else if (current?.singleKey?.(event.key)) {
    event.preventDefault();
  }
}

searchInput.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || event.isComposing) return;
  event.preventDefault();
  event.stopPropagation();
  searchInput.value = "";
  searchInput.blur();
});

document.addEventListener("click", (event) => {
  if (menu.open && !menu.contains(event.target)) menu.open = false;
});
menu.addEventListener("click", (event) => {
  if (event.target.closest(".menu-item")) menu.open = false;
});

async function boot() {
  root.append(header, main);
  updateHeader();
  window.addEventListener("hashchange", onHashChange);
  document.addEventListener("keydown", onKeydown);
  if (!normalized(location.hash).startsWith("#/login")) {
    try {
      app.session = await api.getSession();
    } catch (error) {
      if (error.code !== "not_authenticated") showError(error);
    }
    updateHeader();
  }
  await show(normalized(location.hash));
}

boot();
