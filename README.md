<div align="center">

# ⚔️ SLCW Fleet

**Multi-wallet game automation that decides with arithmetic, not with an if/else ladder.**

[![tests](https://github.com/rygroup-dev/slcw-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/rygroup-dev/slcw-bot/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![tests](https://img.shields.io/badge/tests-834%20passing-4c1)](tests/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Every action is priced in gold-per-hour before it runs.
Anything that spends premium currency or moves funds cannot run at all — by construction, not by policy.

</div>

---

## Contents

[Install](#install) · [Setup](#setup) · [Control](#control) · [How it decides](#how-it-decides) · [Guardrails](#guardrails) · [Multi-wallet](#multi-wallet) · [Staying unremarkable](#staying-unremarkable) · [Configuration](#configuration) · [Layout](#project-layout) · [Limits](#known-limits)

---

## Install

**One-liner**

```bash
curl -fsSL https://raw.githubusercontent.com/rygroup-dev/slcw-bot/main/install.sh | bash
```

**From a checkout**

```bash
git clone https://github.com/rygroup-dev/slcw-bot.git /root/slcw-bot
cd /root/slcw-bot && bash install.sh
```

Either way the installer detects your package manager, installs prerequisites,
builds an isolated virtualenv, installs pinned dependencies, and hands over to the
setup wizard. `SLCW_HOME=/opt/slcw` installs elsewhere; `SLCW_REPO=<url>` installs
a fork.

> [!NOTE]
> **Re-running updates in place.** On a machine that already has credentials and a
> vault, the same command pulls the new source, reinstalls dependencies, runs the
> test suite, and restarts the service — no wizard, no second wallet. If the tests
> fail the service is left on the previous code. A dirty working tree is never
> touched: the pull is skipped and your uncommitted files stay exactly as they are.

**Requirements:** Linux with systemd, Python 3.11+, root. All four Python
dependencies ship prebuilt wheels, so no compiler is needed.

<details>
<summary><b>Manual install</b> — if you would rather not run a script</summary>

```bash
# 1. Prerequisites
apt-get install -y python3 python3-venv git curl        # or dnf / apk

# 2. Source and virtualenv
git clone https://github.com/rygroup-dev/slcw-bot.git /root/slcw-bot
cd /root/slcw-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Credentials
cp .env.example .env && chmod 600 .env
#    fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SLCW_FIREBASE_API_KEY

# 4. Vault and first wallet
./slcwctl init          # creates the encrypted vault
./slcwctl new 1         # or: ./slcwctl import

# 5. Service
cp slcw-fleet.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now slcw-fleet

# 6. Check
./slcwctl doctor
journalctl -u slcw-fleet -f
```

The vault stays locked after each restart until you send `/unlock <passphrase>` in
Telegram. For unattended reboots, write the passphrase to a root-only file the
unit already loads:

```bash
printf 'SLCW_VAULT_PASSPHRASE=your-passphrase\n' > .vault-key && chmod 600 .vault-key
systemctl restart slcw-fleet
```

</details>

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

Re-running `bash install.sh` on a machine that already has a vault is an **update, not
an install**: it pulls the latest source, reinstalls dependencies, runs the test suite,
and restarts the service only if the tests pass. The wizard does not run, no wallet is
created or imported, and `.env` is not written. A dirty working tree stops the pull
rather than stashing your changes.

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
┌─────────────────────────────┐        ⚗️ Ekonomi
│  📊 Status      💰 Profit   │        ├── 🔗 Rantai profit
│  🏪 Market      ⚗️ Ekonomi  │        ├── 🌾 Gathering
│  ⚔️ Combat      🎯 Task     │        ├── ⚗️ Refining
│  🎒 Inventory   🧬 Profil   │        ├── ⚡ Energi  · 🗺 Peta
│  👛 Wallets     🔨 Crafting │        ├── 🟢 Gold-mode ON/OFF
│  ⚙️ Kontrol     🔐 Vault    │        ├── ⏱ Durasi gold-mode
└─────────────────────────────┘        └── 🟢 Auto-travel ON/OFF
```

| View | Shows |
|---|---|
| **📊 Status** | per-wallet HP/energy bars, gold, current activity, next wake time |
| **💰 Profit** | realised gold and XP, rate per hour, win rate, drops valued at live bids |
| **🏪 Market** | order book, crossed spreads, your holdings at best bid |
| **🔗 Rantai profit** | the full raw → catalyst → refined chain with the margin at each link |
| **🌾 Gathering** | what every gathering site would pay, both funding modes |
| **⚗️ Refining** | per-workshop feasibility and exactly what each run is short of |
| **🧬 Profil** | attributes, the stats they produce, active set bonus, next point |
| **⚡ Energi** | free refill quota per wallet — three a day, easy to leave unused |
| **🗺 Peta** | where each wallet stands and how far every useful destination is |
| **🎯 Task** | hunt-ladder progress, reward, and why it may be locked |
| **🎒 Inventory** | slots, unopened chests, which gear slots are still bare |
| **🔨 Crafting** | every recipe you could start here, and what the rest are short of |
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
| `buyLevel` | **∞** — free with the "none" booster; only diamonds cost |
| `spendAttributePoints` | **∞** — free progression, allocated by build policy |
| `refillEnergyFree` | **∞** — three a day, free, and energy gates everything |
| `claimTaskReward`, `acceptTask` | **∞** — free gold from the hunt ladder |
| `openChests` | **∞** — free loot sitting in an inventory slot |
| `equipItem` | **∞** — gear in an empty slot is pure gain |
| `startTravel` | the destination's best action, amortised over travel time |
| `startRefining` | output × live best bid − gold cost |
| `purchaseCraftingItem` | the refining run it unlocks − its own cost |
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

### The production chain

Gathering on its own loses money — raw ores, logs, fibers and skins carry **no market
bids at all**. Only refined goods trade. The engine models the whole chain and prices
every link from live data:

```
  gather (gold mode, 0 energy)      9 × copper_ore        37 g
  buy catalyst from the city shop   1 × smelting_flux_1   20 g
  refine                                                   5 g
                                                    ─────────
                                          cost            62 g
                                          →  1 copper_ingot @ 888 g   🟢 +826 g · 14×
```

Every number there is measured: the gathering cost comes from the gold-mode formula,
the catalyst price from the city shop table, the refining cost from the per-tier
table, and the bid from the live order book.

Catalysts are the one input that cannot be gathered or traded, so when they are the
only thing blocking a profitable run the engine buys them — scored by the value of
the run they unlock, not as a bare outflow.

### It walks the chain by itself

Gathering happens at farm zones and refining in city workshops, so a wallet that
never moves is limited to whatever its current tile offers. The engine evaluates
every economically meaningful destination, prices its best action, and **amortises
that over the travel time** it would cost to get there:

```
at Crystal Cave, holding 900 copper_ore
  → startTravel   193,829 g/h   travel 5m to Agnos for startRefining
at Agnos
  → startRefining 317,880 g/h   50× copper_ingot from 450 ore + 50 flux + 250g
```

Travel time follows the client's own formula — 20 seconds per unit of map distance,
less any mount bonus. A destination must beat staying put by `SLCW_TRAVEL_MARGIN`
(1.35× by default) before the trip is taken, because travel time is dead time and a
marginal gain does not repay it.

### Two switches worth understanding

Both are on by default, both are toggled from **⚗️ Ekonomi**, and they answer
different questions.

**🌾 Gold-mode gathering — how a gathering run is paid for**

Gathering has two funding modes, and the engine scores both:

| | Energy mode | Gold mode |
|---|---|---|
| Costs | 1 energy + `3^(tier−1)` gold per unit | gold only, no energy |
| Duration | 1 minute per unit | a fixed 1–8 hours |
| Wallet is | free after each short run | **locked for the whole run** |

Gold mode is efficient on paper — it turns idle gold into materials without
touching the energy bar, so it runs alongside the energy economy rather than
competing with it. But it takes the wallet out of circulation entirely: no
levelling, no chest opening, no task claims, no reacting to anything, until it
finishes. A gold-per-hour score cannot express that cost, which is exactly why
this is a switch and not something the engine decides.

Shorter runs stay nimble and get interrupted more often; longer runs return the
most per trip and cost the most flexibility. Four hours is a reasonable middle
while accounts are still low level and each level is cheap.

**🗺 Auto-travel — whether a wallet may relocate**

Gathering happens at farm zones, refining in city workshops, production and
battle somewhere else again. A wallet that cannot move is limited to whatever
tile it happens to stand on.

Each destination is valued by what the wallet would actually *do* there — not
one action, but the whole stay, because a wallet that travels to fight fights
until its energy runs out. That distinction matters more than it sounds:

```
walk 22 min to farm_3, counted as one 45s battle     →    465 g/h   (never goes)
walk 22 min to farm_3, counted as the real stay      →  8,900 g/h   (goes)
```

Scored the first way, travel always lost, and every wallet stayed pinned to
whatever it started doing — the gold-rich ones farming forever with no XP, the
gold-poor ones battling forever with no gold. A destination must still beat
staying put by `SLCW_TRAVEL_MARGIN` (1.35x) before the trip is taken, which is
what stops the fleet pacing between locations for marginal gains.

**In short:** gold-mode decides *how long a wallet disappears for*; auto-travel
decides *whether it is allowed to go somewhere better*. Turning gold-mode off
keeps wallets responsive; turning auto-travel off keeps them where they are.

### It refuses to lose money

Actions returning less than they cost are discarded. If an output has no bid, it is
valued at zero and loses to anything measurable, rather than being taken on optimism.

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

### A price you cannot collect is not a price

The scorer values gathering, refining and crafting at live market bids. That is
the obvious thing to do and it is wrong here, because **nothing this bot can
sell is a raw resource**. The player market wants premium currency no wallet
has; the only sale it can make is `sellEquipmentItem`, and that takes finished
gear. Every ore in the ground is priced in gold the bot has no way to reach.

It never showed, because free actions always won and every wallet stood in the
Borderlands forever. Sending wallets to Greyholm broke the spell: one that
finishes at the altar re-picks from nothing, and the winner was a twenty-minute
walk to Crystal Cave scored at 24,084 gold an hour — against a hunt task chain
that pays 1,700 a task and has actually banked it, every hour, all along.

So a wallet holding a hunt task it cannot advance walks back to the Borderlands,
and that trip is a free action rather than a scored one. Not because travel is
special, but because the gold at the end of it is real and the number it is
competing with is not.

That gap is now closed, and closing it meant finding out where the game
actually pays. Every open order on both markets was pulled and counted:

| | Orders | Buy side |
|---|---|---|
| Player market (`market_orders`) | 3,495 | three distinct items, none the fleet holds — and placing an order needs premium currency no wallet has |
| **Black Market** (`blackmarket_orders`) | **6,000** | **refined goods, and only refined goods** |

Raw ore, logs, hides and monster drops have no bid anywhere. Refined ones have
standing demand in the thousands: copper_ingot at 899 with 6,324 wanted,
iron_ingot 1,350, steel_ingot 2,100, mithril_ingot 3,300, echo_ferocity 15,001.

`executeBlackMarketOrder` fills against that book instantly, is paid in gold,
costs no premium and needs no travel. Measured before allowlisting: five
copper_ingot returned a quote of 4,495 with tax 899, and the balance rose by
3,596. So the market takes a fifth, and the ledger records the net.

That makes the whole production chain real for the first time — gather, refine,
sell — and it is why the gathering valuations were not nonsense after all, only
unrealisable. A stack is sold down to a reserve rather than emptied, because the
same ingots are what the crafting bench turns into gear the shop pays ten times
as much for.

### The ceiling, and the way through it

A character cannot pass level `15 × grade`. Every wallet in this fleet was grade
1 at level 15, which means every point of XP any of them earned — around a
thousand fights a day — was discarded on arrival. Nothing reported this either;
the wallets were winning.

The way through was already half done and nobody had noticed. The newbie quest
chain's last step pays **four Imperial Seals**, fifteen wallets were holding
them, and grade 2 wants five. The table, read from the game's own bundle:

| Grade | Level | Seals |
|---|---|---|
| 2 | 15 | 5 |
| 3 | 30 | 25 |
| 4 | 45 | 125 |
| 5 | 60 | 625 |
| 6 | 75 | 3,125 |
| 7 | 90 | 15,625 |

The fifth seal is bought, not farmed. `purchaseImperialSeal` is paid in **gold** —
base 3,500, moved by how full the city warehouse is, and Greyholm quoted 2,883
when measured. It had been sitting in the denied list labelled "spends diamonds",
which was simply wrong, alongside `payCityEntryFee` labelled as having no
measured return — it buys the fifty-gold door that the shop and the altar stand
behind.

`evolveGrade` is the one that really is irreversible, and it is allowed on an
explicit decision rather than a correction. The loop earns the right to call it:
it moves only when the level gate for the next grade is already met, it checks
it can afford the shortfall **before** travelling rather than after arriving —
Greyholm has no farm and no battle zone, so a wallet stranded there has simply
stopped playing — and it proposes the ascent only with the seals already in hand.

### Gear it can never wear is not an asset

Every wallet in this fleet is grade 1, and a piece of equipment needs a grade at
least equal to its tier. So t2 gear drops, goes into the bag, and stays there —
unwearable, unsellable through the player market (that one wants premium
currency nobody has), and occupying a slot in a bag of forty. Sixteen of thirty
wallets were at 39 or 40 slots.

The Black Market buys it. `sellEquipmentItem` was measured before it was
allowlisted: **8,948 gold for one plate_greaves_t2**, tax zero, premium balance
untouched, no travel required. The fleet was holding 56 t2 pieces — roughly half
a million gold, and 56 slots, sitting still. For scale, a hunt task pays 1,700.

What it will not take is worth knowing, because both refusals are benign and
therefore silent:

| Refusal | Scope | What it means |
|---|---|---|
| `Shop stock is full for this item` | the **item type** | The shop holds a limited stock of each template. Common t1 gear is usually full. |
| `Cannot sell upgraded or slotted items` | the **piece** | Levelled, bonus-rolled or slotted gear is out; plain pieces only. |

The two are parked at different scopes on purpose. Parking one full-shop
template by its instance id would simply offer the next identical piece on the
next cycle, and the one after that on the cycle after — a refusal turned into a
loop, which is the failure this codebase keeps finding.

The full-shop scope is wider still: `shop_equipment_stock` is one document per
item type with no owner, so a full shop is true for every wallet at once. That
refusal is parked fleet-wide, which is the difference between one refused cycle
and thirty.

Gear at or below the grade is kept while there is room, since it may yet be worn,
and offered only once the bag is nearly full. Whatever the equip logic would put
on is never offered at all.

### Sometimes the drop is the point

Monster choice is a price comparison: XP at its gold rate, gold, and drops at
live market bids. That is the right question almost always, and exactly wrong
when something other than the market wants an item.

A clan quest asks for 2,000 of a single raw drop. Raw materials have no bids at
all, so the monster that supplies them is worth zero on the only scale the
chooser has, and it is never picked. The fleet's own quest showed what that
costs: 471 of 2,000 frogslime after twelve hours — four an hour, every one of
them a coincidence — against a seven-day expiry. It could not have finished, and
nothing anywhere reported a problem.

So a wallet in a clan with an outstanding quest fights for the item instead of
for the money. The monster is chosen from measured drops only, since drop tables
are server-side and a guess sends a wallet to grind something that drops nothing,
and among monsters that supply it at a similar rate the easiest one wins.

"Easiest" has a floor, which cost half an hour to learn. Combat is gated from
below as well as above: `startBattle` answers *"Monster level is outside your
reach"* for anything more than five levels under the character, and that refusal
classifies as benign. The first version of this errand sent seven level-15
wallets at a level-1 frog, and they reported no error at all while collecting
nothing — the same failure shape as everything else on this page. The frog
exists at levels 1, 7, 13, 19 and 26; a level-15 character fights the level-13
one.

The errand outranks the hunt task chain, which is not free: the chain pays 1,700
gold a task and a wallet on the errand earns none of it. It is still the right
trade. One quest pays 3,500 clan XP, a clan holds `5 × level + 5` members, and
3,417 XP carries a new clan from ten seats to thirty-five — which is the
difference between ten of the fleet's wallets in a clan and all thirty.

### Every free system gets found and used

Anything that returns real value at zero risk is taken before anything scored:

- **Hunt tasks** (level 10+) — a free gold-reward chain gated behind a level, not a
  cost. The engine accepts the next task, fights the specific monster it assigns
  with `startTaskBattle`, and claims the reward the moment it completes.
- **Newbie quest chain** — a tutorial line that pays escalating XP for a bare,
  argument-free call. There is no status endpoint to say when it ends *and no field
  for it on the player document either*, so progress is remembered locally in
  `data/newbie_quests.json`. The chain is item-gated: it rejects with
  `FAILED_PRECONDITION "Insufficient items: 0/1"` when the wallet cannot pay the
  step, which is a "not yet" rather than a "never", so a refusal parks that wallet's
  chain for six hours instead of abandoning it. The refusal is recorded even though
  it classifies as benign — see the note on benign rejections below.
- **Battles left open** — a battle activity carries no `endTime`, so a process that
  died mid-fight left the wallet holding one indefinitely. Any battle found open at
  startup is resumed through the normal turn loop and then settled; blind-settling
  it is refused by the server with HTTP 500 while the fight is unresolved.
- **Equipment upgrades** — a strictly better item in an already-full slot used to
  sit there unused, because equipping into an occupied slot needs the worn piece
  removed first. The engine now does both calls — unequip, then equip — as one
  decision, so gear stops going stale in inventory.

### The bag can jam shut, and only one thing opens it

Measured on 2026-08-22: twenty-four of thirty wallets sat at 40/40 slots, and no
route out existed. Monster drops carry **no market bid**, appear in **none of the
165 crafting recipe inputs**, are **none of the 28 refining raw materials**, the
gear shop answers `sellEquipmentItem` with "Shop stock is full" once thirty
wallets have dumped the same t1 plate into it, and `expandInventory` is priced in
diamonds no wallet holds (100 × 2^expansions of them, which the gold market
would sell for about 400,000 gold a wallet — paid to store more of what this
section is about destroying). A full bag then refuses `openChests`,
`claimInitialReward` and `upgradeEquip` — so the fighting that fills it stops
paying out, and the wallet loops on a refusal that classifies as benign.

The one sink monster drops have in the entire game is a clan quest asking for
2,000 of a single one of them, and a quest names one item while a wallet
accumulates thirty-one kinds.

`SLCW_DISCARD_JUNK` opens `deleteInventoryItem` for exactly that jam. It is off
by default, it is the last branch tried, and `slcw/discard.py` holds the proof an
item has to fail before it is even a candidate:

1. not equipment — gear sells back for thousands once shop stock rotates
2. not a container — an unopened chest is loot, not clutter
3. not a currency — imperial seals buy grades
4. consumed by nothing — no crafting recipe, no refining input, no refined good
5. no bid, **on a market snapshot known fresh** — a stale book cannot tell "worth
   nothing" apart from "price never loaded", and that is the one mistake this
   cannot take back

Then the active clan quest's items are removed, and what remains is destroyed
smallest stack first: the fewest items lost per slot recovered, and the large
stacks a quest could actually finish are the last to go.

Every deletion is written to `data/discards.jsonl` before it is announced, and
that file — not the chat — is the audit trail. Telegram gets a digest once an
hour instead: at roughly twenty stacks an hour, a message per stack is a message
every three minutes, and an alert channel that constant is one the operator stops
reading, including the circuit breakers it also carries.

Two systems were checked live and found to be dead ends, on purpose rather than by
accident: **mining quests** return a live HTTP 404 (no deployed function answers
the name, whatever the frontend bundle implies), and **citizenship** — the gate in
front of citizenship quests — was confirmed to cost diamonds, not gold, on a real
account with plenty of gold and zero diamonds. Both stay out of the decision loop
for exactly that reason, recorded in `guardrails.py`.

#### Live systems this engine does not use yet

Swept against the deployed functions on 2026-08-21, so this is what the server
answers to today rather than what a bundle implies:

- **Clans** are wired; see "Clan seats" below for the part that governs how much
  of a fleet can be inside one. `searchClans` and `getClanMembers` both return
  data, `createClan` and `leaveClan` exist, and `applyClan` answers "Clan not
  found" for a bad id — so that, not `joinClan`, is the join path.
- **Expeditions** appear in the frontend bundle (`startExpedition`,
  `finishExpedition`, `claimExpeditionRewards`) and every one of them 404s. The
  feature is built client-side but not deployed, so there is nothing to wire.
- **Referrals** work: `handleReferral` accepts a referral id.

The hunt task chain, by contrast, is fully wired and is currently the engine's
only gold stream — `claimTaskReward` pays 1,000 gold per completed task.

#### Clan seats

Read from Firestore on 2026-08-21 across every clan that exists in the game —
all eight of them, from Wolf at level 1 to LEGION at level 31:

```
maxMembers = 5 x clan level + 5
```

No exceptions. A clan is founded at level 1, so it starts with **ten seats**,
which is the constraint that matters for a fleet larger than ten wallets.

The level curve is `xpRequired = 1.5 * (level + 1) * (level^2 + 100)`, exact for
levels 12, 15, 22, 27 and 31 — the five the live game has an example of. Level 1
is the one exception: the formula gives 303 and both level-1 clans store 300, so
the measured value is used.

That makes the weekly clan quest the whole story. A quest asks for 2,000 of one
raw item and pays **3,500 clan XP**, while carrying a new clan from level 1 to
level 6 costs 3,417:

| clan level | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| seats | 10 | 15 | 20 | 25 | 30 | **35** |
| XP to leave it | 300 | 468 | 654 | 870 | 1,125 | 1,428 |

So one finished quest takes a brand-new clan past 35 seats. The items it asks
for are raw drops with no market bids at all, so the fleet gives up nothing it
could have sold. `generateClanQuest` is therefore taken automatically by the
leader whenever no quest is running, and `submitQuestResources` feeds it.

Whether anything else grants clan XP is not established: Asgard reached level 12
having completed a single quest, so there is a second source this fleet has not
measured. Donations are the obvious candidate and stay off by default anyway.

Until the level is bought, the seats have to be rationed. The runner ranks every
wallet by level and only the top `maxMembers` of them apply, so a thirty-wallet
fleet does not queue twenty-nine applications against nine free seats. The
ranking is recomputed each cycle, which is what makes this self-correcting: as
quests raise the level the roster widens on its own, a wallet added to the vault
later competes on the same terms, and the leader serves its queue highest level
first.

#### A benign rejection is not always a no-op

`ALREADY_EXISTS` and `FAILED_PRECONDITION` mean "the server already did this", so
they deliberately do not count toward the circuit breaker. That is right for an
idempotent initializer and wrong for an action that will keep being chosen. On
2026-08-21 the newbie quest chain was rejecting every wallet with
`FAILED_PRECONDITION "Insufficient items: 0/1"`; because that reads as benign it
*cleared* the error counter, so 25 wallets reported zero errors while none of them
had done anything productive for four days.

The rule this repo now follows: an action that can be re-picked must record its own
refusal somewhere, benign or not, and consult that record before being offered
again. Health is measured by what the ledger records, not by what the error counter
does not.

That rule is now enforced generally rather than per-action. `slcw/rejections.py`
parks the exact call — action *and* arguments — that the server refused, for an
hour, and `build_candidates` skips a parked call. Battles are deliberately not
parkable: an open fight has to be resolved before anything else can be chosen at
all, so parking `resumeBattle` would freeze the wallet harder than the bug it
guards against.

Three real instances of the pattern were measured on 2026-08-21, each of them a
wallet that had been "healthy with zero errors" for hours while producing nothing:

| Call | Refusal | Fix |
| --- | --- | --- |
| `equipItem` | `Your grade (1) is too low for this item (Grade 2)` | a piece needs `grade >= tier`, so gear the character cannot wear is never proposed |
| `openChests` | `Not enough space in inventory` | the batch is capped at the free slot count, and skipped entirely at zero |
| `processTurn` | `Battle is not active` | the fight was already won; the turn loop now breaks and *settles* instead of letting the benign error skip `finishActivity` |

The last one is the sharpest illustration of why benign-means-no-op is wrong. The
battle had been decided in the wallet's favour — 40 xp and two runeshards were
sitting behind `finishActivity` — but the exception left the turn loop before the
settle step, so the activity stayed open, the wallet read as busy on every later
cycle, and it did nothing at all for nine and a half hours.

**Hunting** is a third: a passive, turn-free alternative to battle, measured live
at 11 xp + 1× spiderfang for 3 gold and 3 energy against a tier-1 monster,
settling ~180 seconds later through the same activity-claim path as farming.
Energy-for-energy it loses to battle (11 xp / 3 energy vs 22 xp / 1 energy) and it
spends gold battle does not — so it is not "better," it is *available where battle
is not*. Battle only fires at `farm_3`/`wildland_1`; hunting has no such gate, so
it now fills the gap at gathering zones that otherwise have zero combat option.

It scales the same way battle does rather than staying pinned to the one monster
that was actually measured: both share the exact same `select_monster` call, so
hunting always targets whatever monster the engine would currently fight — the
pick that already accounts for level, real combat stats (weapon power, defense)
derived from current attributes and equipped gear, and each monster's own
survivability check. As gear and levels improve, so does the monster hunting
points at, automatically. Its *value* is estimated from that monster's own
learned battle average once one exists (CombatMemory — real XP and drop rates
from actual fights), falling back to the flat battle constant only for a monster
never yet fought, all scaled by the one measured hunting/battle ratio rather than
a separate guess per monster.

---

## Where the money goes, and where it does not

The obvious question about a game with a token in its name is whether any of
this reaches a wallet. Every route the app links to was fetched and read on
2026-08-22 to answer it, and the answer is worth writing down plainly.

**Out of the game.** `/withdraw` calls `createWithdrawalRequest` — no arguments
— which sends the account's entire `usdt_balance` to its linked Solana address.
The minimum is 10 USDT. Beyond that sits a milestone ladder on *cumulative*
withdrawals: $1,000 pays $100 USDT, $5,000 pays $300, $10,000 pays $500, and
past $30,000 the prizes stop being money and start being an iPhone, a MacBook
and eventually a Ferrari.

**Into the game.** `/shop/purchase/solana` buys diamonds with SOL. That is the
only inbound route.

**Between the two, nothing.** `usdt_balance` is credited by referrals and by
Genesis Era season prizes — the account's own play does not touch it. The gold
market (`placeGoldOrder`, `executeGoldMarketOrder`, 100,000 gold to a lot)
trades gold against *diamonds*, not USDT, and diamonds are what SOL buys. So
there is a way in, a way out, and no bridge from one to the other: farming gold
does not become USDT, however much of it there is. The `$SLCW` token itself is
still unlaunched — the client's own copy says to expect it in Q2 2026.

All of it stays denied. This bot plays the game; it does not move funds.

## Guardrails

`guardrails.check()` runs **inside the transport**, not in the orchestrator. Every
path to the network passes through it, so a denied call fails before a request is
even constructed — no caller has to remember.

All **89 callables** found in the game's frontend are classified — each is either
allowed or denied **with a recorded reason**, so none is left to chance.

**Permanently denied. Not configurable, not toggleable:**

| Denied | Why |
|---|---|
| `skipActivityTime` | costs 5 diamonds per remaining minute |
| `refillEnergyPaid` | costs 99 × 2ⁿ diamonds for what the free call also gives |
| farming `diamond` mode | licence costs 49 × 3^(tier−1) diamonds |
| `withdraw`, `transfer`, `signTransaction` | moves real funds |
| `placeGoldOrder`, `createOrder`, `cancelOrder` | the bot never trades on its own |
| `evolveGrade`, `sharpenItem`, `deleteInventoryItem` | consumes items irreversibly |
| `payCityEntryFee`, `buyLevel`, arena | costed, with no measured return |
| `handleReferral` | binds accounts together; an operator decision |

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
persistent 3–4 hour daily offline window. A failing wallet pauses **itself** and
leaves the rest of the fleet running.

### Wallet tools

The first wallet added becomes the **primary** — the one that funds the others and
receives them back. 👑 moves the flag whenever you like.

| Tool | Does |
|---|---|
| 📤 **Export** | every private key as a JSON file, auto-deleted from the chat after 60s |
| 💸 **Primary → semua** | fan out a fixed amount to every other wallet |
| ↩️ **Semua → primary** | sweep every spare lamport back to the primary |
| 🔁 **Antar wallet** | one wallet to one wallet |

Amounts are preset buttons or typed by hand — `/send 0.02`, `/send 0.02 wallet-03`,
`/send 0.02 wallet-03 wallet-07`, `/sweep`. A comma decimal is accepted, because
misreading `0,02` as `2` would move a hundred times too much.

Every transfer shows the exact amounts, recipients, fees and remaining balance
before anything is signed, and the balance is **re-read at the moment you confirm**
rather than trusted from the preview. Sweeps leave the rent-exempt minimum and the
signature fee behind so no account is stranded. A failed transfer in a batch is
reported individually and is safe to retry — it never partially sent.

> [!WARNING]
> These two tools are the only place the bot touches keys or funds, and they change
> its security posture. Everything else here cannot move money at all; with these
> enabled, a leaked bot token means every wallet can be exported and drained from
> the chat. `./slcwctl export` on the host avoids putting keys on the wire at all.

---

## Staying unremarkable

The goal is not to be undetectable — nothing is. It is to not stand out.

| | Approach |
|---|---|
| **TLS** | `curl_cffi` reproduces Chrome's JA3 handshake |
| **Headers** | full browser set; personas are sticky per wallet, so an account always looks like the same browser |
| **Sessions** | log in once, refresh thereafter — roughly **1 login/day** instead of 288 |
| **Timing** | wake times derive from the *server's* activity clock plus a log-normal reaction delay (90s median, long tail) |
| **Rhythm** | each wallet sleeps 3–4 hours daily, anchored to its own hour |
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
| `SLCW_MAX_ERRORS` | `3` | consecutive real errors before a wallet self-pauses |
| `SLCW_REST_HP_RATIO` | `0.55` | rest below this fraction of health |
| `SLCW_GOLD_RESERVE` | `500` | gold-funded actions never spend below this |
| `SLCW_AUTO_TRAVEL` | `true` | let the engine relocate along the production chain |
| `SLCW_XP_GOLD` | `5.0` | gold one xp point is worth. Lower favours gold, higher favours levelling |
| `SLCW_CARAVAN_MIN_LEVEL` | `20` | level a wallet must reach before it may trade. Below it, nothing changes |
| `SLCW_CITIES_TTL_SECONDS` | `300` | how long warehouse prices are reused before being re-read |

> [!NOTE]
> **Pacing is the biggest single lever on throughput, and the one with a cost that
> is not measured in gold.** The shipped defaults are deliberately slow: a wallet
> waits a log-normal median of 90 seconds after each action, which reads like
> someone playing rather than a script. Measured on a 30-wallet fleet, dropping
> that to 30 seconds took a wallet from 7.1 fights an hour to 23.4 and experience
> from 431 an hour to 1,420 — a level 30 that was eight days away arrived in two.
> Nothing about the decisions changed; the engine simply stopped waiting.
>
> That gain is bought with a more machine-like cadence, so the numbers are given
> here rather than the fast values being shipped. It is a per-install judgement,
> it lives in `.env` rather than in code, and it is reversible with a restart.
| `SLCW_DISCARD_JUNK` | `false` | destroy drops nothing in the game can use, once the bag is full |
| `SLCW_CLAN_ENABLED` | `true` | clan participation at all |
| `SLCW_CLAN_DONATE_GOLD` | `false` | donate gold to the treasury (1,000 gold = 1 DKP, once a day). Off because the treasury is the leader's to distribute |
| `SLCW_CLAN_GOLD_RESERVE` | `5000` | gold held back from any donation |
| `SLCW_CLAN_FOUNDER_WALLET` | *(unset)* | wallet that saves for the 20,000 gold a clan costs; it spends no gold until it can |
| `SLCW_CLAN_AUTO_FOUND` | `false` | let that wallet found the clan by itself, exactly once |
| `SLCW_CLAN_NAME` / `SLCW_CLAN_TAG` | *(unset)* | clan identity; tag is at most 4 characters and cannot be changed later |
| `SLCW_CLAN_AUTO_JOIN` | `true` | the highest-level wallets apply up to the clan's seat count, and the leader admits this vault's wallets only |
| `SLCW_FARMING_GOLD` | `true` | allow gold-mode gathering, which locks a wallet for hours |
| `SLCW_FARMING_GOLD_HOURS` | `8` | how long a gold-mode run lasts (1-8) |
| `SLCW_TRAVEL_MARGIN` | `1.35` | how much better a destination must be before moving |
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
│   ├── refining.py       workshops, recipes, catalyst shops
│   ├── world.py          map, distances, travel times
│   ├── tasks.py          hunt-task ladder
│   ├── inventory.py      slots, chests, equipment decisions
│   ├── crafting.py       154 equipment recipes across 3 workshops
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

**Only tier 1 is reachable at grade 1.** Higher refining tiers pay far more, but each
is gated by character grade, and raising grade consumes imperial seals — an
irreversible spend the engine will not make on its own.

**Expeditions, arena and mounts are unmapped.** The callables exist and are denied until
their reward and cost models are measured, rather than enabled on a guess.

**Trading stops when the next grade is paid for.** A caravan pays gold and
reputation and no xp at all, and a grade gate is a level gate first — grade 3 wants
level 30 before it wants its twenty-five seals. So a wallet below that level trades
only until it can afford the ascent, then goes back to fighting for the level; at its
grade's level ceiling, where xp is discarded anyway, it trades whatever it holds. The
seal price comes from the citadel's own shelf when the map has been read, and from the
top of the price curve when it has not.

**Caravans open at level 20.** Each city's warehouse turns out one refined good and
consumes two others, and `dispatchCaravan` buys a warehouse's output with gold, hauls it
at twenty seconds per unit of map distance, and sells it on arrival. The cargo never
touches the bag: a caravan pays in gold and reputation, not in items.

Two prices, and only one of them follows the client's curve. A workshop prices its shelf
— twice base when it is empty, half base when it is full, and a whole load is priced once
off the shelf as it stands. Virtan, the single trade hub, is a market maker instead: a
flat 20% spread either side of base regardless of stock, and no tax on what it buys. The
roads are the server's too — *"Caravans from cities must go to Hub"* — so the shape of
the trade is a spoke, out from the hub loaded and back with whatever the city makes.

Measured twice on 2026-08-23, neither run robbed: ten chronicle_page Ostrim → Virtan cost
27,890 and returned 33,600; ten battle_ember Virtan → Greyholm cost 54,000 and returned
59,599. That is 5k–11k profit for twenty energy, several times what the same energy earns
fighting. Every leg is priced against the live `cities` documents before the call, so a
route that has stopped paying is never offered.

One thing is still unmeasured: cities carry a `caravanRobberyDefenseBonus`, so caravans
can evidently be robbed, but the chance and the loss are resolved server-side and have
never been seen. A lost load costs the load the wallet already committed, which is why
this is allowed while a diamond spend is not.

---

## License

[MIT](LICENSE).

Automating a game may conflict with its terms of service. You are responsible for
how you use this.
