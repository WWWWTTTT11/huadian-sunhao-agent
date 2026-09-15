const { fetchDepth, calculateSlippage, fetchFearGreed } = require('./_shared');

function response(statusCode, body) {
  return {
    statusCode,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*' },
    body: JSON.stringify(body)
  };
}

exports.handler = async function(event) {
  try {
    const params = event.queryStringParameters || {};
    const symbol = (params.symbol || 'BTCUSDT').toUpperCase().trim();
    if (!/^[A-Z0-9]{1,20}$/.test(symbol)) return response(400, { error: '交易对格式无效' });
    const side = (params.side || 'buy').toLowerCase();
    if (!['buy', 'sell'].includes(side)) return response(400, { error: '交易方向无效' });
    let amount = Number(params.amount || 10000);
    if (!Number.isFinite(amount)) amount = 10000;
    amount = Math.max(100, Math.min(amount, 1000000));
    const depth = await fetchDepth(symbol);
    const amounts = [1000, 5000, 10000, 30000];
    if (!amounts.includes(amount)) amounts.push(amount);
    amounts.sort((a, b) => a - b);
    const slippage = calculateSlippage(depth, side, amounts);
    const fearGreed = await fetchFearGreed();
    const previous = fearGreed.history.length >= 2 ? fearGreed.history[fearGreed.history.length - 2] : null;
    const delta = previous ? Number(fearGreed.now.value) - Number(previous.value) : 0;
    const selected = slippage.results.reduce((a, b) => Math.abs(b.amount - amount) < Math.abs(a.amount - amount) ? b : a);
    return response(200, {
      symbol, requested_amount: amount, depth, slippage,
      fear_greed: { ...fearGreed, delta },
      history: [{ id: String(Date.now()), symbol, side, amount, created_at: new Date().toLocaleString('zh-CN', { hour12: false }), risk_rating: slippage.risk_rating, slippage_pct: selected.slippage_pct, cost_usdt: selected.cost_usdt }]
    });
  } catch (error) {
    return response(500, { error: error.message || '分析失败' });
  }
};
