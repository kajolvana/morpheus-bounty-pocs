# C-2 — notes (penyiapan paket submit Morpheus)

Status: **SEND-READY** (Low, timing-only) — fork PoC 3/3 lolos, menargetkan alamat live,
tanpa prank privileged. Satu-satunya caller yang di-prank adalah `stranger`
`0x9999` (alamat sembarang, unprivileged). Tidak ada transaksi mainnet/testnet.

## 1. Blok yang di-pin + RPC

- Chain: Arbitrum One. Fork block: **523422593**.
- RPC (default di test): `https://arb1.arbitrum.io/rpc` (env `FORK_RPC_URL` bisa override).
  `https://arbitrum.publicnode.com` TIDAK bisa dipakai untuk fork block yang di-pin:
  `403 "Archive requests require a personal token"` pada `eth_getAccount` state historis.
  Alternatif yang terverifikasi bekerja untuk blok yang di-pin:
  `https://arbitrum-one.public.blastapi.io`, `https://1rpc.io/arb`.
- Perintah run persis (dari folder ini):
  `forge test -vv` — atau eksplisit: `FORK_RPC_URL=https://arb1.arbitrum.io/rpc forge test -vv`

## 2. Hasil run PoC (12 Nov 2026 ~07:25 WIB)

```
Ran 3 tests for test/C2_CollectFees_Fork.t.sol:C2CollectFeesForkTest
[PASS] test_LiveState_V2ConfiguredNoPosition() (gas: 117998)
[PASS] test_RevertDiscrimination_StrangerCollectFeesHasNoOnlyOwner() (gas: 117310)
[PASS] test_StrangerCollectsFeesOnLiveReceiver_EndToEnd() (gas: 1960826)
Logs:
  position tokenId: 5768574
  position amount0 MOR used: 26140799623662697432269 (26.1408 MOR)
  position amount1 WETH used: 10000000000000000000 (10 WETH)
  fees owed token0 MOR (stale until touched): 0
  fees owed token1 WETH (stale until touched): 0
  collected amount0 MOR: 2700685665114711548 (2.700685665114711548 MOR)
  collected amount1 WETH: 675171416278677 (0.000675171416278677 WETH)
Suite result: ok. 3 passed; 0 failed.
```

Catatan: `positions().tokensOwed` tetap 0 setelah swap karena nilai itu hanya
di-sync saat posisi disentuh (collect/perubahan likuiditas) — bukan karena fee
tidak terakru. Buktinya adalah return `collectFees` di atas (> 0 di kedua token).

## 3. Verifikasi alamat live (read-only, `cast`)

Target tugas: L2TokenReceiverV2 (Arbitrum) `0x47176B2Af9885dC6C4575d4eFd63895f7Aaa4790`.

| Cek | Hasil (blok ~523421808–523422593) |
|---|---|
| `version()` | `2` → **benar V2 di alamat ini** |
| `owner()` | `0x151c2b49CdEC10B150B2763dF3d1C00D70C90956` |
| `router()` | `0xE592427A0AEce92De3Edee1F18E0157C05861564` (Arbitrum SwapRouter02) |
| `nonfungiblePositionManager()` | `0xC36442b4a4522E871399CD717aBDD847Ab11FE88` (NPM kanonis Arbitrum) |
| `secondSwapParams()` | WETH `0x82aF…` → MOR `0x092bAaDB…`, fee 3000, sqrtPriceLimitX96 0 |
| `firstSwapParams()` | wstETH `0x5979…` → WETH, fee 100, sqrtPriceLimitX96 0 |
| Slot EIP-1967 impl | `0x27353fFaDFD53538e8BDF81be7041C56CE2d5ae4` |
| NPM `balanceOf(receiver)` | `0` → receiver TIDAK punya posisi V3 |
| Saldo receiver | wstETH 0, WETH 0, MOR `2980659085866802225` (= 2.980659085866802225 MOR) |
| `collectFees(1)` dari stranger (`cast call --from 0x…9999`) | revert `Not approved` (dari NPM, bukan Ownable) |
| `swap(...)` dari stranger | revert `Ownable: caller is not the owner` |
| `collectFees(1)` dari owner (`cast call --from 0x151c…`, kontrol off-chain; owner TIDAK di-prank di PoC) | revert `Not approved` — SAMA seperti stranger. Satu-satunya gate adalah otorisasi NPM, bukan cek di receiver. |

### Byte-identity vs repo (HEAD 07f6032)

Runtime code impl live vs build lokal dari `~/workspace/morpheus-audit-C/forge`
(solc 0.8.20, optimizer 200, evm paris): panjang sama (7.646 byte); seluruh byte
yang berbeda hanya berada di 5 rentang immutable-reference artifact
(byte 1392–1412, 1465–1485, 1624–1644, 1688–1708, 2263–2283) + hash metadata
di ekor (byte 7603–7635). Artinya: **kode live identik dengan source repo
kecuali nilai immutable deployment + metadata hash** — sebagaimana mestinya.
Codehash live (di-pin di test): `0x90a7a2937f998c001250c26a4279822ee50ec710b4d990bf52320cac6f935d8b`.

### Koreksi terhadap laporan audit (§1): alamat `0x845F…` BUKAN L2TokenReceiver V1

Koordinator menyimpulkan `0x845FBB4B3e2207BF03087b8B94D2430AB11088eE` (Arbitrum)
"masih V1" hanya karena `version()` revert. Verifikasi langsung membantah itu:

- `owner()` = `0x151c…` (sama), tetapi `router()` / `nonfungiblePositionManager()` /
  `params()` (getter struct V1) semuanya revert, begitu pula `distributor()` / `stETH()`.
- `config()` BERHASIL: `(gateway 0x3c2269811836af69497E5F486A85D7316753cf62 [LZ Endpoint V1 Arbitrum],
  sender 0x845FBB4B… [dirinya sendiri], senderChainId 101)`.
- `rewardToken()` = `0x3c3A26c978Bf6AF40D7c1A36e9cBD3C1c055786E` (MOR lama).
- Slot impl-nya `0x2Efd4430489e1a05A89c2f51811aC661B7E5FF84`.

Jadi `0x845F…` adalah **proxy L2MessageReceiver (deployment lama)**, bukan
L2TokenReceiver V1: `sender` di config-nya menunjuk ke dirinya sendiri (placeholder
deploy script, lih. `deploy/3_init_bridge.migration.ts` yang memakai `0x845F…`
sebagai alamat L1Sender placeholder), dan `rewardToken`-nya adalah MOR lama
`0x3c3A…`. L2MessageReceiver yang benar-benar aktif adalah
`0xd4a8ECcBe696295e68572A98b1aA70Aa9277d427` (config sender = L1Sender
`0x2Efd…`, chain 101; `rewardToken` = MOR `0x092b…`; `isMinter` = true di MOR
`0x092b…`). Koreksi ini tidak mengubah C-2 — target C-2 yang benar
(`0x47176B2A…`) memang V2 — tetapi klaim "L2 live masih V1" di laporan audit
adalah keliru.

(Catatan alamat: `0x47176B2A…` yang sama di Ethereum adalah proxy DepositPool
stETH — deployer yang sama, nonce yang sama, kontrak berbeda per chain.)

## 4. Amount at risk — cara hitung

- **Live hari ini: $0.** Bukti: NPM `balanceOf(receiver) == 0` (tidak ada posisi),
  saldo token receiver 0 wstETH / 0 WETH / 2,98 MOR (debu, bukan fee).
  Tidak ada fee yang bisa dikumpulkan siapa pun saat ini.
- **Kehilangan yang didemonstrasikan (PoC end-to-end):** stranger mengumpulkan
  **2,700685665114711548 MOR + 0,000675171416278677 WETH** dari posisi yang
  didonasikan penyerang sendiri (posisi #5768574: 26,1408 MOR + 10 WETH, lalu swap
  5 WETH→MOR dan 20.000 MOR→WETH di pool WETH/MOR 0,3%
  `0xE5Cf22EE4988d54141B77050967E1052Bd9c7F7A` untuk mengakru fee).
  Nilai demo: 2,7007 MOR × ~$0,79 ≈ $2,13 + 0,0006752 WETH × ~$2.060 ≈ $1,39
  → **≈ $3,52**. Harga: MOR ~$0,79 (DefiLlama $0,78, CoinCodex $0,79, MEXC
  $0,7868, 12 Nov 2026); ETH ~$2.056–2.082 (Finnhub/OKX/CoinGlass, 12 Nov 2026).
  Angka $3,52 adalah ukuran demonstrasi, BUKAN dana live yang berisiko.
- **Batas atas bila receiver suatu hari memegang posisi:** sebesar fee yang
  terakru pada posisi itu (tidak terbatas oleh kontrak) — tetapi dana tetap
  masuk ke receiver, tidak bisa dicuri; yang hilang hanya kontrol timing.

## 5. Caveat jujur

- Hanya timing, bukan pencurian (`recipient = address(this)`).
- Nilai live $0 saat ini; temuan bersifat laten sampai receiver memegang posisi.
- Terkait tematik dengan C4 M-2, tetapi instance baru (missing check, bukan owner trust).
- Repo `~/workspace/morpheus-smartcontracts` tidak diubah. Tidak ada repo/gist
  publik dibuat; tidak ada email dikirim; tidak ada transaksi mainnet/testnet.
