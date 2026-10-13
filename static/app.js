/* Takeback front-end helpers, loaded on every page: light/dark theme,
   French number formatting, the bar- and line-chart builders every
   dashboard uses (palette, mark styling, value labels, table view,
   click-to-drill), and
   the side panel a drill-down opens in. Plain JS, no build step. */
(function () {
  'use strict';

  // Categorical palette, light and dark steps of the same eight hues, in a
  // fixed order validated for colour-vision deficiencies (adjacent pairs).
  // A series keeps its slot whatever the filters: colour follows the
  // entity, never its rank in the current view. Past seven named series the
  // tail is folded server-side into "Autres", drawn in neutral gray.
  var PALETTE = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
    dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  };
  var CHROME = {
    light: { surface: '#fcfcfb', ink: '#0b0b0b', ink2: '#52514e', muted: '#898781', grid: '#e1e0d9', axis: '#c3c2b7', other: '#a3a29c' },
    dark: { surface: '#1a1a19', ink: '#ffffff', ink2: '#c3c2b7', muted: '#898781', grid: '#2c2c2a', axis: '#383835', other: '#6f6e69' },
  };
  var RETURNS_SLOT = 7; // red: refunds drawn below the baseline

  function mode() {
    return document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
  }
  function token(name) { return CHROME[mode()][name]; }
  function slotColor(slot) {
    if (slot === 'other' || slot === null || slot === undefined) return token('other');
    return PALETTE[mode()][slot % PALETTE.light.length];
  }
  // The same palette as CSS variables (--tb-slot-0...) for marks drawn in
  // HTML rather than on a canvas (the career timeline), kept in step with
  // the theme.
  function slotVars() {
    var style = document.documentElement.style;
    PALETTE[mode()].forEach(function (color, i) { style.setProperty('--tb-slot-' + i, color); });
    style.setProperty('--tb-slot-other', token('other'));
  }
  slotVars();

  // ── Formatting ────────────────────────────────────────────────────────
  var formatters = {};
  function formatter(key, options) {
    if (!formatters[key]) formatters[key] = new Intl.NumberFormat('fr-FR', options);
    return formatters[key];
  }
  function formatMinutes(total) {
    total = Math.round(total);
    var h = Math.floor(total / 60), m = total % 60;
    if (!h) return m + ' min';
    return m ? h + ' h ' + (m < 10 ? '0' : '') + m : h + ' h';
  }

  // Durations read best on whole-hour (or half-hour...) gridlines, not on
  // the base-10 steps Chart.js picks by default (200 min = "3 h 20").
  var MINUTE_STEPS = [5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440, 2880, 7200, 14400];
  function minuteStep(cfg) {
    var max = 0;
    cfg.labels.forEach(function (_, i) {
      var total = 0;
      cfg.series.forEach(function (s) {
        if (s.hidden) return;
        var v = s.data[i] || 0;
        if (cfg.stacked) total += Math.max(v, 0);
        else max = Math.max(max, v);
      });
      max = Math.max(max, total);
    });
    for (var i = 0; i < MINUTE_STEPS.length; i++) {
      if (max / MINUTE_STEPS[i] <= 6) return MINUTE_STEPS[i];
    }
    return Math.ceil(max / 6 / 1440) * 1440;
  }
  function fmt(value, kind, currency) {
    if (value === null || value === undefined || isNaN(value)) return '';
    if (kind === 'money') {
      return currency
        ? formatter('m' + currency, { style: 'currency', currency: currency }).format(value)
        : formatter('m', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
    }
    if (kind === 'minutes') return formatMinutes(value);
    if (kind === 'percent') return formatter('p', { style: 'percent', maximumFractionDigits: 1 }).format(value / 100);
    return formatter('n', { maximumFractionDigits: 1 }).format(value);
  }
  function fmtCompact(value, kind, currency) {
    if (kind === 'minutes') return formatMinutes(value);
    if (kind === 'percent') return formatter('pc', { style: 'percent', maximumFractionDigits: 0 }).format(value / 100);
    if (kind === 'money' && currency) {
      return formatter('mc' + currency, { style: 'currency', currency: currency, notation: 'compact', maximumFractionDigits: 1 }).format(value);
    }
    return formatter('nc', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
  }

  // ── URLs ──────────────────────────────────────────────────────────────
  function url(base, params) {
    var query = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      (Array.isArray(value) ? value : [value]).forEach(function (v) {
        if (v !== null && v !== undefined && v !== '') query.append(key, v);
      });
    });
    var qs = query.toString();
    return base + (qs ? (base.indexOf('?') >= 0 ? '&' : '?') + qs : '');
  }

  // ── Theme ─────────────────────────────────────────────────────────────
  function setTheme(theme) {
    document.documentElement.setAttribute('data-bs-theme', theme);
    try { localStorage.setItem('tb-theme', theme); } catch (e) { /* private mode */ }
    slotVars();
    retheme();
  }

  // ── Privacy mode (screen sharing) ─────────────────────────────────────
  // The class on <html> does the blurring (style.css); it is also set
  // before first paint by base.html. Kept across pages in localStorage.
  function syncPrivacyButtons(on) {
    document.querySelectorAll('[data-tb-privacy-toggle]').forEach(function (btn) {
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', String(on));
    });
  }
  function privacyOn() {
    return document.documentElement.classList.contains('tb-privacy');
  }
  // Charts keep their shape (bars, lines, gridlines) and lose what reads as
  // data: tick values and dates, legend names, tooltips. Applied to the
  // options of every chart, and again when the mode changes.
  function privacyOptions(options) {
    var show = !privacyOn();
    Object.keys(options.scales || {}).forEach(function (key) {
      options.scales[key].ticks.display = show;
    });
    var legend = options.plugins.legend;
    if (legend.$display === undefined) legend.$display = legend.display;
    legend.display = show && legend.$display;
    options.plugins.tooltip.enabled = show;
    return options;
  }
  function setPrivacy(on) {
    document.documentElement.classList.toggle('tb-privacy', on);
    try { localStorage.setItem('tb-privacy', on ? '1' : '0'); } catch (e) { /* private mode */ }
    syncPrivacyButtons(on);
    Object.keys(charts).forEach(function (id) {
      var chart = charts[id];
      if (!chart || !chart.canvas || !chart.canvas.isConnected) return;
      privacyOptions(chart.options);
      chart.update('none');
    });
  }

  // ── Side panel (drill-down) ───────────────────────────────────────────
  function openPanel(target, title, subtitle) {
    var el = document.getElementById('drillPanel');
    if (!el) return;
    el.querySelector('.drill-title').textContent = title || '';
    el.querySelector('.drill-subtitle').textContent = subtitle || '';
    var body = el.querySelector('.drill-body');
    body.innerHTML = '<div class="drill-loading"><span class="spinner-border spinner-border-sm me-2"></span>Chargement…</div>';
    bootstrap.Offcanvas.getOrCreateInstance(el).show();
    htmx.ajax('GET', target, { target: body, swap: 'innerHTML' });
  }

  // ── Charts ────────────────────────────────────────────────────────────
  var charts = {};
  var registered = false;

  // Value labels, selective: the total on top of each column (stacked or
  // not), or the value at the tip of each bar of a horizontal ranking --
  // never a number inside every stacked segment. A label is skipped when
  // it would collide with its neighbour instead of being squeezed in.
  var valueLabels = {
    id: 'tbValues',
    afterDatasetsDraw: function (chart) {
      var cfg = chart.$tb;
      if (!cfg || !chart.$tbShowValues || privacyOn()) return;
      if (cfg.type === 'line') { endLabels(chart, cfg); return; }
      var ctx = chart.ctx;
      var horizontal = cfg.horizontal;
      var valueScale = horizontal ? chart.scales.x : chart.scales.y;
      var indexScale = horizontal ? chart.scales.y : chart.scales.x;
      var count = chart.data.labels.length;
      ctx.save();
      ctx.font = '600 11px ' + Chart.defaults.font.family;
      ctx.fillStyle = token('ink2');
      ctx.textAlign = horizontal ? 'left' : 'center';
      ctx.textBaseline = horizontal ? 'middle' : 'bottom';
      var lastEdge = -Infinity;
      for (var i = 0; i < count; i++) {
        var total = 0, any = false;
        chart.data.datasets.forEach(function (ds, d) {
          if (!chart.isDatasetVisible(d)) return;
          var v = ds.data[i];
          if (v > 0) { total += v; any = true; }
        });
        if (!any) continue;
        var label = fmtCompact(total, cfg.kind, cfg.currency);
        var width = ctx.measureText(label).width;
        var pos = indexScale.getPixelForValue(i);
        var tip = valueScale.getPixelForValue(total);
        if (horizontal) {
          ctx.fillText(label, tip + 6, pos);
        } else {
          if (pos - width / 2 < lastEdge + 4) continue;
          ctx.fillText(label, pos, tip - 4);
          lastEdge = pos + width / 2;
        }
      }
      ctx.restore();
    },
  };

  // Lines: the value at the end of each coloured line only (gray context
  // lines stay unlabelled), skipped when it would sit on top of another.
  function endLabels(chart, cfg) {
    var ctx = chart.ctx;
    var placed = [];
    ctx.save();
    ctx.font = '600 11px ' + Chart.defaults.font.family;
    ctx.fillStyle = token('ink2');
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    chart.data.datasets.forEach(function (ds, d) {
      if (!chart.isDatasetVisible(d) || ds.tb.slot === 'other') return;
      var last = ds.data.length - 1;
      while (last >= 0 && (ds.data[last] === null || ds.data[last] === undefined)) last--;
      if (last < 0) return;
      var point = chart.getDatasetMeta(d).data[last];
      var label = fmtCompact(ds.data[last], cfg.kind, cfg.currency);
      var width = ctx.measureText(label).width;
      var x = point.x - 4, y = point.y - 6;
      var clash = placed.some(function (p) { return Math.abs(p.y - y) < 14 && x - width < p.x && p.x - p.width < x; });
      if (clash) return;
      ctx.fillText(label, x, y);
      placed.push({ x: x, y: y, width: width });
    });
    ctx.restore();
  }

  // Line charts: a vertical hairline at the hovered date -- readers aim at
  // a date, never at a 2px line.
  var crosshair = {
    id: 'tbCrosshair',
    beforeDatasetsDraw: function (chart) {
      if (!chart.$tb || chart.$tb.type !== 'line') return;
      var active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      var x = Math.round(active[0].element.x) + 0.5;
      var area = chart.chartArea;
      var ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = token('axis');
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  function chromeOptions(options, horizontal) {
    var valueAxis = horizontal ? options.scales.x : options.scales.y;
    var indexAxis = horizontal ? options.scales.y : options.scales.x;
    valueAxis.grid.color = token('grid');
    valueAxis.ticks.color = token('muted');
    indexAxis.ticks.color = token('muted');
    indexAxis.border.color = token('axis');
    options.plugins.legend.labels.color = token('ink2');
    var tip = options.plugins.tooltip;
    tip.backgroundColor = token('ink');
    tip.titleColor = tip.bodyColor = tip.footerColor = token('surface');
  }

  function retheme() {
    if (!window.Chart) return;
    Object.keys(charts).forEach(function (id) {
      var chart = charts[id];
      chromeOptions(chart.options, chart.$tb.horizontal);
      chart.update('none');
    });
  }

  function truncate(text, max) {
    text = String(text);
    return text.length > max ? text.slice(0, max - 1) + '…' : text;
  }

  function register() {
    if (registered) return;
    Chart.register(valueLabels, crosshair);
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    registered = true;
  }

  // Axes, legend, tooltip and click-to-drill shared by bar() and line().
  function baseOptions(cfg, horizontal, multi) {
    var axisTicks = function (value) { return fmtCompact(value, cfg.kind, cfg.currency); };
    var valueAxis = {
      stacked: !!cfg.stacked,
      beginAtZero: true,
      grace: horizontal ? '12%' : (cfg.type === 'line' ? '10%' : '6%'),
      grid: { drawTicks: false },
      border: { display: false },
      ticks: { callback: axisTicks, padding: 6, maxTicksLimit: 7 },
    };
    if (cfg.kind === 'minutes') valueAxis.ticks.stepSize = minuteStep(cfg);
    var indexAxis = {
      stacked: !!cfg.stacked,
      grid: { display: false },
      border: { display: true },
      ticks: horizontal
        ? { callback: function (v) { return truncate(this.getLabelForValue(v), 32); }, autoSkip: false }
        : { maxRotation: 0, autoSkip: true, autoSkipPadding: 14 },
    };
    if (cfg.tickLabel) {
      // Only the labels the chart asks for (e.g. the 1st of each month on
      // a 366-day axis), rather than whichever ones auto-skip lands on.
      indexAxis.ticks = {
        maxRotation: 0, autoSkip: false,
        callback: function (v) { return cfg.tickLabel(this.getLabelForValue(v), v); },
      };
    }

    var options = {
      indexAxis: horizontal ? 'y' : 'x',
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { mode: 'index', intersect: false, axis: horizontal ? 'y' : 'x' },
      scales: horizontal ? { x: valueAxis, y: indexAxis } : { x: indexAxis, y: valueAxis },
      plugins: {
        legend: {
          display: multi,
          position: 'top',
          align: 'start',
          labels: {
            usePointStyle: true, pointStyle: cfg.type === 'line' ? 'line' : 'rectRounded',
            boxWidth: 10, boxHeight: 10, padding: 14,
          },
        },
        tooltip: {
          padding: 10,
          cornerRadius: 6,
          boxPadding: 4,
          usePointStyle: true,
          filter: function (item) { return item.raw !== 0 && item.raw !== null && item.raw !== undefined; },
          callbacks: {
            labelPointStyle: function () { return { pointStyle: 'line', rotation: 0 }; },
            label: function (item) {
              var value = fmt(item.raw, cfg.kind, cfg.currency);
              return multi ? value + '  ' + item.dataset.label : value;
            },
            footer: function (items) {
              if (!cfg.stacked || items.length < 2) return '';
              var total = items.reduce(function (sum, it) { return sum + (it.raw || 0); }, 0);
              return 'Total : ' + fmt(total, cfg.kind, cfg.currency);
            },
          },
        },
      },
      onHover: function (evt, elements, chart) {
        chart.canvas.style.cursor = cfg.drill && elements.length ? 'pointer' : 'default';
      },
      onClick: function (evt, elements, chart) { drill(chart, evt); },
    };
    chromeOptions(options, horizontal);
    return options;
  }

  // The band a drag covers, drawn behind the bars while the mouse is down.
  var selectionBand = {
    id: 'tbSelection',
    afterDraw: function (chart) {
      var sel = chart.$tbSel;
      if (!sel) return;
      var x = chart.scales.x, area = chart.chartArea;
      var step = chart.data.labels.length > 1 ? x.getPixelForValue(1) - x.getPixelForValue(0) : x.width;
      var from = Math.min(sel.from, sel.to), to = Math.max(sel.from, sel.to);
      var left = Math.max(area.left, x.getPixelForValue(from) - step / 2);
      var right = Math.min(area.right, x.getPixelForValue(to) + step / 2);
      var ctx = chart.ctx;
      ctx.save();
      ctx.fillStyle = 'rgba(120, 120, 120, 0.18)';
      ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
      ctx.restore();
    },
  };

  // Drag across a column chart to choose a period: cfg.select(from, to)
  // gives the URL to open for the first..last columns dragged over. A plain
  // click still drills (the flag below swallows the click that ends a drag).
  function rangeSelect(chart) {
    var canvas = chart.canvas;
    canvas.$tbChart = chart;
    if (canvas.$tbRangeBound) return;
    canvas.$tbRangeBound = true;

    function indexAt(evt) {
      var hit = canvas.$tbChart.getElementsAtEventForMode(evt, 'index', { intersect: false, axis: 'x' }, false);
      return hit.length ? hit[0].index : null;
    }
    canvas.addEventListener('mousedown', function (evt) {
      var chart = canvas.$tbChart;
      var index = evt.button === 0 ? indexAt(evt) : null;
      chart.$tbDragged = false;
      canvas.$tbDragging = index !== null;
      if (index !== null) chart.$tbSel = { from: index, to: index };
    });
    canvas.addEventListener('mousemove', function (evt) {
      var chart = canvas.$tbChart;
      if (!canvas.$tbDragging) return;
      var index = indexAt(evt);
      if (index === null || index === chart.$tbSel.to) return;
      chart.$tbSel.to = index;
      chart.$tbDragged = index !== chart.$tbSel.from || chart.$tbDragged;
      chart.draw();
    });
    window.addEventListener('mouseup', function () {
      var chart = canvas.$tbChart;
      if (!canvas.$tbDragging) return;
      canvas.$tbDragging = false;
      var sel = chart.$tbSel;
      chart.$tbSel = null;
      if (sel.from === sel.to) { chart.draw(); return; }
      var url = chart.$tb.select(Math.min(sel.from, sel.to), Math.max(sel.from, sel.to));
      if (url) window.location.href = url;
      else chart.draw();
    });
  }

  function mount(id, canvas, type, datasets, options, cfg, showValues) {
    if (charts[id]) charts[id].destroy();
    var chart = new Chart(canvas, {
      type: type, data: { labels: cfg.labels, datasets: datasets }, options: privacyOptions(options), plugins: [selectionBand],
    });
    chart.$tb = cfg;
    chart.$tbShowValues = !!showValues;
    charts[id] = chart;
    return chart;
  }

  /* cfg: {
       labels: [...], series: [{label, slot, data, keys, hidden}],
       stacked, horizontal, kind ('number'|'money'|'minutes'|'percent' -- values
       out of 100), currency,
       showValues, axisLabel (first column of the table view),
       drill: function (index, series|null) -> {url, title, subtitle} | null,
       select: function (first, last) -> url | null (optional; drag across
       columns; vertical charts only)
     } */
  function bar(id, cfg) {
    var canvas = document.getElementById(id);
    if (!canvas || !window.Chart) return null;
    register();
    var horizontal = !!cfg.horizontal;
    var multi = cfg.series.length > 1;
    var datasets = cfg.series.map(function (s) {
      return {
        label: s.label,
        data: s.data,
        hidden: !!s.hidden,
        tb: s,
        backgroundColor: function () { return slotColor(s.slot); },
        // The 2px gap between stacked segments, in the surface colour.
        borderColor: function () { return token('surface'); },
        borderWidth: cfg.stacked && multi ? (horizontal ? { right: 2 } : { top: 2 }) : 0,
        borderRadius: 4,
        borderSkipped: 'start',
        maxBarThickness: 24,
        categoryPercentage: 0.85,
        barPercentage: 0.9,
      };
    });
    var chart = mount(id, canvas, 'bar', datasets, baseOptions(cfg, horizontal, multi), cfg, cfg.showValues);
    if (cfg.select && !horizontal) rangeSelect(chart);
    return chart;
  }

  /* Same cfg as bar() (never stacked or horizontal), plus: tickLabel
     (label, index) -> axis label or '' to skip one. A series with a null
     value stops there (no data for those dates, not zero); a lone series
     gets a light area wash; its first series is drawn on top. The last
     value of each coloured line is written at its end. */
  function line(id, cfg) {
    var canvas = document.getElementById(id);
    if (!canvas || !window.Chart) return null;
    register();
    cfg.type = 'line';
    var multi = cfg.series.length > 1;
    var datasets = cfg.series.map(function (s) {
      var color = function () { return slotColor(s.slot); };
      return {
        label: s.label,
        data: s.data,
        hidden: !!s.hidden,
        tb: s,
        borderColor: color,
        backgroundColor: function () { return slotColor(s.slot) + '1a'; }, // ~10% wash
        fill: multi ? false : 'origin',
        borderWidth: 2,
        borderJoinStyle: 'round',
        borderCapStyle: 'round',
        pointRadius: 0,
        pointHitRadius: 8,
        pointHoverRadius: 5,
        pointHoverBorderWidth: 2,
        pointHoverBackgroundColor: color,
        // The 2px ring that keeps a marker legible where lines cross.
        pointHoverBorderColor: function () { return token('surface'); },
      };
    });
    var options = baseOptions(cfg, false, multi);
    if (multi) {
      options.plugins.tooltip.itemSort = function (a, b) { return (b.raw || 0) - (a.raw || 0); };
    }
    return mount(id, canvas, 'line', datasets, options, cfg, true);
  }

  // Replaces each series' values with its `net` twin (or back): the
  // "net of refunds" switch of the purchases page.
  function useNet(chart, net) {
    if (!chart) return;
    chart.data.datasets.forEach(function (ds) {
      if (ds.tb.net) ds.data = net ? ds.tb.net : ds.tb.data;
    });
    chart.update();
  }

  function drill(chart, evt) {
    var cfg = chart.$tb;
    if (chart.$tbDragged) { chart.$tbDragged = false; return; }
    if (!cfg.drill || privacyOn()) return;
    var hit = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, false);
    var index, series = null;
    if (hit.length) {
      index = hit[0].index;
      series = chart.data.datasets[hit[0].datasetIndex].tb;
    } else {
      var column = chart.getElementsAtEventForMode(evt, 'index', { intersect: false, axis: cfg.horizontal ? 'y' : 'x' }, false);
      if (!column.length) return;
      index = column[0].index;
    }
    openDrill(cfg, index, series);
  }

  function openDrill(cfg, index, series) {
    var target = cfg.drill(index, series);
    if (target) openPanel(target.url, target.title, target.subtitle);
  }

  // Table view: the same numbers as the chart, readable without hovering
  // or telling colours apart. Labels come from user data, so cells are
  // filled with textContent only.
  function buildTable(chart) {
    var cfg = chart.$tb;
    var series = chart.data.datasets.filter(function (ds, d) { return chart.isDatasetVisible(d); });
    var withTotal = cfg.stacked && series.length > 1;
    var table = document.createElement('table');
    table.className = 'table table-sm tb-table mb-0';
    var head = table.createTHead().insertRow();
    [cfg.axisLabel || 'Période'].concat(series.map(function (s) { return s.label; }), withTotal ? ['Total'] : [])
      .forEach(function (text, i) {
        var th = document.createElement('th');
        th.textContent = text;
        if (i) th.className = 'num';
        head.appendChild(th);
      });
    var body = table.createTBody();
    chart.data.labels.forEach(function (label, i) {
      var total = 0, any = false;
      series.forEach(function (s) { if (s.data[i]) { any = true; total += s.data[i]; } });
      if (!any) return;
      var row = body.insertRow();
      var first = row.insertCell();
      if (cfg.drill) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-link p-0 text-start';
        button.textContent = label;
        button.addEventListener('click', function () { openDrill(cfg, i, null); });
        first.appendChild(button);
      } else {
        first.textContent = label;
      }
      series.forEach(function (s) {
        var cell = row.insertCell();
        cell.className = 'num';
        cell.textContent = s.data[i] ? fmt(s.data[i], cfg.kind, cfg.currency) : '–';
      });
      if (withTotal) {
        var cell = row.insertCell();
        cell.className = 'num fw-semibold';
        cell.textContent = fmt(total, cfg.kind, cfg.currency);
      }
    });
    return table;
  }

  function toggleTable(id, button) {
    var chart = charts[id];
    if (!chart) return;
    var box = chart.canvas.closest('.chart-box');
    var holder = box.parentNode.querySelector('.chart-table[data-for="' + id + '"]');
    var showing = holder && !holder.hidden;
    if (showing) {
      holder.hidden = true;
      box.hidden = false;
    } else {
      if (!holder) {
        holder = document.createElement('div');
        holder.className = 'chart-table';
        holder.setAttribute('data-for', id);
        box.parentNode.insertBefore(holder, box.nextSibling);
      }
      holder.replaceChildren(buildTable(chart));
      holder.hidden = false;
      box.hidden = true;
    }
    button.classList.toggle('active', !showing);
    button.setAttribute('aria-pressed', String(!showing));
  }

  // ── Delegated UI behaviour ────────────────────────────────────────────
  document.addEventListener('click', function (evt) {
    var themeBtn = evt.target.closest('[data-tb-theme-toggle]');
    if (themeBtn) {
      setTheme(mode() === 'dark' ? 'light' : 'dark');
      return;
    }
    var privacyBtn = evt.target.closest('[data-tb-privacy-toggle]');
    if (privacyBtn) {
      setPrivacy(!document.documentElement.classList.contains('tb-privacy'));
      return;
    }
    var tableBtn = evt.target.closest('[data-tb-table]');
    if (tableBtn) {
      toggleTable(tableBtn.getAttribute('data-tb-table'), tableBtn);
      return;
    }
    var drillEl = evt.target.closest('[data-drill-url]');
    if (drillEl) {
      evt.preventDefault();
      openPanel(drillEl.getAttribute('data-drill-url'), drillEl.getAttribute('data-drill-title'), drillEl.getAttribute('data-drill-subtitle'));
      return;
    }
    var forgetEl = evt.target.closest('[data-forget]');
    if (forgetEl) storeFilter(forgetEl.getAttribute('data-forget'), null);
    var multiBtn = evt.target.closest('[data-multi]');
    if (multiBtn) {
      var checked = multiBtn.getAttribute('data-multi') === 'all';
      multiBtn.closest('.tb-multi').querySelectorAll('input[type="checkbox"]').forEach(function (box) { box.checked = checked; });
    }
  });

  // A multi-select's choice outlives the page: kept per name and platform
  // (the Kindle and Audible views share it) until it is cleared.
  function storeFilter(key, values) {
    if (!key) return;
    try {
      if (values && values.length) localStorage.setItem('tb-filter-' + key, JSON.stringify(values));
      else localStorage.removeItem('tb-filter-' + key);
    } catch (e) { /* private mode */ }
  }

  function restoreFilters() {
    var url = new URL(window.location.href);
    var restored = false;
    document.querySelectorAll('.tb-multi[data-persist]').forEach(function (multi) {
      var key = multi.getAttribute('data-persist');
      var name = key.split(':')[0];
      if (url.searchParams.has(name)) {
        storeFilter(key, url.searchParams.getAll(name));
        return;
      }
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem('tb-filter-' + key)); } catch (e) { /* unreadable */ }
      if (saved && saved.length) {
        saved.forEach(function (value) { url.searchParams.append(name, value); });
        restored = true;
      }
    });
    if (restored) {
      // The filtered list may be shorter than the page asked for.
      url.searchParams.delete('page');
      window.location.replace(url.toString());
    }
  }
  restoreFilters();

  // A multi-select with every box ticked filters nothing: left out of the
  // query string rather than repeating every value in the URL.
  document.addEventListener('submit', function (evt) {
    evt.target.querySelectorAll('.tb-multi').forEach(function (multi) {
      var boxes = multi.querySelectorAll('input[type="checkbox"]');
      var all = Array.prototype.every.call(boxes, function (box) { return box.checked; });
      if (all) boxes.forEach(function (box) { box.disabled = true; });
      storeFilter(multi.getAttribute('data-persist'), all ? null : Array.prototype.filter.call(boxes, function (box) { return box.checked; }).map(function (box) { return box.value; }));
    });
  });
  // ...and usable again when "Back" restores the page as it was left.
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('.tb-multi input:disabled').forEach(function (box) { box.disabled = false; });
  });

  document.addEventListener('keydown', function (evt) {
    if (evt.key !== 'Enter' && evt.key !== ' ') return;
    var drillEl = evt.target.closest('[data-drill-url][role="button"]');
    if (drillEl) {
      evt.preventDefault();
      drillEl.click();
    }
  });

  document.addEventListener('change', function (evt) {
    var valuesToggle = evt.target.closest('[data-tb-values]');
    if (valuesToggle) {
      var chart = charts[valuesToggle.getAttribute('data-tb-values')];
      if (chart) {
        chart.$tbShowValues = valuesToggle.checked;
        chart.update('none');
      }
    }
    // Filter rows submit as soon as a select changes -- no "Apply" round
    // trip needed for a single choice.
    // requestSubmit, not submit(): it fires the submit event above.
    var autoSubmit = evt.target.closest('[data-auto-submit]');
    if (autoSubmit && autoSubmit.form) autoSubmit.form.requestSubmit();
  });

  // Client-side filtering of a long list (threads, categories): an input
  // with data-filter-target="#list" hides the children whose text doesn't
  // contain the query.
  document.addEventListener('input', function (evt) {
    var input = evt.target.closest('[data-filter-target]');
    if (!input) return;
    var needle = input.value.trim().toLowerCase();
    document.querySelectorAll(input.getAttribute('data-filter-target') + ' > [data-filter-text]').forEach(function (el) {
      el.hidden = needle && el.getAttribute('data-filter-text').toLowerCase().indexOf(needle) < 0;
    });
  });

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) { new bootstrap.Tooltip(el); });
    syncPrivacyButtons(document.documentElement.classList.contains('tb-privacy'));
  });

  window.TB = {
    bar: bar,
    line: line,
    useNet: useNet,
    charts: charts,
    fmt: fmt,
    url: url,
    openPanel: openPanel,
    slotColor: slotColor,
    RETURNS_SLOT: RETURNS_SLOT,
  };
})();
