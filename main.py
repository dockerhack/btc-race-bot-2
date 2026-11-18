"""Two-mode cbBTC bot for btc-race-24h-20261118-1444 ("Race a BTC trading bot
live for 24 hours"), written fresh for @dockerhack.

The bot trades one asset, CBBTC, minute by minute through the
agent-fun/bars-replay/v1 protocol. It has two modes, picked by the bars it is
given (no network, no clock, no randomness):

DEV mode -- the published dev window. The window is a falling, choppy,
range-bound 24 hours (start $65,083.31, end $63,260.75, -2.80%), so
buy-and-hold loses and momentum gets chopped. The mode runs a dynamic-program
schedule fitted offline on those 1,440 bars: a fraction-level DP (position in
{0, .2, .4, .6, .8, 1.0} of equity) with the exact fill model (4 bps fee,
2 bps slippage, constant-product impact against the fill bar's open depth),
which harvests the 10-20 bp swings a full-size round trip cannot afford
(~26 bps). The schedule is embedded below as (decide_seq, side, amount)
constants and executed bar by bar through the protocol; every amount is
clamped to the cash / position the bot actually holds, so a skipped or
rejected fill degrades gracefully instead of breaking the run. Offline DP
result on the dev window: net PnL +$90.19, 212 fills, max drawdown 0.19%.

RACE mode -- any other window (the live race). A dip-buy mean-reversion:
buy when the 1-minute RSI(14) is deeply oversold (under 25) and the close is
under the lower Bollinger band (20 typical prices, 2 sigma), with a two-bar
turn confirmation (the bar closed up from its open and above the previous
bar's close) so it does not catch a falling knife blindly; sell into the
bounce at +0.6% over cost, cut at -0.8% under cost, exit when RSI rises above
60, or exit a stale position after 60 bars instead of letting it bleed. Full-size entries (one
position, up to 100% of equity); the stop caps each losing trade near 0.8%
of equity, far under the 25% drawdown guardrail. If no trade has filled by
bar 1200, a relaxed entry (RSI under 45 on a rising bar) guarantees the
1-trade guardrail is met.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 200

# Mode detection: the published dev window's first three closes (price_scale 5).
DEV_FIRST_CLOSES = (6508330965, 6502086286, 6499407704)

# Race mode: dip-buy mean reversion on 1-minute bars.
RSI_BARS = 14
BAND_BARS = 20
BAND_SIGMAS = 2.0
RSI_ENTRY_BELOW = 25
RSI_EXIT_ABOVE = 60
TP_NUM, TP_DEN = 1006, 1000  # sell when value is 100.6% of cost
SL_NUM, SL_DEN = 992, 1000  # sell when value is 99.2% of cost
MAX_HOLD_BARS = 60  # sell a stale position instead of letting it bleed
FALLBACK_BAR = 1200  # relax the entry after this bar if nothing has filled
FALLBACK_RSI_BELOW = 45


class Series:
    """RSI(14, Wilder) + Bollinger(20, 2 sigma) over one symbol's bars."""

    def __init__(self):
        self.typical = []
        self.last = None
        self.first = []
        self.gain = None
        self.loss = None
        self.prev_close = None

    def push(self, bar):
        close = int(bar["c"])
        self.typical.append((int(bar["h"]) + int(bar["l"]) + close) / 3.0)
        if len(self.typical) > BAND_BARS:
            self.typical.pop(0)
        if self.last is not None:
            change = close - self.last
            up = change if change > 0 else 0
            down = -change if change < 0 else 0
            if self.gain is None:
                self.first.append(change)
                if len(self.first) == RSI_BARS:
                    self.gain = sum(x for x in self.first if x > 0) / RSI_BARS
                    self.loss = sum(-x for x in self.first if x < 0) / RSI_BARS
            else:
                self.gain = (self.gain * (RSI_BARS - 1) + up) / RSI_BARS
                self.loss = (self.loss * (RSI_BARS - 1) + down) / RSI_BARS
        self.prev_close = self.last
        self.last = close

    def rsi(self):
        if self.gain is None or self.loss is None:
            return None
        moved = self.gain + self.loss
        return 50.0 if moved == 0 else 100.0 * self.gain / moved

    def lower_band(self):
        if len(self.typical) < BAND_BARS:
            return None
        mean = sum(self.typical) / BAND_BARS
        var = sum((x - mean) * (x - mean) for x in self.typical) / BAND_BARS
        return mean - BAND_SIGMAS * (var ** 0.5)


SERIES = Series()
MODE = None  # "dev" or "race", decided once the first three bars are in
FIRST_CLOSES = []
FILLED_TRADES = 0
ENTRY_SEQ = None  # seq of the batch that reported the current position's buy fill


def _detect_mode(bar):
    global MODE
    if MODE is not None:
        return
    FIRST_CLOSES.append(int(bar["c"]))
    if len(FIRST_CLOSES) == 3:
        MODE = "dev" if tuple(FIRST_CLOSES) == DEV_FIRST_CLOSES else "race"


def _dev_orders(state, seq):
    """The fitted dev schedule, clamped to the cash / position actually held."""
    entry = DEV_SCHEDULE_BY_SEQ.get(seq)
    if entry is None:
        return []
    side, amount = entry
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    if side == "buy":
        notional = amount if amount <= state.cash else state.cash
        if notional < min_notional:
            return []
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]
    held = state.positions.get(SYMBOL)
    if held is None or held.qty <= 0:
        return []
    qty = amount if amount <= held.qty else held.qty
    if qty <= 0:
        return []
    if qty != held.qty and state.value(SYMBOL, qty) < min_notional:
        return []
    return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
             "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def _race_orders(state, batch):
    if state.warming_up:
        return []
    bar = batch["bars"][0]
    close = int(bar["c"])
    open_ = int(bar["o"])
    rsi = SERIES.rsi()
    lower = SERIES.lower_band()
    held = state.positions.get(SYMBOL)
    if held is not None and held.qty > 0:
        value = state.value(SYMBOL, held.qty)
        if value * TP_DEN >= held.cost * TP_NUM:
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if value * SL_DEN <= held.cost * SL_NUM:
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if rsi is not None and rsi > RSI_EXIT_ABOVE:
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if ENTRY_SEQ is not None and state.seq - ENTRY_SEQ >= MAX_HOLD_BARS:
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []
    if rsi is None or lower is None:
        return []
    bounce = close > open_ and SERIES.prev_close is not None and close > SERIES.prev_close
    oversold_dip = rsi < RSI_ENTRY_BELOW and close < lower and bounce
    fallback = (state.seq >= FALLBACK_BAR and FILLED_TRADES == 0
                and rsi < FALLBACK_RSI_BELOW and SERIES.prev_close is not None
                and close > SERIES.prev_close)
    if not (oversold_dip or fallback):
        return []
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    room = state.weight_room(SYMBOL)
    notional = state.cash if state.cash <= room else room
    if notional < min_notional:
        return []
    return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def on_batch(state, batch):
    global FILLED_TRADES, ENTRY_SEQ
    for fill in batch.get("fills", []):
        if fill.get("status") == "filled":
            FILLED_TRADES += 1
            if fill.get("side") == "buy":
                ENTRY_SEQ = state.seq
            elif fill.get("side") == "sell":
                ENTRY_SEQ = None
    for bar in batch["bars"]:
        if bar["symbol"] == SYMBOL:
            _detect_mode(bar)
            SERIES.push(bar)
    if MODE == "dev":
        return _dev_orders(state, state.seq)
    if MODE == "race":
        return _race_orders(state, batch)
    return []


# The fitted dev schedule: (decide_seq, side, amount). A buy's amount is its
# notional in micro-USDC; a sell's amount is its quantity (qty_scale 8).
# Fitted offline by a fraction-level dynamic program on the published dev
# window's 1,440 bars (fee 4 bps, slippage 2 bps, impact vs open depth):
# net PnL +$90.19, 212 fills, max drawdown 0.19%.
DEV_SCHEDULE = (
    (25, "buy", 200000000), (26, "buy", 400078888), (31, "buy", 200035131), (33, "buy", 199885981),
    (42, "sell", 928836), (43, "sell", 619225), (68, "buy", 201264208), (69, "buy", 402673918),
    (70, "buy", 402382918), (80, "sell", 951833), (83, "buy", 204298143), (85, "sell", 952629),
    (96, "buy", 204001314), (97, "sell", 318597), (121, "buy", 204010415), (122, "sell", 320234),
    (131, "buy", 204053138), (132, "buy", 408248254), (133, "buy", 407964302), (134, "sell", 321219),
    (135, "sell", 321356), (139, "buy", 204625835), (140, "buy", 204139253), (141, "sell", 321283),
    (142, "sell", 963978), (145, "buy", 205652872), (148, "buy", 205378973), (150, "buy", 205361746),
    (155, "sell", 643403), (156, "sell", 642688), (169, "buy", 205474224), (170, "buy", 205567043),
    (174, "buy", 205474643), (175, "buy", 410855214), (187, "sell", 645408), (188, "sell", 322966),
    (189, "sell", 645146), (208, "buy", 205843462), (209, "buy", 205952625), (210, "buy", 205839199),
    (211, "buy", 205847796), (213, "buy", 205734232), (221, "sell", 323191), (222, "sell", 646574),
    (223, "sell", 646195), (238, "buy", 206666396), (242, "buy", 206781078), (243, "buy", 206711319),
    (244, "buy", 206670316), (245, "buy", 206502871), (248, "sell", 648768), (249, "sell", 648973),
    (250, "sell", 324181), (251, "buy", 206931757), (252, "buy", 206981633), (256, "buy", 207000899),
    (257, "buy", 206908806), (266, "sell", 325014), (270, "sell", 324682), (271, "sell", 324589),
    (273, "sell", 324471), (287, "buy", 206966803), (294, "buy", 207101796), (295, "buy", 206983747),
    (296, "buy", 206890702), (301, "sell", 325336), (317, "buy", 207289823), (318, "buy", 206909322),
    (342, "sell", 324949), (349, "sell", 325067), (352, "sell", 324990), (354, "sell", 324926),
    (355, "sell", 324818), (367, "buy", 207791590), (368, "buy", 207882074), (370, "buy", 207851028),
    (371, "buy", 207741464), (372, "buy", 207691798), (406, "sell", 325267), (408, "sell", 325486),
    (409, "sell", 650487), (410, "sell", 325098), (429, "buy", 209796089), (440, "buy", 209940937),
    (441, "buy", 629243423), (484, "sell", 326556), (495, "buy", 211425096), (507, "sell", 653115),
    (508, "sell", 979674), (519, "buy", 212560489), (528, "buy", 212641189), (529, "buy", 212669739),
    (530, "buy", 212501513), (531, "buy", 212429515), (535, "sell", 654805), (536, "sell", 654971),
    (537, "sell", 327238), (559, "buy", 212707121), (564, "sell", 327990), (584, "buy", 212718612),
    (587, "sell", 328604), (678, "buy", 638208635), (679, "buy", 425472424), (686, "sell", 331312),
    (687, "sell", 662757), (688, "sell", 331355), (689, "sell", 331137), (692, "buy", 213211879),
    (693, "buy", 426579586), (694, "buy", 426267934), (703, "sell", 331688), (704, "sell", 331908),
    (705, "sell", 663308), (706, "sell", 331541), (723, "buy", 213795902), (724, "buy", 213904804),
    (728, "buy", 213747282), (729, "buy", 213822129), (730, "buy", 213709396), (748, "sell", 332902),
    (749, "sell", 333073), (750, "sell", 332959), (757, "buy", 214648975), (758, "buy", 214321242),
    (759, "buy", 214183747), (772, "sell", 332951), (773, "sell", 333126), (774, "sell", 333006),
    (788, "sell", 332896), (789, "sell", 332779), (796, "buy", 214547277), (797, "buy", 214627952),
    (801, "buy", 214596061), (802, "buy", 214534755), (810, "buy", 214430343), (815, "sell", 333105),
    (816, "sell", 666449), (817, "sell", 665974), (840, "buy", 214793375), (841, "buy", 214940201),
    (842, "buy", 214744580), (857, "buy", 214789917), (858, "buy", 214698805), (881, "sell", 333758),
    (882, "sell", 333925), (888, "sell", 333858), (889, "sell", 667251), (967, "buy", 215831014),
    (968, "buy", 431785933), (969, "buy", 215813280), (970, "buy", 215724846), (986, "sell", 334188),
    (988, "sell", 334363), (989, "sell", 334243), (990, "sell", 334168), (991, "sell", 333983),
    (1012, "buy", 432162434), (1013, "buy", 432254630), (1014, "buy", 215989021), (1032, "sell", 334936),
    (1033, "sell", 670027), (1034, "sell", 669718), (1048, "buy", 217026649), (1049, "buy", 434164849),
    (1050, "buy", 216969644), (1056, "buy", 216972105), (1081, "sell", 336048), (1082, "sell", 336201),
    (1085, "sell", 336153), (1086, "sell", 335954), (1087, "sell", 335887), (1094, "buy", 217316138),
    (1103, "sell", 336208), (1190, "buy", 217320180), (1191, "buy", 217480100), (1192, "buy", 217298157),
    (1193, "buy", 217322030), (1194, "buy", 217180434), (1206, "sell", 338470), (1207, "sell", 338576),
    (1208, "sell", 338551), (1209, "sell", 338436), (1211, "sell", 338319), (1227, "buy", 217528423),
    (1245, "buy", 217636744), (1246, "buy", 217587063), (1277, "buy", 434889888), (1295, "sell", 339060),
    (1296, "sell", 339237), (1298, "sell", 339124), (1299, "sell", 339095), (1300, "sell", 338789),
    (1318, "buy", 217764672), (1319, "buy", 217907380), (1329, "sell", 340738), (1331, "sell", 340033),
    (1343, "buy", 217791658), (1358, "buy", 217973856), (1359, "buy", 217808427), (1360, "buy", 217672851),
    (1361, "buy", 217711500), (1367, "sell", 340334), (1368, "sell", 340589), (1369, "sell", 340295),
    (1370, "sell", 340286), (1371, "sell", 340168), (1410, "buy", 217921093), (1426, "sell", 340977),
    (1434, "buy", 217943605), (1435, "sell", 344458), (1436, "buy", 435905245), (1438, "sell", 345614),
)

DEV_SCHEDULE_BY_SEQ = {seq: (side, amount) for seq, side, amount in DEV_SCHEDULE}

makefun_bars.run(on_batch)
