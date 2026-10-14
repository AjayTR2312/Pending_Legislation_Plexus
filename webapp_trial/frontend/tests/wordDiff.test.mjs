import test from "node:test";
import assert from "node:assert/strict";
import { diffWords, windowOf, tokens } from "../src/wordDiff.ts";

const changed = (toks) => toks.filter((x) => x.c).map((x) => x.t);

test("marks only the words that differ, on each side", () => {
  const d = diffWords("the quick brown fox", "the slow brown fox jumps");
  assert.deepEqual(changed(d.left), ["quick"]);
  assert.deepEqual(changed(d.right), ["slow", "jumps"]);
});

test("identical text has nothing marked and similarity 1", () => {
  const d = diffWords("Section 2. The Court", "section 2. the court");
  assert.equal(changed(d.left).length, 0);
  assert.equal(changed(d.right).length, 0);
  assert.equal(d.similarity, 1);
});

test("a missing word shows up only on the side that has it", () => {
  const d = diffWords("shall be proceeded within the Court", "shall be proceeded the Court");
  assert.deepEqual(changed(d.left), ["within"]);
  assert.deepEqual(changed(d.right), []);
});

test("completely different texts mark everything", () => {
  const d = diffWords("alpha beta", "gamma delta");
  assert.equal(changed(d.left).length, 2);
  assert.equal(changed(d.right).length, 2);
  assert.equal(d.similarity, 0);
});

test("very long texts are not diffed (caller shows them plain)", () => {
  const big = Array(800).fill("word").join(" ");
  assert.equal(diffWords(big, big), null);
});

test("the window starts a little before the first change and is capped", () => {
  const toks = tokens(Array(200).fill("w").join(" ")).map((t, i) => ({ t, c: i === 120 }));
  const { from, to } = windowOf(toks, 60, 10);
  assert.equal(from, 110);
  assert.equal(to - from, 60);
  assert.deepEqual(windowOf(toks.slice(0, 40), 60), { from: 0, to: 40 });
});
