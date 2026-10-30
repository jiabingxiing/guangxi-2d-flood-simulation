import time, urllib.request, statistics
base = "http://127.0.0.1:8797/"
def get(path):
    req = urllib.request.Request(base + path, headers={"Connection": "close"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    return time.perf_counter() - t, len(data)
for label, path, n in [
    ("catalog.json", "catalog.json", 10),
    ("dem 1MB png", "frames/guigang/dem_r0c0.png", 6),
    ("numeric 256KB bin", "numeric/guigang/scen_yagi_q11500_30m_b5/f0010/4/4.bin", 10),
    ("frame png ~40KB", "frames/guigang/scen_yagi_q11500_30m_b5/f0010_r0c0.png", 10),
]:
    ts, sizes = [], []
    for _ in range(n):
        dt, ln = get(path)
        ts.append(dt); sizes.append(ln)
    mb = sum(sizes) / 1e6
    print("%-20s n=%d avg=%.0f ms  max=%.0f ms  thr=%.1f MB/s  bytes=%d" % (
        label, n, statistics.median(ts)*1000, max(ts)*1000, mb / sum(ts), sizes[0]))
