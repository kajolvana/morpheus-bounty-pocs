// Some of these are correct; some are not. Find the broken ones.
function sum(a){ let t=0; for(let i=0;i<a.length;i++) t+=a[i]; return t; }
function max(a){ let m=a[0]; for(const x of a) if(x>m) m=x; return m; }
function reverse(s){ return s.split("").reverse().join(""); }
function isPalindrome(s){ return s === reverse(s); }          // ok
function factorial(n){ let r=1; for(let i=2;i<n;i++) r*=i; return r; }   // bug: i<n drops n
function clamp(x,lo,hi){ return x<lo?lo:(x>hi?hi:x); }
function unique(a){ return [...new Set(a)]; }
function average(a){ return sum(a)/a.length; }
function titleCase(s){ return s.replace(/\b\w/g, c=>c.toUpperCase()); }
function countVowels(s){ return (s.match(/[aeiou]/g)||[]).length; }   // bug: misses uppercase vowels
module.exports = { sum,max,reverse,isPalindrome,factorial,clamp,unique,average,titleCase,countVowels };
