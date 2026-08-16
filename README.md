<div align="center">

# ⚔️ SLCW Fleet

**Multi-wallet game automation that decides with arithmetic, not with an if/else ladder.**

[![tests](https://github.com/rygroup-dev/slcw-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/rygroup-dev/slcw-bot/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![tests](https://img.shields.io/badge/tests-237%20passing-4c1)](tests/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Every action is priced in gold-per-hour before it runs.
Anything that spends premium currency or moves funds cannot run at all — by construction, not by policy.

</div>

---

## Contents

[Install](#install) · [Setup](#setup) · [Control](#control) · [How it decides](#how-it-decides) · [Guardrails](#guardrails) · [Multi-wallet](#multi-wallet) · [Staying unremarkable](#staying-unremarkable) · [Configuration](#configuration) · [Layout](#project-layout) · [Limits](#known-limits)

---

## Install

```bash
git clone https://github.com/rygroup-dev/slcw-bot.git /root/slcw-bot
cd /root/slcw-bot && bash install.sh
```

That is the whole thing. The installer detects your package manager, installs
system prerequisites, builds an isolated virtualenv, installs pinned dependencies,
and hands over to the setup wizard.

**Requirements:** Linux with systemd, Python 3.11+, root. All four Python
dependencies ship prebuilt wheels, so no compiler is needed.

---

## Setup

```
  1) Credentials    Telegram bot token, your chat id, the game's Firebase key
  2) Vault          generated passphrase, or type your own
  3) Unlock mode    automatic on boot  ·  or ask in Telegram each restart
  4) First wallet   ┌─ 1) Import — paste a seed phrase or private key
                    └─ 2) Create a new one automatically
  5) Service        systemd unit installed, enabled, started
```

Re-running `bash install.sh` is safe — anything already configured is detected and
left alone.

> [!TIP]
> **Start in dry-run.** `.env.example` ships with `SLCW_DRY_RUN=true`, so the first
> cycles decide and log without touching the game. Watch the reasoning in
> **🧠 Kenapa?**, then flip it when it looks right.

### Unlock mode is a real trade-off

| | Automatic | Telegram |
|---|---|---|
| After a reboot | starts on its own | idle until you send `/unlock` |
| Passphrase on disk | in root-only `.vault-key` | never |
| Protects against | a stolen backup or repo | a stolen backup, repo, **and root** |

The wizard states this plainly and defaults to automatic, because a bot that
silently stops after an unattended reboot is its own kind of failure.

---

## Control

Everything runs from inline keyboards that edit **one live dashboard**, rather than
flooding the chat with stale snapshots.

```
┌─────────────────────────────┐
│  📊 Status      💰 Profit   │
│  🏪 Market      🌾 Farming  │
│  ⚔️ Combat      👛 Wallets  │
│  ⚙️ Kontrol     🔐 Vault    │
└─────────────────────────────┘
```

| View | Shows |
|---|---|
| **📊 Status** | per-wallet HP/energy bars, gold, current activity, next wake time |
| **💰 Profit** | realised gold and XP, rate per hour, win rate, drops valued at live bids |
| **🏪 Market** | order book, crossed spreads, your holdings at best bid |
| **🌾 Farming** | what every gathering site would actually pay, both funding modes |
| **⚔️ Combat** | the learned per-monster model — which zones it blocks and attacks |
| **👛 Wallets** | per-wallet detail, pause/resume, force cycle, **🧠 Kenapa?** |
| **⚙️ Kontrol** | resume, pause, force cycle, dry-run toggle, logs, doctor |

Only two commands are typed — `/unlock <passphrase>` and `/import <secret>` — and
both **delete your message before the secret is parsed**, so a mistyped key never
sits in the chat history.

### Alerts earn their interruption

A bot that reports every cycle trains you to ignore it. These push; nothing else does:

🛑 circuit-breaker trip · 🔑 auth failure · 🚧 guardrail violation · ⬆️ level-up ·
⚡ crossed spread · 💎 drop above your value threshold · 🔐 vault locked

---

## How it decides

Each cycle builds every legal action, scores it, discards the ones that lose money,
and takes the best.

```
              gold_equivalent − gold_cost − energy × energy_price − hp × hp_value
    score  =  ─────────────────────────────────────────────────────────────────────
                                        duration_hours

    energy_price  =  base × (1 − energy / max_energy)²
```

| Action | Value |
|---|---|
| `finishActivity` | **∞** — the reward is already earned, it is sitting there |
| `claimInitialReward` | **∞** — free |
| `spendAttributePoints` | **∞** — free progression |
| `startProduction` | 1,000 gold per 18 minutes for 10 energy |
| `startFarming` | resources × live best bid − gold cost |
| `battle` | XP value + expected drop value − HP risk |
| `startRelax` | option value of the HP and MP it restores |

### The energy shadow price

Energy is not free, but it is not always scarce either. Its price rises quadratically
as the bar drains:

- **Full bar** → price ≈ 0 → the binding constraint is *time*, so actions rank by **gold per hour**
- **Empty bar** → price at maximum → the binding constraint is *energy*, so actions rank by **gold per energy**

The blend is continuous, so there is no threshold for the engine to oscillate around.

### It refuses to lose money

Actions returning less than they cost are discarded. This is why the bot currently
**declines to gather**: raw ores, logs, fibers and skins have no market bid, and it
will not spend gold producing output it cannot price. That is the guard working, not
a bug.

### It shows its work

Every decision stores its full score vector. **🧠 Kenapa?** prints it:

```
Chose: startProduction — 1000 gold over 18m
  startProduction         3,333 g/h   (10en, 18m) 1000 gold over 18m
  battle                  1,204 g/h   (1en, 1m)   22 xp @ 8g + drops worth 0g
  startRelax                412 g/h   (0en, 2m)   restores 6 HP / 0 MP
```

### Combat is learned, not rolled

Each turn the server reveals both sides — which zone it blocked, which zone it
attacked. Both distributions are per-monster and stable enough to model:

```
forestspider_lvl1_2 · 340 rounds
  Blocks:  H 61%  T 24%  L 18%   →  attack legs
  Attacks: H 22%  T 19%  L 59%   →  defend legs
```

18% of moves stay random, which keeps the estimates honest if the monster changes
and stops our own choices from becoming predictable.

---

## Guardrails

`guardrails.check()` runs **inside the transport**, not in the orchestrator. Every
path to the network passes through it, so a denied call fails before a request is
even constructed — no caller has to remember.

**Permanently denied. Not configurable, not toggleable:**

| Denied | Why |
|---|---|
| `skipActivityTime` | costs 5 diamonds per remaining minute |
| farming `diamond` mode | licence costs 49 × 3^(tier−1) diamonds |
| `withdraw`, `transfer`, `signTransaction` | moves real funds |
| `createOrder`, `cancelOrder` | the bot never trades on its own |
| `payCityEntryFee`, `buyLevel`, arena queue | costed, with no measured return |

Crossed spreads are surfaced with the numbers so **you** can act. The bot will not
place the order.

---

## Multi-wallet

Add accounts from **👛 Wallets → ➕**, or `./slcwctl new 3`.

> [!NOTE]
> **New accounts need no SOL.** The game accepts a bare public key with an empty
> balance, so scaling costs nothing. In-game onboarding runs automatically on the
> first cycle.

Import existing accounts with `/import` in Telegram or `./slcwctl import` for a
hidden prompt that keeps the secret out of shell history. Accepted formats:

`base58` · `JSON byte array` · `hex` (with or without `0x`) · `12/24-word seed phrase`

> [!WARNING]
> **A seed phrase does not identify one account.** Wallet apps derive at
> `m/44'/501'/0'/0'`; `solana-keygen` uses the bare BIP39 seed. The same twelve
> words produce **two different addresses**.
>
> Picking wrong is silent — it authenticates fine, then farms an empty profile
> forever. So both are derived and offered as buttons, and you confirm which is
> yours. The SLIP-0010 implementation is verified against the official ed25519 test
> vectors, and imported keys are re-derived and checked against the expected public
> key so a truncated paste fails at import.

Each wallet gets its own browser persona, session, error counter, schedule, and a
persistent 6–9 hour daily offline window. A failing wallet pauses **itself** and
leaves the rest of the fleet running.

---

## Staying unremarkable

The goal is not to be undetectable — nothing is. It is to not stand out.

| | Approach |
|---|---|
| **TLS** | `curl_cffi` reproduces Chrome's JA3 handshake |
| **Headers** | full browser set; personas are sticky per wallet, so an account always looks like the same browser |
| **Sessions** | log in once, refresh thereafter — roughly **1 login/day** instead of 288 |
| **Timing** | wake times derive from the *server's* activity clock plus a log-normal reaction delay (90s median, long tail) |
| **Rhythm** | each wallet sleeps 6–9 hours daily, anchored to its own hour |
| **Retries** | transient failures are absorbed in the transport, so noise never trips a breaker |

> [!IMPORTANT]
> The realistic risk for a Firebase game is **server-side behavioural analysis** —
> accounts playing 20 hours a day with mechanically even spacing — not HTTP
> fingerprinting. The sleep personas and log-normal delays exist for that, and they
> are the part that makes multi-wallet viable.

---

## Configuration

Every key in `.env` is read by `slcw/config.py`. There are no decorative settings.
See [`.env.example`](.env.example) for the annotated full list.

| Key | Default | Effect |
|---|---|---|
| `SLCW_ENABLED` | `true` | `false` = claims and rest only, no gameplay |
| `SLCW_DRY_RUN` | `true` | decide and log, never call the game |
| `SLCW_REACTION_MEDIAN_SECONDS` | `90` | median delay after a server activity ends |
| `SLCW_IDLE_MIN/MAX_SECONDS` | `240`/`900` | poll spacing when nothing is pending |
| `SLCW_SLEEP_MIN/MAX_HOURS` | `6`/`9` | daily offline window per wallet |
| `SLCW_MAX_ERRORS` | `3` | consecutive real errors before a wallet self-pauses |
| `SLCW_REST_HP_RATIO` | `0.55` | rest below this fraction of health |
| `SLCW_GOLD_RESERVE` | `500` | gold-funded actions never spend below this |
| `SLCW_RICH_DROP_GOLD` | `2000` | drop value that triggers an alert |

### Administration

```bash
./slcwctl doctor          # configuration and environment health
./slcwctl list            # wallets, public keys only
./slcwctl new 3           # generate wallets
./slcwctl import          # import from a hidden prompt
./slcwctl why wallet-01   # last decision with its full score vector
journalctl -u slcw-fleet -f
```

---

## Project layout

One daemon runs a fleet of independent wallet workers and the Telegram control
plane. No external scheduler, no per-cycle subprocess.

```
daemon.py                 fleet + control plane in one process
├── slcw/
│   ├── config.py         typed settings; every key is actually read
│   ├── keys.py           secret parsing, BIP39, SLIP-0010 derivation
│   ├── vault.py          AES-256-GCM wallet storage, scrypt KDF
│   ├── guardrails.py     allowlist, enforced inside the transport
│   ├── transport.py      Chrome impersonation, personas, retry/backoff
│   ├── auth.py           login once, refresh thereafter
│   ├── api.py            typed game calls, paginated Firestore queries
│   ├── model.py          player state, timestamp normalisation
│   ├── market.py         order book, valuation, crossed detection
│   ├── farming.py        gathering catalog and cost model
│   ├── combat.py         learned per-monster zone strategy
│   ├── economy.py        expected-value scoring
│   ├── orchestrator.py   choose, execute, record why
│   ├── scheduler.py      per-wallet wake times and sleep personas
│   ├── runner.py         worker threads, per-wallet circuit breakers
│   ├── notify.py         rate-limited alerts
│   └── ledger.py         realised reward accounting
├── bot/                  Telegram keyboards, routing, rendering
└── tests/                237 tests, fake transport only
```

The leaf modules do no I/O, which is why the whole suite runs in under a second.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Nothing authenticates, calls the game, or touches a real account — safe to run
anywhere, including CI. Coverage sits where mistakes are expensive: timestamp
classification, score ordering, guardrail enforcement, the refresh-versus-login
path, retry behaviour, battle settlement at the turn cap, market depth and
pagination, key parsing and SLIP-0010 derivation, vault encryption, and callback
routing.

---

## Known limits

Stated plainly, because a README that only lists strengths is not useful.

**No proxies configured.** Every wallet exits from the host IP, which reads
server-side as one operator. The per-wallet proxy field exists and fails closed when
set but unreachable — it is simply empty. This is the largest remaining gap and it
cannot be closed in code.

**Gathering does not pay yet.** Only *refined* goods trade — ingots, planks, cloth,
leather. Raw ores, logs, fibers and skins have no bids at all, so gathering is only
profitable through crafting. Mapping the crafting chain is the next meaningful
unlock, and the engine already declines to gather until then.

**Expeditions and arena are unmapped.** The callables exist and are denied until
their reward and cost models are measured, rather than enabled on a guess.

---

## License

[MIT](LICENSE).

Automating a game may conflict with its terms of service. You are responsible for
how you use this.
