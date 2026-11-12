# Morpheus Bug Bounty v2 — PoCs

Foundry fork PoCs for two findings submitted to the Morpheus Bug Bounty v2
(mor.org/bug-bounty, devs@mor.org). Each folder is a self-contained Foundry
project with `forge-std` vendored under `lib/` — no `forge install` needed,
no mainnet/testnet transactions (forks are read-only).

- `c1-claimfor-griefing/` — **C-1 [Medium]** `claimFor` griefing resets a
  victim's lock multiplier to 1x (DepositPool stETH, Ethereum, fork block
  26395272). Run: `cd c1-claimfor-griefing && forge test`
  (`FORK_RPC_URL` override available; a default RPC is embedded).
- `c2-collect-fees/` — **C-2 [Low]** `collectFees` callable by anyone, missing
  `onlyOwner` (L2TokenReceiverV2, Arbitrum, fork block 523422593). Run:
  `cd c2-collect-fees && FORK_RPC_URL=<url> forge test`.

The one-page reports (`REPORT.md` in each folder) are the submitted reports.
