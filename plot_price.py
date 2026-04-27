import json
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

matplotlib.use("macosx")  # interactive backend with zoom/pan toolbar

DATA_FILE = "data/btc_reference.jsonl"

timestamps = []
prices = []
open_15m_vals = []

with open(DATA_FILE) as f:
    for line in f:
        obj = json.loads(line)
        timestamps.append(obj["timestamp"])
        prices.append(obj["last_price"][0])
        open_15m_vals.append(obj["raw"]["candlesticks"]["15M"]["open_ts_ms"])

# Detect where the 15M candle changes — those are the divider x positions.
divider_xs = []
segment_refs = [open_15m_vals[0]]  # 15M open_ts_ms for each segment

for i in range(1, len(timestamps)):
    if open_15m_vals[i] != open_15m_vals[i - 1]:
        divider_xs.append(timestamps[i])
        segment_refs.append(open_15m_vals[i])

def get_segment_ref(x):
    """Return the 15M open_ts_ms for whichever segment x falls in."""
    ref = segment_refs[0]
    for j, dx in enumerate(divider_xs):
        if x >= dx:
            ref = segment_refs[j + 1]
        else:
            break
    return ref

def format_x(x, _pos):
    ref = get_segment_ref(x)
    secs = (x - ref) / 1000.0
    return f"{secs:.0f}s"

# Build tick positions: every 10 s (10 000 ms) from each segment's 15M open_ts_ms.
tick_positions = []
n_segs = len(segment_refs)
for j, ref in enumerate(segment_refs):
    seg_start = divider_xs[j - 1] if j > 0 else timestamps[0]
    seg_end = divider_xs[j] if j < len(divider_xs) else timestamps[-1]
    k = 0
    while True:
        t = ref + k * 10_000
        if t > seg_end:
            break
        if t >= seg_start:
            tick_positions.append(t)
        k += 1

fig, ax = plt.subplots(figsize=(16, 6))
ax.plot(timestamps, prices, linewidth=0.8, color="steelblue")

for dx in divider_xs:
    ax.axvline(x=dx, color="crimson", linestyle="--", linewidth=1.0, alpha=0.75)

ax.set_xticks(tick_positions)
ax.xaxis.set_major_formatter(ticker.FuncFormatter(format_x))
ax.tick_params(axis="x", labelsize=7, rotation=60)

ax.set_ylabel("BTC Price (USD)")
ax.set_xlabel("Seconds since last 15M candle open")
ax.set_title("BTC Last Price  |  dashed lines = new 15M candle")
ax.grid(True, alpha=0.25)

plt.tight_layout()
plt.show()
