// The pages, for the App (fetch /api) and the export (window.WS_SNAPSHOT). Classic script, hash routes.
(function () {
  "use strict";
  var SNAP = window.WS_SNAPSHOT || null;
  var main = document.getElementById("main");

  function wsNow() { return window.WS_NOW ? new Date(window.WS_NOW) : new Date(); }
  function isoNow() { return wsNow().toISOString().slice(0, 19) + "Z"; }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function add(parent) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]); return parent; }
  function hhmm(iso) { return iso ? String(iso).slice(11, 16) + " UTC" : "unknown"; }

  // One seam: the App reads /api, the export reads the snapshot it was built with.
  function load(path) {
    if (!SNAP) {
      return fetch("api/" + path, { headers: { accept: "application/json" } }).then(function (r) {
        if (!r.ok) throw new Error(path + ": " + r.status);
        return r.json();
      });
    }
    var p = SNAP.payload, env = function (data) { return { data: data, status: p.status, snapshot_built_at: SNAP.built_at, read_at: SNAP.read_at }; };
    var m;
    if (path === "status") return Promise.resolve(env(p.status));
    if (path === "sources") return Promise.resolve(env(p.sources));
    if (path === "outlook") return Promise.resolve(env(p.outlook));
    if (path === "index") return Promise.resolve(env(p.index));
    if (path === "runs") return Promise.resolve(env(p.runs));
    if (path === "intraday") return Promise.resolve(env(p.intraday || { bars: [], calls: [] }));
    if (path === "calls?horizon=0") {
      var last = {};
      (p.history || []).forEach(function (c) {
        if (c.horizon_days === 0 && (!last[c.target_date] || c.as_of > last[c.target_date].as_of)) last[c.target_date] = c;
      });
      return Promise.resolve(env(Object.keys(last).sort().map(function (k) { return last[k]; })));
    }
    if ((m = path.match(/^calls\/([^/]+)\/(\d{4}-\d{2}-\d{2})$/))) {
      var rows = p.evidence.filter(function (c) { return c.as_of === m[1] && c.target_date === m[2]; });
      rows.forEach(function (c) { c.trace_summary = (p.traces || {})[c.trace_id] || null; });
      return Promise.resolve(env(rows));
    }
    return Promise.reject(new Error("not in this snapshot: " + path));
  }

  function integrityOf(call, status, now) {
    var row = Object.assign({}, call, {
      open_incident: status ? status.open_incident : null,
      post_verified_as_of: status ? status.post_verified_as_of : null,
      guarded_tables: status ? status.guarded_tables : null,
      read_at: status ? status.read_at : null
    });
    return window.integrityState(row, now);
  }

  function badge(call, status) {
    var now = integrityOf(call, status, isoNow());
    var then = integrityOf(call, status, status && status.read_at);
    var b = el("span", "badge " + now, window.integrityLabel(now, status && status.read_at));
    var wrap = el("span");
    add(wrap, b, el("span", "at-read", "at snapshot, " + hhmm(status && status.read_at) + ": " + window.integrityLabel(then, status && status.read_at)));
    return wrap;
  }

  // U3: only the agent's action is a call.
  function callText(c) {
    if (c.shown_action) return { cls: c.shown_action, text: c.shown_action };
    var age = (wsNow() - Date.parse(c.as_of)) / 60e3;
    if (age >= 0 && age < 41) return { cls: "none", text: "awaiting agent (runs at :40)" };
    if (c.run_outcome && c.run_outcome !== "ok") return { cls: "none", text: "no agent call: " + c.run_outcome };
    if (c.run_outcome === "ok") return { cls: "none", text: "no agent call: run ok, no row" };
    return { cls: "none", text: "no agent call: no run recorded" };
  }

  // signal.reasons, the model's codes for a forced hold, as short phrases; an unknown code shows as is.
  var REASONS = {
    horizon_unavailable: "no forecast this far ahead",
    stale_unknown: "inputs missing at call time",
    forecast_quarantined: "forecast quarantined by the guard",
    no_normal: "no climate normal for this date",
    stale_forecast: "forecast not available in time",
    stale_prices: "prices not available in time",
    stale_input: "observed weather over 14 days old"
  };
  // A null anomaly names its cause from the hold reasons; "no normal" only when the model said so.
  var NO_ANOMALY = { no_normal: "no normal", horizon_unavailable: "no forecast this far ahead",
    forecast_quarantined: "forecast quarantined", stale_forecast: "forecast not in time" };
  function anomalyText(c) {
    var a = c.forecast_anomaly_c;
    if (a !== null && a !== undefined) return "anomaly " + Number(a).toFixed(2) + " C";
    var rs = (c.reasons || []).filter(function (r) { return NO_ANOMALY[r]; });
    return rs.length ? NO_ANOMALY[rs[0]] : "anomaly not recorded";
  }
  function holdReason(c) {
    if (c.signal_action !== "hold") return null;
    var rs = c.reasons || [];
    if (!rs.length) return "the model's own call, inputs complete";
    return rs.map(function (r) { return REASONS[r] || r; }).join("; ");
  }

  function sourcesStrip(rows) {
    var s = el("div", "strip");
    (rows || []).forEach(function (r) {
      var t = r.kind === "source" ? r.name + ": " + (r.label || r.state) + (r.at ? " " + String(r.at).slice(0, 16) : "")
        : r.kind === "fresh" ? r.layer + " " + r.name + ": " + (r.label || "") + " " + String(r.at || "none").slice(0, 10)
        : r.kind === "update" ? "pipeline update " + (r.state || "unknown")
        : r.counted ? r.name + ": " + r.n + " rows checked, " + r.n_bad + " runs quarantined (" + r.label + ")"
        : r.name + ": " + r.n + " rows checked, " + r.n_bad + " violations (" + (r.label || r.state) + ")";
      add(s, el("span", "chip " + (r.state === "stale" || (r.n_bad > 0 && !r.counted) ? "bad" : ""), t));
    });
    return s;
  }

  function outlook() {
    return Promise.all([load("outlook"), load("sources")]).then(function (res) {
      var o = res[0], status = o.status;
      main.textContent = "";
      add(main, el("h2", null, "Outlook"), sourcesStrip(res[1].data));
      if (!o.data || !o.data.length) { add(main, el("p", null, "No outlook yet.")); return; }
      add(main, el("p", "mode", "As of " + o.data[0].as_of + ". The newest outlook reads awaiting guard check until the next check; open the previous hour's call for a checked one."));
      outlookChart(o.data.slice().sort(function (a, b) { return a.target_date < b.target_date ? -1 : 1; }));
      var cards = el("div", "cards");
      o.data.forEach(function (c) {
        var card = el("div", "card"), a = el("a");
        a.href = "#/call/" + encodeURIComponent(c.as_of) + "/" + c.target_date;
        var ct = callText(c);
        add(a, el("div", "date", c.target_date + "  (h" + c.horizon_days + ")"), el("div", "call " + ct.cls, ct.text),
          el("div", "signal", "model signal: " + c.signal_action + ", " + anomalyText(c)),
          holdReason(c) ? el("div", "signal reason", "hold reason: " + holdReason(c)) : null,
          c.downgrade_reason ? el("div", "signal", "downgraded: " + c.downgrade_reason) : null);
        add(card, a, badge(c, status));
        cards.appendChild(card);
      });
      main.appendChild(cards);
    });
  }

  function callPage(asOf, date) {
    return load("calls/" + asOf + "/" + date).then(function (r) {
      main.textContent = "";
      add(main, el("h2", null, "Call for " + date + ", made " + asOf));
      if (!r.data || !r.data.length) { add(main, el("p", null, "This call is not in the snapshot.")); return; }
      r.data.forEach(function (c) {
        var dl = el("dl"), ct = callText(c);
        [["call", ct.text], ["integrity", null], ["model signal", c.signal_action], ["hold reason", holdReason(c) || "not a hold"],
         ["confidence", c.signal_confidence],
         ["forecast anomaly (C)", c.forecast_anomaly_c === null || c.forecast_anomaly_c === undefined ? anomalyText(c) : c.forecast_anomaly_c],
         ["forecast known at", c.forecast_known_at], ["last close", c.last_close], ["5-day return", c.ret_5d],
         ["model", c.model_name + " " + c.model_version], ["downgrade", c.downgrade_reason], ["audit", c.audit_problem || "none"],
         ["written at", c.written_at], ["rationale", c.rationale],
         ["trace", c.trace_summary ? (c.trace_summary.unavailable ? "unavailable: " + c.trace_summary.unavailable
           : c.trace_summary.spans.map(function (s) { return s.name + " " + s.ms + " ms"; }).join(", ")) : "untraced"]
        ].forEach(function (kv) {
          add(dl, el("dt", null, kv[0]), kv[1] === null && kv[0] === "integrity" ? add(el("dd"), badge(c, r.status)) : el("dd", null, kv[1] === undefined || kv[1] === null ? "none" : kv[1]));
        });
        main.appendChild(dl);
      });
    });
  }

  // Owner, 2026-11-18: date on x, temperature anomaly on the left y, stock price on the right y,
  // trigger points as dots on the price line; each y axis takes its series' colour.
  var COLOR = { temp: "#c2621a", usd: "#3a5f9a", buy: "#1f8a3b", sell: "#c0392b" };
  // Backtest dots are smaller and lighter than live calls, so a live call stays readable on top.
  var LIGHT = { buy: "rgba(31, 138, 59, 0.45)", sell: "rgba(192, 57, 43, 0.45)" };
  var BACKTEST = "backtest (walk-forward threshold model)";
  var YEAR = 365 * 86400;
  function day(t) { return new Date(t * 1000).toISOString().slice(0, 10); }
  // The traded stock is read from the rows (or the snapshot); the page names no ticker itself.
  function tickerOf(rows) {
    for (var i = 0; i < rows.length; i++) if (rows[i] && rows[i].ticker) return rows[i].ticker;
    return SNAP && SNAP.payload.ticker ? SNAP.payload.ticker : "Stock";
  }
  // Ticks a year or more apart read as years, a month or more as months, otherwise as days.
  function tick(incr) { return incr >= 300 * 86400 ? 4 : incr >= 28 * 86400 ? 7 : 10; }
  // A line breaks only across more than 7 days, so weekends without a close stay joined.
  function weekGaps(u, si, i0, i1, gaps) {
    var x0 = u.data[0][0], week = u.valToPos(x0 + 7 * 86400, "x", true) - u.valToPos(x0, "x", true);
    return gaps.filter(function (g) { return g[1] - g[0] > week; });
  }
  function usd(v) { return "$" + v.toFixed(v < 100 ? 2 : 0); }
  function cents(v) { return "$" + v.toFixed(2); }
  function degC(v) { return (v > 0 ? "+" : "") + v.toFixed(1) + " °C"; }
  window.WS_CHARTS = [];

  // A trigger is an agent call (solid dot) or, where the agent made none, the model signal (ring); holds are omitted.
  function trigger(c) {
    if (c.shown_action === "buy" || c.shown_action === "sell") return { action: c.shown_action, by: "agent call" };
    if (!c.shown_action && (c.signal_action === "buy" || c.signal_action === "sell")) return { action: c.signal_action, by: "model signal" };
    return null;
  }

  // xs: unix seconds; temp, price: values per x; trig: per x, null or {action, by, confidence, anomaly}.
  function twoAxis(box, o) {
    var font = "12px system-ui, sans-serif", bold = "bold 12px system-ui, sans-serif";
    var kinds = [["buy", "agent call"], ["sell", "agent call"], ["buy", "model signal"], ["sell", "model signal"]];
    var data = [o.xs, o.temp, o.price], series = [
      { label: "Date", value: function (u, v) { return v === null ? "none" : day(v); } },
      { label: o.tempLabel, scale: "temp", stroke: COLOR.temp, width: 2, spanGaps: false, gaps: weekGaps, points: { show: false },
        value: function (u, v) { return v === null ? "none" : degC(v); } },
      { label: o.priceLabel, scale: "usd", stroke: COLOR.usd, width: 2, spanGaps: false, gaps: weekGaps, dash: o.priceDash, points: { show: false },
        value: function (u, v) { return v === null ? "none" : usd(v); } }
    ];
    kinds.forEach(function (k) {
      data.push(o.trig.map(function (t, i) { return t && t.action === k[0] && t.by === k[1] ? o.price[i] : null; }));
      series.push({ label: k[0] + " (" + k[1] + ")", scale: "usd", stroke: COLOR[k[0]], paths: function () { return null; },
        points: { show: true, size: 10, width: 2, stroke: COLOR[k[0]], fill: k[1] === "agent call" ? COLOR[k[0]] : "#fff" },
        value: function (u, v, si, i) {
          var t = i === null || i === undefined ? null : o.trig[i];
          if (v === null || !t) return "none";
          return t.action + ", confidence " + (t.confidence === null || t.confidence === undefined ? "none" : Number(t.confidence).toFixed(2))
            + ", anomaly " + (t.anomaly === null || t.anomaly === undefined ? "none" : degC(Number(t.anomaly)));
        } });
    });
    if (o.bt) ["buy", "sell"].forEach(function (a) {
      data.push(o.bt.map(function (b, i) { return b && b.action === a ? o.price[i] : null; }));
      series.push({ label: a + ", " + BACKTEST, scale: "usd", stroke: LIGHT[a], paths: function () { return null; },
        points: { show: true, size: 5, width: 0, stroke: LIGHT[a], fill: LIGHT[a] },
        value: function (u, v, si, i) { return v === null || i === null || i === undefined || !o.bt[i] ? "none" : btText(o.bt[i]); } });
    });
    var u = new window.uPlot({ width: Math.min(1040, main.clientWidth || 900), height: 300,
      scales: { x: { time: true }, temp: { auto: true }, usd: { auto: true } },
      axes: [
        { label: "Date (UTC)", labelFont: bold, font: font, labelSize: 22, space: 90,
          stroke: getComputedStyle(document.body).color,
          values: function (u, vals, ax, space, incr) { return vals.map(function (v) { return day(v).slice(0, tick(incr)); }); } },
        { scale: "temp", side: 3, label: o.tempAxis, labelFont: bold, font: font, labelSize: 22, size: 70,
          stroke: COLOR.temp, values: function (u, vals) { return vals.map(degC); } },
        { scale: "usd", side: 1, label: o.priceAxis, labelFont: bold, font: font, labelSize: 22, size: 70,
          stroke: COLOR.usd, grid: { show: false }, values: function (u, vals) { return vals.map(usd); } }
      ],
      series: series }, data, box);
    window.WS_CHARTS.push(u);
    return u;
  }

  function btText(b) {
    var head = b.action + ", anomaly " + (b.anomaly_c === null || b.anomaly_c === undefined ? "none" : degC(Number(b.anomaly_c)))
      + " (" + b.input_kind + "), fit for " + b.target_date.slice(0, 4) + ": ";
    if (!b.model_coef) return head + "no fit yet";
    return head + "sign " + (b.model_coef > 0 ? "+1, warmer buys" : "-1, warmer sells") + ", threshold "
      + (b.model_threshold_c === null || b.model_threshold_c === undefined ? "none" : Number(b.model_threshold_c).toFixed(2) + " °C");
  }

  function pct(v) { return (100 * v).toFixed(1) + "%"; }
  // The App measures the run from its own rows (ws_ui/measure.py), so figures always match the dots.
  // Inside the 95% band is not evidence of no edge, only that random days could have done as well.
  function verdict(m) {
    return m.hit_rate > m.random_band[1] ? "above the random band"
      : m.hit_rate < m.random_band[0] ? "below the random band" : "not distinguishable from random";
  }
  function slice(m) {
    return pct(m.hit_rate) + " against " + pct(m.always_buy) + " for always buying (random band " + pct(m.random_band[0])
      + " to " + pct(m.random_band[1]) + "), " + verdict(m);
  }
  function inputs(m) {
    var tb = m.trades_by_input, by = m.by_input || {}, n = function (k) { return tb[k].toLocaleString("en-US"); };
    if (!tb || tb.forecast == null || tb.observed == null) return "Input kinds were not measured for this run.";
    return n("observed") + " of them used the latest observed ERA5 anomaly in the forecast's place and " + n("forecast")
      + " used a forecast" + (tb.observed > tb.forecast ? ", so the whole run measures mostly the observed anomaly, not forecast skill" : "")
      + ". " + (!tb.forecast ? "No trade used a forecast." : by.forecast ? "Forecast days alone: " + slice(by.forecast) + "."
        : "Forecast days alone: too few days to measure.");
  }
  function caption(bt, m) {
    var run = bt.length ? bt[0].backtest_run_id : null;
    if (!m || m.backtest_run_id !== run) return "No measured result is recorded for backtest run " + run + ".";
    return "Backtest " + run + ", " + m.first + " to " + m.last + ": " + m.n_trades.toLocaleString("en-US")
      + " buy or sell days matched the next close's direction " + slice(m) + ". " + inputs(m);
  }

  function rangeControl(u, box) {
    var bar = el("div", "range"), xs = u.data[0], hi = xs[xs.length - 1];
    [["All", 0], ["5 years", 5 * YEAR], ["1 year", YEAR], ["90 days", 90 * 86400]].forEach(function (r) {
      var b = el("button", null, r[0]);
      b.type = "button";
      b.addEventListener("click", function () { u.setScale("x", { min: r[1] ? Math.max(xs[0], hi - r[1]) : xs[0], max: hi }); });
      bar.appendChild(b);
    });
    add(bar, el("span", "mode", " Drag across the chart to zoom; double-click to see it all."));
    box.insertBefore(bar, box.firstChild);
  }

  function historyChart(index, calls, bt, measured) {
    // Break the lines across a gap of more than 7 days instead of joining them.
    var xs = [], temp = [], price = [], trig = [], tests = bt ? [] : null, byDate = {}, btDate = {}, prev = null;
    calls.forEach(function (c) { byDate[c.target_date] = c; });
    (bt || []).forEach(function (b) { if (b.action === "buy" || b.action === "sell") btDate[b.target_date] = b; });
    index.forEach(function (r) {
      var t = Date.parse(r.date + "T00:00:00Z") / 1000;
      if (prev !== null && t - prev > 7 * 86400) { xs.push(prev + 86400); temp.push(null); price.push(null); trig.push(null); if (tests) tests.push(null); }
      var c = byDate[r.date], g = c ? trigger(c) : null;
      xs.push(t); temp.push(r.anomaly_c === undefined ? null : r.anomaly_c); price.push(r.close === undefined ? null : r.close);
      trig.push(g && price[price.length - 1] !== null ? { action: g.action, by: g.by, confidence: c.signal_confidence, anomaly: r.anomaly_c } : null);
      if (tests) tests.push(btDate[r.date] && price[price.length - 1] !== null ? btDate[r.date] : null);
      prev = t;
    });
    var box = el("div", "chart"), tk = tickerOf(index.concat(bt || []));
    add(main, el("h3", null, tk + " close and temperature anomaly, with past trigger points"), box);
    if (!xs.length) { add(box, el("p", "mode", "No data in this range.")); return; }
    var u = twoAxis(box, { xs: xs, temp: temp, price: price, trig: trig, bt: tests, tempLabel: "Temperature index anomaly",
      tempAxis: "Temperature anomaly (°C)", priceLabel: tk + " close", priceAxis: tk + " close (USD)" });
    rangeControl(u, box);
    if (!temp.some(function (v) { return v !== null; })) add(box, el("p", "mode", "No temperature anomaly in this range yet."));
    add(box, el("p", "caption", bt ? caption(bt, measured) : "The backtest is read live by the App; this page shows the last 400 days without it."));
  }

  function outlookChart(rows) {
    var xs = [], temp = [], price = [], trig = [];
    var close = rows.map(function (c) { return c.last_close; }).filter(function (v) { return v !== null && v !== undefined; })[0];
    close = close === undefined ? null : close;
    rows.forEach(function (c) {
      var g = trigger(c);
      xs.push(Date.parse(c.target_date + "T00:00:00Z") / 1000);
      temp.push(c.forecast_anomaly_c === undefined ? null : c.forecast_anomaly_c);
      price.push(close);
      trig.push(g && close !== null ? { action: g.action, by: g.by, confidence: c.signal_confidence, anomaly: c.forecast_anomaly_c } : null);
    });
    var box = el("div", "chart");
    add(main, el("h3", null, "Next 7 days: forecast anomaly and recommended trigger points"), box);
    twoAxis(box, { xs: xs, temp: temp, price: price, trig: trig, tempLabel: "Forecast anomaly",
      tempAxis: "Forecast anomaly (°C)", priceLabel: "Last close, carried forward (not a forecast)", priceDash: [6, 4],
      priceAxis: tickerOf(rows) + " close (USD)" });
    add(box, el("p", "mode", "The price axis shows the last close carried forward; the model gives no price path, so none is drawn."));
  }

  function history() {
    var bt = load("backtest").catch(function () { return null; });
    return Promise.all([load("index"), load("calls?horizon=0"), bt]).then(function (res) {
      main.textContent = "";
      var full = res[2] && res[2].data ? res[2].data : null, tests = full ? full.backtest || [] : [];
      var rows = ((full ? full.index : res[0].data) || []).slice().sort(function (a, b) { return a.date < b.date ? -1 : 1; });
      var tk = tickerOf(rows.concat(tests)), first = tests.length ? tests[0].target_date : null;
      add(main, el("h2", null, "History"), el("p", "mode", (full ? tk + " closes and the temperature index anomaly since " + first + ", the backtest's first session."
        : "The last 400 days of " + tk + " closes and the temperature index anomaly.")
        + " Large dots are the horizon-0 trigger points: filled for an agent call, a ring for a model signal where the agent made none. Small light dots are the "
        + BACKTEST + ". Holds are not drawn. A blank stretch has no data; nothing is joined across it."));
      historyChart(rows, res[1].data || [], full ? tests : null, full ? full.measured : null);
    });
  }

  // Intraday (display only): hourly closes of recent sessions with the horizon-0 calls made in them.
  var MARK = { buy: COLOR.buy, sell: COLOR.sell, hold: "#8a6d1c" };
  function callAction(k) { return k.shown_action || k.signal_action || null; }
  function estText(k) {
    if (k.est_price !== null) return cents(Number(k.est_price));
    return k.est_reason ? "- " + k.est_reason : "-";
  }
  // Pure: x is every bar start and call time (unix s); a call sits on the last close before it.
  function intradaySeries(bars, calls) {
    var at = {}, xs = [];
    bars.forEach(function (bar) { at[Date.parse(bar.bar_start) / 1000] = bar.close; });
    calls.forEach(function (k) { var t = Date.parse(k.as_of) / 1000; if (!(t in at)) at[t] = null; });
    Object.keys(at).forEach(function (t) { xs.push(Number(t)); });
    xs.sort(function (a, b) { return a - b; });
    var byX = {}, last = null, out = { xs: xs, close: [], est: [], marks: { buy: [], sell: [], hold: [] } };
    calls.forEach(function (k) { byX[Date.parse(k.as_of) / 1000] = k; });
    xs.forEach(function (t) {
      var seen = last;
      if (at[t] !== null) last = at[t];
      var k = byX[t], a = k ? callAction(k) : null, y = seen !== null ? seen : last;
      out.close.push(at[t]);
      out.est.push(k && k.est_price !== null ? Number(k.est_price) : null);
      ["buy", "sell", "hold"].forEach(function (m) { out.marks[m].push(a === m ? y : null); });
    });
    out.calls = byX;
    return out;
  }
  // A line breaks only across more than 3 hours, so a session's bars stay joined and nights do not.
  function sessionGaps(u, si, i0, i1, gaps) {
    var x0 = u.data[0][0], span = u.valToPos(x0 + 3 * 3600, "x", true) - u.valToPos(x0, "x", true);
    return gaps.filter(function (g) { return g[1] - g[0] > span; });
  }
  function intradayChart(box, s, tk) {
    var font = "12px system-ui, sans-serif", bold = "bold 12px system-ui, sans-serif";
    var hm = function (v) { return new Date(v * 1000).toISOString().slice(5, 16).replace("T", " "); };
    var data = [s.xs, s.close], series = [
      { label: "Time (UTC)", value: function (u, v) { return v === null ? "none" : hm(v) + " UTC"; } },
      { label: tk + " hourly close", scale: "usd", stroke: COLOR.usd, width: 2, spanGaps: true, gaps: sessionGaps,
        points: { show: false }, value: function (u, v) { return v === null ? "none" : usd(v); } }
    ];
    ["buy", "sell", "hold"].forEach(function (m) {
      data.push(s.marks[m]);
      series.push({ label: m + " call", scale: "usd", stroke: MARK[m], paths: function () { return null; },
        points: { show: true, size: 10, width: 2, stroke: MARK[m], fill: MARK[m] },
        value: function (u, v, si, i) { var k = s.calls[s.xs[i]]; return v === null || !k ? "none" : m + ", est. price " + estText(k); } });
    });
    data.push(s.est);
    series.push({ label: "est_price", scale: "usd", stroke: COLOR.temp, paths: function () { return null; },
      points: { show: true, size: 9, width: 2, stroke: COLOR.temp, fill: "#fff" },
      value: function (u, v) { return v === null ? "none" : usd(v); } });
    var u = new window.uPlot({ width: Math.min(1040, main.clientWidth || 900), height: 300,
      scales: { x: { time: true }, usd: { auto: true } },
      axes: [
        { label: "Time (UTC)", labelFont: bold, font: font, labelSize: 22, space: 90, stroke: getComputedStyle(document.body).color,
          values: function (u, vals) { return vals.map(hm); } },
        { scale: "usd", side: 3, label: tk + " price (USD)", labelFont: bold, font: font, labelSize: 22, size: 70,
          stroke: COLOR.usd, values: function (u, vals) { return vals.map(usd); } }
      ],
      series: series }, data, box);
    window.WS_CHARTS.push(u);
    return u;
  }

  function intraday() {
    return load("intraday").then(function (r) {
      main.textContent = "";
      var d = r.data || {}, bars = d.bars || [], calls = d.calls || [], tk = tickerOf(bars);
      add(main, el("h2", null, "Intraday"), el("p", "mode", "Hourly " + tk + " closes for the last five sessions, from Yahoo's 60-minute chart,"
        + " for display only: every call is made from daily closes, and no model input reads these bars."
        + " Dots are the horizon-0 calls made in each session (the agent's call, else the model signal), drawn at the last close before the call;"
        + " a hollow dot is that call's estimated price."));
      var box = el("div", "chart");
      main.appendChild(box);
      if (!bars.length) add(box, el("p", "mode", "No intraday bars yet: they land hourly during XNYS sessions."));
      else intradayChart(box, intradaySeries(bars, calls), tk);
      var t = el("table"), h = el("tr");
      ["as_of", "target_date", "call", "model signal", "last close", "est. price"].forEach(function (x) { h.appendChild(el("th", null, x)); });
      t.appendChild(h);
      calls.slice().reverse().forEach(function (k) {
        var tr = el("tr");
        [k.as_of, k.target_date, k.shown_action || "no agent call", k.signal_action,
         k.last_close === null || k.last_close === undefined ? "-" : cents(Number(k.last_close)), estText(k)
        ].forEach(function (v) { tr.appendChild(el("td", null, v)); });
        t.appendChild(tr);
      });
      add(main, el("h3", null, "Recent calls"), calls.length ? t : el("p", "mode", "No calls in these sessions."));
    });
  }

  function runs() {
    return load("runs").then(function (r) {
      main.textContent = "";
      var t = el("table"), h = el("tr");
      ["as_of", "started_at", "route", "outcome", "duration_s"].forEach(function (k) { h.appendChild(el("th", null, k)); });
      t.appendChild(h);
      (r.data || []).slice().reverse().forEach(function (x) {
        var tr = el("tr");
        ["as_of", "started_at", "route", "outcome", "duration_s"].forEach(function (k) { tr.appendChild(el("td", null, x[k])); });
        t.appendChild(tr);
      });
      add(main, el("h2", null, "Agent runs"), t);
    });
  }

  function route() {
    var h = decodeURIComponent(location.hash || "");
    var m = h.match(/^#\/call\/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\/(\d{4}-\d{2}-\d{2})$/);
    var p = m ? callPage(m[1], m[2]) : h === "#/history" ? history() : h === "#/runs" ? runs() : h === "#/intraday" ? intraday() : null;
    if (!p) { if (h !== "#/outlook") { location.hash = "#/outlook"; return; } p = outlook(); }
    p.catch(function (e) { main.textContent = ""; add(main, el("p", null, "Could not load: " + e.message)); });
  }

  var mode = document.getElementById("mode");
  mode.textContent = SNAP
    ? "Static snapshot built " + SNAP.built_at + ". Integrity is shown as of the snapshot; after 75 minutes the live state is unknown. Live drill-down is not available here. A viewer clock up to 5 minutes behind the snapshot is tolerated."
    : "Live App over the hourly snapshot; integrity is computed as you view it.";
  window.addEventListener("hashchange", route);
  route();
})();
