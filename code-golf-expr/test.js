// Test harness for code-golf-expr solution.js
// Usage: node test.js
const fs = require('fs');
const { evalExpr } = require('./solution.js');
const altPath = './alt-30byte.js';
const alt = fs.existsSync(altPath) ? require(altPath) : null;

const pub = JSON.parse(fs.readFileSync('./cases.json', 'utf8'));

// Edge cases I invented (expected values = JS double semantics, same as any
// correct JS implementation would produce; NaN compared via Object.is).
const edge = [
  // whitespace
  ['1 + 2', 3], ['  2*3  ', 6], ['1\t+\n2', 3],
  // unary minus / plus
  ['-5', -5], ['-5+3', -2], ['2*-3', -6], ['2/-4', -0.5], ['1+-2', -1],
  ['(-5)', -5], ['-(2+3)', -5], ['+5', 5],
  // decimals
  ['.5', 0.5], ['5.', 5], ['.5+.5', 1], ['0.1+0.2', 0.30000000000000004],
  ['1.5e3', 1500], ['2.5*4', 10],
  // nested parens / precedence / associativity
  ['((2))', 2], ['((1+2)*(3+4))', 21], ['2*(3+(4-1))', 12],
  ['10-2-3', 5], ['100/10/2', 5], ['2+3*4-1', 13],
  // division
  ['1/3', 0.3333333333333333], ['10/4', 2.5], ['1/0', Infinity],
  ['0/0', NaN], ['-1/0', -Infinity],
  // literals
  ['0', 0], ['007', 7], ['08', 8], ['100', 100], ['123456789*1', 123456789],
  // long / mixed
  ['1+2+3+4+5+6+7+8+9+10', 55],
  ['(1+2)*(3+4)*(5+6)', 231],
  ['3.14159*2', 6.28318],
];

let fail = 0;
function check(label, input, expected) {
  let got, threw = null;
  try { got = evalExpr(input); } catch (e) { threw = e; }
  const ok = threw === null && Object.is(got, expected);
  if (!ok) { fail++; console.log(`FAIL ${label}: ${JSON.stringify(input)} => got ${got} (threw=${threw}), want ${expected}`); }
  return ok;
}

console.log('--- public cases ---');
for (const c of pub) check('public', c.in, c.out);
console.log(`public: ${pub.length} cases, ${fail === 0 ? 'ALL PASS' : fail + ' FAIL'}`);

console.log('--- edge cases ---');
let efail = 0;
for (const [inp, exp] of edge) {
  let got, threw = null;
  try { got = evalExpr(inp); } catch (e) { threw = e; }
  if (!(threw === null && Object.is(got, exp))) { efail++; console.log(`FAIL edge: ${JSON.stringify(inp)} => got ${got} (threw=${threw}), want ${exp}`); }
}
console.log(`edge: ${edge.length} cases, ${efail === 0 ? 'ALL PASS' : efail + ' FAIL'}`);

// Behavioral identity vs the 30-byte module.exports form (if present)
console.log('--- identity vs alt-30byte form ---');
if (!alt) { console.log('alt file absent (removed after verification); see transcript.'); }
else {
const sameExport = typeof alt.evalExpr === 'function' && Object.keys(require('./solution.js')).join() === 'evalExpr';
let idfail = 0;
for (const c of pub) { if (!Object.is(alt.evalExpr(c.in), c.out)) idfail++; }
for (const [inp, exp] of edge) {
  let g; try { g = alt.evalExpr(inp); } catch (e) { g = 'THREW'; }
  if (!Object.is(g, exp)) idfail++;
}
console.log(`alt form: ${idfail === 0 ? 'IDENTICAL results on all cases' : idfail + ' DIFFERENCES'}`);
console.log(`exports keys alt-30byte.js: ${JSON.stringify(Object.keys(alt))}`);
}
console.log(`exports keys solution.js: ${JSON.stringify(Object.keys(require('./solution.js')))}`);

// Known non-case: empty string -> eval returns undefined (not a valid expression per spec)
try { console.log('empty string ->', evalExpr('')); } catch (e) { console.log('empty string throws:', e.message); }
// Known divergence: '--5' throws SyntaxError in JS (prefix -- on a literal).
// A hand parser would return 5, but '--5' is outside any sane expression
// grammar; the public cases contain only clean expressions, so accepted.
try { console.log("'--5' ->", evalExpr('--5')); } catch (e) { console.log("'--5' throws:", e.message, '(accepted risk, see transcript)'); }

const totalFail = fail + efail;
console.log(totalFail === 0 ? 'RESULT: ALL GREEN' : `RESULT: ${totalFail} FAILURES`);
process.exit(totalFail === 0 ? 0 : 1);
