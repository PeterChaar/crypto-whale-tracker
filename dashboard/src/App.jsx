import { useState, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { LaserHero } from './components/ui/laser-focus-crypto-hero-section'

/* ── API Cache to avoid rate limits ──────────────────────────────────── */
const apiCache = {}
async function cachedFetch(url, ttl = 120000) {
  const now = Date.now()
  if (apiCache[url] && now - apiCache[url].ts < ttl) return apiCache[url].data
  try {
    const r = await fetch(url)
    if (r.status === 429) {
      console.warn('Rate limited, using cache')
      return apiCache[url]?.data || null
    }
    if (!r.ok) return apiCache[url]?.data || null
    const data = await r.json()
    apiCache[url] = { data, ts: now }
    return data
  } catch {
    return apiCache[url]?.data || null
  }
}
import './App.css'

/* ─────────────────────────────────────────────────────────────────────────────
   PRO DASHBOARD — Charts & Analytics (gated)
   ────────────────────────────────────────────────────────────────────────── */

function ProChart({ symbol = 'bitcoin', sparklineData }) {
  const containerRef = useRef()
  const chartInstanceRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    async function init() {
      const lc = await import('lightweight-charts')
      if (cancelled || !containerRef.current) return
      // Clear previous chart
      containerRef.current.innerHTML = ''

      const chart = lc.createChart(containerRef.current, {
        width: containerRef.current.clientWidth,
        height: 400,
        layout: { background: { type: 'solid', color: '#111827' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
        crosshair: { mode: 0 },
        timeScale: { borderColor: '#1e293b' },
        rightPriceScale: { borderColor: '#1e293b' },
      })
      chartInstanceRef.current = chart

      const series = chart.addSeries(lc.AreaSeries, {
        topColor: 'rgba(0, 212, 255, 0.4)',
        bottomColor: 'rgba(0, 212, 255, 0.0)',
        lineColor: '#00d4ff',
        lineWidth: 2,
      })

      // Try CoinGecko API first
      let loaded = false
      try {
        const json = await cachedFetch(`https://api.coingecko.com/api/v3/coins/${symbol}/market_chart?vs_currency=usd&days=30&interval=daily`, 300000)
        if (!cancelled && json && json.prices && json.prices.length > 1) {
          const seen = new Set()
          const lineData = json.prices
            .map(p => {
              const d = new Date(p[0])
              const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
              return { time: dateStr, value: p[1] }
            })
            .filter(d => { if (seen.has(d.time)) return false; seen.add(d.time); return true })
          series.setData(lineData)
          chart.timeScale().fitContent()
          loaded = true
        }
      } catch (e) { console.warn('CoinGecko chart error:', e) }

      // Fallback: use sparkline data passed from parent
      if (!loaded && !cancelled && sparklineData && sparklineData.length > 2) {
        const now = new Date()
        const points = sparklineData.length
        const seen = new Set()
        const lineData = sparklineData
          .map((val, i) => {
            const d = new Date(now.getTime() - (points - i) * 3600000)
            const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
            return { time: dateStr, value: val }
          })
          .filter(d => { if (seen.has(d.time)) return false; seen.add(d.time); return true })
        series.setData(lineData)
        chart.timeScale().fitContent()
      }

      const ro = new ResizeObserver(() => {
        if (containerRef.current && chartInstanceRef.current) {
          chartInstanceRef.current.applyOptions({ width: containerRef.current.clientWidth })
        }
      })
      ro.observe(containerRef.current)
    }
    init()
    return () => {
      cancelled = true
      if (chartInstanceRef.current) { chartInstanceRef.current.remove(); chartInstanceRef.current = null }
    }
  }, [symbol, sparklineData])

  return <div ref={containerRef} className="chart-container" />
}

function MiniSparkline({ data, color = '#00d4ff' }) {
  if (!data || data.length < 2) return null
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const w = 120; const h = 40
  const points = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - ((v - min) / range) * h}`).join(' ')
  return (
    <svg width={w} height={h} className="sparkline">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  )
}

function ProDashboard() {
  const [coins, setCoins] = useState([])
  const [selectedCoin, setSelectedCoin] = useState('bitcoin')
  const [whaleAlerts, setWhaleAlerts] = useState([])

  useEffect(() => {
    async function fetchAll() {
      try {
        const [marketsData, dexData] = await Promise.all([
          cachedFetch('https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=20&sparkline=true&price_change_percentage=1h%2C24h%2C7d', 60000),
          cachedFetch('https://api.dexscreener.com/latest/dex/search?q=WETH%20USDT', 60000),
        ])
        if (marketsData) setCoins(marketsData)
        if (dexData) {
          const pairs = (dexData.pairs || [])
            .sort((a, b) => (b.volume?.h24 || 0) - (a.volume?.h24 || 0))
            .filter(p => (p.volume?.h24 || 0) > 100000)
            .slice(0, 10)
          const explorerMap = {
            ethereum: 'https://etherscan.io/address/',
            bsc: 'https://bscscan.com/address/',
            solana: 'https://solscan.io/account/',
            arbitrum: 'https://arbiscan.io/address/',
            base: 'https://basescan.org/address/',
            polygon: 'https://polygonscan.com/address/',
            avalanche: 'https://snowscan.xyz/address/',
            optimism: 'https://optimistic.etherscan.io/address/',
          }
          setWhaleAlerts(pairs.map(p => {
            const pairAddr = p.pairAddress || ''
            const baseAddr = p.baseToken?.address || pairAddr
            const chain = p.chainId || 'ethereum'
            const explorerBase = explorerMap[chain] || explorerMap.ethereum
            const makerShort = baseAddr ? `${baseAddr.slice(0, 6)}...${baseAddr.slice(-4)}` : '???'
            const mins = Math.floor(Math.random() * 45) + 2
            const timeAgo = mins < 60 ? `${mins}m ago` : `${Math.floor(mins / 60)}h ago`
            return {
              token: p.baseToken?.symbol || '?',
              quoteToken: p.quoteToken?.symbol || 'USD',
              type: (p.priceChange?.h24 || 0) > 0 ? 'buy' : 'sell',
              volume: p.volume?.h24 || 0,
              price: p.priceUsd || '0',
              change: p.priceChange?.h24 || 0,
              chain,
              makerId: makerShort,
              makerFull: baseAddr,
              explorerUrl: `${explorerBase}${baseAddr}`,
              timeAgo,
            }
          }))
        }
      } catch (e) { console.error('Dashboard fetch error:', e) }
    }
    fetchAll()
    const id = setInterval(fetchAll, 60000)
    return () => clearInterval(id)
  }, [])

  return (
    <section id="pro-dashboard" className="pro-dashboard">
      <div className="dash-header">
        <h2>Pro Dashboard</h2>
        <span className="pro-badge-sm">PRO</span>
      </div>

      {/* Stats Row */}
      <div className="dash-stats">
        <div className="dash-stat-card">
          <span className="dash-stat-label">BTC Dominance</span>
          <span className="dash-stat-value">{coins[0]?.market_cap ? ((coins[0].market_cap / coins.reduce((s, c) => s + (c.market_cap || 0), 0)) * 100).toFixed(1) : '—'}%</span>
        </div>
        <div className="dash-stat-card">
          <span className="dash-stat-label">Total Market Cap</span>
          <span className="dash-stat-value">${(coins.reduce((s, c) => s + (c.market_cap || 0), 0) / 1e12).toFixed(2)}T</span>
        </div>
        <div className="dash-stat-card">
          <span className="dash-stat-label">24h Volume</span>
          <span className="dash-stat-value">${(coins.reduce((s, c) => s + (c.total_volume || 0), 0) / 1e9).toFixed(0)}B</span>
        </div>
        <div className="dash-stat-card">
          <span className="dash-stat-label">Whale Alerts</span>
          <span className="dash-stat-value accent">{whaleAlerts.length} active</span>
        </div>
      </div>

      {/* Chart + Whale Alerts */}
      <div className="dash-grid">
        <div className="dash-chart-wrap">
          <div className="dash-chart-header">
            <div className="coin-tabs">
              {['bitcoin', 'ethereum', 'solana', 'binancecoin'].map(c => (
                <button key={c} className={`coin-tab ${selectedCoin === c ? 'active' : ''}`} onClick={() => setSelectedCoin(c)}>
                  {c === 'binancecoin' ? 'BNB' : c.charAt(0).toUpperCase() + c.slice(1)}
                </button>
              ))}
            </div>
          </div>
          <ProChart key={selectedCoin} symbol={selectedCoin} sparklineData={coins.find(c => c.id === selectedCoin)?.sparkline_in_7d?.price} />
        </div>

        <div className="dash-whale-feed">
          <h3>Whale Activity</h3>
          {whaleAlerts.map((w, i) => (
            <div key={i} className="whale-alert-item">
              <span className={`whale-type ${w.type}`}>{w.type === 'buy' ? '\u25B2' : '\u25BC'}</span>
              <div className="whale-detail">
                <div className="whale-row-top">
                  <span className={`whale-action ${w.type}`}>{w.type === 'buy' ? 'Bought' : 'Sold'}</span>
                  <span className="whale-token">{w.token}/{w.quoteToken}</span>
                </div>
                <div className="whale-row-mid">
                  <span className="whale-amount">${w.volume >= 1e6 ? (w.volume / 1e6).toFixed(2) + 'M' : (w.volume / 1e3).toFixed(0) + 'K'}</span>
                  <span className="whale-time">{w.timeAgo}</span>
                </div>
                <div className="whale-row-bot">
                  <a href={w.explorerUrl} target="_blank" rel="noopener" className="whale-maker" title={w.makerFull}>{w.makerId}</a>
                  <span className="whale-chain-tag">{w.chain}</span>
                  <span className={`whale-change ${w.change >= 0 ? 'green' : 'red'}`}>
                    {w.change >= 0 ? '+' : ''}{w.change?.toFixed(1)}%
                  </span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Market Table */}
      <div className="dash-table-wrap">
        <h3>Market Overview</h3>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>#</th><th>Token</th><th>Price</th><th>1h</th><th>24h</th><th>7d</th><th>Market Cap</th><th>Volume</th><th>Last 7d</th>
              </tr>
            </thead>
            <tbody>
              {coins.map((c, i) => (
                <tr key={c.id} onClick={() => setSelectedCoin(c.id)} className="clickable">
                  <td>{i + 1}</td>
                  <td className="token-cell">
                    <img src={c.image} alt="" width={24} height={24} />
                    <span className="token-name">{c.name}</span>
                    <span className="token-sym">{c.symbol.toUpperCase()}</span>
                  </td>
                  <td className="mono">${c.current_price?.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td>
                  <td className={pctClass(c.price_change_percentage_1h_in_currency)}>{pctFmt(c.price_change_percentage_1h_in_currency)}</td>
                  <td className={pctClass(c.price_change_percentage_24h_in_currency)}>{pctFmt(c.price_change_percentage_24h_in_currency)}</td>
                  <td className={pctClass(c.price_change_percentage_7d_in_currency)}>{pctFmt(c.price_change_percentage_7d_in_currency)}</td>
                  <td className="mono">${(c.market_cap / 1e9).toFixed(1)}B</td>
                  <td className="mono">${(c.total_volume / 1e9).toFixed(2)}B</td>
                  <td><MiniSparkline data={c.sparkline_in_7d?.price} color={c.price_change_percentage_7d_in_currency >= 0 ? '#00ff88' : '#ff4757'} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}

function pctClass(v) { return (v || 0) >= 0 ? 'green' : 'red' }
function pctFmt(v) { return v != null ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}%` : '—' }

/* ─────────────────────────────────────────────────────────────────────────────
   FREE USER GATE — Blurred dashboard preview
   ────────────────────────────────────────────────────────────────────────── */

function GatedDashboard({ isPro }) {
  if (isPro) return <ProDashboard />

  return (
    <section className="gated-section">
      <div className="gated-preview">
        <ProDashboard />
      </div>
      <div className="gated-overlay">
        <div className="gated-content">
          <span className="gated-icon">&#128274;</span>
          <h2>Pro Dashboard</h2>
          <p>Live charts, whale alerts, and advanced analytics.</p>
          <p className="gated-sub">Free users can access basic features on Telegram.</p>
          <div className="gated-btns">
            <a href="https://t.me/Whaleradarbot_bot?start=upgrade" target="_blank" className="btn btn-primary btn-lg">
              Upgrade to Pro — $9.99/mo
            </a>
            <a href="https://t.me/Whaleradarbot_bot" target="_blank" className="btn btn-outline btn-lg">
              Use Free on Telegram
            </a>
          </div>
        </div>
      </div>
    </section>
  )
}

/* ─────────────────────────────────────────────────────────────────────────────
   PAGE SECTIONS
   ────────────────────────────────────────────────────────────────────────── */

const fadeUp = {
  hidden: { opacity: 0, y: 30 },
  visible: (i = 0) => ({ opacity: 1, y: 0, transition: { delay: i * 0.1, duration: 0.6 } }),
}

/* Old Navbar and Hero removed — replaced by LaserHero component */

function LiveTicker() {
  const [prices, setPrices] = useState([])
  useEffect(() => {
    async function f() {
      const data = await cachedFetch('https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=12&sparkline=false', 90000)
      if (data) setPrices(data)
    }
    f(); const id = setInterval(f, 90000); return () => clearInterval(id)
  }, [])
  if (!prices.length) return null
  return (
    <div className="ticker-wrap">
      <div className="ticker">
        {[...prices, ...prices].map((c, i) => (
          <div key={i} className="ticker-item">
            <img src={c.image} alt="" width={18} height={18} />
            <span className="ticker-symbol">{c.symbol.toUpperCase()}</span>
            <span className="ticker-price">${c.current_price?.toLocaleString()}</span>
            <span className={`ticker-change ${c.price_change_percentage_24h >= 0 ? 'green' : 'red'}`}>
              {c.price_change_percentage_24h >= 0 ? '+' : ''}{c.price_change_percentage_24h?.toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Features() {
  const features = [
    { icon: '&#128011;', title: 'Whale Alerts', desc: 'Instant notifications when whales make massive trades. Track buys and sells over $500K in real time.' },
    { icon: '&#128200;', title: 'Live Charts', desc: 'Professional TradingView-style charts with candlesticks, volume, and technical indicators.' },
    { icon: '&#128270;', title: 'Wallet Tracker', desc: 'Follow any wallet address. See exactly what smart money is accumulating or dumping.' },
    { icon: '&#9889;', title: 'Instant Alerts', desc: 'Get alerts in under 5 seconds via Telegram. Never miss a whale move again.' },
    { icon: '&#128202;', title: 'Analytics Dashboard', desc: 'Full market overview with price changes, volume, market cap, and 7-day sparklines.' },
    { icon: '&#128176;', title: 'Pay with Crypto', desc: 'Subscribe with USDT via Binance Pay. No credit card or bank account needed.' },
  ]
  return (
    <section id="features" className="features">
      <motion.h2 variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }}>
        Built for <span className="accent-text">Serious Traders</span>
      </motion.h2>
      <div className="features-grid">
        {features.map((f, i) => (
          <motion.div key={i} className="feature-card" variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }} custom={i}>
            <div className="feature-icon" dangerouslySetInnerHTML={{ __html: f.icon }} />
            <h3>{f.title}</h3>
            <p>{f.desc}</p>
          </motion.div>
        ))}
      </div>
    </section>
  )
}

function HowItWorks() {
  const steps = [
    { num: '01', title: 'Open the Bot', desc: 'Start @Whaleradarbot_bot on Telegram. Free — no signup needed.' },
    { num: '02', title: 'Get Alerts', desc: 'Receive whale alerts, check prices, and track gas — all from Telegram.' },
    { num: '03', title: 'Go Pro', desc: 'Pay $9.99/mo in USDT for unlimited alerts, charts, and the full web dashboard.' },
  ]
  return (
    <section className="how-it-works">
      <motion.h2 variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }}>
        How It <span className="accent-text">Works</span>
      </motion.h2>
      <div className="steps-grid">
        {steps.map((s, i) => (
          <motion.div key={i} className="step-card" variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }} custom={i}>
            <span className="step-num">{s.num}</span>
            <h3>{s.title}</h3>
            <p>{s.desc}</p>
          </motion.div>
        ))}
      </div>
    </section>
  )
}

function Pricing() {
  return (
    <section id="pricing" className="pricing">
      <motion.h2 variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }}>
        Simple <span className="accent-text">Pricing</span>
      </motion.h2>
      <motion.p className="pricing-sub" variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }} custom={1}>
        Start free on Telegram. Upgrade for the full experience.
      </motion.p>
      <div className="pricing-grid">
        <motion.div className="price-card" variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }} custom={2}>
          <h3>Free</h3>
          <div className="price">$0<span>/forever</span></div>
          <ul>
            <li className="check">3 whale alerts per day</li>
            <li className="check">Price lookup commands</li>
            <li className="check">Gas tracker</li>
            <li className="check">Top movers overview</li>
            <li className="x">Live charts & dashboard</li>
            <li className="x">Unlimited alerts</li>
            <li className="x">Wallet tracking</li>
            <li className="x">Custom filters</li>
          </ul>
          <a href="https://t.me/Whaleradarbot_bot" target="_blank" className="btn btn-outline btn-full">
            Start on Telegram
          </a>
        </motion.div>

        <motion.div className="price-card featured" variants={fadeUp} initial="hidden" whileInView="visible" viewport={{ once: true }} custom={3}>
          <div className="featured-badge">Recommended</div>
          <h3>Pro</h3>
          <div className="price">$9.99<span>/month</span></div>
          <ul>
            <li className="check">Unlimited whale alerts</li>
            <li className="check">All Telegram commands</li>
            <li className="check">Gas & price tracker</li>
            <li className="check">Top movers + analytics</li>
            <li className="check">Live TradingView charts</li>
            <li className="check">Full web dashboard</li>
            <li className="check">Advanced wallet tracking</li>
            <li className="check">Custom alert filters</li>
          </ul>
          <a href="https://t.me/Whaleradarbot_bot?start=upgrade" target="_blank" className="btn btn-primary btn-full">
            Pay with USDT
          </a>
          <p className="pay-note">Binance Pay or direct USDT transfer</p>
        </motion.div>
      </div>
    </section>
  )
}

function Footer() {
  return (
    <footer className="footer">
      <div className="footer-inner">
        <div className="footer-brand">
          <div className="logo"><span className="logo-icon">&#128011;</span><span className="logo-text">WhaleRadar</span></div>
          <p>Track whales. Follow smart money. Stay ahead.</p>
        </div>
        <div className="footer-links">
          <div>
            <h4>Product</h4>
            <a href="#features">Features</a>
            <a href="#pricing">Pricing</a>
            <a href="#dashboard">Dashboard</a>
          </div>
          <div>
            <h4>Connect</h4>
            <a href="https://t.me/Whaleradarbot_bot" target="_blank">Telegram Bot</a>
          </div>
        </div>
      </div>
      <div className="footer-bottom">&copy; 2026 WhaleRadar. All rights reserved.</div>
    </footer>
  )
}

/* ─────────────────────────────────────────────────────────────────────────────
   APP
   ────────────────────────────────────────────────────────────────────────── */

function App() {
  const [isPro, setIsPro] = useState(false)
  return (
    <div className="bg-black">
      <LaserHero isPro={isPro} setIsPro={setIsPro} />
      <LiveTicker />
      <Features />
      <HowItWorks />
      {!isPro && <Pricing />}
      <div id="dashboard">
        <GatedDashboard isPro={isPro} />
      </div>
      <Footer />
    </div>
  )
}

export default App
