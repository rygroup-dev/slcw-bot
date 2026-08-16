"""Keyboard layouts and message rendering for the Telegram control plane."""
from __future__ import annotations

import datetime as _dt
import html
import json
import time


def keyboard(rows: list[list[tuple[str, str]]]) -> str:
    """Build an inline keyboard payload from (label, callback_data) pairs."""
    return json.dumps({"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row] for row in rows]})


def main_menu() -> str:
    return keyboard([
        [("📊 Status", "nav:status"), ("💰 Profit", "nav:profit")],
        [("🏪 Market", "nav:market"), ("⚗️ Ekonomi", "nav:economy")],
        [("⚔️ Combat", "nav:combat"), ("🎯 Task", "nav:tasks")],
        [("👛 Wallets", "wallet:list"), ("🗺 Peta", "nav:map")],
        [("⚙️ Kontrol", "nav:control"), ("🔐 Vault", "nav:vault")],
        [("🔄 Refresh", "nav:status")],
    ])


def economy_menu() -> str:
    return keyboard([
        [("🔗 Rantai profit", "nav:chain")],
        [("🌾 Gathering", "nav:farming"), ("⚗️ Refining", "nav:refining")],
        [("⚡ Energi", "nav:energy"), ("🗺 Peta", "nav:map")],
        back_row(),
    ])


def back_row(target: str = "nav:main") -> list[tuple[str, str]]:
    return [("⬅️ Menu", target)]


def control_menu(paused_count: int, total: int, dry_run: bool) -> str:
    dry_label = "🧪 Dry-run: ON" if dry_run else "🚀 Dry-run: OFF"
    return keyboard([
        [("▶️ Resume semua", "ctl:resume_all"), ("⏸ Pause semua", "ctl:pause_all")],
        [("🔄 Paksa siklus", "ctl:force"), (dry_label, "ctl:toggle_dry")],
        [("📜 Logs", "ctl:logs"), ("🩺 Doctor", "ctl:doctor")],
        back_row(),
    ])


IMPORT_HELP = (
    "<b>📥 Import wallet yang sudah ada</b>\n\n"
    "Kirim:\n<code>/import &lt;kunci-rahasia&gt;</code>\n\n"
    "Format yang diterima:\n"
    "• base58 (hasil export standar, 88 karakter)\n"
    "• array JSON <code>[12,34,…]</code> dari solana-keygen atau Phantom\n"
    "• hex, dengan atau tanpa <code>0x</code>\n"
    "• seed phrase 12/24 kata\n\n"
    "Pesanmu <b>langsung dihapus</b> dari chat setelah dibaca, dan kuncinya "
    "disimpan terenkripsi.\n\n"
    "⚠️ Seed phrase tidak menunjuk satu akun: Phantom pakai jalur turunan "
    "<code>m/44'/501'/0'/0'</code>, solana-keygen pakai seed mentah. Kalau kamu "
    "kirim frasa, saya tampilkan kedua alamatnya dan kamu pilih yang benar."
)


def wallet_list(wallets: list[dict], status: dict) -> str:
    rows = []
    for wallet in wallets:
        state = status.get(wallet["id"], {})
        mark = "⏸" if state.get("paused") else "▶️"
        label = f"{mark} {wallet['id']} · {wallet.get('nickname', '')}"
        rows.append([(label, f"wallet:show:{wallet['id']}")])
    rows.append([("➕ Buat wallet", "wallet:new")])
    rows.append(back_row())
    return keyboard(rows)


def wallet_detail(wallet_id: str, paused: bool) -> str:
    toggle = ("▶️ Resume", f"wallet:resume:{wallet_id}") if paused else \
             ("⏸ Pause", f"wallet:pause:{wallet_id}")
    return keyboard([
        [toggle, ("🔄 Siklus", f"wallet:force:{wallet_id}")],
        [("🧠 Kenapa?", f"wallet:why:{wallet_id}")],
        [("⬅️ Wallets", "wallet:list"), ("🏠 Menu", "nav:main")],
    ])


def new_wallet_menu() -> str:
    return keyboard([
        [("1", "wallet:create:1"), ("3", "wallet:create:3"), ("5", "wallet:create:5")],
        [("📥 Import wallet", "wallet:importhelp")],
        [("⬅️ Wallets", "wallet:list")],
    ])


def import_choice_menu(candidates: list) -> str:
    """One button per address a seed phrase could mean."""
    rows = [[(f"{c.source} · {c.public_key[:8]}…{c.public_key[-4:]}",
              f"wallet:pick:{index}")] for index, c in enumerate(candidates)]
    rows.append([("✖️ Batal", "wallet:cancelimport")])
    return keyboard(rows)


def vault_menu(unlocked: bool) -> str:
    rows = []
    if unlocked:
        rows.append([("🔒 Lock", "vault:lock")])
    else:
        rows.append([("🔓 Cara unlock", "vault:howto")])
    rows.append(back_row())
    return keyboard(rows)


# --- renderers -----------------------------------------------------------

def _bar(current: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return "─" * width
    filled = max(0, min(width, round(width * current / maximum)))
    return "█" * filled + "░" * (width - filled)


def _ago(timestamp: int) -> str:
    if not timestamp:
        return "belum pernah"
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return f"{delta}d lalu"
    if delta < 3600:
        return f"{delta // 60}m lalu"
    return f"{delta // 3600}j {(delta % 3600) // 60}m lalu"


def _in(timestamp: int) -> str:
    if not timestamp:
        return "—"
    delta = int(timestamp - time.time())
    if delta <= 0:
        return "segera"
    if delta < 60:
        return f"{delta}d"
    if delta < 3600:
        return f"{delta // 60}m {delta % 60}d"
    return f"{delta // 3600}j {(delta % 3600) // 60}m"


def render_status(fleet_state: dict) -> str:
    if not fleet_state.get("unlocked"):
        return ("🔐 <b>Vault terkunci</b>\n\n"
                "Engine idle sampai passphrase dimasukkan.\n"
                "Kirim: <code>/unlock passphrase-kamu</code>")

    wallets = fleet_state.get("wallets", {})
    if not wallets:
        return "Belum ada wallet. Buka 👛 Wallets → ➕ Buat wallet."

    mode = "🧪 DRY-RUN" if fleet_state.get("dry_run") else "🚀 LIVE"
    engine = "aktif" if fleet_state.get("enabled") else "claim-only"
    lines = [f"<b>SLCW Fleet</b> · {mode} · engine {engine}", ""]

    for wallet_id, status in sorted(wallets.items()):
        state = status.get("state") or {}
        mark = "⏸" if status.get("paused") else "▶️"
        lines.append(f"{mark} <b>{wallet_id}</b> · {html.escape(str(status.get('nickname', '')))}")

        if state:
            lines.append(
                f"   Lv{state.get('level', '?')} · {state.get('gold', 0):,}g · "
                f"💎{state.get('diamonds', 0)}")
            lines.append(
                f"   ❤️ {_bar(state.get('health', 0), state.get('max_health', 1))} "
                f"{state.get('health', 0)}/{state.get('max_health', 0)}")
            lines.append(
                f"   ⚡ {_bar(state.get('energy', 0), state.get('max_energy', 1))} "
                f"{state.get('energy', 0)}/{state.get('max_energy', 0)}")
            activity = state.get("activity", "idle")
            remaining = state.get("activity_remaining_s", 0)
            if activity and activity != "idle" and remaining:
                lines.append(f"   🎯 {activity} · sisa {int(remaining) // 60}m")
            else:
                lines.append(f"   📍 {state.get('location', '?')}")

        action = status.get("last_action") or "—"
        lines.append(f"   ⚙️ {action} · {_ago(status.get('last_run_ts', 0))}")
        lines.append(f"   ⏭ bangun {_in(status.get('next_wake_ts', 0))} "
                     f"({html.escape(str(status.get('next_wake_reason', '')))})")

        if status.get("last_error"):
            lines.append(f"   ⚠️ <code>{html.escape(str(status['last_error'])[:120])}</code>")
        if status.get("paused"):
            lines.append(f"   ⏸ {html.escape(str(status.get('pause_reason', '')))}")
        lines.append("")

    age = fleet_state.get("market_age_s")
    if age is not None:
        lines.append(f"<i>Market snapshot {int(age) // 60}m lalu</i>")
    return "\n".join(lines)


def render_profit(totals, item_value: float, per_wallet: dict) -> str:
    lines = ["<b>💰 Ledger terealisasi</b>", ""]
    lines.append(f"Gold: <b>{totals.gold:,}</b>")
    lines.append(f"XP: <b>{totals.xp:,}</b>")
    if totals.hours:
        lines.append(f"Rate: {totals.gold_per_hour:,.0f} gold/jam · "
                     f"{totals.xp_per_hour:,.0f} xp/jam")
        lines.append(f"Rentang: {totals.hours:.1f} jam · {totals.entries} entri")
    if totals.battles_won or totals.battles_lost:
        lines.append(f"Battle: {totals.battles_won}M / {totals.battles_lost}K "
                     f"({totals.win_rate:.0%} menang)")
    if totals.items:
        lines.append("")
        lines.append("<b>Item</b>")
        for name, quantity in sorted(totals.items.items(), key=lambda x: -x[1]):
            lines.append(f"  {html.escape(name)} ×{quantity}")
        lines.append(f"Nilai di best-bid: <b>{item_value:,.0f} gold</b>"
                     if item_value else "Nilai item: belum ada harga market")

    if per_wallet:
        lines.append("")
        lines.append("<b>Per wallet</b>")
        for wallet_id, wallet_totals in sorted(per_wallet.items()):
            lines.append(f"  {wallet_id}: {wallet_totals.gold:,}g · {wallet_totals.xp:,}xp")

    lines.append("")
    lines.append("<i>Nilai USD belum bisa dihitung sampai $SLCW punya likuiditas.</i>")
    return "\n".join(lines)


def render_market(snapshot, holdings: dict | None = None) -> str:
    if not snapshot or not snapshot.books:
        return "Belum ada snapshot market. Tunggu siklus berikutnya."

    lines = [f"<b>🏪 Black market</b> · {len(snapshot.books)} item · "
             f"snapshot {int(snapshot.age_seconds) // 60}m lalu", ""]

    crossed = snapshot.crossed()
    if crossed:
        lines.append("<b>⚡ Spread crossed (bid &gt; ask)</b>")
        for book in crossed[:8]:
            lines.append(f"  {html.escape(book.template_id)}: "
                         f"bid {book.best_bid:,.0f} / ask {book.best_ask:,.0f} "
                         f"→ {abs(book.spread):,.0f} margin")
        lines.append("<i>Order tetap butuh persetujuan manual.</i>")
        lines.append("")

    priced = [b for b in snapshot.books.values() if b.best_bid is not None]
    priced.sort(key=lambda b: -(b.best_bid or 0))
    lines.append("<b>Bid tertinggi</b>")
    for book in priced[:12]:
        ask = f"{book.best_ask:,.0f}" if book.best_ask is not None else "—"
        lines.append(f"  {html.escape(book.template_id)}: {book.best_bid:,.0f} / {ask}")

    if holdings:
        value = snapshot.value_of(holdings)
        lines.append("")
        lines.append(f"<b>Holding kamu di best-bid: {value:,.0f} gold</b>")
    return "\n".join(lines)


def render_wallet(wallet: dict, status: dict) -> str:
    state = status.get("state") or {}
    lines = [
        f"<b>{wallet['id']}</b> · {html.escape(str(wallet.get('nickname', '')))}",
        f"<code>{html.escape(str(wallet.get('public_key', '')))}</code>",
        "",
    ]
    if state:
        lines += [
            f"Level {state.get('level', '?')} · XP {state.get('xp', 0):,}",
            f"Gold {state.get('gold', 0):,} · 💎 {state.get('diamonds', 0)} · "
            f"USDT {state.get('usdt', 0)}",
            f"❤️ {state.get('health', 0)}/{state.get('max_health', 0)}  "
            f"💙 {state.get('mana', 0)}/{state.get('max_mana', 0)}  "
            f"⚡ {state.get('energy', 0)}/{state.get('max_energy', 0)}",
            f"📍 {state.get('location', '?')} · 🎯 {state.get('activity', 'idle')}",
            "",
        ]
    lines += [
        f"Aksi terakhir: <b>{status.get('last_action', '—')}</b>",
        f"Alasan: {html.escape(str(status.get('last_reason', '—')))}",
        f"Dijalankan: {_ago(status.get('last_run_ts', 0))}",
        f"Bangun lagi: {_in(status.get('next_wake_ts', 0))}",
        f"Sesi: {status.get('refreshes', 0)} refresh (bukan login ulang)",
        f"Proxy: {wallet.get('proxy') or 'tidak ada — keluar lewat IP VPS'}",
    ]
    if status.get("last_error"):
        lines.append(f"⚠️ <code>{html.escape(str(status['last_error'])[:300])}</code>")
    return "\n".join(lines)


def render_farming(market, level: int, grade: int, gold: int, energy: int,
                   config) -> str:
    """Show what each gathering site would actually pay at current market bids."""
    from slcw import farming

    lines = [f"<b>🌾 Gathering</b> · grade {grade} · level {level}",
             f"Gold {gold:,} · energi {energy}", ""]

    for location_id, entry in farming.FARM_LOCATIONS.items():
        eligible = farming.eligible_resources(location_id, level, grade)
        if not eligible:
            lines.append(f"<b>{location_id}</b> ({entry['profession']}) — belum memenuhi syarat")
            continue

        best = farming.best_resource(location_id, level, grade, market)
        bid = (market.best_bid(best.item_id) if market else None) or 0
        cycles = farming.max_energy_cycles(best.tier, energy, gold)
        energy_cost = farming.energy_mode_cost(best.tier, cycles) if cycles else None
        gold_cost = farming.gold_mode_cost(best.tier, config.farming_gold_hours)

        lines.append(f"<b>{location_id}</b> · {entry['profession']}")
        lines.append(f"  Terbaik: {html.escape(best.item_id)} (T{best.tier})")
        lines.append(f"  Bid pasar: {f'{bid:,.0f}g' if bid else 'tidak diperdagangkan'}")
        if energy_cost:
            net = bid * cycles - energy_cost["gold"]
            lines.append(f"  ⚡ energy: {cycles} unit / {cycles}m · "
                         f"{energy_cost['gold']:,}g + {cycles}en → "
                         f"{'+' if net >= 0 else ''}{net:,.0f}g")
        units = 60 * config.farming_gold_hours
        net_gold = bid * units - gold_cost["gold"]
        lines.append(f"  💰 gold: {units} unit / {config.farming_gold_hours}j · "
                     f"{gold_cost['gold']:,}g, 0 energi → "
                     f"{'+' if net_gold >= 0 else ''}{net_gold:,.0f}g")
        lines.append("")

    lines.append("<i>Resource tanpa bid dinilai nol — engine tidak menebak harga.</i>")
    return "\n".join(lines)


def render_chain(market, config) -> str:
    """The whole raw → refined economics in one view.

    Gathering alone loses money because raw materials carry no bids. This shows
    where the value actually appears, and what each link costs.
    """
    from slcw import farming, refining

    per_unit = (farming.gold_mode_cost(1, config.farming_gold_hours)["gold"]
                / (60 * config.farming_gold_hours))

    lines = ["<b>🔗 Rantai profit</b> · tier 1, mode gold (tanpa energi)", "",
             f"<i>Gathering: {per_unit:.2f} gold per unit mentah "
             f"({config.farming_gold_hours} jam, 0 energi)</i>", ""]

    for workshop in refining.WORKSHOPS.values():
        item = workshop.output_for_tier(1)
        raw = workshop.raw_for(item)
        farm = refining.PROFESSION_FARM[workshop.profession]
        raw_needed = refining.raw_per_cycle(1)
        raw_cost = per_unit * raw_needed
        refine_gold = refining.GOLD_PER_CYCLE[1]
        catalyst_gold = refining.catalyst_price(1)
        bid = (market.best_bid(item) if market else None) or 0
        catalyst = workshop.catalyst_for(1)
        total = raw_cost + refine_gold + catalyst_gold

        lines.append(f"<b>{workshop.id}</b> · {farm} → {workshop.city_id}")
        lines.append(f"  {raw_needed}× {html.escape(raw)} ({raw_cost:.0f}g)")
        lines.append(f"  1× {html.escape(catalyst)} ({catalyst_gold}g) "
                     f"+ {refine_gold}g refine")
        lines.append(f"  = modal <b>{total:.0f}g</b>")
        if bid:
            margin = bid - total
            arrow = "🟢" if margin > 0 else "🔴"
            multiple = bid / total if total else 0
            lines.append(f"  → 1× {html.escape(item)} @ <b>{bid:,.0f}g</b>")
            lines.append(f"  {arrow} <b>{margin:+,.0f}g</b> per unit · {multiple:.1f}×")
        else:
            lines.append(f"  → 1× {html.escape(item)} — <i>belum ada bid</i>")
        lines.append("")

    lines.append("<i>Semua angka terukur: biaya gathering dari rumus mode-gold, "
                 "harga katalis dari shop kota, biaya refine per tier, dan bid "
                 "dari order book langsung.</i>")
    return "\n".join(lines)


def render_refining(market, level: int, grade: int, gold: int, holdings: dict,
                    config) -> str:
    """Per-workshop feasibility given what the account actually holds."""
    from slcw import refining

    lines = [f"<b>⚗️ Refining</b> · grade {grade} · {gold:,} gold", ""]

    for workshop in refining.WORKSHOPS.values():
        lines.append(f"<b>{workshop.id}</b> · {workshop.city_id} · {workshop.profession}")
        recipe = refining.best_recipe(
            workshop, level, grade, holdings or {},
            max(0, gold - config.gold_reserve), market)

        if recipe is None:
            # Show the cheapest tier's shortfall so the operator knows what to get.
            item = workshop.output_for_tier(1)
            probe = refining.Recipe(workshop, item, 1, cycles=1)
            short = probe.missing(holdings or {}, gold)
            need = ", ".join(f"{q}× {html.escape(str(i))}" for i, q in short.items())
            lines.append(f"  ❌ kurang: {need or 'grade terlalu rendah'}")
        else:
            bid = (market.best_bid(recipe.item_id) if market else None) or 0
            value = bid * recipe.output_quantity
            lines.append(f"  ✅ {recipe.cycles}× {html.escape(recipe.item_id)} "
                         f"(T{recipe.tier}) · {recipe.duration_seconds // 60}m")
            lines.append(f"     biaya {recipe.gold_cost}g + "
                         + ", ".join(f"{q}× {html.escape(i)}"
                                     for i, q in recipe.inputs().items()))
            if value:
                lines.append(f"     hasil ≈ <b>{value:,.0f}g</b> "
                             f"(bersih {value - recipe.gold_cost:+,.0f}g)")
            else:
                lines.append("     hasil belum punya bid")
        lines.append("")
    return "\n".join(lines)


def render_energy(fleet_state: dict) -> str:
    """Free refill quota per wallet — three a day, and easy to leave unused."""
    wallets = fleet_state.get("wallets", {})
    if not wallets:
        return "Belum ada data wallet."

    lines = ["<b>⚡ Energi</b> · refill gratis 3× per hari per wallet", ""]
    for wallet_id, status in sorted(wallets.items()):
        state = status.get("state") or {}
        energy = state.get("energy", 0)
        maximum = state.get("max_energy", 100)
        left = state.get("free_refills_left")
        lines.append(f"<b>{wallet_id}</b> {status.get('nickname', '')}")
        lines.append(f"  {_bar(energy, maximum)} {energy}/{maximum}")
        if left is not None:
            marks = "🟢" * left + "⚪" * (3 - left)
            lines.append(f"  refill tersisa: {marks} ({left}/3)")
        lines.append("")
    lines.append("<i>Bot menunggu bar turun di bawah 35% sebelum memakai refill, "
                 "supaya satu jatah tidak terbuang untuk beberapa poin saja. "
                 "Refill berbayar (99×2ⁿ diamond) diblokir permanen.</i>")
    return "\n".join(lines)


def render_map(fleet_state: dict) -> str:
    """Where each wallet is, and how far the useful destinations are from there."""
    from slcw import refining, world

    wallets = fleet_state.get("wallets", {})
    lines = ["<b>🗺 Peta</b>", ""]

    for wallet_id, status in sorted(wallets.items()):
        here = (status.get("state") or {}).get("location", "")
        if not here:
            continue
        lines.append(f"<b>{wallet_id}</b> di {html.escape(world.name_of(here))} "
                     f"<code>{html.escape(here)}</code>")
        targets = []
        for destination in world.economic_locations():
            if destination == here:
                continue
            seconds = world.travel_seconds(here, destination)
            if seconds == float("inf"):
                continue
            targets.append((seconds, destination))
        for seconds, destination in sorted(targets)[:4]:
            purpose = _purpose_of(destination, refining)
            lines.append(f"   {int(seconds) // 60}m {int(seconds) % 60:02d}s → "
                         f"{html.escape(world.name_of(destination))} · {purpose}")
        lines.append("")

    if len(lines) <= 2:
        return "Belum ada posisi wallet. Tunggu siklus pertama."

    lines.append("<i>Waktu tempuh = 20 detik per satuan jarak, dikurangi bonus "
                 "tunggangan. Bot hanya pindah kalau nilai di tujuan mengalahkan "
                 "tinggal di tempat setelah dibagi waktu perjalanan.</i>")
    return "\n".join(lines)


def _purpose_of(location_id: str, refining) -> str:
    from slcw import farming

    workshop = refining.workshop_at(location_id)
    if workshop:
        return f"⚗️ {workshop.id}"
    if location_id in farming.FARM_LOCATIONS:
        return f"🌾 {farming.FARM_LOCATIONS[location_id]['profession']}"
    if location_id == "city_2":
        return "🏭 produksi"
    if location_id == "farm_3":
        return "⚔️ battle"
    return "—"


def render_tasks(status) -> str:
    """Hunt-task ladder state, or why it is not available yet."""
    from slcw import tasks

    if status is None:
        return ("<b>🎯 Hunt task</b>\n\n"
                f"Belum ada data. Task baru terbuka di level {tasks.MIN_LEVEL}, "
                f"jadi bot belum menanyakannya ke server.")

    if status.player_level < tasks.MIN_LEVEL:
        return (f"<b>🎯 Hunt task</b>\n\n"
                f"Terkunci sampai level {tasks.MIN_LEVEL} "
                f"(sekarang level {status.player_level}).")

    if status.all_done:
        return (f"<b>🎯 Hunt task</b>\n\n"
                f"Semua task selesai — {status.completed_count} total.")

    lines = [f"<b>🎯 Hunt task</b> · {status.completed_count} selesai", ""]
    task = status.task
    if task is None:
        lines.append("Tidak ada task aktif. Bot akan mengambil yang berikutnya.")
        return "\n".join(lines)

    lines.append(f"Target: {html.escape(task.monster_id)} (lv{task.monster_level})")
    lines.append(f"Progres: {_bar(task.kills_progress, task.kills_required)} "
                 f"{task.kills_progress}/{task.kills_required}")
    lines.append(f"Hadiah: <b>{task.gold_reward:,} gold</b> "
                 f"({task.gold_per_kill:,.0f}/kill)")
    lines.append(f"Status: {html.escape(task.status)}")
    if status.can_claim:
        lines.append("\n✅ Siap diklaim — bot mengambilnya di siklus berikutnya.")
    return "\n".join(lines)


def render_combat(memory) -> str:
    """Expose what the bot has learned about each monster."""
    if not memory.models:
        return ("<b>⚔️ Combat</b>\n\nBelum ada data. Model per-monster terisi "
                "setelah beberapa pertarungan.")

    lines = ["<b>⚔️ Model combat yang dipelajari</b>", ""]
    for monster_id, model in sorted(memory.models.items(),
                                    key=lambda kv: -kv[1].rounds):
        lines.append(f"<b>{html.escape(monster_id)}</b> · {model.rounds} ronde")
        blocks = " ".join(f"{z[:1].upper()}{model.block_rate(z):.0%}"
                          for z in ("head", "torso", "legs"))
        attacks = " ".join(f"{z[:1].upper()}{model.attack_rate(z):.0%}"
                           for z in ("head", "torso", "legs"))
        lines.append(f"  Blok lawan: {blocks} → serang <b>{model.best_attack_zone()}</b>")
        lines.append(f"  Serangan lawan: {attacks} → tangkis <b>{model.best_defense_zone()}</b>")
        lines.append("")
    lines.append("<i>Serang zona yang paling jarang diblok, tangkis zona yang "
                 "paling sering diserang. 18% langkah tetap acak untuk eksplorasi.</i>")
    return "\n".join(lines)


def render_why(status: dict) -> str:
    rationale = status.get("rationale") or []
    if not rationale:
        return "Belum ada keputusan tercatat untuk wallet ini."
    body = "\n".join(html.escape(line) for line in rationale)
    return (f"<b>🧠 Kenapa {status.get('wallet_id')} pilih aksi itu</b>\n\n"
            f"<pre>{body}</pre>\n"
            f"<i>Skor = gold-ekuivalen bersih per jam, setelah harga bayangan energi "
            f"dan biaya HP.</i>")
