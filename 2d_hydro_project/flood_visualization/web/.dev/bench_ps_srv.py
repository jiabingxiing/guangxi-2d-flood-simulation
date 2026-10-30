import time, urllib.request, statistics, concurrent.futures as cf, json
BASE = "http://127.0.0.1:8795/"
def get(path):
    req = urllib.request.Request(BASE + path, headers={"Connection": "close"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as r:
        n = len(r.read())
    return time.perf_counter() - t, n
def burst(n):
    return [get("catalog.json")[0] for _ in range(n)]
out = {}
# 串行
paths = ["catalog.json"] * 20
ts = [get(p)[0] for p in paths]
out["serial_20_catalog"] = {"median_ms": round(statistics.median(ts)*1000), "total_s": round(sum(ts),2)}
# 并发 8（模拟浏览器并行连接）
t0 = time.perf_counter()
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    list(ex.map(lambda _: get("catalog.json"), range(40)))
out["concurrent8_40_catalog_s"] = round(time.perf_counter()-t0, 2)
# 大文件并发
t0 = time.perf_counter()
with cf.ThreadPoolExecutor(max_workers=6) as ex:
    sizes = list(ex.map(lambda _: get("frames/guigang/dem_r0c0.png")[1], range(6)))
out["concurrent6_1MB_s"] = round(time.perf_counter()-t0, 2)
out["1MB_bytes"] = sizes[0]
print(json.dumps(out, ensure_ascii=False))
