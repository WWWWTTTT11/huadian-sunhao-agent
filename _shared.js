const BINANCE_URL = 'https://data-api.binance.vision/api/v3/depth';
const FNG_URL = 'https://api.alternative.me/fng/?limit=7';

async function getJson(url) {
  const response = await fetch(url, { headers: { 'User-Agent': 'huadian-sunhao-agent/1.0' } });
  if (!response.ok) throw new Error(`数据源请求失败：HTTP ${response.status}`);
  return response.json();
}

function depthNumber(value) {
  return Number(value);
}

async function fetchDepth(symbol) {
  const data = await getJson(`${BINANCE_URL}?symbol=${encodeURIComponent(symbol)}&limit=100`);
  const bids = (data.bids || []).map(([price, quantity]) => [depthNumber(price), depthNumber(quantity)]).filter(x => x[0] > 0 && x[1] > 0);
  const asks = (data.asks || []).map(([price, quantity]) => [depthNumber(price), depthNumber(quantity)]).filter(x => x[0] > 0 && x[1] > 0);
  if (!bids.length || !asks.length) throw new Error('盘口深度为空');
  return { symbol, last_update_id: data.lastUpdateId, bids, asks };
}

function walkBook(levels, quoteAmount) {
  let remaining = Number(quoteAmount), spent = 0, base = 0, fills = [];
  for (const [price, quantity] of levels) {
    if (remaining <= 0) break;
    const levelQuote = price * quantity;
    const takeQuote = Math.min(remaining, levelQuote);
    const takeQuantity = takeQuote / price;
    spent += takeQuote;
    base += takeQuantity;
    remaining -= takeQuote;
    fills.push({ price, quantity: takeQuantity, quote: takeQuote });
  }
  return {
    requested_quote: quoteAmount, filled_quote: spent, filled_base: base,
    average_price: base ? spent / base : 0, unfilled_quote: Math.max(0, remaining),
    levels_used: fills.length, fills
  };
}

function calculateSlippage(depth, side, quoteAmounts) {
  const bids = depth.bids, asks = depth.asks;
  const bestBid = bids[0][0], bestAsk = asks[0][0], midpoint = (bestBid + bestAsk) / 2;
  const levels = side === 'buy' ? asks : bids;
  const reference = side === 'buy' ? bestAsk : bestBid;
  const results = quoteAmounts.map(amount => {
    const fill = walkBook(levels, amount);
    const impact = reference ? ((fill.average_price - reference) / reference * 100) : 0;
    const midpointImpact = midpoint ? ((fill.average_price - midpoint) / midpoint * 100) : 0;
    return {
      amount, avg_price: fill.average_price, best_price: reference,
      slippage_pct: Math.abs(impact), mid_slippage_pct: Math.abs(midpointImpact),
      cost_usdt: amount * Math.abs(impact) / 100, levels_used: fill.levels_used,
      unfilled_usdt: fill.unfilled_quote, filled_base: fill.filled_base
    };
  });
  let criticalAmount = null;
  for (let i = 1; i < results.length; i++) {
    const previous = results[i - 1], current = results[i];
    if (previous.slippage_pct > 0 && current.slippage_pct / previous.slippage_pct >= 1.8) {
      criticalAmount = current.amount; break;
    }
  }
  const maximum = Math.max(...results.map(row => row.slippage_pct), 0);
  const rating = maximum < 0.1 ? '低' : maximum < 0.5 ? '中' : maximum < 1.5 ? '高' : '极高';
  return {
    side, best_bid: bestBid, best_ask: bestAsk, mid_price: midpoint,
    spread_pct: midpoint ? (bestAsk - bestBid) / midpoint * 100 : 0,
    results, critical_amount: criticalAmount, risk_rating: rating
  };
}

async function fetchFearGreed() {
  const payload = await getJson(FNG_URL);
  const history = (payload.data || []).map(item => {
    const timestamp = Number(item.timestamp || 0);
    return {
      value: Number(item.value || 0), classification: item.value_classification || 'Unknown',
      timestamp, date: new Date(timestamp * 1000).toISOString().slice(0, 10)
    };
  }).sort((a, b) => a.timestamp - b.timestamp);
  return { now: history[history.length - 1] || {}, history };
}

module.exports = { fetchDepth, calculateSlippage, fetchFearGreed };
