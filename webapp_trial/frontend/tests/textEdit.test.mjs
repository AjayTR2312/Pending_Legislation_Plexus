// Run:  node --test tests/        (Node 22+, no extra packages)
import test from "node:test";
import assert from "node:assert/strict";
import { wrapTag, untag, splitAt, mergeUp, tagStateAt, toText, isError } from "../src/textEdit.ts";

const ok = (r) => { assert.ok(!isError(r), `unexpected error: ${r.error}`); return r; };

test("wrapTag wraps a selection and keeps surrounding spaces outside the tag", () => {
  const r = ok(wrapTag(["see the court now"], 0, 3, 13, "A")); // " the court"
  assert.deepEqual(r.lines, ["see [A>the court<A] now"]);
});

test("wrapTag refuses to nest or overlap an existing tag", () => {
  assert.match(wrapTag(["a [A>b c<A] d"], 0, 7, 8, "D").error, /already inside/);
  assert.match(wrapTag(["a [A>b<A] c d"], 0, 0, 12, "D").error, /existing tag/);
  assert.match(wrapTag(["abc"], 1, 1, 1, "A").error, /Select the text/);
});

test("untag removes only the tag under the cursor and keeps its text", () => {
  const lines = ["[A>one<A] and [D>two<D]"];
  assert.deepEqual(ok(untag(lines, 0, 4)).lines, ["one and [D>two<D]"]);
  assert.deepEqual(ok(untag(lines, 0, 18)).lines, ["[A>one<A] and two"]);
  assert.match(untag(lines, 0, 11).error, /inside a tagged span/);
});

test("splitAt on plain text leaves a blank line between two paragraphs", () => {
  const r = ok(splitAt(["First part. Second part."], 0, 11));
  assert.deepEqual(r.lines, ["First part.", "", "Second part."]);
  assert.equal(r.focus, 2);
});

test("splitAt inside a tag gives each paragraph its OWN single tag", () => {
  const r = ok(splitAt(["[A>(a) one sentence. (b) two sentence.<A]"], 0, 21));
  assert.deepEqual(r.lines, ["[A>(a) one sentence.<A]", "", "[A>(b) two sentence.<A]"]);
  assert.equal(tagStateAt("[A>x y<A]", 5), "A");
  assert.equal(tagStateAt("[A>x<A] y", 9), null);
});

test("splitAt rejects the ends of a paragraph", () => {
  assert.match(splitAt(["abc def"], 0, 0).error, /between two words/);
  assert.match(splitAt(["abc def"], 0, 7).error, /between two words/);
});

test("mergeUp collapses touching tags of the same type into one tag", () => {
  const lines = ["[A>(a) first paragraph.<A]", "", "[A>(b) second paragraph.<A]"];
  const r = ok(mergeUp(lines, 2));
  assert.deepEqual(r.lines, ["[A>(a) first paragraph. (b) second paragraph.<A]"]);
  assert.match(r.message, /became one/);
});

test("mergeUp joins different tag types without collapsing and skips blank lines", () => {
  const r = ok(mergeUp(["plain end", "", "", "[D>gone<D]"], 3));
  assert.deepEqual(r.lines, ["plain end [D>gone<D]"]);
  const mixed = ok(mergeUp(["[A>new<A]", "", "[D>old<D]"], 2));
  assert.deepEqual(mixed.lines, ["[A>new<A] [D>old<D]"]);
});

test("mergeUp errors on the first paragraph or a blank line", () => {
  assert.match(mergeUp(["only"], 0).error, /first paragraph/);
  assert.match(mergeUp(["a", "", ""], 2).error, /inside a paragraph/);
});

test("toText keeps the original trailing newline", () => {
  assert.equal(toText(["a", "b"], "x\n"), "a\nb\n");
  assert.equal(toText(["a", "b"], "x"), "a\nb");
});

test("a split then a merge round-trips a tagged paragraph", () => {
  const original = ["[A>(a) one sentence. (b) two sentence.<A]"];
  const split = ok(splitAt(original, 0, 21));
  const merged = ok(mergeUp(split.lines, 2));
  assert.deepEqual(merged.lines, ["[A>(a) one sentence. (b) two sentence.<A]"]);
});
