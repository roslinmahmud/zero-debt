(async () => {
  const res = await fetch('/api/charts', { credentials: 'same-origin' });
  const data = await res.json();
  const tk = v => 'Tk ' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const money = { ticks: { callback: tk } };
  const tooltip = { callbacks: { label: c => `${c.dataset.label}: ${tk(c.parsed.y)}` } };

  // Outstanding: actual + dashed projection on a shared category axis.
  const actual = data.outstanding.actual, projected = data.outstanding.projected;
  const labels = [...actual.map(p => p.x), ...projected.slice(1).map(p => p.x)];
  const pad = n => Array(n).fill(null);
  document.getElementById('proj-note').textContent =
    `Projection assumes ${tk(data.outstanding.monthly)} per month (your average over the last 90 days), deposited on the 1st.`;
  new Chart(document.getElementById('outstanding'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Actual', data: actual.map(p => p.y), borderColor: '#0f766e', pointRadius: 0, borderWidth: 2 },
        {
          label: 'Projected',
          data: [...pad(Math.max(actual.length - 1, 0)), ...projected.map(p => p.y)],
          borderColor: '#94a3b8', borderDash: [6, 4], pointRadius: 0, borderWidth: 2,
        },
      ],
    },
    options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: { y: money, x: { ticks: { maxTicksLimit: 12 } } }, plugins: { tooltip } },
  });

  new Chart(document.getElementById('postings'), {
    type: 'bar',
    data: {
      labels: data.postings.map(p => p.x),
      datasets: [{
        label: 'Profit posted',
        data: data.postings.map(p => p.y),
        backgroundColor: data.postings.map(p => p.estimated ? '#cbd5e1' : '#0f766e'),
      }],
    },
    options: { maintainAspectRatio: false, scales: { y: money }, plugins: { tooltip, legend: { display: false } } },
  });

  new Chart(document.getElementById('cumulative'), {
    type: 'line',
    data: {
      labels: data.cumulative.map(p => p.x),
      datasets: [{ label: 'Cumulative profit', data: data.cumulative.map(p => p.y), borderColor: '#b45309', fill: false }],
    },
    options: { maintainAspectRatio: false, scales: { y: money }, plugins: { tooltip } },
  });

  const eur = data.eur_cost;
  new Chart(document.getElementById('eur'), {
    type: 'bar',
    data: {
      labels: eur.map(p => p.x),
      datasets: [
        { label: 'Effective BDT per EUR', data: eur.map(p => p.effective), backgroundColor: '#0f766e', yAxisID: 'y' },
        { label: 'EUR spent (incl. fee)', data: eur.map(p => p.eur), type: 'line', borderColor: '#6366f1', yAxisID: 'y1' },
      ],
    },
    options: {
      maintainAspectRatio: false,
      scales: {
        y: { position: 'left', beginAtZero: false },
        y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { callback: v => '€' + v } },
      },
    },
  });
})();
