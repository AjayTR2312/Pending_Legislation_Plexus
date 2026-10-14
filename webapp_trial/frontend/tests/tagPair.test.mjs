import test from "node:test";
import assert from "node:assert/strict";
import { wrapTag, isError } from "../src/textEdit.ts";

test("with a selection, the tags go around it (unchanged behaviour)", () => {
  const r = wrapTag(["see the court now"], 0, 3, 13, "A");
  assert.ok(!isError(r));
  assert.deepEqual(r.lines, ["see [A>the court<A] now"]);
  assert.equal(r.caret, undefined);
});

test("with no selection, an empty pair is inserted at the cursor and the caret goes between the tags", () => {
  const r = wrapTag(["see the court now"], 0, 8, 8, "A");
  assert.ok(!isError(r));
  assert.deepEqual(r.lines, ["see the [A><A]court now"]);
  assert.equal(r.caret, 8 + 3);
  assert.equal(r.lines[0].slice(r.caret - 3, r.caret + 3), "[A><A]");
});

test("D works the same way", () => {
  const r = wrapTag(["abc"], 0, 3, 3, "D");
  assert.ok(!isError(r));
  assert.deepEqual(r.lines, ["abc[D><D]"]);
  assert.equal(r.caret, 6);
});

test("a selection of only spaces counts as a cursor", () => {
  const r = wrapTag(["a   b"], 0, 1, 4, "A");
  assert.ok(!isError(r));
  assert.ok(r.lines[0].includes("[A><A]"));
});

test("a cursor inside an existing tag is refused (tags never nest)", () => {
  const r = wrapTag(["x [A>some text<A] y"], 0, 8, 8, "D");
  assert.ok(isError(r));
  assert.match(r.error, /already inside/);
});
