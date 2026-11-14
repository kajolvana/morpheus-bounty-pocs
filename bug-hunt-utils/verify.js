const u = require('./utils.js');
function check(fn, input, expected) {
  const actual = u[fn](input);
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  console.log(`${fn}(${JSON.stringify(input)}) => actual ${JSON.stringify(actual)} | expected ${JSON.stringify(expected)} | ${ok ? 'CORRECT' : 'BUG'}`);
}
// --- suspected bugs ---
check('factorial', 5, 120);
check('factorial', 2, 2);
check('factorial', 0, 1);
check('factorial', 1, 1);
check('countVowels', 'AEIOU', 5);
check('countVowels', 'Hello World', 3);
check('countVowels', 'aeiou', 5);
check('titleCase', "don't stop", "Don't Stop");
check('titleCase', 'hello world', 'Hello World');
// --- sanity: functions believed correct ---
check('sum', [1,2,3], 6);
check('sum', [], 0);
check('max', [3,1,2], 3);
check('max', [-5,-2], -2);
check('reverse', 'abc', 'cba');
check('reverse', '', '');
check('isPalindrome', 'racecar', true);
check('isPalindrome', 'hello', false);
check('clamp', 5, 5); // placeholder, clamp needs 3 args - tested below
console.log('clamp(5,0,10) =>', u.clamp(5,0,10), '| expect 5');
console.log('clamp(-1,0,10) =>', u.clamp(-1,0,10), '| expect 0');
console.log('clamp(20,0,10) =>', u.clamp(20,0,10), '| expect 10');
check('unique', [1,1,2,2,3], [1,2,3]);
check('average', [2,4], 3);
check('average', [1,2,3,4], 2.5);
