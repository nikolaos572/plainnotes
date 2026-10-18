/**
 * Safe element creation. Strings always become text nodes; nothing here parses HTML.
 */

/**
 * Create an element.
 * @param {string} tag
 * @param {Object} [attributes] attribute values; `on` maps event names to listeners,
 *   `value` sets the property, true adds an empty attribute, false/null/undefined skip it.
 * @param {...*} children nodes, strings (text) or nested arrays; null/false are skipped.
 * @returns {HTMLElement}
 */
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "on") {
      for (const [type, listener] of Object.entries(value)) node.addEventListener(type, listener);
    } else if (name === "value") {
      node.value = value;
    } else {
      node.setAttribute(name, value === true ? "" : String(value));
    }
  }
  append(node, children);
  return node;
}

/**
 * Append children (nodes or text) to a node.
 * @param {Node} node
 * @param {Array} children
 */
export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
}

/**
 * Build text and <mark> nodes from server segments ({text, mark}).
 * @param {{text: string, mark: boolean}[]} segments
 * @returns {(Node|string)[]}
 */
export function segmentNodes(segments) {
  return segments.map((segment) => (segment.mark ? el("mark", {}, segment.text) : segment.text));
}

/**
 * Format an ISO timestamp as local "YYYY-MM-DD HH:MM".
 * @param {string} iso
 * @returns {string}
 */
export function formatDate(iso) {
  const date = new Date(iso);
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * Format a byte count for people.
 * @param {number} bytes
 * @returns {string}
 */
export function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${size} B` : `${size.toFixed(size < 10 ? 1 : 0)} ${units[unit]}`;
}
