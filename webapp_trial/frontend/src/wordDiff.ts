/** Word-level diff for the findings cards: which words of A are not in B, and which words of B are not in A.
 *  Longest-common-subsequence on whitespace tokens (case-insensitive). Returns null when the texts are too long
 *  to diff cheaply: the caller then shows them plain. Pure and dependency-free, so `node --test` can run it. */

export interface Tok {
  t: string;
  /** true = this word has no counterpart on the other side */
  c: boolean;
}

export interface WordDiff {
  left: Tok[];
  right: Tok[];
  /** share of words that match (0..1) */
  similarity: number;
}

const MAX_TOKENS = 700;

export function tokens(s: string): string[] {
  return s.split(/\s+/).filter(Boolean);
}

export function diffWords(a: string, b: string): WordDiff | null {
  const ta = tokens(a);
  const tb = tokens(b);
  if (ta.length > MAX_TOKENS || tb.length > MAX_TOKENS) return null;
  const na = ta.map((w) => w.toLowerCase());
  const nb = tb.map((w) => w.toLowerCase());
  const w = nb.length + 1;
  const dp = new Uint16Array((na.length + 1) * w);
  for (let i = na.length - 1; i >= 0; i--) {
    for (let j = nb.length - 1; j >= 0; j--) {
      dp[i * w + j] = na[i] === nb[j] ? dp[(i + 1) * w + j + 1] + 1 : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
    }
  }
  const left: Tok[] = ta.map((t) => ({ t, c: true }));
  const right: Tok[] = tb.map((t) => ({ t, c: true }));
  let i = 0;
  let j = 0;
  let same = 0;
  while (i < na.length && j < nb.length) {
    if (na[i] === nb[j]) {
      left[i].c = false;
      right[j].c = false;
      same++;
      i++;
      j++;
    } else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) {
      i++;
    } else {
      j++;
    }
  }
  const longest = Math.max(ta.length, tb.length, 1);
  return { left, right, similarity: same / longest };
}

/** A window of at most `limit` tokens that starts a little before the first changed word. */
export function windowOf(toks: Tok[], limit: number, lead = 10): { from: number; to: number } {
  if (toks.length <= limit + 10) return { from: 0, to: toks.length };
  const first = toks.findIndex((x) => x.c);
  const from = first > lead ? first - lead : 0;
  return { from, to: Math.min(toks.length, from + limit) };
}
