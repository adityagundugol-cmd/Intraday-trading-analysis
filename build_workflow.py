#!/usr/bin/env python3
"""
Builder for the OODA-INTRADAY n8n workflow.

Why a builder instead of hand-writing JSON?
  - The agent system prompts are large; building the dict in Python and dumping
    with json.dump guarantees valid escaping and a single source of truth.
  - Re-running keeps node positions, ids, and connections internally consistent.
  - Optimization/scaling tweaks (models, temperatures, data sources) live here.

Output: workflow/OODA-Intraday-Stock-Analysis.json  (import into n8n directly)
"""

import json
import uuid
import os

# Deterministic-ish ids so re-builds produce stable diffs.
_NS = uuid.UUID("00000000-0000-0000-0000-00000000ce5a")
def nid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))

# Current, capable Claude models. DECIDE runs cooler/stronger than ORIENT.
MODEL_ORIENT = "claude-sonnet-4-6"
MODEL_DECIDE = "claude-sonnet-4-6"

nodes = []
connections = {}

def add_node(name, ntype, type_version, position, parameters,
             credentials=None, on_error=None, extra=None):
    node = {
        "parameters": parameters,
        "id": nid(name),
        "name": name,
        "type": ntype,
        "typeVersion": type_version,
        "position": position,
    }
    if credentials:
        node["credentials"] = credentials
    if on_error:
        node["onError"] = on_error
    if extra:
        node.update(extra)
    nodes.append(node)
    return name

def connect(src, dst, src_type="main", dst_type="main", src_index=0, dst_index=0):
    connections.setdefault(src, {}).setdefault(src_type, [])
    arr = connections[src][src_type]
    while len(arr) <= src_index:
        arr.append([])
    arr[src_index].append({"node": dst, "type": dst_type, "index": dst_index})

# ---------------------------------------------------------------------------
# AGENT PROMPTS (phase-scoped slices of docs/SYSTEM_PROMPT.md)
# ---------------------------------------------------------------------------

PROMPT_PREAMBLE = (
    "You are \"OODA-INTRADAY\", a professional intraday trading analyst and mentor "
    "modeled on a senior proprietary desk trader with 10+ years in Indian and global "
    "markets. You are NOT a tipster, NOT a buy/sell signal bot, and NOT a financial "
    "advisor. You are a structured thinking partner. Prime Directive: no setup = no "
    "trade; capital preservation > opportunity. Be direct, professional and calm; show "
    "reasoning; quantify everything; use probabilistic language (\"favors\", \"tilts "
    "toward\"), never \"will\" or \"guaranteed\". Educational analysis only. Not "
    "investment advice."
)

ORIENT_SYSTEM = PROMPT_PREAMBLE + "\n\n" + (
    "PHASE: ORIENT. You are given a Market Observation Brief (raw data only). Your job "
    "is to synthesize it into a Market Thesis. Do NOT construct a trade here.\n"
    "- Classify the regime as Uptrend (HH+HL), Downtrend (LH+LL) or Sideways/Range, and "
    "state the timeframe (intraday).\n"
    "- Treat support/resistance as ZONES (+/-0.3-0.5%), ranked by touches, time held and "
    "volume. Identify liquidity pools above PDH / below PDL.\n"
    "- Interpret volume: breakout on >2x 20-day avg = genuine; below-average = suspect; "
    "climactic volume = possible exhaustion; price up + volume down = momentum fading.\n"
    "- Read VWAP posture: price > rising VWAP = bullish (buy dips to VWAP); price < falling "
    "VWAP = bearish (sell rallies); repeated crosses = choppy / no-trade.\n"
    "- Weight candles ONLY at key zones (support, resistance, VWAP, trendline).\n"
    "- Decode news/events and sector rotation; note if today is an event day.\n"
    "Output strictly via the structured parser: regime(s), timeframe, directional bias, "
    "ranked support/resistance zones, liquidity pools, volume read, VWAP posture, dominant "
    "sector, event risk, and a 4-6 sentence thesis."
)

DECIDE_SYSTEM = PROMPT_PREAMBLE + "\n\n" + (
    "PHASE: DECIDE. Using the Observation Brief and the Market Thesis, construct a single "
    "intraday trade plan for the symbol ONLY IF the multi-confirmation filter passes.\n\n"
    "CONFIRMATION STACK (need 3 or more): aligned with NIFTY/BANK NIFTY direction; at a "
    "clean support/resistance ZONE (not mid-range); confirmation candle; volume "
    "confirmation; correct side of VWAP; sector strength aligned; no major event in the "
    "next 30 minutes against the position; R:R >= 1:2 (prefer 1:3); India VIX not "
    "exploding.\n\n"
    "RISK RULES (non-negotiable): risk/trade <= 1% of capital (hard cap 2%); R:R minimum "
    "1:2; stop set BEFORE entry and never widened; position size = (Capital x Risk%) / "
    "(Entry - Stop); halve size or stand aside 30 min around RBI/Fed/CPI/results/expiry.\n\n"
    "Set stopLoss on the correct side of entry (below for Long, above for Short) and "
    "target1 in the direction of the trade. If fewer than 3 confirmations align, or R:R < "
    "1:2, or price is mid-range with no clean zone, you MUST return decision = NO_TRADE "
    "with explicit reasons rather than forcing a trade.\n\n"
    "If a PRIOR ATTEMPT was rejected by the deterministic Risk Validator, read its "
    "feedback and FIX exactly those issues (tighten stop, re-anchor entry to a real zone, "
    "raise the target to meet R:R, or downgrade to NO_TRADE). Never widen risk just to "
    "manufacture a trade. Output strictly via the structured parser."
)

# ---------------------------------------------------------------------------
# OUTPUT PARSER SCHEMAS (JSON examples → structured output)
# ---------------------------------------------------------------------------

ORIENT_EXAMPLE = {
    "regimeNifty": "Uptrend",
    "regimeStock": "Sideways",
    "timeframe": "intraday",
    "bias": "long",
    "keyLevels": {"support": [2450.5, 2432.0], "resistance": [2510.0, 2536.0]},
    "liquidityPools": ["stops above PDH 2510", "stops below PDL 2440"],
    "volumeInterpretation": "Reclaim of VWAP on ~2.1x 20-day average volume — genuine.",
    "vwapPosture": "Price above a rising VWAP — bullish; buy dips toward VWAP.",
    "dominantSector": "Energy",
    "eventRisk": "No major scheduled event in the next 30 minutes.",
    "thesis": "Nifty is in an intraday uptrend and the stock has reclaimed VWAP on strong "
              "volume. Bias favors longs above the 2452 zone toward 2510, with invalidation "
              "below 2440. Sector (Energy) is leading. Event risk is low this session.",
}

DECIDE_EXAMPLE = {
    "decision": "TRADE",
    "instrument": "RELIANCE",
    "direction": "Long",
    "setupType": "VWAP retest",
    "entryZoneLow": 2452.0,
    "entryZoneHigh": 2456.0,
    "trigger": "5-min close above 2456 with volume > 1.5x average",
    "stopLoss": 2442.0,
    "target1": 2476.0,
    "target2": 2496.0,
    "target3": 2516.0,
    "riskPerShare": 13.0,
    "riskReward": 2.3,
    "confirmations": ["aligned with Nifty uptrend", "at VWAP support zone",
                      "bullish engulfing candle", "volume 2.1x average"],
    "confirmationCount": 4,
    "invalidation": "Loss of VWAP and the 2440 demand zone on a 5-min close.",
    "timeStop": "Exit by 11:30 IST if price has not moved toward T1.",
    "rationale": "Pullback into a rising VWAP within an intraday uptrend, confirmed by a "
                 "bullish engulfing on above-average volume; R:R clears 1:2.",
}

# ---------------------------------------------------------------------------
# CODE BLOCKS (kept as raw strings; no f-strings to avoid brace clashes)
# ---------------------------------------------------------------------------

VALIDATE_JS = r"""
// === INPUT GATE: validate & normalize the request ===
const incoming = $input.first().json || {};
const body = incoming.body || incoming.query || incoming;

let symbol = (body.symbol || body.ticker || '').toString().trim().toUpperCase();
const warnings = [];
if (!symbol) {
  // Keep the workflow demoable from the Manual Trigger; webhook callers should send a symbol.
  symbol = 'RELIANCE';
  warnings.push('No "symbol" supplied — defaulting to RELIANCE for demo.');
}
if (!/^[A-Z0-9.^&-]{1,20}$/.test(symbol)) {
  throw new Error('VALIDATION_ERROR: symbol "' + symbol + '" is not a valid ticker.');
}

// Map common Indian indices; otherwise route NSE cash symbols to Yahoo (.NS).
const indexMap = { 'NIFTY': '^NSEI', 'NIFTY50': '^NSEI', 'BANKNIFTY': '^NSEBANK',
                   'FINNIFTY': '^CNXFIN', 'SENSEX': '^BSESN' };
let yahooSymbol;
if (indexMap[symbol]) yahooSymbol = indexMap[symbol];
else if (symbol.startsWith('^') || symbol.includes('.')) yahooSymbol = symbol;
else yahooSymbol = symbol + '.NS';

const capital = Number(body.capital) > 0 ? Number(body.capital) : 100000;
let riskPercent = Number(body.riskPercent);
if (!(riskPercent > 0)) riskPercent = 1;
if (riskPercent > 2) { warnings.push('riskPercent capped at 2% by risk policy.'); riskPercent = 2; }

return [{ json: {
  symbol,
  yahooSymbol,
  newsQuery: (body.newsQuery || (symbol + ' stock NSE India')),
  capital,
  riskPercent,
  session: (body.session || 'auto').toString(),
  maxAttempts: 2,
  requestedAt: new Date().toISOString(),
  inputWarnings: warnings,
  feedback: ''
}}];
""".strip()

INDICATORS_JS = r"""
// === OODA OBSERVE: compute technical indicators from gathered market data ===
const cfg = $('Validate & Normalize Input').first().json;
const num = (x) => { const n = Number(x); return Number.isFinite(n) ? n : null; };

function ema(values, period) {
  const v = values.filter((x) => x != null);
  if (v.length < period) return null;
  const k = 2 / (period + 1);
  let e = v.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < v.length; i++) e = v[i] * k + e * (1 - k);
  return +e.toFixed(2);
}
function rsi(closes, period) {
  const v = closes.filter((x) => x != null);
  if (v.length < period + 1) return null;
  let g = 0, l = 0;
  for (let i = v.length - period; i < v.length; i++) {
    const d = v[i] - v[i - 1];
    if (d >= 0) g += d; else l -= d;
  }
  const ag = g / period, al = l / period;
  if (al === 0) return 100;
  return +(100 - 100 / (1 + ag / al)).toFixed(2);
}
function atr(highs, lows, closes, period) {
  const tr = [];
  for (let i = 1; i < closes.length; i++) {
    if ([highs[i], lows[i], closes[i - 1]].some((x) => x == null)) continue;
    tr.push(Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1])));
  }
  if (tr.length < period) return null;
  const s = tr.slice(-period);
  return +(s.reduce((a, b) => a + b, 0) / period).toFixed(2);
}

const warnings = [];

// ---- Intraday 5m: VWAP, opening range, last price ----
let intraday = {};
try {
  const res = $('Market Data (5m)').first().json.chart.result[0];
  const ts = res.timestamp || [];
  const q = res.indicators.quote[0];
  const tz = (res.meta && res.meta.exchangeTimezoneName) || 'Asia/Kolkata';
  const rows = ts.map((t, i) => ({ t, h: num(q.high[i]), l: num(q.low[i]), c: num(q.close[i]), v: num(q.volume[i]) }))
                 .filter((x) => x.c != null);
  const dayKey = (t) => new Date(t * 1000).toLocaleDateString('en-CA', { timeZone: tz });
  const lastDay = rows.length ? dayKey(rows[rows.length - 1].t) : null;
  const today = rows.filter((x) => dayKey(x.t) === lastDay);
  let cumPV = 0, cumV = 0;
  today.forEach((x) => { const tp = (x.h + x.l + x.c) / 3; cumPV += tp * (x.v || 0); cumV += (x.v || 0); });
  const orBars = today.slice(0, 3);
  const last = today.length ? today[today.length - 1] : rows[rows.length - 1];
  intraday = {
    vwap: cumV > 0 ? +(cumPV / cumV).toFixed(2) : null,
    openingRangeHigh: orBars.length ? +Math.max(...orBars.map((x) => x.h)).toFixed(2) : null,
    openingRangeLow: orBars.length ? +Math.min(...orBars.map((x) => x.l)).toFixed(2) : null,
    lastPrice: last ? last.c : (res.meta ? res.meta.regularMarketPrice : null),
    lastBarVolume: last ? last.v : null,
    sessionBars: today.length
  };
} catch (e) { warnings.push('intraday parse failed: ' + e.message); }

// ---- Daily: PDH/PDL/PDC, EMAs, RSI, ATR, 20-day avg volume ----
let daily = {};
try {
  const res = $('Daily Levels').first().json.chart.result[0];
  const q = res.indicators.quote[0];
  const closes = (q.close || []).map(num).filter((x) => x != null);
  const highs = (q.high || []).map(num);
  const lows = (q.low || []).map(num);
  const vols = (q.volume || []).map(num);
  const n = closes.length;
  daily = {
    pdc: n >= 2 ? closes[n - 2] : (res.meta ? res.meta.previousClose : null),
    pdh: highs.length >= 2 ? highs[highs.length - 2] : null,
    pdl: lows.length >= 2 ? lows[lows.length - 2] : null,
    ema20: ema(closes, 20),
    ema50: ema(closes, 50),
    rsi14: rsi(closes, 14),
    atr14: atr(highs, lows, closes, 14),
    avg20Volume: vols.length >= 21 ? Math.round(vols.slice(-21, -1).reduce((a, b) => a + (b || 0), 0) / 20) : null,
    dailyBars: n
  };
} catch (e) { warnings.push('daily parse failed: ' + e.message); }

// ---- Macro / global proxies ----
let macro = [];
try {
  const arr = ($('Macro & Global').first().json.spark || {}).result || [];
  macro = arr.map((s) => {
    const m = ((s.response && s.response[0]) || {}).meta || {};
    const last = m.regularMarketPrice != null ? m.regularMarketPrice : null;
    const prev = m.previousClose != null ? m.previousClose : (m.chartPreviousClose != null ? m.chartPreviousClose : null);
    return { symbol: s.symbol, last, changePct: (last != null && prev) ? +(((last - prev) / prev) * 100).toFixed(2) : null };
  });
} catch (e) { warnings.push('macro parse failed: ' + e.message); }

// ---- News headlines from RSS ----
let news = [];
try {
  const raw = $('News (Google RSS)').first().json;
  const text = typeof raw === 'string' ? raw : (raw.data || raw.body || JSON.stringify(raw));
  const titles = [...text.matchAll(/<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/title>/g)].map((m) => m[1].trim());
  news = titles.slice(1, 9);
} catch (e) { warnings.push('news parse failed: ' + e.message); }

const lastPrice = intraday.lastPrice != null ? intraday.lastPrice : (daily.pdc != null ? daily.pdc : null);
const dayChangePct = (lastPrice != null && daily.pdc) ? +(((lastPrice - daily.pdc) / daily.pdc) * 100).toFixed(2) : null;

const observe = {
  symbol: cfg.symbol,
  yahooSymbol: cfg.yahooSymbol,
  asOf: new Date().toISOString(),
  price: { last: lastPrice, prevClose: daily.pdc != null ? daily.pdc : null, dayChangePct },
  levels: {
    pdh: daily.pdh != null ? daily.pdh : null,
    pdl: daily.pdl != null ? daily.pdl : null,
    pdc: daily.pdc != null ? daily.pdc : null,
    openingRangeHigh: intraday.openingRangeHigh != null ? intraday.openingRangeHigh : null,
    openingRangeLow: intraday.openingRangeLow != null ? intraday.openingRangeLow : null
  },
  technicals: {
    vwap: intraday.vwap != null ? intraday.vwap : null,
    ema20: daily.ema20 != null ? daily.ema20 : null,
    ema50: daily.ema50 != null ? daily.ema50 : null,
    rsi14: daily.rsi14 != null ? daily.rsi14 : null,
    atr14: daily.atr14 != null ? daily.atr14 : null,
    priceVsVwap: (lastPrice != null && intraday.vwap != null) ? (lastPrice >= intraday.vwap ? 'above' : 'below') : null,
    emaTrend: (daily.ema20 != null && daily.ema50 != null) ? (daily.ema20 >= daily.ema50 ? 'up' : 'down') : null
  },
  volume: { lastBar: intraday.lastBarVolume != null ? intraday.lastBarVolume : null, avg20Daily: daily.avg20Volume != null ? daily.avg20Volume : null },
  macro,
  news,
  dataQuality: {
    intradayBars: intraday.sessionBars || 0,
    dailyBars: daily.dailyBars || 0,
    newsCount: news.length,
    warnings: warnings.concat(cfg.inputWarnings || [])
  }
};

return [{ json: Object.assign({}, cfg, { observe }) }];
""".strip()

VALIDATOR_JS = r"""
// === RISK / ACT VALIDATOR (deterministic gate + feedback loop) ===
// attempt counter = how many times THIS node has run in the current execution.
const attempt = $runIndex; // 0 on first pass, 1 after first refine, ...
const cfg = $('Validate & Normalize Input').first().json;
const maxAttempts = cfg.maxAttempts != null ? cfg.maxAttempts : 2;
const plan = ($input.first().json.output) || {};

// The model deliberately declined → that is a valid, disciplined outcome.
if ((plan.decision || '').toString().toUpperCase() === 'NO_TRADE') {
  return [{ json: {
    route: 'rejected', attempt, plan,
    checks: [{ check: 'model_no_trade', pass: true }],
    failures: [],
    reasons: ['OODA confirmation stack not met — agent returned NO_TRADE (capital preservation).'],
    risk: null, feedback: ''
  }}];
}

const checks = [];
const failures = [];
const ok = (name, cond) => { checks.push({ check: name, pass: !!cond }); if (!cond) failures.push(name); };

const dir = (plan.direction || '').toString().toLowerCase();
const isLong = dir.startsWith('l');
const isShort = dir.startsWith('s');
const entry = isShort ? Number(plan.entryZoneLow != null ? plan.entryZoneLow : plan.entryZoneHigh)
                      : Number(plan.entryZoneHigh != null ? plan.entryZoneHigh : plan.entryZoneLow);
const stop = Number(plan.stopLoss);
const t1 = Number(plan.target1);
const conf = Number(plan.confirmationCount != null ? plan.confirmationCount
            : (Array.isArray(plan.confirmations) ? plan.confirmations.length : 0));

ok('direction_valid', isLong || isShort);
ok('entry_numeric', Number.isFinite(entry));
ok('stop_numeric', Number.isFinite(stop));
ok('target_numeric', Number.isFinite(t1));
ok('stop_correct_side', Number.isFinite(entry) && Number.isFinite(stop) && (isLong ? stop < entry : stop > entry));
ok('target_correct_side', Number.isFinite(entry) && Number.isFinite(t1) && (isLong ? t1 > entry : t1 < entry));
ok('confirmations_min_3', conf >= 3);

const riskPerShare = (Number.isFinite(entry) && Number.isFinite(stop)) ? +Math.abs(entry - stop).toFixed(2) : null;
const reward = (Number.isFinite(entry) && Number.isFinite(t1)) ? Math.abs(t1 - entry) : null;
const rr = (riskPerShare && reward) ? +(reward / riskPerShare).toFixed(2) : null;
ok('risk_reward_min_2', rr != null && rr >= 2);

const riskPct = Math.min(Number(cfg.riskPercent) || 1, 2);
const riskAmount = +(((Number(cfg.capital) || 0) * riskPct) / 100).toFixed(2);
const positionSize = (riskPerShare && riskPerShare > 0) ? Math.floor(riskAmount / riskPerShare) : 0;
ok('position_size_positive', positionSize > 0);

const risk = {
  riskPerShare,
  riskReward: rr,
  positionSize,
  riskAmount,
  capitalAtRiskPct: riskPct,
  capitalDeployed: (positionSize && Number.isFinite(entry)) ? +(positionSize * entry).toFixed(2) : null
};

let route;
if (failures.length === 0) route = 'approved';
else if (attempt < maxAttempts) route = 'refine';
else route = 'rejected';

const feedback = failures.length
  ? ('Risk Validator rejected attempt ' + (attempt + 1) + '. Failed checks: ' + failures.join(', ') +
     '. Recomputed risk/share=' + riskPerShare + ', R:R=' + rr + '. Requirements: stop on correct side, '
     + 'R:R >= 2, and >= 3 confirmations. Fix these precisely or return NO_TRADE.')
  : '';

return [{ json: {
  route, attempt, plan, checks, failures, risk, feedback,
  reasons: failures.map((f) => 'Failed: ' + f)
}}];
""".strip()

REFINE_JS = r"""
// === FEEDBACK LOOP: hand the validator's critique back to the DECIDE agent ===
const v = $input.first().json;
return [{ json: { feedback: v.feedback || '', attempt: (v.attempt != null ? v.attempt : 0) + 1 } }];
""".strip()

COMPOSE_JS = r"""
// === REPORT COMPOSER: assemble the final structured report ===
const v = $input.first().json;
const cfg = $('Validate & Normalize Input').first().json;
const observe = $('Compute Technical Indicators').first().json.observe;
let orient = null;
try { orient = $('ORIENT Agent').first().json.output; } catch (e) {}

const disclaimer = 'Educational analysis only. Not investment advice. Verify independently.';
const approved = v.route === 'approved';

const report = {
  disclaimer,
  agent: 'OODA-INTRADAY',
  symbol: cfg.symbol,
  capital: cfg.capital,
  riskPercent: cfg.riskPercent,
  generatedAt: new Date().toISOString(),
  status: approved ? 'TRADE_PLAN' : 'NO_TRADE',
  observe,
  orient,
  decide: approved ? v.plan : { decision: 'NO_TRADE' },
  risk: approved ? v.risk : null,
  validation: {
    route: v.route,
    attempts: (v.attempt != null ? v.attempt : 0) + 1,
    checks: v.checks || [],
    failures: v.failures || [],
    reasons: v.reasons || []
  }
};
return [{ json: report }];
""".strip()

# ---------------------------------------------------------------------------
# NODES
# ---------------------------------------------------------------------------

UA = "Mozilla/5.0 (compatible; n8n-OODA-Intraday/1.0)"

def header_param(pairs):
    return {"parameters": [{"name": k, "value": v} for k, v in pairs]}

# --- Triggers ---
add_node("Webhook Trigger", "n8n-nodes-base.webhook", 2, [-360, 240], {
    "httpMethod": "POST",
    "path": "stock-analysis",
    "responseMode": "responseNode",
    "options": {},
}, extra={"webhookId": nid("Webhook Trigger")})

add_node("Manual Trigger (test)", "n8n-nodes-base.manualTrigger", 1, [-360, 460], {})

# --- Input gate ---
add_node("Validate & Normalize Input", "n8n-nodes-base.code", 2, [-120, 360], {
    "jsCode": VALIDATE_JS,
})

# --- OBSERVE: parallel data gathering (all fail-soft) ---
add_node("Market Data (5m)", "n8n-nodes-base.httpRequest", 4.2, [160, 60], {
    "url": "=https://query1.finance.yahoo.com/v8/finance/chart/{{ $json.yahooSymbol }}",
    "sendQuery": True,
    "queryParameters": header_param([("interval", "5m"), ("range", "5d")]),
    "sendHeaders": True,
    "headerParameters": header_param([("User-Agent", UA)]),
    "options": {"timeout": 15000},
}, on_error="continueRegularOutput")

add_node("Daily Levels", "n8n-nodes-base.httpRequest", 4.2, [160, 240], {
    "url": "=https://query1.finance.yahoo.com/v8/finance/chart/{{ $json.yahooSymbol }}",
    "sendQuery": True,
    "queryParameters": header_param([("interval", "1d"), ("range", "3mo")]),
    "sendHeaders": True,
    "headerParameters": header_param([("User-Agent", UA)]),
    "options": {"timeout": 15000},
}, on_error="continueRegularOutput")

add_node("News (Google RSS)", "n8n-nodes-base.httpRequest", 4.2, [160, 420], {
    "url": "=https://news.google.com/rss/search?q={{ encodeURIComponent($json.newsQuery) }}&hl=en-IN&gl=IN&ceid=IN:en",
    "sendHeaders": True,
    "headerParameters": header_param([("User-Agent", UA)]),
    "options": {"timeout": 15000, "response": {"response": {"responseFormat": "text"}}},
}, on_error="continueRegularOutput")

add_node("Macro & Global", "n8n-nodes-base.httpRequest", 4.2, [160, 600], {
    "url": "https://query1.finance.yahoo.com/v7/finance/spark",
    "sendQuery": True,
    "queryParameters": header_param([
        ("symbols", "^NSEI,^NSEBANK,^DJI,^IXIC,CL=F,GC=F,INR=X,^VIX,^INDIAVIX"),
        ("range", "5d"), ("interval", "1d"),
    ]),
    "sendHeaders": True,
    "headerParameters": header_param([("User-Agent", UA)]),
    "options": {"timeout": 15000},
}, on_error="continueRegularOutput")

# --- Convergence ---
add_node("Merge Observe Data", "n8n-nodes-base.merge", 3, [440, 360], {
    "mode": "combine",
    "combineBy": "combineByPosition",
    "numberInputs": 4,
    "options": {},
})

# --- Compute indicators (Observe output) ---
add_node("Compute Technical Indicators", "n8n-nodes-base.code", 2, [680, 360], {
    "jsCode": INDICATORS_JS,
})

# --- ORIENT agent (Basic LLM Chain + Anthropic + parser) ---
add_node("ORIENT Agent", "@n8n/n8n-nodes-langchain.chainLlm", 1.6, [940, 360], {
    "promptType": "define",
    "text": "={{ 'MARKET OBSERVATION BRIEF (JSON):\\n' + JSON.stringify($json.observe, null, 2) }}",
    "hasOutputParser": True,
    "messages": {"messageValues": [{"message": ORIENT_SYSTEM}]},
})
add_node("ORIENT Model", "@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.3, [900, 600], {
    "model": {"__rl": True, "value": MODEL_ORIENT, "mode": "list", "cachedResultName": "Claude Sonnet 4.6"},
    "options": {"temperature": 0.3, "maxTokensToSample": 2048},
}, credentials={"anthropicApi": {"id": "REPLACE_WITH_ANTHROPIC_CREDENTIAL", "name": "Anthropic account"}})
add_node("ORIENT Parser", "@n8n/n8n-nodes-langchain.outputParserStructured", 1.2, [1080, 600], {
    "schemaType": "fromJson",
    "jsonSchemaExample": json.dumps(ORIENT_EXAMPLE, indent=2),
})

# --- DECIDE agent (Basic LLM Chain + Anthropic + parser) ---
DECIDE_TEXT = (
    "={{ "
    "'OBSERVATION BRIEF (JSON):\\n' + JSON.stringify($('Compute Technical Indicators').first().json.observe) "
    "+ '\\n\\nMARKET THESIS (Orient, JSON):\\n' + JSON.stringify($('ORIENT Agent').first().json.output) "
    "+ '\\n\\nTRADER CONSTRAINTS: capital=' + $('Validate & Normalize Input').first().json.capital "
    "+ ', riskPercent=' + $('Validate & Normalize Input').first().json.riskPercent "
    "+ ($json.feedback ? ('\\n\\nPRIOR ATTEMPT REJECTED BY RISK VALIDATOR — FIX EXACTLY THESE:\\n' + $json.feedback) : '') "
    "+ '\\n\\nConstruct the DECIDE-phase trade plan for the symbol in the required structured format, "
    "or return decision=NO_TRADE with reasons if the confirmation stack or R:R is not met.' "
    "}}"
)
add_node("DECIDE Agent", "@n8n/n8n-nodes-langchain.chainLlm", 1.6, [1240, 360], {
    "promptType": "define",
    "text": DECIDE_TEXT,
    "hasOutputParser": True,
    "messages": {"messageValues": [{"message": DECIDE_SYSTEM}]},
})
add_node("DECIDE Model", "@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.3, [1200, 600], {
    "model": {"__rl": True, "value": MODEL_DECIDE, "mode": "list", "cachedResultName": "Claude Sonnet 4.6"},
    "options": {"temperature": 0.1, "maxTokensToSample": 2048},
}, credentials={"anthropicApi": {"id": "REPLACE_WITH_ANTHROPIC_CREDENTIAL", "name": "Anthropic account"}})
add_node("DECIDE Parser", "@n8n/n8n-nodes-langchain.outputParserStructured", 1.2, [1380, 600], {
    "schemaType": "fromJson",
    "jsonSchemaExample": json.dumps(DECIDE_EXAMPLE, indent=2),
})

# --- Risk validator + router ---
add_node("Risk / Act Validator", "n8n-nodes-base.code", 2, [1540, 360], {
    "jsCode": VALIDATOR_JS,
})
add_node("Route Decision", "n8n-nodes-base.switch", 3, [1780, 360], {
    "rules": {"values": [
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                        "conditions": [{"id": nid("rule-approved"), "leftValue": "={{ $json.route }}",
                                        "rightValue": "approved",
                                        "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"}, "renameOutput": True, "outputKey": "approved"},
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                        "conditions": [{"id": nid("rule-refine"), "leftValue": "={{ $json.route }}",
                                        "rightValue": "refine",
                                        "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"}, "renameOutput": True, "outputKey": "refine"},
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                        "conditions": [{"id": nid("rule-rejected"), "leftValue": "={{ $json.route }}",
                                        "rightValue": "rejected",
                                        "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"}, "renameOutput": True, "outputKey": "rejected"},
    ]},
    "options": {},
})

# --- Feedback loop node ---
add_node("Refine Loop", "n8n-nodes-base.code", 2, [1780, 600], {
    "jsCode": REFINE_JS,
})

# --- Report + response ---
add_node("Compose Report", "n8n-nodes-base.code", 2, [2040, 300], {
    "jsCode": COMPOSE_JS,
})
add_node("Respond to Webhook", "n8n-nodes-base.respondToWebhook", 1.1, [2280, 300], {
    "respondWith": "json",
    "responseBody": "={{ $json }}",
    "options": {},
})

# ---------------------------------------------------------------------------
# CONNECTIONS
# ---------------------------------------------------------------------------

connect("Webhook Trigger", "Validate & Normalize Input")
connect("Manual Trigger (test)", "Validate & Normalize Input")

for n in ["Market Data (5m)", "Daily Levels", "News (Google RSS)", "Macro & Global"]:
    connect("Validate & Normalize Input", n)

connect("Market Data (5m)", "Merge Observe Data", dst_index=0)
connect("Daily Levels", "Merge Observe Data", dst_index=1)
connect("News (Google RSS)", "Merge Observe Data", dst_index=2)
connect("Macro & Global", "Merge Observe Data", dst_index=3)

connect("Merge Observe Data", "Compute Technical Indicators")
connect("Compute Technical Indicators", "ORIENT Agent")

connect("ORIENT Model", "ORIENT Agent", src_type="ai_languageModel", dst_type="ai_languageModel")
connect("ORIENT Parser", "ORIENT Agent", src_type="ai_outputParser", dst_type="ai_outputParser")

connect("ORIENT Agent", "DECIDE Agent")
connect("DECIDE Model", "DECIDE Agent", src_type="ai_languageModel", dst_type="ai_languageModel")
connect("DECIDE Parser", "DECIDE Agent", src_type="ai_outputParser", dst_type="ai_outputParser")

connect("DECIDE Agent", "Risk / Act Validator")
connect("Risk / Act Validator", "Route Decision")

# Switch outputs: 0=approved, 1=refine, 2=rejected
connect("Route Decision", "Compose Report", src_index=0)
connect("Route Decision", "Refine Loop", src_index=1)
connect("Route Decision", "Compose Report", src_index=2)

connect("Refine Loop", "DECIDE Agent")  # feedback loop back into DECIDE

connect("Compose Report", "Respond to Webhook")

# ---------------------------------------------------------------------------
# STICKY NOTES (on-canvas flowchart annotations)
# ---------------------------------------------------------------------------

def sticky(name, content, position, size, color):
    add_node(name, "n8n-nodes-base.stickyNote", 1, position,
             {"content": content, "height": size[1], "width": size[0], "color": color})

sticky("note-observe",
       "## 🔍 OBSERVE — Data Gathering Agent\nFan-out HTTP calls (fail-soft) collect "
       "intraday 5m candles, daily levels, news headlines and macro proxies, then a Code "
       "node computes VWAP, EMA20/50, RSI14, ATR14, PDH/PDL/PDC, opening range and volume "
       "vs 20-day average → **Market Observation Brief**.",
       [120, -200], [620, 200], 5)

sticky("note-orient",
       "## 🧭 ORIENT — Synthesis Agent\nLLM classifies regime, maps S/R zones, reads "
       "volume & VWAP posture, decodes news/sector rotation → **Market Thesis** "
       "(structured).",
       [900, -200], [420, 200], 4)

sticky("note-decide",
       "## 🎯 DECIDE — Trade Construction Agent\nLLM applies the 3+ confirmation stack and "
       "risk rules → structured **Trade Plan** or **NO_TRADE**. Receives validator feedback "
       "on refine loops.",
       [1180, -200], [420, 200], 3)

sticky("note-act",
       "## ⚡ ACT — Risk Validator + Router (feedback loop)\nDeterministic Code gate checks "
       "stop side, R:R ≥ 2, ≥3 confirmations and recomputes position size. Switch routes:\n"
       "• **approved** → Compose Report\n• **refine** → loop back to DECIDE with critique "
       "(max 2 retries)\n• **rejected** → NO_TRADE report.",
       [1640, -200], [640, 220], 6)

sticky("note-io",
       "## 🚪 I/O\nPOST `/webhook/stock-analysis` with `{ symbol, capital, riskPercent }` "
       "(Manual Trigger for testing). Returns the structured OODA report via Respond node.",
       [-360, 40], [240, 160], 7)

# ---------------------------------------------------------------------------
# ASSEMBLE & WRITE
# ---------------------------------------------------------------------------

workflow = {
    "name": "OODA-INTRADAY — AI Stock Analysis Agent",
    "nodes": nodes,
    "connections": connections,
    "active": False,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "meta": {"templateId": "ooda-intraday-stock-analysis"},
    "tags": [{"name": "trading"}, {"name": "ai-agent"}, {"name": "ooda"}],
}

out_path = os.path.join(os.path.dirname(__file__), "workflow", "OODA-Intraday-Stock-Analysis.json")
with open(out_path, "w") as f:
    json.dump(workflow, f, indent=2, ensure_ascii=False)

print("Wrote", out_path)
print("Nodes:", len(nodes), "| Connection sources:", len(connections))
