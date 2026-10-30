const SUPABASE_URL = "https://dcbovugmihmwprsidcvk.supabase.co";
const SUPABASE_KEY = "sb_publishable_8UknbOeF-3j2GA638zTptA_oT8IiSFn";

const PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY", "GBPJPY"];
const HISTORY_LIMIT = 100;

const { createClient } = window.supabase;
const sb = createClient(SUPABASE_URL, SUPABASE_KEY);

let signals = [];
let soundOn = false;
let currentPair = PAIRS[0];
let audioCtx = null;

const $ = (id) => document.getElementById(id);
const fmtPrice = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: n < 10 ? 5 : n < 1000 ? 3 : 2 });
const fmtTime = (iso) => new Date(iso).toLocaleTimeString("bn-BD", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const pairLabel = (s) => (s.endsWith("USDT") ? s.replace("USDT", "/USDT") : s.slice(0, 3) + "/" + s.slice(3));

function loadChart(symbol) {
  currentPair = symbol;
  $("pairSelect").value = symbol;
  $("tv_chart").innerHTML = "";
  new TradingView.widget({
    container_id: "tv_chart",
    autosize: true,
    symbol: (symbol.endsWith("USDT") ? "BINANCE:" : "FX:") + symbol,
    interval: "1",
    timezone: "Etc/UTC",
    theme: "dark",
    style: "1",
    locale: "en",
    toolbar_bg: "#141C2E",
    enable_publishing: false,
    hide_side_toolbar: true,
    allow_symbol_change: false,
    studies: ["RSI@tv-basicstudies", "MAExp@tv-basicstudies"],
  });
}

function beep(direction) {
  if (!soundOn) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const notes = direction === "CALL" ? [660, 880] : [880, 660];
    notes.forEach((freq, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, audioCtx.currentTime + i * 0.18);
      gain.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + i * 0.18 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + i * 0.18 + 0.16);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(audioCtx.currentTime + i * 0.18);
      osc.stop(audioCtx.currentTime + i * 0.18 + 0.17);
    });
  } catch (e) { console.warn("Beep failed", e); }
}

function notify(sig) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  new Notification(`${sig.direction} — ${pairLabel(sig.symbol)}`, {
    body: `${sig.strategy} | এন্ট্রি ${fmtPrice(sig.entry_price)} | এক্সপায়ারি ১ মিনিট`,
  });
}

$("soundBtn").addEventListener("click", async () => {
  soundOn = !soundOn;
  $("soundBtn").textContent = soundOn ? "🔔 সাউন্ড চালু" : "🔕 সাউন্ড বন্ধ";
  $("soundBtn").setAttribute("aria-pressed", String(soundOn));
  if (soundOn) {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    beep("CALL");
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  }
});

function statusBadge(sig) {
  const map = {
    win: ["জয়", "text-call"],
    loss: ["হার", "text-put"],
    draw: ["ড্র", "text-muted"],
    expired: ["মেয়াদোত্তীর্ণ", "text-muted"],
    pending: ["চলছে", "text-wait"],
  };
  const [label, cls] = map[sig.status] || map.pending;
  return `<span class="${cls} font-semibold">${label}</span>`;
}

function renderStats() {
  const done = signals.filter((s) => s.status === "win" || s.status === "loss");
  const wins = done.filter((s) => s.status === "win").length;
  const losses = done.length - wins;
  $("statWin").textContent = wins;
  $("statLoss").textContent = losses;
  $("statRate").textContent = done.length ? ((wins / done.length) * 100).toFixed(1) + "%" : "—";
}

function renderFeed() {
  const list = $("feed");
  if (!signals.length) {
    list.innerHTML = `<li class="text-muted text-sm text-center py-8">এখনো কোনো সিগন্যাল নেই।</li>`;
    return;
  }
  list.innerHTML = signals.map((s) => `
    <li>
      <button data-sym="${s.symbol}" class="w-full text-left px-3 py-3 rounded-lg hover:bg-ink flex items-center gap-3">
        <span class="h-9 w-9 shrink-0 rounded-md flex items-center justify-center font-extrabold text-ink ${s.direction === "CALL" ? "bg-call" : "bg-put"}">
          ${s.direction === "CALL" ? "▲" : "▼"}
        </span>
        <span class="flex-1 min-w-0">
          <span class="block font-semibold">${pairLabel(s.symbol)} <span class="text-muted font-normal">${s.direction}</span></span>
          <span class="block text-xs text-muted truncate">${s.strategy} · RSI ${Number(s.rsi).toFixed(0)} · ${fmtTime(s.candle_time)}</span>
        </span>
        <span class="text-right text-sm">${statusBadge(s)}<span class="block text-xs text-muted">${fmtPrice(s.entry_price)}</span></span>
      </button>
    </li>`).join("");
  list.querySelectorAll("button[data-sym]").forEach((b) =>
    b.addEventListener("click", () => loadChart(b.dataset.sym))
  );
}

const RING = 2 * Math.PI * 34;
function renderHero() {
  const s = signals[0];
  if (!s) return;
  const color = s.direction === "CALL" ? "#34D399" : "#F87171";
  $("hero").innerHTML = `
    <div class="slide-in flex items-center gap-5">
      <div class="relative h-20 w-20 shrink-0">
        <svg viewBox="0 0 80 80" class="h-20 w-20 -rotate-90">
          <circle cx="40" cy="40" r="34" fill="none" stroke="#222C42" stroke-width="6"/>
          <circle id="ring" class="ring" cx="40" cy="40" r="34" fill="none" stroke="${color}" stroke-width="6"
                  stroke-linecap="round" stroke-dasharray="${RING}" stroke-dashoffset="0"/>
        </svg>
        <span id="count" class="absolute inset-0 flex items-center justify-center font-extrabold text-lg">—</span>
      </div>
      <div class="min-w-0">
        <div class="text-3xl font-extrabold" style="color:${color}">${s.direction === "CALL" ? "▲ CALL" : "▼ PUT"}</div>
        <div class="font-semibold">${pairLabel(s.symbol)}</div>
        <div class="text-xs text-muted">${s.strategy} · এন্ট্রি ${fmtPrice(s.entry_price)}</div>
        <div class="text-xs mt-1">ইন্ডিকেটর স্কোর <b>${s.strength}</b>/100 · <span id="heroStatus">${statusBadge(s)}</span></div>
      </div>
    </div>`;
}

setInterval(() => {
  const s = signals[0];
  const ring = $("ring"), count = $("count");
  if (!s || !ring || !count) return;
  if (s.status !== "pending") { count.textContent = s.status === "win" ? "✓" : s.status === "loss" ? "✗" : "–"; ring.style.strokeDashoffset = 0; return; }
  const left = Math.max(0, Math.ceil((new Date(s.expires_at) - Date.now()) / 1000));
  count.textContent = left + "s";
  ring.style.strokeDashoffset = RING * (1 - Math.min(1, left / 120));
}, 1000);

function renderAll() { renderHero(); renderFeed(); renderStats(); }

async function loadHistory() {
  const { data, error } = await sb.from("signals").select("*").order("created_at", { ascending: false }).limit(HISTORY_LIMIT);
  if (error) {
    console.error(error);
    setStatus(false, "ডাটা লোড হয়নি — schema.sql চালিয়েছেন?");
    return;
  }
  signals = data;
  renderAll();
}

function setStatus(ok, text) {
  $("statusDot").className = "h-2.5 w-2.5 rounded-full " + (ok ? "bg-call" : "bg-wait");
  $("statusText").textContent = text;
}

function subscribe() {
  sb.channel("signals-live")
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "signals" }, (payload) => {
      const sig = payload.new;
      signals.unshift(sig);
      signals = signals.slice(0, HISTORY_LIMIT);
      renderAll();
      beep(sig.direction);
      notify(sig);
      loadChart(sig.symbol);
    })
    .on("postgres_changes", { event: "UPDATE", schema: "public", table: "signals" }, (payload) => {
      const i = signals.findIndex((s) => s.id === payload.new.id);
      if (i !== -1) { signals[i] = payload.new; renderAll(); }
    })
    .subscribe((status) => {
      if (status === "SUBSCRIBED") setStatus(true, "লাইভ সংযুক্ত");
      else if (status === "CHANNEL_ERROR" || status === "TIMED_OUT") setStatus(false, "সংযোগ সমস্যা — আবার চেষ্টা চলছে");
      else if (status === "CLOSED") setStatus(false, "সংযোগ বিচ্ছিন্ন");
    });
}

$("pairSelect").innerHTML = PAIRS.map((p) => `<option value="${p}">${pairLabel(p)}</option>`).join("");
$("pairSelect").addEventListener("change", (e) => loadChart(e.target.value));
loadChart(currentPair);
loadHistory().then(subscribe);
