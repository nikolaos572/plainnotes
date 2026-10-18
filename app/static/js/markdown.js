/**
 * The Markdown rendering pipeline: the only place where HTML derived from a note enters the page.
 * markdown-it (raw HTML off) → DOMPurify → DOM fragment → post-processing with DOM APIs only.
 */

import { opensInBrowser } from "./api.js";
import { el } from "./dom.js";
import { icon } from "./icons.js";
import { copyText } from "./ui.js";

const PURIFY_OPTIONS = {
  USE_PROFILES: { html: true },
  FORBID_ATTR: ["style"],
  ALLOW_DATA_ATTR: false,
  RETURN_DOM_FRAGMENT: true,
};
const LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);
const IMAGE_PROTOCOLS = new Set(["http:", "https:"]);
const ATTACHMENT_URL = /^(?:\.\/)?attachments\/([^/?#]+)$/;

// Tag rules shared with the server (app/text.py). (?=(X+))\1 is an atomic match of X+, the
// JavaScript equivalent of Python's possessive [\w-]++, so the regex never backtracks.
const TAG_BODY = String.raw`(?=([\p{L}\p{N}_-]+))\1`;
const TAG_END = String.raw`(?=[\s.,;:!?)\]}]|$)`;
const TAG_AFTER_SPACE = new RegExp(String.raw`(?<=\s)#${TAG_BODY}${TAG_END}`, "gu");
const TAG_AT_LINE_START = new RegExp(String.raw`(?:^|(?<=\s))#${TAG_BODY}${TAG_END}`, "gu");
const HAS_LETTER = /\p{L}/u;
const WIKILINK = /\[\[([^[\]\n]+)\]\]/g;

let parser = null;

function markdown() {
  if (!parser) {
    parser = window.markdownit({ html: false, linkify: true, typographer: false, breaks: true });
    parser.linkify.set({ fuzzyLink: false, fuzzyEmail: false, fuzzyIP: false });
    // Only http(s):// and mailto: text becomes a link.
    parser.linkify.add("ftp:", null).add("//", null);
  }
  return parser;
}

/**
 * The link for a tag search.
 * @param {string} tag tag body without "#"
 * @returns {string}
 */
export function tagHref(tag) {
  return `#/?q=${encodeURIComponent(`#${tag.toLowerCase()}`)}`;
}

/**
 * Find tags in one text node's text.
 * @param {string} text
 * @param {boolean} atLineStart whether the text starts a line
 * @returns {{index: number, text: string, body: string}[]}
 */
export function findTags(text, atLineStart) {
  const pattern = atLineStart ? TAG_AT_LINE_START : TAG_AFTER_SPACE;
  return [...text.matchAll(pattern)]
    .filter((match) => HAS_LETTER.test(match[1]))
    .map((match) => ({ index: match.index, text: match[0], body: match[1] }));
}

/**
 * The key wikilinks use to match note titles, ignoring case.
 * @param {string} title
 * @returns {string}
 */
export function titleKey(title) {
  return title.trim().normalize("NFC").toLowerCase();
}

/**
 * A heading slug: lowercase letters and digits joined by "-".
 * @param {string} text
 * @returns {string}
 */
export function slugify(text) {
  return text
    .normalize("NFC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
}

function protocolOf(url) {
  try {
    return new URL(url, document.baseURI).protocol;
  } catch {
    return null;
  }
}

function decoded(segment) {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function attachmentHref(noteId, segment) {
  return `api/notes/${encodeURIComponent(noteId)}/attachments/${segment}`;
}

function rewriteUrls(root, noteId) {
  for (const link of root.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href");
    const attachment = ATTACHMENT_URL.exec(href);
    if (attachment) {
      link.setAttribute("href", attachmentHref(noteId, attachment[1]));
      if (opensInBrowser(decoded(attachment[1]))) {
        link.setAttribute("target", "_blank");
        link.setAttribute("rel", "noopener noreferrer");
      }
    } else if (!LINK_PROTOCOLS.has(protocolOf(href))) {
      link.removeAttribute("href");
    } else if (/^\s*https?:/i.test(href)) {
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
    }
  }
  for (const image of root.querySelectorAll("img")) {
    const src = image.getAttribute("src") ?? "";
    const attachment = ATTACHMENT_URL.exec(src);
    if (attachment) {
      image.setAttribute("src", attachmentHref(noteId, attachment[1]));
    } else {
      const protocol = protocolOf(src);
      const isImageData = protocol === "data:" && /^\s*data:image\//i.test(src);
      if (!IMAGE_PROTOCOLS.has(protocol) && !isImageData) image.removeAttribute("src");
    }
    image.setAttribute("loading", "lazy");
  }
}

function addCopyButtons(root) {
  for (const pre of root.querySelectorAll("pre")) {
    const code = pre.querySelector("code") ?? pre;
    const button = el(
      "button",
      { type: "button", class: "button small copy-code", on: { click: () => copyText(code.textContent, code) } },
      icon("copy"),
      "Copy",
    );
    const block = el("div", { class: "code-block" });
    pre.replaceWith(block);
    block.append(button, pre);
  }
}

function leadingText(item) {
  let node = item.firstChild;
  while (node?.nodeType === Node.TEXT_NODE && node.data.trim() === "") node = node.nextSibling;
  if (node?.nodeName === "P") node = node.firstChild;
  return node?.nodeType === Node.TEXT_NODE ? node : null;
}

function addTaskCheckboxes(root) {
  for (const item of root.querySelectorAll("li")) {
    const text = leadingText(item);
    const marker = text && /^\[( |x)\] /.exec(text.data);
    if (!marker) continue;
    text.data = text.data.slice(marker[0].length);
    const done = marker[1] === "x";
    const box = el("input", {
      type: "checkbox",
      disabled: true,
      checked: done,
      "aria-label": done ? "Done" : "Not done",
    });
    text.before(box);
    item.classList.add("task");
  }
}

function addHeadingIds(root) {
  const used = new Map();
  for (const heading of root.querySelectorAll("h1, h2, h3, h4, h5, h6")) {
    const slug = slugify(heading.textContent) || "section";
    const count = (used.get(slug) ?? 0) + 1;
    used.set(slug, count);
    heading.setAttribute("id", `h-${count === 1 ? slug : `${slug}-${count}`}`);
  }
}

function textNodesOutsideLinksAndCode(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement.closest("a, code, pre") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  return nodes;
}

function replaceInTextNode(node, matches, build) {
  if (!matches.length) return;
  const text = node.data;
  const parts = [];
  let position = 0;
  for (const match of matches) {
    parts.push(text.slice(position, match.index), build(match));
    position = match.index + match.text.length;
  }
  parts.push(text.slice(position));
  node.replaceWith(...parts.filter((part) => part !== ""));
}

function linkWikilinks(root, titles) {
  for (const node of textNodesOutsideLinksAndCode(root)) {
    const matches = [...node.data.matchAll(WIKILINK)]
      .filter((match) => match[1].trim() !== "")
      .map((match) => ({ index: match.index, text: match[0], title: match[1].trim() }));
    replaceInTextNode(node, matches, ({ title }) => {
      const id = titles.get(titleKey(title));
      return id
        ? el("a", { class: "wikilink", href: `#/note/${encodeURIComponent(id)}` }, title)
        : el("a", { class: "wikilink missing", href: `#/new?title=${encodeURIComponent(title)}` }, title);
    });
  }
}

function linkTags(root) {
  for (const node of textNodesOutsideLinksAndCode(root)) {
    const previous = node.previousSibling;
    const atLineStart = previous === null || previous.nodeName === "BR";
    replaceInTextNode(node, findTags(node.data, atLineStart), (tag) =>
      el("a", { class: "tag", href: tagHref(tag.body) }, tag.text),
    );
  }
}

/**
 * Render a note's Markdown into container.
 * @param {HTMLElement} container
 * @param {string} content the note text
 * @param {{noteId: string, titles?: Map<string, string>}} options titles maps titleKey → note ID
 */
export function renderMarkdown(container, content, { noteId, titles = new Map() }) {
  const html = markdown().render(content);
  container.replaceChildren(window.DOMPurify.sanitize(html, PURIFY_OPTIONS));
  rewriteUrls(container, noteId);
  addCopyButtons(container);
  addTaskCheckboxes(container);
  addHeadingIds(container);
  linkWikilinks(container, titles);
  linkTags(container);
}

/**
 * Click handler for rendered Markdown: in-note links (#heading) scroll without changing the route.
 * @param {MouseEvent} event
 * @param {HTMLElement} container
 */
export function followInNoteLink(event, container) {
  const link = event.target.closest?.("a[href]");
  if (!link || !container.contains(link)) return;
  const href = link.getAttribute("href");
  if (!href.startsWith("#") || href.startsWith("#/")) return;
  event.preventDefault();
  const target = document.getElementById(`h-${slugify(decoded(href.slice(1)))}`);
  target?.scrollIntoView({ block: "start" });
}
