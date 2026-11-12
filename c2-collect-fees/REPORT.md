# C-2 [Low] — `collectFees` without `onlyOwner`

TARGET: L2TokenReceiverV2 @ 0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790 on Arbitrum, block 523422593

INVARIANT: Every state-changing function of L2TokenReceiverV2 except `collectFees` requires `onlyOwner`, so only the owner decides when the contract's Uniswap V3 fees are collected.

ATTACK:
1. Attacker (any unprivileged address, no approval, no NFT, no funds) calls `collectFees(tokenId)` on the live receiver for a position the receiver owns.
2. The call succeeds — the receiver performs no access check of its own; only the downstream NonfungiblePositionManager checks that the receiver owns the position.
3. The `FeesCollected` event fires and the fees move into the receiver contract at a time of the attacker's choosing (e.g. right before/after the owner's own operations); the sibling `swap` and `increaseLiquidityCurrentRange` called by the same attacker revert with `Ownable: caller is not the owner`.
4. Note: step 1 needs the receiver to own a position. At block 523422593 it owns none, so the attacker can first donate one permissionlessly (mint a V3 position and transfer the NFT to the receiver — `onERC721Received` accepts it), as demonstrated in the PoC.

IMPACT: Amount at risk on live today: $0 — at block 523422593 the receiver owns no V3 position (NPM `balanceOf == 0`) and holds 0 wstETH / 0 WETH / 2.98 MOR, so there are no fees to collect right now. The loss is timing control only, never theft: `recipient` is hardcoded to `address(this)`, so collected fees always land in the receiver. In the PoC's end-to-end demonstration a stranger collected real fees of 2.7007 MOR + 0.0006752 WETH (≈ $3.52 at MOR ≈ $0.79, WETH ≈ $2,060, 12 Nov 2026) from a position donated by the attacker himself — proving the missing check on the live contract, not a live balance at risk.

POC: https://github.com/kajolvana/morpheus-bounty-pocs/tree/main/c2-collect-fees (fork block 523422593)

FIX: Add `onlyOwner` to `collectFees`, consistent with every other state-changing function in the contract (also in the live V1 code path, `old/L2TokenReceiver.sol:126`).

Caveat: Low severity, timing-only. No funds can be stolen through this function, and no fee is collectable on live until/unless the receiver holds a V3 position. Related thematically to C4 M-2 (centralization) but a new instance — the issue is a missing check, not owner trust.
