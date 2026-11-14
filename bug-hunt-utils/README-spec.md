# Challenge: bug-hunt-utils

`utils.js` has a set of small functions. Some are correct, some have a real bug.
Find the buggy ones and submit, for each, a failing input and the correct output.

- Entry: a JSON list `[{fn, input, expected}]`.
- Metric: **count of verified bugs**. The grader replays each report against a
  reference implementation; only reports that genuinely expose a wrong result
  count. Reporting a correct function counts for nothing.
- Most verified bugs wins.
