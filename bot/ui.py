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


# Single source of truth for the home grid, so the paginated and plain variants
# can never drift apart.
MAIN_ROWS = (
    [("📊 Status", "nav:status"), ("💰 Profit", "nav:profit")],
    [("🏪 Market", "nav:market"), ("⚗️ Ekonomi", "nav:economy")],
    [("⚔️ Combat", "nav:combat"), ("🎯 Task", "nav:tasks")],
    [("🎒 Inventory", "nav:inventory"), ("🧬 Profil", "nav:profile")],
    [("👛 Wallets", "wallet:list"), ("🔨 Crafting", "nav:crafting")],
    [("🛡 Clan", "nav:clan")],
    [("⚙️ Kontrol", "nav:control"), ("🔐 Vault", "nav:vault")],
)


def main_menu() -> str:
    return keyboard([list(row) for row in MAIN_ROWS]
                    + [[("🔄 Refresh", "nav:status")]])


def status_menu(page: int = 1, pages: int = 1) -> str:
    """Home grid, with page arrows only when the fleet is large enough to need them."""
    rows = []
    if pages > 1:
        previous = pages if page <= 1 else page - 1
        following = 1 if page >= pages else page + 1
        rows.append([("◀️", f"nav:status:{previous}"),
                     (f"{page}/{pages}", f"nav:status:{page}"),
                     ("▶️", f"nav:status:{following}")])
    rows += [list(row) for row in MAIN_ROWS]
    rows.append([("🔄 Refresh", f"nav:status:{page}")])
    return keyboard(rows)


def economy_menu(gold_farming: bool = True, gold_hours: int = 8,
                 auto_travel: bool = True, discard_junk: bool = False) -> str:
    gold_label = (f"🟢 Gold-mode: ON ({gold_hours}j)" if gold_farming
                  else "🔴 Gold-mode: OFF")
    travel_label = "🟢 Auto-travel: ON" if auto_travel else "🔴 Auto-travel: OFF"
    # The only switch here that destroys something. Turning it OFF is one tap
    # because that direction is always safe; turning it ON goes through a
    # confirmation screen first.
    discard_label = ("🟢 Buang sampah: ON" if discard_junk
                     else "🔴 Buang sampah: OFF")
    return keyboard([
        [("🔗 Rantai profit", "nav:chain")],
        [("🌾 Gathering", "nav:farming"), ("⚗️ Refining", "nav:refining")],
        [("⚡ Energi", "nav:energy"), ("🗺 Peta", "nav:map")],
        [(gold_label, "ctl:toggle_goldfarm")],
        [("⏱ Durasi gold-mode", "ctl:goldhours")],
        [(travel_label, "ctl:toggle_travel")],
        [(discard_label, "ctl:toggle_discard")],
        back_row(),
    ])


def discard_confirm_menu() -> str:
    return keyboard([
        [("⚠️ Ya, nyalakan", "ctl:discardgo")],
        [("✖️ Batal", "nav:economy")],
    ])


def discard_help(destroyed: dict) -> str:
    """What turning this on means, and what it has already cost."""
    lines = [
        "<b>🗑 Buang sampah otomatis</b>\n",
        "Satu-satunya aksi bot yang <b>menghancurkan item, tanpa bisa "
        "dibatalkan</b>.\n",
        "Dipakai waktu tas mentok 40/40 dan tidak ada jalan keluar lain: drop "
        "monster tidak punya bid di market, tidak dipakai resep crafting mana "
        "pun, bukan bahan refining, stok toko gear penuh, dan perluasan tas "
        "dibayar diamond.\n",
        "<b>Tidak akan pernah dihapus:</b> equipment, peti, imperial seal, "
        "apa pun yang dipakai resep atau refinery, apa pun yang ada bid-nya, "
        "dan item yang diminta quest clan aktif.\n",
        "Kalau harga pasar sedang basi, <b>tidak ada yang dihapus sama "
        "sekali</b> — harga hilang tidak sama dengan tidak berharga.\n",
        "Yang dibuang: stack <b>terkecil dulu</b>, jadi paling sedikit item "
        "hilang per slot yang dibebaskan.\n",
    ]
    if destroyed:
        total = sum(destroyed.values())
        top = sorted(destroyed.items(), key=lambda item: -item[1])[:5]
        rows = ", ".join(f"{item} ×{count}" for item, count in top)
        lines.append(f"<b>Sudah dibuang sejauh ini:</b> {total} item — {rows}")
    else:
        lines.append("<i>Belum ada item yang dibuang.</i>")
    return "\n".join(lines)


def gold_hours_menu(current: int) -> str:
    """How long a gold-mode run locks the wallet."""
    rows = []
    for start in (1, 4):
        rows.append([((f"✅ {h}j" if h == current else f"{h}j"),
                      f"ctl:setgoldhours:{h}") for h in range(start, start + 3)])
    rows.append([(("✅ 7j" if current == 7 else "7j"), "ctl:setgoldhours:7"),
                 (("✅ 8j" if current == 8 else "8j"), "ctl:setgoldhours:8")])
    rows.append([("⬅️ Ekonomi", "nav:economy")])
    return keyboard(rows)


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
    rows.append([("➕ Buat wallet", "wallet:new"), ("🛠 Tools", "wallet:tools")])
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


def wallet_tools_menu() -> str:
    """Actions that touch key material or funds, kept off the main list."""
    return keyboard([
        [("📤 Export private key", "wallet:exportask")],
        [("💸 Primary → semua", "wallet:sendask")],
        [("↩️ Semua → primary", "wallet:sweepask")],
        [("🔁 Antar wallet", "wallet:p2pask")],
        [("👑 Ganti primary", "wallet:primaryask")],
        [("⬅️ Wallets", "wallet:list")],
    ])


def primary_picker_menu(wallets: list[dict]) -> str:
    rows = [[(("👑 " if w.get("is_primary") else "") + f"{w['id']} · {w['nickname']}",
              f"wallet:primaryset:{w['id']}")] for w in wallets[:10]]
    rows.append([("⬅️ Tools", "wallet:tools")])
    return keyboard(rows)


def sweep_confirm_menu(destination_id: str) -> str:
    return keyboard([
        [("⚠️ Ya, tarik semua", f"wallet:sweepgo:{destination_id}")],
        [("✖️ Batal", "wallet:tools")],
    ])


def p2p_source_menu(wallets: list[dict]) -> str:
    rows = [[(f"{w['id']} · {w['balance_sol']:.4f} SOL", f"wallet:p2psrc:{w['id']}")]
            for w in wallets[:8]]
    rows.append([("⬅️ Tools", "wallet:tools")])
    return keyboard(rows)


def p2p_dest_menu(source_id: str, wallets: list[dict]) -> str:
    rows = [[(("👑 " if w.get("is_primary") else "") + f"{w['id']} · {w['nickname']}",
              f"wallet:p2pdst:{source_id}:{w['id']}")]
            for w in wallets[:10] if w["id"] != source_id]
    rows.append([("⬅️ Ganti sumber", "wallet:p2pask")])
    return keyboard(rows)


def amount_menu(callback_prefix: str, amounts=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5)) -> str:
    """Preset amounts plus a manual-entry escape hatch."""
    rows = [[(f"{a:g}", f"{callback_prefix}:{a:g}") for a in amounts[i:i + 3]]
            for i in range(0, len(amounts), 3)]
    rows.append([("✏️ Ketik manual", "wallet:manualhelp")])
    rows.append([("⬅️ Tools", "wallet:tools")])
    return keyboard(rows)


def export_confirm_menu() -> str:
    return keyboard([
        [("⚠️ Ya, kirim file kunci", "wallet:exportgo")],
        [("✖️ Batal", "wallet:list")],
    ])


def send_amount_menu() -> str:
    return amount_menu("wallet:sendamt")


def send_source_menu(wallets: list[dict], amount: float) -> str:
    """Pick which wallet pays. Only funded wallets are offered."""
    rows = [[(f"{w['id']} · {w['balance_sol']:.4f} SOL",
              f"wallet:sendsrc:{amount:g}:{w['id']}")]
            for w in wallets[:8]]
    rows.append([("⬅️ Ganti jumlah", "wallet:sendask"), ("✖️ Batal", "wallet:list")])
    return keyboard(rows)


def send_confirm_menu(amount: float, source_id: str) -> str:
    return keyboard([
        [("⚠️ Ya, kirim sekarang", f"wallet:sendgo:{amount:g}:{source_id}")],
        [("✖️ Batal", "wallet:list")],
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

# --- shared copy ---------------------------------------------------------

WAITING_FOR_CYCLE = ("Belum ada state wallet.\n\n"
                     "<i>Tunggu siklus pertama selesai — biasanya di bawah satu "
                     "menit setelah vault dibuka.</i>")

ECONOMY_INTRO = (
    "<b>⚗️ Ekonomi</b>\n\n"
    "Bahan mentah tidak punya bid sama sekali; yang laku hanya barang olahan. "
    "Rantai profit menunjukkan di mana nilainya muncul dan berapa ongkos tiap "
    "mata rantainya.\n\n"
    "<i>Semua angka diambil dari order book langsung dan rumus biaya milik game, "
    "bukan perkiraan.</i>")

NEW_WALLET_INTRO = (
    "<b>➕ Tambah wallet</b>\n\n"
    "<b>Buat baru</b> — keypair Solana dibuat lokal, langsung dienkripsi, lalu "
    "onboarding in-game jalan otomatis di siklus pertama. Tidak butuh SOL sama "
    "sekali.\n\n"
    "<b>Import</b> — pakai akun yang sudah ada.\n\n"
    "Bot <b>tidak pernah</b> memindahkan dana.\n\n"
    "Berapa wallet baru?")


def _dot(ok: bool, warn: bool = False) -> str:
    return "🟡" if warn else ("🟢" if ok else "🔴")


def _kv(label: str, value: str, width: int = 14) -> str:
    """Aligned label/value line — keeps numeric columns readable on mobile."""
    return f"<code>{label:<{width}}</code>{value}"


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


WALLETS_PER_PAGE = 4


def page_count(total: int, per_page: int = WALLETS_PER_PAGE) -> int:
    return max(1, (total + per_page - 1) // per_page)


def render_status(fleet_state: dict, page: int = 1) -> str:
    """Fleet dashboard, paginated so a large fleet still fits one message."""
    if not fleet_state.get("unlocked"):
        return ("<b>🔐 Vault terkunci</b>\n\n"
                "Engine idle sampai passphrase dimasukkan.\n"
                "Kirim <code>/unlock passphrase-kamu</code>.\n\n"
                "<i>Pesannya langsung dihapus dari chat setelah dibaca.</i>")

    wallets = fleet_state.get("wallets", {})
    if not wallets:
        return ("<b>SLCW Fleet</b>\n\nBelum ada wallet.\n\n"
                "<i>Buka 👛 Wallets → ➕ untuk membuat atau mengimpor.</i>")

    ordered = sorted(wallets.items())
    total = len(ordered)
    pages = page_count(total)
    page = max(1, min(page, pages))
    window = ordered[(page - 1) * WALLETS_PER_PAGE: page * WALLETS_PER_PAGE]

    # Fleet-level summary first, so the headline reads at a glance.
    active = sum(1 for _, s in ordered if not s.get("paused"))
    errored = sum(1 for _, s in ordered if s.get("last_error"))
    gold = sum(int((s.get("state") or {}).get("gold", 0) or 0) for _, s in ordered)

    mode = "🧪 DRY-RUN" if fleet_state.get("dry_run") else "🚀 LIVE"
    engine = "aktif" if fleet_state.get("enabled") else "claim-only"

    lines = [
        f"<b>⚔️ SLCW Fleet</b> · {mode}",
        f"<code>{active}/{total} aktif</code> · <code>{gold:,}g total</code>"
        + (f" · <code>{errored} error</code>" if errored else "")
        + f" · engine {engine}",
        "",
    ]

    for wallet_id, status in window:
        state = status.get("state") or {}
        mark = "⏸" if status.get("paused") else ("⚠️" if status.get("last_error") else "▶️")
        lines.append(f"{mark} <b>{wallet_id}</b> · "
                     f"{html.escape(str(status.get('nickname', '')))}")

        if state:
            lines.append(f"   Lv{state.get('level', '?')} · "
                         f"{state.get('gold', 0):,}g · 💎{state.get('diamonds', 0)}"
                         + (f" · 📦{state['chests']}" if state.get("chests") else ""))
            lines.append(f"   ❤️ {_bar(state.get('health', 0), state.get('max_health', 1))} "
                         f"{state.get('health', 0)}/{state.get('max_health', 0)}")
            lines.append(f"   ⚡ {_bar(state.get('energy', 0), state.get('max_energy', 1))} "
                         f"{state.get('energy', 0)}/{state.get('max_energy', 0)}")

            activity = state.get("activity", "idle")
            remaining = state.get("activity_remaining_s", 0)
            place = html.escape(str(state.get("location", "?")))
            if activity and activity != "idle" and remaining:
                lines.append(f"   🎯 {html.escape(str(activity))} · "
                             f"sisa {int(remaining) // 60}m · 📍{place}")
            else:
                lines.append(f"   📍 {place}")

        lines.append(f"   ⚙️ {html.escape(str(status.get('last_action') or '—'))} · "
                     f"{_ago(status.get('last_run_ts', 0))} · "
                     f"⏭ {_in(status.get('next_wake_ts', 0))}")

        if status.get("last_error"):
            lines.append(f"   ⚠️ <code>"
                         f"{html.escape(str(status['last_error'])[:110])}</code>")
        elif status.get("paused"):
            lines.append(f"   ⏸ {html.escape(str(status.get('pause_reason', '')))}")
        lines.append("")

    footer = []
    if pages > 1:
        footer.append(f"Halaman {page}/{pages}")
    age = fleet_state.get("market_age_s")
    if age is not None:
        footer.append(f"market {int(age) // 60}m lalu")
    if footer:
        lines.append(f"<i>{' · '.join(footer)}</i>")
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


EQUIP_SLOTS = ("head", "chest", "gauntlets", "greaves", "boots",
               "two_hand_weapon", "right_weapon", "left_weapon")


def render_inventory(fleet_state: dict) -> str:
    """Slots, chests waiting to be opened, and which gear slots are still bare."""
    wallets = fleet_state.get("wallets", {})
    if not wallets:
        return "Belum ada data inventory."

    lines = ["<b>🎒 Inventory & equipment</b>", ""]
    for wallet_id, status in sorted(wallets.items()):
        state = status.get("state") or {}
        holdings = status.get("holdings") or {}
        equipment = status.get("equipment") or {}

        used, maximum = state.get("slots_used"), state.get("slots_max")
        lines.append(f"<b>{wallet_id}</b> {html.escape(str(status.get('nickname', '')))}")
        if maximum:
            lines.append(f"  Slot {used}/{maximum}"
                         + ("  ⚠️ hampir penuh" if maximum - used <= 2 else ""))

        chests = state.get("chests") or 0
        if chests:
            lines.append(f"  📦 {chests} peti belum dibuka — bot membukanya otomatis")

        worn = [s for s in EQUIP_SLOTS if isinstance(equipment.get(s), dict)
                and equipment.get(s)]
        empty = [s for s in EQUIP_SLOTS if s not in worn]
        if worn:
            for slot in worn:
                template = (equipment[slot] or {}).get("templateId", "?")
                lines.append(f"  ✅ {slot}: {html.escape(str(template))}")
        if empty:
            lines.append(f"  ⬜ kosong: {', '.join(empty)}")

        if holdings:
            top = sorted(holdings.items(), key=lambda kv: -kv[1])[:8]
            lines.append("  " + " · ".join(
                f"{html.escape(k)}×{v}" for k, v in top))
        lines.append("")

    lines.append("<i>Peti dibuka otomatis, dan gear dipasang otomatis ke slot yang "
                 "kosong. Penggantian gear yang sudah terpakai butuh lepas dulu, "
                 "jadi hanya dilakukan kalau tier-nya jelas lebih tinggi.</i>")
    return "\n".join(lines)


def render_crafting(fleet_state: dict, holdings: dict, gold: int, grade: int,
                    professions: dict, location: str) -> str:
    """What could be crafted here, and what each blocked recipe is short of."""
    from slcw import crafting

    shops = crafting.workshops_at(location)
    lines = [f"<b>🔨 Crafting</b> · {html.escape(world_name(location))}", ""]

    if not shops:
        cities = sorted({w.city_id for w in crafting.WORKSHOPS.values()})
        lines.append("Tidak ada bengkel crafting di lokasi ini.")
        lines.append("")
        for workshop in crafting.WORKSHOPS.values():
            lines.append(f"  {workshop.id} · {workshop.city_id} · "
                         f"{workshop.profession} ({len(workshop.items)} resep)")
        lines.append("")
        lines.append("<i>Equipment hasil crafting tidak diperdagangkan di market, "
                     "jadi engine tidak bisa mengukur nilainya dan tidak pernah "
                     "memilihnya sendiri. Ini keputusan kamu.</i>")
        return "\n".join(lines)

    ready = crafting.craftable(location, holdings, gold, grade, professions)
    if ready:
        lines.append(f"<b>✅ Bisa dibuat sekarang ({len(ready)})</b>")
        for plan in ready[:10]:
            inputs = ", ".join(f"{q}× {html.escape(i)}"
                               for i, q in plan.ingredients().items())
            lines.append(f"  {html.escape(plan.recipe_id)} ×{plan.quantity}")
            lines.append(f"     {inputs} + {plan.gold_cost:,}g · "
                         f"{plan.duration_seconds // 60}m")
        lines.append("")

    for workshop in shops:
        blocked = []
        for recipe_id in workshop.items:
            if any(p.recipe_id == recipe_id for p in ready):
                continue
            plan = crafting.CraftPlan(workshop, recipe_id, 1)
            reasons = plan.blockers(holdings, gold, grade, professions)
            if reasons:
                blocked.append((recipe_id, reasons))
        if blocked:
            lines.append(f"<b>{workshop.id}</b> · {workshop.profession}")
            for recipe_id, reasons in blocked[:5]:
                lines.append(f"  ❌ {html.escape(recipe_id)}: "
                             f"{html.escape('; '.join(reasons[:2]))}")
            lines.append("")

    lines.append("<i>Equipment tidak punya bid di market, jadi engine tidak "
                 "mengukurnya dan tidak crafting sendiri.</i>")
    return "\n".join(lines)


def world_name(location_id: str) -> str:
    from slcw import world
    return world.name_of(location_id)


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


def render_control(total: int, paused: int, dry_run: bool, enabled: bool,
                   unlocked: bool, latency_ms: float, queue_depth: int) -> str:
    active = total - paused
    lines = [
        "<b>⚙️ Kontrol</b>", "",
        _kv("Wallet", f"{total} · {_dot(active > 0)} {active} aktif · ⏸ {paused} pause"),
        _kv("Mode", "🧪 dry-run" if dry_run else "🚀 live"),
        _kv("Engine", f"{_dot(enabled)} {'aktif' if enabled else 'claim-only'}"),
        _kv("Vault", f"{_dot(unlocked)} {'terbuka' if unlocked else 'terkunci'}"),
        "",
        "<b>Telegram</b>",
        _kv("Latensi", f"{_dot(latency_ms < 400, warn=latency_ms >= 400)} "
                       f"{latency_ms:.0f} ms rata-rata"),
        _kv("Antrian", f"{_dot(queue_depth < 3, warn=queue_depth >= 3)} "
                       f"{queue_depth} update"),
    ]
    return "\n".join(lines)


def render_vault(unlocked: bool, wallet_count: int) -> str:
    if unlocked:
        return ("<b>🔐 Vault terbuka</b>\n\n"
                f"{wallet_count} wallet terdekripsi di memori.\n\n"
                "<i>Kunci privat tidak pernah ditulis ke disk dalam bentuk polos, "
                "dan tidak pernah dikirim lewat Telegram.</i>")
    return ("<b>🔐 Vault terkunci</b>\n\n"
            "Engine idle sampai dibuka.\n"
            "Kirim <code>/unlock passphrase-kamu</code>.\n\n"
            "<i>Pesannya langsung dihapus dari chat setelah dibaca.</i>")


def render_doctor(python: str, problems: list, unlocked: bool, wallets: int,
                  workers_alive: int, workers_total: int, market_age: float,
                  proxied: int, latency_ms: float, api_calls: int,
                  queue_depth: int) -> str:
    workers_ok = workers_total > 0 and workers_alive == workers_total
    age_text = ("belum ada" if market_age == float("inf")
                else f"{int(market_age) // 60}m lalu")

    lines = [
        "<b>🩺 Doctor</b>", "",
        "<b>Runtime</b>",
        _kv("Python", f"<code>{html.escape(python)}</code>"),
        _kv("Vault", f"{_dot(unlocked)} {'terbuka' if unlocked else 'TERKUNCI'}"),
        _kv("Wallet", str(wallets)),
        _kv("Worker", f"{_dot(workers_ok)} {workers_alive}/{workers_total} hidup"),
        "",
        "<b>Data</b>",
        _kv("Market", f"{_dot(market_age < 3600, warn=market_age >= 3600)} {age_text}"),
        _kv("Proxy", f"{_dot(proxied > 0, warn=proxied == 0)} {proxied} wallet"),
        "",
        "<b>Telegram</b>",
        _kv("Latensi", f"{_dot(latency_ms < 400, warn=latency_ms >= 400)} "
                       f"{latency_ms:.0f} ms"),
        _kv("Call", str(api_calls)),
        _kv("Antrian", str(queue_depth)),
        "",
    ]
    if problems:
        lines.append("<b>⚠️ Masalah config</b>")
        lines += [f"  • {html.escape(p)}" for p in problems]
    else:
        lines.append("✅ Config lengkap")

    if proxied == 0:
        lines.append("")
        lines.append("<i>Tanpa proxy, semua wallet keluar lewat IP VPS yang sama "
                     "dan terlihat sebagai satu operator.</i>")
    return "\n".join(lines)


def render_wallet_list(wallets: list[dict], status: dict) -> str:
    if not wallets:
        return ("<b>👛 Wallets</b>\n\nBelum ada wallet.\n\n"
                "<i>Tekan ➕ untuk membuat atau mengimpor.</i>")

    lines = [f"<b>👛 Wallets</b> · {len(wallets)} akun", ""]
    for wallet in wallets:
        state = (status.get(wallet["id"]) or {})
        vitals = state.get("state") or {}
        mark = "⏸" if state.get("paused") else "▶️"
        lines.append(f"{mark} <b>{wallet['id']}</b> · "
                     f"{html.escape(str(wallet.get('nickname', '')))}")
        if vitals:
            lines.append(f"   Lv{vitals.get('level', '?')} · "
                         f"{vitals.get('gold', 0):,}g · "
                         f"⚡{vitals.get('energy', '?')} · "
                         f"📍{html.escape(str(vitals.get('location', '?')))}")
        lines.append(f"   <code>{html.escape(wallet['public_key'][:24])}…</code> · "
                     f"proxy {wallet.get('proxy', 'none')}")
    return "\n".join(lines)


def render_created(created: list[dict]) -> str:
    listing = "\n".join(
        f"<code>{html.escape(w['public_key'])}</code>\n"
        f"  {w['id']} · {html.escape(str(w['nickname']))}" for w in created)
    return (f"<b>✅ {len(created)} wallet dibuat</b>\n\n{listing}\n\n"
            f"<i>Kunci privat ada di vault terenkripsi dan tidak akan pernah "
            f"ditampilkan di sini. Onboarding in-game jalan otomatis.</i>")


WALLET_TOOLS_INTRO = (
    "<b>🛠 Wallet tools</b>\n\n"
    "Dua aksi di sini menyentuh kunci dan dana sungguhan. Semua yang lain di bot "
    "ini tidak bisa memindahkan uang sama sekali.\n\n"
    "<b>📤 Export</b> — cadangan semua private key sebagai file JSON.\n"
    "<b>💸 Kirim SOL</b> — bagikan SOL dari satu wallet ke semua wallet lain.\n\n"
    "<i>Keduanya cukup dikonfirmasi dengan tombol. Siapa pun yang memegang token "
    "bot ini bisa menjalankannya.</i>")


def render_export_warning(count: int) -> str:
    return (f"<b>📤 Export {count} private key</b>\n\n"
            f"File JSON berisi kunci <b>polos</b> akan dikirim ke chat ini, lalu "
            f"dihapus otomatis setelah 60 detik.\n\n"
            f"Sebelum lanjut, pahami ini:\n"
            f"  • Siapa pun yang punya file itu <b>menguasai penuh</b> semua wallet\n"
            f"  • Telegram menyimpannya di server mereka sampai terhapus\n"
            f"  • Simpan salinannya terenkripsi, jangan di galeri atau cloud biasa\n\n"
            f"<i>Alternatif paling aman: <code>./slcwctl export</code> di VPS — "
            f"kunci tidak pernah melewati jaringan.</i>")


def render_export_sent(count: int, seconds: int) -> str:
    return (f"📤 <b>{count} wallet</b> diekspor.\n\n"
            f"File dihapus dari chat dalam {seconds} detik — simpan sekarang.\n\n"
            f"<i>Setiap kunci sudah diverifikasi menurunkan public key yang benar, "
            f"jadi cadangan ini terbukti bisa dipulihkan.</i>")


def render_send_ask(funded: list[dict]) -> str:
    if not funded:
        return ("<b>💸 Kirim SOL</b>\n\n"
                "Tidak ada wallet yang punya saldo.\n\n"
                "<i>Danai dulu salah satu wallet dari luar, baru bisa dibagikan "
                "ke yang lain.</i>")
    lines = ["<b>💸 Kirim SOL ke semua wallet</b>", "",
             "Pilih jumlah <b>per wallet penerima</b>:", "",
             "<b>Wallet yang punya saldo</b>"]
    for w in funded[:8]:
        lines.append(f"  {w['id']} · <b>{w['balance_sol']:.6f} SOL</b>")
    return "\n".join(lines)


def render_send_plan(plan, source_nickname: str = "") -> str:
    from slcw.solana import lamports_to_sol

    per = lamports_to_sol(plan.per_recipient_lamports)
    total = lamports_to_sol(plan.total_lamports)
    fees = lamports_to_sol(plan.fee_lamports)
    balance = lamports_to_sol(plan.source_balance)

    lines = [
        "<b>💸 Konfirmasi pengiriman</b>", "",
        _kv("Dari", f"{plan.source_id} {html.escape(source_nickname)}"),
        _kv("Saldo", f"{balance:.6f} SOL"),
        "",
        _kv("Per wallet", f"<b>{per:.6f} SOL</b>"),
        _kv("Penerima", f"{plan.count} wallet"),
        _kv("Total", f"<b>{total:.6f} SOL</b>"),
        _kv("Biaya", f"~{fees:.6f} SOL"),
        "",
    ]
    if plan.affordable:
        left = balance - lamports_to_sol(plan.total_lamports + plan.fee_lamports)
        lines.append(f"Sisa setelah kirim: <b>{left:.6f} SOL</b>")
        lines.append("")
        lines.append("<i>Transaksi on-chain tidak bisa dibatalkan. Periksa "
                     "angkanya sekali lagi sebelum menekan.</i>")
    else:
        lines.append(f"❌ <b>Saldo kurang "
                     f"{lamports_to_sol(plan.shortfall_lamports):.6f} SOL</b>")
        lines.append("")
        lines.append("<i>Jumlah itu sudah termasuk biaya per transaksi dan "
                     "cadangan rent-exempt yang tidak boleh diambil.</i>")
    return "\n".join(lines)


def render_send_results(results: list) -> str:
    from slcw.solana import lamports_to_sol

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    lines = [f"<b>💸 Hasil: {len(ok)} berhasil · {len(failed)} gagal</b>", ""]
    for result in ok[:10]:
        lines.append(f"  ✅ {result.wallet_id} · "
                     f"{lamports_to_sol(result.lamports):.6f} SOL")
        lines.append(f"     <code>{html.escape(result.signature[:32])}…</code>")
    if len(ok) > 10:
        lines.append(f"  <i>… dan {len(ok) - 10} lagi</i>")

    if failed:
        lines.append("")
        lines.append("<b>Gagal</b>")
        for result in failed[:6]:
            lines.append(f"  ❌ {result.wallet_id}: "
                         f"<code>{html.escape(str(result.error)[:70])}</code>")
        lines.append("")
        lines.append("<i>Yang gagal tidak terkirim sama sekali — aman diulang "
                     "tanpa risiko dobel.</i>")
    return "\n".join(lines)


GOLD_HOURS_HELP = (
    "<b>⏱ Durasi gold-mode</b>\n\n"
    "Gathering mode-gold tidak memakai energi sama sekali, tapi <b>mengunci "
    "wallet selama durasi itu</b> — tidak bisa naik level, buka peti, atau "
    "klaim task sampai selesai.\n\n"
    "Biaya per jam: <code>round(j/8×1500) + 60j×3^(tier−1)</code> gold.\n\n"
    "<i>Durasi pendek = lebih lincah tapi lebih sering diputuskan. Durasi "
    "panjang = hasil terbesar per perjalanan, tapi wallet hilang dari "
    "peredaran lebih lama.</i>")


MANUAL_AMOUNT_HELP = (
    "<b>✏️ Jumlah manual</b>\n\n"
    "Ketik salah satu di chat:\n\n"
    "<code>/send 0.02</code>\n"
    "  primary → semua wallet, 0.02 SOL masing-masing\n\n"
    "<code>/send 0.02 wallet-03</code>\n"
    "  wallet-03 → semua wallet lain\n\n"
    "<code>/send 0.02 wallet-03 wallet-07</code>\n"
    "  wallet-03 → wallet-07 saja\n\n"
    "<code>/sweep</code>\n"
    "  semua wallet kirim balik ke primary\n\n"
    "<i>Koma juga diterima (0,02). Setiap perintah tetap menampilkan konfirmasi "
    "dengan angka persis sebelum apa pun dikirim.</i>")


def render_primary(wallets: list[dict]) -> str:
    current = next((w for w in wallets if w.get("is_primary")), None)
    lines = ["<b>👑 Wallet primary</b>", ""]
    if current:
        lines.append(f"Sekarang: <b>{current['id']}</b> · "
                     f"{html.escape(str(current.get('nickname', '')))}")
        lines.append(f"<code>{html.escape(current['public_key'])}</code>")
    lines += ["", "Primary adalah wallet yang mendanai yang lain dan menjadi "
                  "tujuan saat semua SOL ditarik balik.", "",
              "Pilih untuk memindahkannya:"]
    return "\n".join(lines)


def render_sweep_plan(plan, balances_known: bool = True) -> str:
    from slcw.solana import lamports_to_sol

    lines = [f"<b>↩️ Tarik semua SOL ke {plan.destination_id}</b>", "",
             f"<code>{html.escape(plan.destination_public_key)}</code>", ""]

    if not plan.entries:
        lines.append("Tidak ada wallet dengan saldo yang bisa ditarik.")
        lines.append("")
        lines.append("<i>Setiap wallet harus menyisakan cadangan rent-exempt "
                     "(0.00089 SOL) plus biaya transaksi, jadi saldo di bawah "
                     "itu tidak menghasilkan apa-apa.</i>")
        return "\n".join(lines)

    lines.append(f"<b>{plan.count} wallet</b> akan mengirim:")
    for wallet, amount in plan.entries[:10]:
        lines.append(f"  {wallet['id']} → {lamports_to_sol(amount):.6f} SOL")
    if plan.count > 10:
        lines.append(f"  <i>… dan {plan.count - 10} lagi</i>")
    lines.append("")
    lines.append(f"Total masuk: <b>{lamports_to_sol(plan.total_lamports):.6f} SOL</b>")
    if plan.skipped:
        lines.append(f"Dilewati (saldo terlalu kecil): {len(plan.skipped)} wallet")
    lines.append("")
    lines.append("<i>Transaksi on-chain tidak bisa dibatalkan.</i>")
    return "\n".join(lines)


def render_p2p_plan(source: dict, destination: dict, amount: float,
                    balance_sol: float, affordable: bool) -> str:
    lines = [
        "<b>🔁 Kirim antar wallet</b>", "",
        _kv("Dari", f"{source['id']} · {html.escape(str(source.get('nickname','')))}"),
        _kv("Saldo", f"{balance_sol:.6f} SOL"),
        _kv("Ke", f"{destination['id']} · "
                  f"{html.escape(str(destination.get('nickname','')))}"),
        f"<code>{html.escape(destination['public_key'])}</code>",
        "",
        _kv("Jumlah", f"<b>{amount:.6f} SOL</b>"),
        "",
    ]
    lines.append("<i>Transaksi on-chain tidak bisa dibatalkan.</i>" if affordable
                 else "❌ <b>Saldo tidak cukup</b> setelah biaya dan cadangan "
                      "rent-exempt.")
    return "\n".join(lines)


def render_profile(fleet_state: dict, build_name: str) -> str:
    """Attributes, the stats they produce, and what the build is aiming at."""
    from slcw import build as build_mod

    policy = build_mod.get_build(build_name)
    wallets = fleet_state.get("wallets", {})

    lines = [f"<b>🧬 Profil</b> · build <b>{policy.name}</b>", "",
             f"<i>{policy.summary}</i>", ""]

    shown = 0
    for wallet_id, status in sorted(wallets.items()):
        state = status.get("state") or {}
        attributes = state.get("attributes") or {}
        if not attributes:
            continue
        stats = build_mod.derive(attributes, status.get("equipment") or {})

        lines.append(f"<b>{wallet_id}</b> · Lv{state.get('level', '?')} · "
                     f"grade {state.get('grade', '?')}")
        lines.append("  " + " ".join(
            f"{name[:3].upper()}{int(attributes.get(name, 0))}"
            for name in build_mod.ATTRIBUTES))
        lines.append(f"  ⚔️ {stats.weapon_power:.0f} atk · "
                     f"🛡 {stats.physical_defense:.0f} def · "
                     f"🎯 {stats.precision:.0f} · ❤️ {stats.max_health}")
        if stats.set_bonus:
            lines.append(f"  ✨ set {html.escape(stats.set_bonus)} aktif")
        points = state.get("attribute_points") or 0
        if points:
            nxt = build_mod.next_attribute(attributes, build_name)
            lines.append(f"  🔵 {points} poin belum dipakai → berikutnya {nxt}")
        lines.append("")
        shown += 1
        if shown >= 5:
            break

    if not shown:
        return ("<b>🧬 Profil</b>\n\n" + WAITING_FOR_CYCLE)

    lines.append("<i>Rumus dari halaman profil game: atk = 2×might, "
                 "def = 2×vitality + might + 1,5×dexterity, "
                 "HP = 100 + 10×vitality. Ganti build lewat "
                 "<code>SLCW_BUILD</code>: sustain · balanced · damage.</i>")
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


def render_clan(clan_snapshots: dict, donate_gold: bool, enabled: bool,
                fleet_size: int = 0) -> str:
    """Per-wallet clan standing: role, DKP, today's donation, active quest."""
    from slcw import clan as clan_mod

    lines = ["<b>🛡 Clan</b>", ""]
    if not enabled:
        lines.append("Clan dimatikan (<code>SLCW_CLAN_ENABLED=false</code>).")
        return "\n".join(lines)

    if not clan_snapshots:
        lines += [
            "Belum ada wallet yang masuk clan.",
            "",
            "Bot memakai dua aksi clan:",
            "• <b>Submit quest resources</b> — menyetor drop mentah yang tidak "
            "punya bid di market sama sekali, ditukar DKP + clan XP. Ini gratis "
            "buat fleet dan selalu aktif.",
            "• <b>Donasi gold</b> — 1.000 gold = 1 DKP, sekali sehari (reset "
            "00:00 UTC). Gold pindah ke treasury yang dipegang leader clan, "
            f"jadi default <b>{'ON' if donate_gold else 'OFF'}</b>.",
            "",
            "Aksi leader (kick, ubah role, bagi treasury, bubarkan, beli/"
            "perpanjang quest) <b>ditolak permanen</b> di guardrails.",
        ]
        return "\n".join(lines)

    info = next((ctx.get("clan_info") for ctx in clan_snapshots.values()
                 if ctx.get("clan_info") is not None), None)
    if info is not None:
        lines.append(f"<b>{info.name or info.clan_id}</b> · level {info.level} · "
                     f"<b>{info.member_count}/{info.max_members}</b> seat")
        # Seats are 5 x level + 5, and the only measured way to buy clan levels
        # is the weekly quest at 3,500 XP a time, so this is the number that
        # decides how much of the fleet can be inside at all.
        if fleet_size and info.max_members < fleet_size:
            need = clan_mod.levels_for_seats(fleet_size)
            xp = max(0, clan_mod.xp_to_reach(info.level, need) - info.xp)
            quests = -(-xp // clan_mod.QUEST_CLAN_XP) if xp else 0
            lines.append(
                f"   butuh <b>level {need}</b> ({clan_mod.max_members(need)} seat) "
                f"buat {fleet_size} wallet — {xp:,} clan XP lagi "
                f"≈ {quests} quest")
        lines.append("")

    for wallet_id, ctx in sorted(clan_snapshots.items()):
        member = ctx.get("membership")
        if member is None or not member.is_member:
            continue
        quest = ctx.get("quest")
        flags = []
        if member.on_probation():
            flags.append("masa percobaan")
        if member.donated_today():
            flags.append("sudah donasi hari ini")
        suffix = f" — {', '.join(flags)}" if flags else ""
        lines.append(f"<b>{wallet_id}</b> · {member.role} · "
                     f"{member.dkp:,} DKP{suffix}")
        if quest is None:
            lines.append("   tidak ada quest aktif")
            continue
        lines.append(f"   quest: pool {quest.reward_dkp_pool:,} DKP, "
                     f"+{quest.reward_clan_xp:,} clan XP")
        for item, missing in list(quest.outstanding().items())[:4]:
            lines.append(f"     • {item}: kurang {missing:,}")
    if not any(ctx.get("membership") is not None
               and ctx["membership"].is_member for ctx in clan_snapshots.values()):
        lines.append("Belum ada wallet yang masuk clan.")
    return "\n".join(lines)
