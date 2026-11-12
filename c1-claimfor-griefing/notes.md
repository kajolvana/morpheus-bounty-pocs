# notes.md — C-1 [Medium] claimFor griefing (paket submission Morpheus)

Paket ini disiapkan untuk program Bug Bounty Morpheus v2 (mor.org). Bukan audit baru.
Target live: DepositPool (stETH) proxy `0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790`, Ethereum mainnet, DepositPool v7.
Mekanisme: `DepositPool.sol:284-292` (claimFor — cek `claimSender` dilewati bila korban pernah `setClaimReceiver`)
dan `:551-552` (`_claim` mereset `claimLockStart/End = 0`, multiplier jadi 1x).

## Status

**SEND-READY** — PoC fork selesai dan lolos 3/3, laporan 1 halaman selesai, judgement CodeHawks 2024-01 submission #137 **TERVERIFIKASI 12 Nov 2026 ~07:40 WIB: BUKAN duplikat** (dinilai Invalid — "Incorrect statement"; #137 soal refund LayerZero gagal ke alamat alias L2 non-payable + pendingRewards di-nol-kan; tidak menyebut lock/multiplier/claimSender/claim receiver). Tidak ada pekerjaan tersisa sebelum kirim.

## Nomor blok & state on-chain (dibaca langsung, 12 Nov 2026 ~00:27 UTC)

- **Fork block yang di-pin: 26395272** (timestamp 1794443279). Blok saat pembacaan awal: 26395231.
- DepositPool stETH: `version()` = 7; distributor `0xDf1AC1AC255d91F5f4B1E3B4Aef57c5350F64C7A`;
  `totalDepositedInPublicPools()` = **4712.134476422148158533 stETH**.
- `rewardPoolsProtocolDetails(0)`: claimLockPeriodAfterStake = claimLockPeriodAfterClaim = 1209600 (14 hari);
  minimalStake 0.01 stETH; distributedRewards **1.216.066,906446844174427447 MOR**.
- `rewardPoolsData(0)`: lastUpdate 1794181727; rate 1.021742071061288906051632842e27;
  totalVirtualDeposited **10721.211560405774850467**.
- RewardPool `0xb7994dE339AEe515C9b2792831CD83f3C9D8df87`, pool 0: payoutStart 1707393600 (10 Feb 2024);
  decreaseInterval 86400; initialReward 3456e18 MOR/hari; rewardDecrease 592558728240000000/hari.
- Distributor `0xDf1AC1AC255d91F5f4B1E3B4Aef57c5350F64C7A`: rewardPool di atas;
  l1Sender `0x2Efd4430489e1a05A89c2f51811aC661B7E5FF84`; `rewardPoolLastCalculatedTimestamp(0)` = 1794180863;
  `minRewardsDistributePeriod` = 86400.
- L1Sender `layerZeroConfig()`: gateway (endpoint LZ V1) `0x66A71Dcef29A0fFBDBE3c6a460a3B5BC225Cd675`;
  receiver `0xd4a8ECcBe696295e68572A98b1aA70Aa9277d427`; receiverChainId **110 (Arbitrum)**;
  zroPaymentAddress 0x0; adapterParams gas 250000.
- `depositPools(0, stETH DP)`: tokenPrice **2148.6067418 USD** (18dp); deposited 4712.134476422148158533 stETH;
  lastUnderlyingBalance 4712.134476422148158534 (yield ≈ 0, +1 wei); strategy 0 (NONE — yield dari rebase Lido;
  aToken 0x0). Empat pool lain memakai AAVE.
- Pool lain (konteks): WBTC deposited 0.10297537, WETH 10.91, USDC 604158.21, USDT 4738.99.
  Kelima proxy DepositPool versi 7, implementasi sama (`0xdB10dAEF167eA2233Ba6811457dD24D676FbD670`).
- StETH/USD Chainlink feed proxy `0xCfE54B5cD566aB89272946F602D76Ea879CAb4a8`:
  jawaban live saat fork 205790000000 ($2057.90, 8dp), updatedAt 1794441443.

## Amount at risk — cara hitung

- **Total staked (stETH pool): 4712.134476422148158533 stETH ≈ $10.126.000**
  (× $2148.6067418, harga stETH on-chain dari Distributor `tokenPrice`, blok 26395272).
- **Emisi pool 0: ≈ 2.859,9 MOR/hari sekarang.** Diukur langsung on-chain:
  `getPeriodRewards(0, 1794443279, 2109803280)` = **6.489.196,08 MOR** (10 tahun ke depan dari blok fork);
  `getPeriodRewards(0, 1794180863, 1794443279)` = 8.687 MOR (≈ 3,04 hari — konsisten).
  Total ter-emisi s.d. blok fork ≈ 3,18 juta MOR; pool stETH sudah menerima 1.216.066,9 MOR kumulatif
  (**≈ 38%** dari total ter-emisi — dipakai sebagai pangsa historis pool stETH).
- **Enumerasi pengguna yang pernah `setClaimReceiver`** — dilakukan via Blockscout logs API
  (`https://eth.blockscout.com/api`, event `ClaimReceiverSet`, rentang penuh dari blok pembuatan proxy
  **19178638** (8 Feb 2024, creator `0x040ef6fb6592a70291954e2a6a1a8f320ff10626`) s.d. 26395272.
  RPC publik `ethereum.publicnode.com` menolak `eth_getLogs` blok lama
  (403 "Archive requests require a personal token") — Blockscout yang dipakai sebagai gantinya.
  - **stETH: tepat 2 event, 2 staker berbeda** — (1) blok 25583640, tx
    `0x621c11df1f98d47bb15418a914e9cfdb74ae47174ab5c3acb19d78cee09bb132` (~21 Jul 2026):
    staker `0x8bf5941d27176242745b716251943ae4892a3c26`, receiver
    `0xB6a93bddfe2f6AFB5cF4120F5b482af6D5F8Bd94`;
    (2) blok 25995414, tx `0xbf1dcdd1004677acd90e0d5f74eeb3a46b1bff2d623c1b4a596cc06d64b6eb74`
    (~29 Jul 2026): staker `0xAf32BB3892F37c518f3841275F131384a41B9b57`, receiver
    `0xbe1bFB539b505fA34496DAF194c6A0F30ca5eE21`.
  - WBTC: 0. WETH: 0. USDT: 0.
  - **USDC: 8 event, 2 staker berbeda** — `0x8bf5941d27176242745b716251943ae4892a3c26` (user A yang sama)
    dan `0x387a161c6b25aa854100abaed39274e51aaffffd` (user B), masing-masing beberapa kali ganti receiver.
- **State korban nyata (blok ~26395272):**
  - Staker A stETH (`0x8bf5941d…`): deposited 0.011 stETH; claimLockStart = claimLockEnd = 0 (tidak pernah lock);
    virtualDeposited 0.0111; multiplier **1.01x** (referral saja); `getLatestUserReward` = **0.16684793886998388 MOR**;
    receiver `0xB6a93bddfe2f6AFB5cF4120F5b482af6D5F8Bd94`.
  - Staker C stETH (`0xAf32BB3892F37c518f3841275F131384a41B9b57`): deposited **0.07 stETH**; claimLockStart =
    claimLockEnd = 1788662903 (lock nol, sudah lewat); virtualDeposited 0.0707; multiplier **1.01x**;
    `getLatestUserReward` = **0.852674181271831644 MOR**; receiver `0xbe1bFB539b505fA34496DAF194c6A0F30ca5eE21`.
  - USDC staker B: 22 USDC, 1.01x, pending 0.405966812543368048 MOR.
  - USDC staker A: 51 USDC, claimLockStart 1787858399, claimLockEnd **1848418284 (27 Jul 2028 — BELUM lewat)**,
    virtual 180.340208, multiplier **3.536x**, pending 3.034052285317322574 MOR.
  - Kesimpulan: kedua receiver-setter stETH saat ini hanya 1.01x (referral, tanpa long lock) — PoC korban nyata
    membuktikan bypass-nya LIVE, tapi bukan magnitudenya. Korban sintetik dibutuhkan untuk demo 10,5x → 1x.
- **Skenario PoC (korban sintetik 100 stETH, lock 10 tahun):** multiplier **10.489062694784925285x**,
  virtualDeposited 1048.906269478492528479 stETH; pangsa pool ≈ 1048.9 / (10721.2 + 1048.9) ≈ **8.9%** emisi pool 0.
  Setelah `claimFor` orang asing: multiplier **tepat 1x**, virtualDeposited = deposited (100 stETH),
  claimLockEnd = 0. Laju yield masa depan terpotong **~9,6x**.
  Estimasi (metode: pangsa virtual × pangsa historis pool stETH 38% × emisi 2859,9 MOR/hari):
  korban ≈ 96,8 MOR/hari sebelum → ≈ 10,0 MOR/hari sesudah; kerugian ≈ **86,8 MOR/hari**, permanen,
  sampai korban re-lock dari nol — dan lock baru punya multiplier maksimum yang makin menurun seiring
  jendela tanh berjalan, jadi kerugian selama masa re-lock tidak pernah kembali.
- **Biaya penyerang (terukur di PoC):** gas + fee LayerZero **0.000514308950474341 ETH ≈ $1,11**
  (pada harga ETH $2148,6); dapat diulang setiap kali korban re-lock.
- **Korban nyata di PoC:** 0.904152578471804067 MOR dipaksa-mint ke receiver korban
  (pending sebelum claim 0.852674181271831644 MOR + ~0,05 MOR dari distribusi saat claim sendiri).
- **MOR → USD: tidak ada harga MOR live yang terverifikasi.** CoinGecko id `morpheus` mengembalikan
  $4,6236e-08 — dinilai TIDAK kredibel (kemungkinan token berbeda); JANGAN dipakai. Semua jumlah MOR
  dinyatakan dalam token; USD hanya dipakai untuk stETH (harga on-chain) dan fee ETH.

## PoC fork — hasil run

Folder: `~/workspace/morpheus-submission-pack/c1-claimfor-griefing/`
File: `foundry.toml`, `test/C1ClaimForGriefingFork.t.sol`, `lib/forge-std` (forge-std v1.17.0, file biasa — BUKAN
submodule; clone biasa + `forge test` cukup, tanpa `--recurse-submodules`), `.gitignore`, `foundry.lock`.

Perintah persis (Forge v1.9.0, solc 0.8.20):
```bash
export PATH="$HOME/.foundry/bin:$PATH"
cd ~/workspace/morpheus-submission-pack/c1-claimfor-griefing
forge clean && forge test -vv
```
RPC dari env `FORK_RPC_URL` (default `https://ethereum.publicnode.com`); blok dari env `FORK_BLOCK`
(default `26395272`). Di `setUp`, bila publicnode gagal, otomatis fallback ke `https://1rpc.io/eth` lalu
`https://rpc.flashbots.net` (keduanya terverifikasi bekerja 12 Nov 2026).

Hasil run terakhir (clean build, 12 Nov 2026):
```
Compiling 20 files with Solc 0.8.20 — Compiler run successful!
[PASS] test_RealVictim_StrangerClaimFor() (gas: 1471669)
  victim: 0xAf32BB3892F37c518f3841275F131384a41B9b57
  attacker (stranger): 0x9dF0C6b0066D5317aA5b38B36850548DaCCa6B4e
  victim pending MOR before (wei): 852674181271831644
  victim claimLockStart/End before: 1788662903 1788662903
  LayerZero fee paid by attacker (wei): 514308950474341
  MOR force-minted to victim receiver (wei): 904152578471804067
[PASS] test_StrangerClaimForWithoutReceiverReverts() (gas: 1089908)
[PASS] test_SyntheticVictim_MultiplierCollapse() (gas: 3214801)
  multiplier before (x1e25): 104890626947849252850000000
  deposited (wei): 99999999999999999998
  virtualDeposited before (wei): 1048906269478492528479
  LayerZero fee paid by attacker (wei): 514308950474341
  multiplier after (x1e25): 10000000000000000000000000
  MOR force-minted to victim receiver (wei, HARNESS ARTIFACT - see header): 3418347574752545595
Suite result: ok. 3 passed; 0 failed; 0 skipped
```

Isi 3 test:
1. `test_RealVictim_StrangerClaimFor` — menyerang staker C yang nyata: assert receiver sudah diset,
   penyerang tidak pernah diotorisasi (`claimSender` false), pending > 0, quote fee LZ via endpoint,
   claim oleh orang asing; assert `UserClaimed` ke receiver korban, lock terhapus 0, pending 0,
   `MintMessageSent` ke receiver.
2. `test_SyntheticVictim_MultiplierCollapse` — korban baru: `deal` 100 ETH → Lido `submit` → ~100 stETH;
   approve **DISTRIBUTOR** (bukan DepositPool — `Distributor.supply` melakukan `safeTransferFrom(holder)`);
   stake 100 stETH lock 10 tahun (~10.489x); `setClaimReceiver`; capture jawaban Chainlink live pre-warp;
   warp 10y+1; mock feed fresh; donasi 0.001 stETH ke Distributor sebagai yield; orang asing claim →
   assert multiplier == PRECISION (1x), claimLockEnd == 0, virtualDeposited == deposited.
3. `test_StrangerClaimForWithoutReceiverReverts` — kontrol: staker tanpa receiver, `claimFor` orang asing
   revert `"DS: invalid caller"`.

## Disclosure harness (juga tertulis di header file test — JANGAN dihapus)

(a) **Chainlink:** setelah warp 10 tahun, jawaban aggregator on-chain sudah stale, sehingga
`distributeRewards` akan gagal pada cek harga. Harness `mockCall` tiap aggregator `latestRoundData()`
untuk mengembalikan HARGA LIVE yang sama yang di-capture saat fork, dengan `updatedAt` = waktu saat ini
(warped) — yaitu apa yang akan dikembalikan feed Chainlink live. `ChainLinkDataConsumer` Morpheus TIDAK di-mock.
(b) **Yield stETH:** pool stETH (strategy NONE) hanya mengakumulasi yield dari rebase Lido, yang berhenti di
fork tanpa laporan oracle. Tanpa yield apa pun, `distributeRewards` memarkir emisi 10 tahun di
`undistributedRewards` dan pending korban tetap 0, sehingga `_claim` revert `"DS: nothing to claim"` —
bukan karena temuannya salah, tapi karena fork membekukan yield. Harness mendonasikan 0.001 stETH
(dibeli via Lido) ke Distributor, yang diperlakukan akuntansi Distributor sendiri sebagai yield (jalur yang
sama persis dengan yang diambil keuntungan rebase nyata).
**Konsekuensi: jumlah MOR yang ter-mint di test 2 adalah ARTEFAK HARNESS dan TIDAK BOLEH dikutip sebagai
yield nyata korban** — test meng-assert KERUSAKAN STATE (multiplier, lock, virtualDeposited), bukan jumlah MOR.
Jumlah 3.418 MOR itu kecil karena pembagian distribusi di fork didominasi yield Aave WETH yang tak tersapu
(perkiraan dari rekayasa balik pangsa ~38 MOR / 6,49 juta: ≈ 1,6 WETH; pool stETH hanya mendapat ~38 MOR
dari distribusi 6,49 juta di fork) — bukan karena temuannya kecil.
Kuantifikasi dampak nyata ada di bagian "Amount at risk" di atas, dihitung dari state on-chain live.

## Workaround teknis (dicatat agar tidak diulang / tidak "diperbaiki" sembarangan)

- **`foundry.toml`: `evm_version = "osaka"` WAJIB** — `"paris"` gagal dengan `EvmError: NotActivated` pada
  `balanceOf` aToken Aave di dalam `distributeRewards` (kode pasca-Fusaka butuh osaka). `via_ir = true`
  dibutuhkan untuk stack-too-deep di test 2.
- **Decode struct `depositPools` (mengandung `string`) via interface call revert di bawah via-IR** (dibuktikan
  dengan test debug sementara, sudah dihapus): struct decode revert, sedangkan raw staticcall + `abi.decode`
  tuple yang sama berhasil. Perbaikan: `_captureFeedAnswers` memakai raw staticcall dengan
  `IDistributor.depositPools.selector` + `abi.decode` tuple untuk mengambil `chainLinkPath`; interface
  mendeklarasikan `depositPools` dengan return tuple + komentar penjelas.
- **`vm.getRecordedLogs()` MENGHABISKAN log yang direkam** — ambil sekali ke `Vm.Log[] memory logs` lalu
  pakai ulang untuk `_findUserClaimed` dan `_findMintMessageSent`.
- **Bug timestamp warp + via-IR:** nilai yang diturunkan dari `block.timestamp` (mis. `warpedTs`) TIDAK BOLEH
  diteruskan sebagai argumen melewati batas `vm.warp` — code generator via-IR mengevaluasi ulang ekspresi
  itu di call site, setelah warp (terbukti: `warpedTs` = 2109803280 di call site, tapi `ts_` di dalam fungsi =
  2425163281 = warpedTs + TEN_YEARS + 1). Perbaikan: `_mockFeedsFresh` membaca `block.timestamp` sendiri
  di dalam fungsi. (Inilah yang diperingatkan linter `vm.warp` — baca lintas batas `vm.warp`.)
- **Lido `submit()` mengembalikan SHARES (~80 untuk 100 ETH), bukan jumlah stETH (~100)** — wajib pakai
  `balanceOf()` setelah submit (korban, donor, dan test kontrol).

## RPC (diamati 12 Nov 2026)

- `https://ethereum.publicnode.com`: OK untuk state terbaru (`eth_call`, nomor blok), TAPI `eth_getLogs`
  blok lama → 403 "Archive requests require a personal token".
- `https://eth.llamarpc.com` → HTTP 525. `https://rpc.ankr.com/eth` → butuh API key.
  `https://eth.drpc.org` → timeout. `https://cloudflare-eth.com` → error -32046.
- **`https://1rpc.io/eth` → BEKERJA** (blok 26395257) dan melayani receipt lama (tx Jul 2026).
  **`https://rpc.flashbots.net` → BEKERJA** (blok 26395257).
- Blockscout API (`https://eth.blockscout.com/api`) bekerja untuk `getLogs` dan `getcontractcreation`
  tanpa key — dipakai untuk enumerasi `ClaimReceiverSet`.

## CodeHawks 2024-01 submission #137 — BELUM TERVERIFIKASI

Judgement submission #137 (area claim, griefing via refund LayerZero yang gagal) **belum terverifikasi**.
Percobaan verifikasi via web dihentikan oleh classifier keamanan di tengah tugas; sesuai aturan tugas,
verifikasi TIDAK dicoba ulang dan status dicatat apa adanya. **C-1 berstatus NEEDS-WORK untuk cek ini.**
Yang perlu dilakukan sebelum kirim: buka halaman CodeHawks 2024-01 (Morhepus) submission #137, konfirmasi
judgement-nya (Accepted / duplikat / invalid / dll.), lalu perbarui caveat di REPORT.md + status di sini.
Jangan klaim sudah dicek sebelum benar-benar dicek.

## Caveat (juga di REPORT.md)

- Terkait tematik dengan CodeHawks #137 (mekanisme berbeda: #137 soal refund gagal; C-1 soal cek otorisasi
  yang dilewati seluruhnya).
- Pengecualian griefing program §1.2: ini kerugian yield masa depan permanen, bukan pencurian principal —
  penyerang tidak mendapat apa-apa, principal korban utuh. Diajukan atas dasar kerugiannya permanen dan
  bukan kesalahan korban sendiri: korban tidak bisa membatalkannya tanpa memulai ulang jam lock, dan
  multiplier maksimum untuk lock baru menurun seiring jendela tanh berjalan.

## File dalam paket

- `test/C1ClaimForGriefingFork.t.sol` — PoC fork (3 test, lolos 3/3 dari clean build).
- `REPORT.md` — draft laporan 1 halaman (heading TARGET / INVARIANT / ATTACK / IMPACT / POC / FIX).
- `notes.md` — file ini.
- `foundry.toml`, `foundry.lock`, `.gitignore`, `lib/forge-std` (v1.17.0, file biasa).

## Aturan keras yang dipatuhi

- TIDAK ada repo/gist publik yang dibuat. TIDAK ada email yang dikirim.
- TIDAK ada transaksi mainnet/testnet — fork/read-only saja.
- Repo `~/workspace/morpheus-smartcontracts` dan folder audit lain TIDAK diubah; kerja hanya di folder output ini.
