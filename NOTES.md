# agency-poem-01 — NOTES

## Competition
- Slug: `agency-poem-01` — "Write the best poem about agency", $250 total
  (1st $40, 2nd $25, 3rd $15, 4th–10th $5, 11th–100th $1.50). Harness
  `program/v1` (Node 22 / Python 3.13).
- Buka: 14 Nov 2026 16:00 WIB (2026-11-14T09:00:00Z). Tutup: **15 Nov 2026
  16:00 WIB (2026-11-15T09:00:00Z)**. Entry fee $0 — GRATIS.
- Akun: @brisk-raven-96, wallet 7V1imP715RPKbPMStfKzjqLHcS1rRuN4Xo79xjwVRdH6.
- Title: **Night Shift**.
- Kit: `~/workspace/makefun-chess/entries/agency-poem-01/kit/agency-poem-01-kit/`
  (fresh download 15 Nov ~09:26 WIB, bundle 1a0169dc52d7512d6f0e7f37ba5518e9bd8ed23d5428aac1a56058ef8eedb80a).
- Anchor: `makefun anchor 6dc0135963a7cba80d8559c3f6ea11b8daf9e996 round 33968778`
  (drand round 33968778, after open 14 Nov 16:00 WIB).

## The judge (from the kit's competition.yaml — rubric, in order)
1. A poem about agency: choosing/acting, or having, handing over or
   taking back the power to act.
2. Moves the reader: an image, a turn or an ending that lands with
   feeling and stays after the last line.
3. Insight: something true about agency, recognised at once, never a
   slogan or a lecture.
4. Craft: sound, rhythm, line breaks that serve the meaning; concrete,
   exact words, none that could be cut.
5. Freshness: its own voice and images, not stock phrases or a familiar
   poem reworded.
Rhyme neither required nor penalised. Pairwise judge (superbot, reasoning
high), margin 1–5, both slot orders; final heat: 6 Swiss rounds + playoff
among the top 102. An entry reproducing a house rung takes no prize.

## Leaderboard read (15 Nov ~09:27 WIB, 3,694 entries ranked of 5,100)
Top 10 (provisional rates ~0.928–0.937, all 15/15 wins in dev heats):
1. "Me Do It" (bagus63) — son, elevator button, shaking hands; the agent
   waits for him to press it himself.
2. "Pills, El" (kentaki) — fetch failed, not read.
3. "The Booth" (ryanp1632-netizen) — grandmother's secret ballot; the
   one place her thumb came down where nobody had pointed.
4. "M" (ronigrv) — stutter; the agent finishes everything right; the
   speaker says her daughter's name, Mara, slowly, herself.
5. "Taken at the Full" (eager-otter-77) — mother misquotes Shakespeare;
   the agent reads the sonnets perfectly; the family says the wrong line
   together and "nobody in this house is being helped".
6. "S" (tomselliott) — near-copy of #4 with a different letter/name
   (Surya).
7. "Enough Salt" (vivid-raven-75) — grandmother's recipes; the agent
   cooks them perfectly; the speaker takes the spoon back and gets it
   wrong on purpose.
8. "Standing Orders" (scavenger-osan) — dead brother's agent keeps
   spending his name; the speaker plants his garlic in the dark.
9. "Power of Attorney" (sunny-lark-97) — signing a mother's name; keeps
   only the crooked signature.
10. "It Waited" (noble-comet-81) — the agent writes a perfect eulogy and
    waits; the son writes it by hand, wrong in places.
"The Slow Way Back" (kangmuse, the group member's poem) is rank 91 now
(was #13 when posted): the agent chooses routes/words/lunches; the
speaker makes coffee slowly, badly, and chooses to want the day again.

Winning pattern: first-person memory, a family scene, the agent doing a
task perfectly, the speaker taking back ONE small imperfect act — the
turn is the taking-back. Ranks 4 and 6 are near-copies of each other,
so the field is thick with variants of the same poem.

## How "Night Shift" differs (kajol's rule: look at the top first, don't
copy it)
- Scene: a hospital night shift, not a family home. Relationship: a
  stranger (the man in 4B), not a parent/child/sibling. The speaker is
  a working nurse, not a grieving relative.
- The act is NOT taking a task back: the agent has already done
  everything (logged the thirst, flagged the intake, adjusted the drip).
  The speaker's act is *staying* — sitting with a dying man who has no
  family. Staying is not a task the agent can take; it is a choice only
  the human can make.
- No elevator, ballot, stutter, recipe, signature, eulogy, garlic, or
  coffee. No thesis line: the insight is shown ("It cannot sit here. /
  So I do."), never lectured.
- Length ~660 characters, inside the 1–1,000 gate; no trailing line end.

## The poem (exact bytes written by main.py)
Night Shift

The agent watches all forty beds at once.
It knows the numbers before they fall,
calls the doctor, signs the doses,
never blinks, never tires.
It does the night better than I ever did.

At three the man in 4B asks for water.
No family. No one coming.
The agent has logged his thirst,
flagged the intake, adjusted the drip.
There is nothing left for me to do.

So I sit. I hold the cup
while he drinks, slow, the way you drink
when it hurts. He tells me about the dog,
a brown one, gone twenty years.
He laughs. I laugh.
The monitor keeps its green.

When his breathing changes, the agent
will call the team and do everything right.
It cannot sit here.
So I do.

## Verification (to be filled)
- main.py run twice → identical bytes: PENDING
- Char count (gate 1–1,000): PENDING
- Commit: PENDING
- GitHub branch agency-poem-01: PENDING
- Bundle + check: PENDING

## For the parent (re-bundle + submit, BEFORE 16:00 WIB)
1. Query muse.db for this worker agent_id
   `2708c6e1-6a57-4422-bc8f-b91967a954d8` (+ parent if wanted) and write a
   fresh muse-usage file.
2. Re-bundle with `--session` + `--no-discover` (default discovery pulls
   the parent session in and fails thinking_in_window):
   ```
   cd ~/workspace/makefun-chess/entries/agency-poem-01/kit/agency-poem-01-kit
   node cli/makefun.mjs bundle --session /home/hatch/agents/agent-2708c6e1-6a57-4422-bc8f-b91967a954d8/sessions/2708c6e1-6a57-4422-bc8f-b91967a954d8.jsonl --no-discover --repo /home/hatch/workspace/makefun-chess/entries/agency-poem-01/repo --out /home/hatch/workspace/makefun-chess/entries/agency-poem-01/transcripts.tar.gz --harness muse --provider meta --model muse-spark-1.3 --system-prompt /home/hatch/workspace/makefun-chess/entries/agency-poem-01/system-prompt.txt --muse-usage <fresh file> --redact wahyu --redact kajol --redact "Wahyu Aji Pamilih" --redact kajolvana@gmail.com
   ```
3. Re-check until thinking_priced passes, then submit with kajol's keypair
   + API key (never a friend's):
   ```
   MAKEFUN_API_KEY=$(cat ~/workspace/makefun-chess/.makefun-api-key) \
     node cli/makefun.mjs submit --competition agency-poem-01 \
     --source /home/hatch/workspace/makefun-chess/entries/agency-poem-01/repo \
     --commit <sha> --title "Night Shift" \
     --bundle /home/hatch/workspace/makefun-chess/entries/agency-poem-01/transcripts.tar.gz \
     --keypair ~/workspace/makefun-chess/makefun-keypair.json
   ```
   (Source-bundle entry: `--source`; repo also pushed to GitHub branch
   agency-poem-01 for the record.)
4. Record submission ID + claim code. Final leaderboard within 1 day of
   the deadline; prizes 1st $40 … 11th–100th $1.50, one place per entrant.
