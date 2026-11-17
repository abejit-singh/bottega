"use strict";

/* A small Markdown renderer for the team's deliverables (research.md, copy.md…)
   and the memory notes.

   Safe by construction: it builds DOM nodes and puts text in them with
   textContent. It never uses innerHTML, so text written by a model cannot
   inject markup or scripts, whatever it contains. Links only become clickable
   when they point to http(s) addresses.

   It supports the subset of Markdown the agents write: headings, paragraphs,
   bold, italics, inline code, code blocks, lists (also nested), block quotes,
   tables, horizontal rules, Obsidian [[wikilinks]] and the
   [PLACEHOLDER: …] markers the client must replace, which are highlighted,
   and color codes such as `#c8452c`, which get a swatch. */

const Markdown = (() => {
  const make = (tag, className) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  };

  // Order matters: placeholders and wikilinks before plain [text](url) links,
  // bold before italics. Italics with _ must not match inside snake_case words.
  const INLINE = new RegExp([
    String.raw`\[PLACEHOLDER[^\]]*\]`,
    String.raw`\[\[[^\]]+\]\]`,
    String.raw`\[[^\]]+\]\([^)\s]+\)`,
    String.raw`\*\*[^*]+\*\*`,
    String.raw`(?<!\w)__[^_]+__(?!\w)`,
    String.raw`(?<![\w*])\*[^*\s][^*]*\*(?![\w*])`,
    String.raw`(?<!\w)_[^_\s][^_]*_(?!\w)`,
    "`[^`]+`",
  ].join("|"), "g");

  function inline(parent, text) {
    let last = 0;
    for (const match of text.matchAll(INLINE)) {
      if (match.index > last) parent.append(text.slice(last, match.index));
      const token = match[0];
      let node;
      if (token.startsWith("[PLACEHOLDER")) {
        node = make("mark", "placeholder");
        node.textContent = token;
      } else if (token.startsWith("[[")) {
        node = make("span", "wikilink");
        node.textContent = token.slice(2, -2);
      } else if (token.startsWith("[")) {
        const [, label, url] = token.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
        if (/^https?:\/\//i.test(url)) {
          node = make("a");
          node.href = url;
          node.target = "_blank";
          node.rel = "noopener noreferrer";
        } else {
          node = make("span");
        }
        inline(node, label);
      } else if (token.startsWith("**") || token.startsWith("__")) {
        node = make("strong");
        inline(node, token.slice(2, -2));
      } else if (token.startsWith("`")) {
        node = make("code");
        const value = token.slice(1, -1);
        // A color code gets a swatch of that color. The value is checked to be
        // a plain hex color first, so nothing else can reach the style.
        if (/^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i.test(value)) {
          const swatch = make("span", "swatch");
          swatch.style.background = value;
          swatch.setAttribute("aria-hidden", "true");
          node.append(swatch);
        }
        node.append(value);
      } else {
        node = make("em");
        inline(node, token.slice(1, -1));
      }
      parent.append(node);
      last = match.index + token.length;
    }
    if (last < text.length) parent.append(text.slice(last));
  }

  const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const FENCE = /^\s*```/;
  const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  const startsBlock = (line) => HEADING.test(line) || LIST_ITEM.test(line) || FENCE.test(line)
    || RULE.test(line) || /^\s*>/.test(line) || /^\s*\|/.test(line);

  const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());

  function render(source) {
    const root = make("div", "doc");
    const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let match;
      if (!line.trim()) { i += 1; continue; }

      if (FENCE.test(line)) {                                    // ``` code block
        const code = [];
        i += 1;
        while (i < lines.length && !FENCE.test(lines[i])) code.push(lines[i++]);
        i += 1;
        const pre = make("pre");
        pre.append(make("code"));
        pre.firstChild.textContent = code.join("\n");
        root.append(pre);
        continue;
      }
      if ((match = line.match(HEADING))) {                       // # Heading
        const node = make(`h${Math.min(match[1].length, 4)}`);
        inline(node, match[2]);
        root.append(node);
        i += 1;
        continue;
      }
      if (RULE.test(line)) {                                     // ---
        root.append(make("hr"));
        i += 1;
        continue;
      }
      if (/^\s*>/.test(line)) {                                  // > quote
        const quote = make("blockquote");
        const text = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) text.push(lines[i++].replace(/^\s*>\s?/, ""));
        const p = make("p");
        inline(p, text.join(" "));
        quote.append(p);
        root.append(quote);
        continue;
      }
      if (/^\s*\|/.test(line) && TABLE_DIVIDER.test(lines[i + 1] ?? "")) {   // | table |
        const table = make("table");
        const head = make("tr");
        for (const cell of cells(line)) {
          const th = make("th");
          inline(th, cell);
          head.append(th);
        }
        table.append(make("thead"));
        table.firstChild.append(head);
        const body = make("tbody");
        i += 2;
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          const tr = make("tr");
          for (const cell of cells(lines[i++])) {
            const td = make("td");
            inline(td, cell);
            tr.append(td);
          }
          body.append(tr);
        }
        table.append(body);
        const scroller = make("div", "table-wrap");
        scroller.append(table);
        root.append(scroller);
        continue;
      }
      if ((match = line.match(LIST_ITEM))) {                     // - item / 1. item
        const ordered = /\d/.test(match[2]);
        const list = make(ordered ? "ol" : "ul");
        while (i < lines.length && (match = lines[i].match(LIST_ITEM)) && /\d/.test(match[2]) === ordered) {
          const item = make("li");
          const depth = Math.min(Math.floor(match[1].replace(/\t/g, "  ").length / 2), 3);
          if (depth) item.style.marginLeft = `${depth * 1.25}em`;
          let text = match[3];
          i += 1;
          // Lines indented under an item continue it.
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST_ITEM.test(lines[i])) {
            text += ` ${lines[i++].trim()}`;
          }
          const box = text.match(/^\[( |x|X)\]\s+(.*)$/);
          if (box) {
            item.className = "task";
            const check = make("span", box[1] === " " ? "box" : "box checked");
            check.setAttribute("aria-hidden", "true");
            item.append(check);
            text = box[2];
          }
          inline(item, text);
          list.append(item);
        }
        root.append(list);
        continue;
      }
      const paragraph = [];                                      // plain paragraph
      while (i < lines.length && lines[i].trim() && (paragraph.length === 0 || !startsBlock(lines[i]))) {
        paragraph.push(lines[i++].trim());
      }
      const p = make("p");
      paragraph.forEach((text, n) => {
        if (n) p.append(make("br"));
        inline(p, text);
      });
      root.append(p);
    }
    return root;
  }

  return { render };
})();
