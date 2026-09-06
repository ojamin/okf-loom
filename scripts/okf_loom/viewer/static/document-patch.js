/* Incremental document reconciliation. No studio state or transport dependency. */
  function blockChildren(parent) {
    const out = [];
    for (let n = parent.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 1) out.push(n);
    }
    return out;
  }
  function blockSig(el) {
    // Whitespace-collapsed text + tag + id (heading ids are stable anchors
    // for comment marks; including them keeps a renamed heading "changed").
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute("id") || "";
    // Mermaid/math blocks: after CDN rendering (mermaid.js, KaTeX), the
    // element's textContent changes from the raw source to the rendered
    // SVG/MathML output. Use the data attribute (the original source) for
    // the signature so the diff treats "rendered" and "raw" versions of
    // the SAME diagram as equal — preventing a visible flash-to-raw-text
    // on live patches that don't actually change the diagram.
    if (el.classList && (el.classList.contains("mermaid") || el.classList.contains("math"))) {
      var source = el.getAttribute("data-source") || el.textContent || "";
      return tag + "|" + id + "|" + source.replace(/\s+/g, " ").trim();
    }
    // Enhancement wrappers (renderers.js table/code UX): sign as the INNER
    // block so an enhanced live table/pre compares equal to the bare
    // server-rendered element on the other side of the diff. The tablewrap
    // uses data-source (the original text captured at enhance time) because
    // user-applied sorting reorders the live textContent without the
    // content having changed. Full-length (no 200-char slice) on both the
    // wrapper AND bare table/pre sides: a sorted table means row-level
    // recursion can't reconcile order, so equality must be exact — a
    // truncated signature would silently drop edits past the prefix.
    if (el.classList && el.classList.contains("okf-tablewrap")) {
      return "table|" + (el.getAttribute("data-source-html") || el.getAttribute("data-source") || "");
    }
    if (el.classList && el.classList.contains("okf-codewrap")) {
      const inner = el.querySelector("pre");
      return inner ? blockSig(inner) : "pre|";
    }
    if (tag === "pre") {
      const code = el.querySelector("code");
      const language = code && (code.className.match(/language-[\w+-]+/) || [""])[0];
      // Whitespace is executable content in code blocks. Highlight spans
      // and copy controls must not change the signature.
      return "pre|" + id + "|" + language + "|" + el.textContent;
    }
    if (tag === "table") return "table|" + el.outerHTML;
    return tag + "|" + id + "|" + semanticContent(el);
  }

  function semanticContent(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return "";
    if (node.classList.contains("okf-heading-anchor")) return "";
    const children = Array.from(node.childNodes).map(semanticContent).join("");
    // Marks and enhancement spans are transparent, while authored semantics
    // retain their position: bolding a different word is a real edit.
    const tag = node.tagName.toLowerCase();
    if (tag === "mark" || tag === "span") return children;
    const attrs = ["id", "href", "src", "alt", "title", "type", "checked", "start", "colspan", "rowspan"]
      .map((attr) => [attr, node.getAttribute(attr)]);
    return JSON.stringify([tag, attrs, children]);
  }

  function parseHtmlToBlocks(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return blockChildren(tmp);
  }
  // Standard LCS DP over signature arrays → edit ops. Returns an array of
  // {op: "keep"|"replace"|"insert"|"remove", oldEl?, newEl?}.
  function lcsOps(oldBlocks, newBlocks) {
    const n = oldBlocks.length, m = newBlocks.length;
    const oldSigs = oldBlocks.map(blockSig);
    const newSigs = newBlocks.map(blockSig);
    // Bound quadratic memory on machine-generated documents. Keep common
    // prefix/suffix identities and replace only the unmatched middle.
    if (n * m > 250000) {
      let prefix = 0, suffix = 0;
      while (prefix < n && prefix < m && oldSigs[prefix] === newSigs[prefix]) prefix++;
      while (suffix < n - prefix && suffix < m - prefix && oldSigs[n - 1 - suffix] === newSigs[m - 1 - suffix]) suffix++;
      return [
        ...oldBlocks.slice(0, prefix).map((oldEl, i) => ({ op: "keep", oldEl, newEl: newBlocks[i] })),
        ...oldBlocks.slice(prefix, n - suffix).map((oldEl) => ({ op: "remove", oldEl })),
        ...newBlocks.slice(prefix, m - suffix).map((newEl) => ({ op: "insert", newEl })),
        ...oldBlocks.slice(n - suffix).map((oldEl, i) => ({ op: "keep", oldEl, newEl: newBlocks[m - suffix + i] })),
      ];
    }
    // dp[i][j] = LCS length of oldSigs[i:] and newSigs[j:].
    const dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = oldSigs[i] === newSigs[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (oldSigs[i] === newSigs[j]) { ops.push({ op: "keep", oldEl: oldBlocks[i], newEl: newBlocks[j] }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ op: "remove", oldEl: oldBlocks[i] }); i++; }
      else { ops.push({ op: "insert", newEl: newBlocks[j] }); j++; }
    }
    while (i < n) { ops.push({ op: "remove", oldEl: oldBlocks[i] }); i++; }
    while (j < m) { ops.push({ op: "insert", newEl: newBlocks[j] }); j++; }
    return ops;
  }
  // diffAndPatchBody mutates `parent` to match `newHtml` at the block level.
  // Returns the list of newly-inserted or replaced element nodes (for the
  // scoped pulse). Recurses into matching list/table blocks that differ.
  export function diffAndPatchBody(parent, newHtml) {
    const newBlocks = parseHtmlToBlocks(newHtml);
    return diffChildren(parent, newBlocks, 0);
  }
  function diffChildren(parent, newBlocks, depth) {
    const oldBlocks = blockChildren(parent);
    const ops = lcsOps(oldBlocks, newBlocks);
    const changed = [];
    // Apply ops. We rebuild the child list by walking ops and using
    // parent.insertBefore / removeChild so untouched nodes keep their
    // identity (and any in-body selection/focus on them survives).
    let cursor = parent.firstChild; // node we're currently considering in the live DOM
    for (let k = 0; k < ops.length; k++) {
      const op = ops[k];
      if (op.op === "keep") {
        // Recurse into matching containers whose children may differ.
        if (depth < 2 && (op.oldEl.tagName === "UL" || op.oldEl.tagName === "OL" || op.oldEl.tagName === "TABLE")) {
          const innerChanged = diffChildren(op.oldEl, blockChildren(op.newEl), depth + 1);
          if (innerChanged.length) { changed.push.apply(changed, innerChanged); }
        }
        cursor = op.oldEl.nextSibling;
      } else if (op.op === "replace") {
        // (Not produced by lcsOps directly; handled as remove+insert.)
      } else if (op.op === "remove") {
        const next = op.oldEl.nextSibling;
        parent.removeChild(op.oldEl);
        cursor = next;
      } else if (op.op === "insert") {
        const imported = parent.ownerDocument.importNode(op.newEl, true);
        parent.insertBefore(imported, cursor);
        changed.push(imported);
      }
    }
    return changed;
  }
