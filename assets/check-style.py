#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-style.py — 断点线性图标风格的自动验收器（24 栅格）

用法:
    python3 check-style.py icon-*.svg [--scale 10] [--tol 0.35] [--png-dir DIR]

它做四件事，全部用像素实测，不靠肉眼：
  1. 连通域计数  —— 与声明的元素数比对，少一块 = 粘连/漏画，多一块 = 意外断裂
  2. 线宽测量    —— 每个 isolate 连通域的最大内切半径 ×2，检查绝对值=2 且彼此一致
  3. 间隙测量    —— 连通域之间的最短间隙，检查 ≥2 且落在模数 {2,3,4,6}
  4. 构成体检    —— 墨量占比、几何中心是否居中、是否越出 keyline

SVG 中用注释声明元素数：<!-- elements: 5 -->；缺省时按 <path>/<rect>/<circle>/<line> 计数。
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, deque

from PIL import Image

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
INF = 10 ** 9
INF_FLOAT = 1e15
ALLOWED_GAPS = (2.0, 3.0, 4.0, 6.0)
# 校验阈值
WIDTH_TARGET = 2.0      # 线宽必须恒等于 2
WIDTH_ABS_TOL = 0.2     # 绝对值容差
GAP_MIN = 2.0           # 最小净间隙（不粘连）
INK_BAND = (9.0, 32.0)  # 墨量带：开放型图标天然偏轻（9–13%），容器型偏重（23–30%）


def parse_elements(svg_text):
    m = re.search(r"elements:\s*(\d+)", svg_text)
    if m:
        return int(m.group(1))
    return len(re.findall(r"<(?:path|rect|circle|line|polyline|polygon)\b", svg_text))


def render(svg_path, px, tmpdir):
    txt = open(svg_path, encoding="utf-8").read()
    if re.search(r'width="\d+"', txt):
        txt = re.sub(r'width="\d+"\s+height="\d+"', f'width="{px}" height="{px}"', txt, count=1)
    else:
        txt = re.sub(r"viewBox=", f'width="{px}" height="{px}" viewBox=', txt, count=1)
    dst = os.path.join(tmpdir, os.path.basename(svg_path).replace(".svg", ".render.svg"))
    png = dst + ".png"
    open(dst, "w", encoding="utf-8").write(txt)
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--default-background-color=FFFFFFFF", "--force-device-scale-factor=1",
         f"--window-size={px},{px}", f"--screenshot={png}", "file://" + dst],
        capture_output=True,
    )
    return png


def components(ink, w, h):
    labels = [-1] * (w * h)
    comps = []
    for i in range(w * h):
        if ink[i] and labels[i] < 0:
            cid = len(comps)
            members = []
            q = deque([i])
            labels[i] = cid
            while q:
                j = q.popleft()
                members.append(j)
                x, y = j % w, j // w
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if nx < 0 or nx >= w:
                        continue
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        ny = y + dy
                        if ny < 0 or ny >= h:
                            continue
                        k = ny * w + nx
                        if ink[k] and labels[k] < 0:
                            labels[k] = cid
                            q.append(k)
            comps.append(members)
    return comps


def edt_1d(f, n):
    """Felzenszwalb 一维平方距离变换（lower envelope）"""
    d = [0.0] * n
    v = [0] * n
    z = [0.0] * (n + 1)
    k = 0
    v[0] = 0
    z[0] = -INF_FLOAT
    z[1] = INF_FLOAT
    for q in range(1, n):
        s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2.0 * q - 2.0 * v[k])
        while s <= z[k]:
            k -= 1
            s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2.0 * q - 2.0 * v[k])
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = INF_FLOAT
    k = 0
    for q in range(n):
        while z[k + 1] < q:
            k += 1
        dq = q - v[k]
        d[q] = dq * dq + f[v[k]]
    return d


def edt(seed_idx, w, h):
    """精确欧氏距离变换，返回像素单位的距离列表（float）"""
    f = [INF_FLOAT] * (w * h)
    for i in seed_idx:
        f[i] = 0.0
    for x in range(w):
        col = [f[y * w + x] for y in range(h)]
        dcol = edt_1d(col, h)
        for y in range(h):
            f[y * w + x] = dcol[y]
    d = [0.0] * (w * h)
    for y in range(h):
        row = edt_1d(f[y * w:(y + 1) * w], w)
        d[y * w:(y + 1) * w] = row
    return [v ** 0.5 for v in d]


# 8 个晶格方向（水平/垂直/45°/±26.6°）及其单步物理长度，用于行程法量厚度
DIRS8 = [
    (1, 0, 1.0), (0, 1, 1.0),
    (1, 1, 2 ** 0.5), (1, -1, 2 ** 0.5),
    (2, 1, 5 ** 0.5), (2, -1, 5 ** 0.5),
    (1, 2, 5 ** 0.5), (1, -2, 5 ** 0.5),
]


def stroke_width(ink, w, h, members, scale, sample=3):
    """局部厚度 = 多方向行程长度的最小值（取最小 = 垂直于笔画走向的那条）

    为什么不用内切半径：round join 处两笔叠加成实心楔形，内切半径虚高到
    1/sin(θ/2)；而端点 caps 又把分布拉低。行程法对两者都不敏感。
    最后取中位数，进一步压掉 join 与端点这类少数样本。
    """
    vals = []
    for k in range(0, len(members), sample):
        i = members[k]
        x, y = i % w, i // w
        best = 1e9
        for dx, dy, sl in DIRS8:
            n = 1
            cx, cy = x + dx, y + dy
            while 0 <= cx < w and 0 <= cy < h and ink[cy * w + cx]:
                n += 1
                cx += dx
                cy += dy
            cx, cy = x - dx, y - dy
            while 0 <= cx < w and 0 <= cy < h and ink[cy * w + cx]:
                n += 1
                cx -= dx
                cy -= dy
            t = (n - 1) * sl + 1.0
            if t < best:
                best = t
        vals.append(best)
    vals.sort()
    return vals[len(vals) // 2] / scale


def analyze(path, scale, tol, tmpdir):
    px = 24 * scale
    png = render(path, px, tmpdir)
    expected = parse_elements(open(path, encoding="utf-8").read())
    im = Image.open(png).convert("L")
    w, h = im.size
    data = list(im.getdata())
    ink = [v < 128 for v in data]
    comps = components(ink, w, h)

    widths, gaps = [], []
    pair_info = []
    for idx, members in enumerate(comps):
        mset = set(members)
        width = stroke_width(ink, w, h, members, scale)
        widths.append(width)
        d_out = edt(members, w, h)
        for jdx, other in enumerate(comps):
            if other is members:
                continue
            best, bj = INF_FLOAT, None
            for j in other:
                if d_out[j] < best:
                    best, bj = d_out[j], j
            g = (best - 1.0) / scale
            gaps.append(g)
            pair_info.append((idx, jdx, g, bj % w / scale, bj // w / scale))

    ink_area = sum(1 for v in ink if v)
    xs = [i % w for i in range(w * h) if ink[i]]
    ys = [i // w for i in range(w * h) if ink[i]]
    cx = ((min(xs) + max(xs)) / 2.0) / scale
    cy = ((min(ys) + max(ys)) / 2.0) / scale
    bbox = (min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale)

    res = {
        "name": os.path.basename(path),
        "expected": expected,
        "found": len(comps),
        "wmin": min(widths) if widths else 0,
        "wmax": max(widths) if widths else 0,
        "gap": min(gaps) if gaps else None,
        "gaps": sorted(set(round(g, 2) for g in gaps)),
        "ink": 100.0 * ink_area / (w * h),
        "center": (cx, cy),
        "bbox": bbox,
        "fails": [],
        "warns": [],
    }

    if len(comps) != expected:
        res["fails"].append(f"元素数 {len(comps)}≠声明 {expected}（粘连或意外断裂）")
    if widths:
        if abs(res["wmax"] - WIDTH_TARGET) > WIDTH_ABS_TOL or abs(res["wmin"] - WIDTH_TARGET) > WIDTH_ABS_TOL:
            res["fails"].append(f"线宽 {res['wmin']:.2f}–{res['wmax']:.2f}≠2")
        elif res["wmax"] - res["wmin"] > tol:
            res["fails"].append(f"线宽不一致 Δ={res['wmax'] - res['wmin']:.2f}")
    if gaps:
        g = res["gap"]
        if g is not None and g < GAP_MIN - tol:
            res["fails"].append(f"最小间隙 {g:.2f}<2（粘连）")
        near = [a for a in ALLOWED_GAPS if abs(a - g) <= tol] if g is not None else []
        if not near:
            res["warns"].append(f"间隙 {g:.2f} 不在模数 {list(ALLOWED_GAPS)}")
    if not (INK_BAND[0] <= res["ink"] <= INK_BAND[1]):
        hint = "偏单薄" if res["ink"] < INK_BAND[0] else "偏堵塞"
        res["warns"].append(f"墨量 {res['ink']:.1f}% 越出 {INK_BAND[0]:.0f}–{INK_BAND[1]:.0f}% 带（{hint}）")
    if abs(cx - 12) > 0.6 or abs(cy - 12) > 0.6:
        res["warns"].append(f"重心偏移 ({cx:.2f},{cy:.2f})")
    if bbox[0] < 0.4 or bbox[1] < 0.4 or bbox[2] > 23.6 or bbox[3] > 23.6:
        res["warns"].append("越出 keyline（描边外缘应落在 1–23）")
    res["pairs"] = pair_info
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--scale", type=int, default=10)
    ap.add_argument("--tol", type=float, default=0.35)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    files = []
    for pat in args.files:
        if os.path.isdir(pat):
            # 目录：只取一级 svg，跳过网格模板这类非图标底稿
            files.extend(sorted(os.path.join(pat, f) for f in os.listdir(pat)
                                if f.endswith(".svg") and "template" not in f))
            continue
        files.extend(sorted(glob.glob(pat)) or [pat])
    tmpdir = tempfile.mkdtemp(prefix="iconcheck-")

    rows = [analyze(f, args.scale, args.tol, tmpdir) for f in files]
    debug = args.debug
    print(f"{'文件':<34}{'元素':>8}{'线宽(最小–最大)':>18}{'最小间隙':>10}{'墨量%':>8}  判读")
    print("-" * 108)
    for r in rows:
        gs = f"{r['gap']:.2f}" if r["gap"] is not None else "—"
        verdict = "PASS" if not r["fails"] else "FAIL"
        print(f"{r['name']:<34}{r['found']:>5}/{r['expected']:<3}{r['wmin']:.2f}–{r['wmax']:.2f}        "
              f"{gs:>10}{r['ink']:>8.1f}  {verdict}")
        if r["gaps"]:
            print("    间隙谱: " + str(r["gaps"]))
            if debug:
                for i, j, g, x, y in r["pairs"]:
                    print(f"      #{i}-#{j} {g:.2f}  最近点({x:.1f},{y:.1f})")
        for f in r["fails"]:
            print(f"    ✗ {f}")
        for wmsg in r["warns"]:
            print(f"    ! {wmsg}")
    bad = [r for r in rows if r["fails"]]
    print("-" * 108)
    print(f"{len(rows)} 个图标，{len(rows) - len(bad)} 通过，{len(bad)} 打回")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
