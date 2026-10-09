/**
 * Tag-aware editing of the converted text: wrap a selection in [A>/[D>, remove a tag,
 * split a paragraph and merge it with the one above.
 *
 * Ported from the Streamlit compare viewer (pdf_compare_viewer.py: wrapTag, doSplitAt,
 * mergeWithPrevious) and kept consistent with the house rule of ONE tag per paragraph:
 *   - splitting inside a tag closes it on the left and reopens it on the right, so each
 *     resulting paragraph carries its own single tag;
 *   - merging two paragraphs whose tags touch (<A] then [A>) collapses them into one tag.
 *
 * Pure functions over a `lines` array (blank lines are "" entries). Each returns the new
 * lines plus the line to focus afterwards, or an `error` string for the reviewer.
 * No DOM, no React — so it can be tested in plain Node.
 */

export interface EditResult {
  lines: string[];
  /** Line to scroll to after the edit. */
  focus: number;
  message: string;
}

export type EditOutcome = EditResult | { error: string };

export function isError(o: EditOutcome): o is { error: string } {
  return "error" in o;
}

const OPEN = { A: "[A>", D: "[D>" } as const;
const CLOSE = { A: "<A]", D: "<D]" } as const;

/** Which tag (if any) is open at character `off` of `text`. */
export function tagStateAt(text: string, off: number): "A" | "D" | null {
  let state: "A" | "D" | null = null;
  for (let i = 0; i < off && i < text.length; ) {
    const tok = text.substr(i, 3);
    if (tok === "[A>") { state = "A"; i += 3; }
    else if (tok === "[D>") { state = "D"; i += 3; }
    else if (tok === "<A]" || tok === "<D]") { state = null; i += 3; }
    else i += 1;
  }
  return state;
}

const DELIM = /\[[AD]>|<[AD]\]/;

/** Wrap text[start, end) of line `li` in a tag of the given type. */
export function wrapTag(lines: string[], li: number, start: number, end: number, type: "A" | "D"): EditOutcome {
  const text = lines[li] ?? "";
  if (start > end) [start, end] = [end, start];
  // Keep the delimiters tight around the words: leave surrounding spaces outside.
  while (start < end && /\s/.test(text[start])) start++;
  while (end > start && /\s/.test(text[end - 1])) end--;
  if (start >= end) return { error: "Select the text to tag first." };

  const sel = text.slice(start, end);
  if (DELIM.test(sel)) {
    return { error: "The selection contains an existing tag. Remove it first, or select plain text only." };
  }
  const inside = tagStateAt(text, start);
  if (inside) {
    return { error: `That text is already inside a [${inside}> tag — tags never nest. Use Untag first.` };
  }

  const next = [...lines];
  next[li] = text.slice(0, start) + OPEN[type] + sel + CLOSE[type] + text.slice(end);
  return { lines: next, focus: li, message: `Tagged ${sel.length} characters as ${type === "A" ? "an addition" : "a deletion"}.` };
}

/** Remove the tag that contains character `off` (keeps its text). */
export function untag(lines: string[], li: number, off: number): EditOutcome {
  const text = lines[li] ?? "";
  const re = /\[([AD])>(.*?)<\1\]/g;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    const s = m.index;
    const e = s + m[0].length;
    if (off >= s && off <= e) {
      const next = [...lines];
      next[li] = text.slice(0, s) + m[2] + text.slice(e);
      return { lines: next, focus: li, message: `Removed the ${m[1]} tag (text kept).` };
    }
  }
  return { error: "Click inside a tagged span first." };
}

/** Split the paragraph at character `off`. Always leaves a blank line between the halves. */
export function splitAt(lines: string[], li: number, off: number): EditOutcome {
  const text = lines[li] ?? "";
  if (off <= 0 || off >= text.length || !text.slice(0, off).trim() || !text.slice(off).trim()) {
    return { error: "Click inside the paragraph, between two words, to split it there." };
  }
  let left = text.slice(0, off).replace(/\s+$/, "");
  let right = text.slice(off).replace(/^\s+/, "");
  const open = tagStateAt(text, off);
  if (open) {
    // Each half keeps its own single tag.
    left += CLOSE[open];
    right = OPEN[open] + right;
  }
  const next = [...lines];
  next.splice(li, 1, left, "", right);
  return { lines: next, focus: li + 2, message: `Split into two paragraphs (lines ${li + 1} and ${li + 3}).` };
}

/** Merge paragraph `li` into the previous non-blank paragraph. */
export function mergeUp(lines: string[], li: number): EditOutcome {
  const curr = lines[li] ?? "";
  if (!curr.trim()) return { error: "Click inside a paragraph first." };
  let prevLi = li - 1;
  while (prevLi >= 0 && !lines[prevLi].trim()) prevLi--;
  if (prevLi < 0) return { error: "This is the first paragraph — there is nothing above to merge with." };

  const prev = lines[prevLi];
  let merged: string;
  const collapsed = (["A", "D"] as const).find(
    (t) => new RegExp(`${CLOSE[t].replace(/[\]]/g, "\\]")}\\s*$`).test(prev) &&
      new RegExp(`^\\s*${OPEN[t].replace(/[\[]/g, "\\[")}`).test(curr)
  );
  if (collapsed) {
    // <A] [A> touching: one tag across the merged paragraph.
    const left = prev.replace(new RegExp(`${CLOSE[collapsed].replace(/[\]]/g, "\\]")}\\s*$`), "").replace(/\s+$/, "");
    const right = curr.replace(new RegExp(`^\\s*${OPEN[collapsed].replace(/[\[]/g, "\\[")}`), "").replace(/^\s+/, "");
    merged = `${left} ${right}`;
  } else {
    merged = prev.replace(/\s+$/, "") + " " + curr.replace(/^\s+/, "");
  }
  const next = [...lines];
  next.splice(prevLi, li - prevLi + 1, merged);
  return {
    lines: next,
    focus: prevLi,
    message: collapsed ? `Merged into line ${prevLi + 1}; the two ${collapsed} tags became one.` : `Merged into line ${prevLi + 1}.`,
  };
}

/** Join lines back into file text, keeping the original trailing newline. */
export function toText(lines: string[], original: string): string {
  return lines.join("\n") + (original.endsWith("\n") ? "\n" : "");
}
