# C-1 [Medium] — `claimFor` griefing via `setClaimReceiver` bypass

TARGET: DepositPool @ 0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790 on Ethereum, block 26395272

INVARIANT: Only the staker, or an address the staker explicitly authorized as a claimSender, may trigger a claim of the staker's rewards. Setting a claim receiver only changes where rewards are sent — it must never waive the caller authorization check.

ATTACK:
1. The victim stakes stETH in the DepositPool with a long lock (e.g. 10 years, multiplier ~10.49x) and calls `setClaimReceiver(receiver)` — a documented feature (DepositPool.sol:284-292).
2. An attacker — any address, never authorized via `claimSender` — waits until the claim lock period has elapsed (14 days after stake) and calls `claimFor(poolIndex, victim)`, paying only the LayerZero fee (~0.000514 ETH).
3. `_claim()` executes for the victim: it resets `claimLockStart = claimLockEnd = 0` and recomputes `virtualDeposited` with multiplier 1x, then force-sends the victim's pending MOR to the victim's receiver via LayerZero.
4. The victim's future yield rate collapses ~9.5x (10.49x → 1x). The victim must start a new lock from scratch to recover any multiplier; the yield lost in between is permanently gone. The attacker gains nothing — pure griefing at ~$1.11 per claim, repeatable as often as the victim re-locks.

IMPACT: Total staked in the stETH DepositPool: 4712.13 stETH ≈ $10.13M (block 26395272, on-chain stETH price $2148.61). Pool-0 emission is ~2,860 MOR/day; ~6.49M MOR remain in the schedule over the next 10 years (measured on-chain via `getPeriodRewards`). Only 2 stakers on this pool have ever set a claim receiver (0.081 stETH combined, both at 1.01x today), so the direct exposure is currently small — but ANY staker who uses the documented `setClaimReceiver` feature becomes attackable, including long-locked stakers: a fork PoC victim with 100 stETH locked 10 years (10.489x) loses ~87 MOR/day of future yield after one stranger's `claimFor` (~9.6x rate cut, virtualDeposited 1048.9 → 100 stETH). The same defect exists on all five DepositPool v7 proxies (same implementation); the USDC pool also has 2 receiver-setters, one with a 3.536x lock until 27 Jul 2028. No verified live MOR price exists at time of writing, so yield-at-risk is stated in MOR, not USD.

POC: https://github.com/kajolvana/morpheus-bounty-pocs/tree/main/c1-claimfor-griefing (fork block 26395272)

FIX: In `claimFor`, always enforce the `claimSender` authorization check when `msg.sender != staker`, including when a claim receiver is set; use the receiver only as the payout destination, never as a waiver of the caller check.

CAVEATS: (1) Thematic relation to CodeHawks 2024-01 submission #137 (claim-area griefing via a failed LayerZero refund, judged Invalid — "Incorrect statement"): verified 12 Nov 2026 NOT a duplicate — #137 is about a LayerZero refund to a non-payable L2-aliased address failing and the L2 mint reverting; it never mentions claimLockStart/claimLockEnd, lock multiplier, claimSender, or claim receiver. This finding is a different mechanism (an authorization check skipped entirely in claimFor when a claim receiver is set) with a different impact (lock multiplier reset to 1x). (2) Program §1.2 griefing exception: this is a permanent loss of future yield, not theft of principal — the attacker gains nothing and the victim's principal is untouched. It is submitted on the basis that the loss is permanent and not self-inflicted: the victim cannot undo it without restarting the lock clock, and the maximum multiplier achievable for a new lock declines as the tanh window advances.
