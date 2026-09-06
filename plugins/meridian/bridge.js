/* Adapt Loom's transport to Meridian's permission-checked network bridge.
   No window.fetch, host cookies, iframe escape, or private SDK implementation. */
import { createClient } from "./client.js";

export function createMeridianTransport(host, origin) {
  const base = new URL(origin);
  if (
    base.protocol !== "https:" ||
    base.username ||
    base.password ||
    base.pathname !== "/" ||
    base.search ||
    base.hash
  ) {
    throw new Error("Invalid Loom origin");
  }
  if (
    !host.can("net.fetch") ||
    !host.hasPermission(`network:${base.hostname}`)
  ) {
    throw new Error(
      `Ask a Meridian administrator to grant this plugin access to ${base.hostname}.`,
    );
  }
  return async (url, options = {}) => {
    const target = new URL(url);
    if (target.origin !== base.origin || !target.pathname.startsWith("/__"))
      throw new Error("Route outside Loom API");
    if (options.signal?.aborted)
      throw new DOMException("Request cancelled", "AbortError");
    // The bridge has no cancellation RPC. Race locally so unmount/navigation
    // stops stale UI updates; a dispatched mutation is never silently retried.
    let cancelled;
    const abort = new Promise((_, reject) => {
      cancelled = () =>
        reject(new DOMException("Request cancelled", "AbortError"));
      options.signal?.addEventListener("abort", cancelled, { once: true });
    });
    try {
      const result = await Promise.race([
        host.net.fetch({
          url: target.href,
          method: options.method || "GET",
          headers: options.headers || {},
          ...(options.body === undefined ? {} : { body: options.body }),
          timeoutMs: 20000,
        }),
        abort,
      ]);
      return {
        ok: result.status >= 200 && result.status < 300,
        status: result.status,
        text: async () => result.body,
      };
    } finally {
      options.signal?.removeEventListener("abort", cancelled);
    }
  };
}

export function connectLoom(host, origin, token = "") {
  return createClient({
    baseUrl: origin,
    token,
    fetchImpl: createMeridianTransport(host, origin),
  });
}

export function placement(host) {
  const c = host.context;
  const scope = c.itemId
    ? `item:${c.itemId}`
    : c.boardId
      ? `board:${c.boardId}`
      : `user:${c.userId}`;
  const key = `loom-placement:${c.widgetId || c.viewId || c.extensionKey}`;
  return { scope, key };
}

// Only data-formatting markup is admitted. Loom operators may opt into active
// render plugins; that consent never authorizes code inside Meridian's frame.
export function safeMarkup(html, document) {
  const template = document.createElement("template");
  template.innerHTML = html;
  const tags = new Set(
    "P H1 H2 H3 H4 H5 H6 A EM STRONG B I S DEL CODE PRE BLOCKQUOTE UL OL LI TABLE THEAD TBODY TFOOT TR TD TH HR BR DL DT DD DETAILS SUMMARY SUP SUB SPAN DIV KBD MARK".split(
      " ",
    ),
  );
  const visit = (root) => {
    for (const node of [...root.children]) {
      if (node.tagName === "IMG") {
        const label = document.createElement("span");
        label.textContent = `[Image: ${node.getAttribute("alt") || "embedded media"}. Open full studio to view.]`;
        node.replaceWith(label);
        continue;
      }
      if (!tags.has(node.tagName)) {
        node.remove();
        continue;
      }
      const id = node.getAttribute("id");
      for (const attr of [...node.attributes]) {
        if (
          !(node.tagName === "A" && attr.name === "href") &&
          !["colspan", "rowspan", "start"].includes(attr.name)
        )
          node.removeAttribute(attr.name);
      }
      if (id) node.id = "loom-document-" + encodeURIComponent(id);
      if (node.tagName === "A") {
        const href = node.getAttribute("href") || "";
        if (
          /^(?:https?:\/\/|mailto:|#|\/[^/]|\.{1,2}\/)/i.test(href) &&
          !/[\u0000-\u0020\\]/.test(href)
        ) {
          if (/^(?:https?:|mailto:)/i.test(href)) {
            node.target = "_blank";
            node.rel = "noopener noreferrer";
          }
          if (href.startsWith("#")) {
            try {
              node.setAttribute(
                "href",
                "#loom-document-" +
                  encodeURIComponent(decodeURIComponent(href.slice(1))),
              );
            } catch {
              node.removeAttribute("href");
            }
          }
        } else node.removeAttribute("href");
      }
      visit(node);
    }
  };
  visit(template.content);
  return template.innerHTML;
}

export function summarizeError(error) {
  if (error.status === 409)
    return "This changed while you were working. Your draft is preserved. Reload the current document and compare before saving again.";
  if (error.status === 403)
    return "Editing is unavailable. Check the Loom session token and whether this server is read-only.";
  const findings = error.payload?.findings?.map((f) => f.message).join("; ");
  return findings || error.message || "Loom could not complete this request.";
}
