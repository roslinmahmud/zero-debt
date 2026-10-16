(async () => {
  const form = document.getElementById('proj-form');
  const monthlyInput = document.getElementById('proj-monthly'), dayInput = document.getElementById('proj-day');
  monthlyInput.value = localStorage.getItem('projMonthly') || '';
  dayInput.value = localStorage.getItem('projDay') || '1';

  const load = async () => {
    const q = new URLSearchParams({ monthly: monthlyInput.value.trim(), day: dayInput.value || '1' });
    const res = await fetch('/api/charts?' + q, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('Invalid projection input');
    return res.json();
  };
  const data = await load().catch(() => {
    monthlyInput.value = '';
    dayInput.value = '1';
    return load();
  });
  const tk = v => 'Tk ' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const money = { ticks: { callback: tk } };
  const tooltip = { callbacks: { label: c => `${c.dataset.label}: ${tk(c.parsed.y)}` } };

  // Outstanding: actual + dashed projection on a shared category axis.
  const outstanding = new Chart(document.getElementById('outstanding'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Actual', data: [], borderColor: '#0f766e', pointRadius: 0, borderWidth: 2 },
      { label: 'Projected', data: [], borderColor: '#94a3b8', borderDash: [6, 4], pointRadius: 0, borderWidth: 2 },
    ] },
    options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: { y: money, x: { ticks: { maxTicksLimit: 12 } } }, plugins: { tooltip } },
  });
  const drawOutstanding = o => {
    const pad = n => Array(n).fill(null);
    outstanding.data.labels = [...o.actual.map(p => p.x), ...o.projected.slice(1).map(p => p.x)];
    outstanding.data.datasets[0].data = o.actual.map(p => p.y);
    outstanding.data.datasets[1].data = [...pad(Math.max(o.actual.length - 1, 0)), ...o.projected.map(p => p.y)];
    outstanding.update();
    const source = o.is_average ? ' (your average over the last 90 days)' : '';
    const result = o.payoff
      ? `Paid off ${new Date(o.payoff).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}, future profit ${tk(o.future_profit)}.`
      : 'Not paid off within 10 years at this amount.';
    document.getElementById('proj-note').textContent =
      `Projection: ${tk(o.monthly)} per month${source} on day ${o.deposit_day}. ${result}`;
  };
  drawOutstanding(data.outstanding);

  form.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      drawOutstanding((await load()).outstanding);
      localStorage.setItem('projMonthly', monthlyInput.value.trim());
      localStorage.setItem('projDay', dayInput.value);
    } catch (err) {
      document.getElementById('proj-note').textContent = 'Enter a valid amount and a day between 1 and 31.';
    }
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
})();
