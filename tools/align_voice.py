#!/usr/bin/env python3
"""把一組數字人聲音檔對齊成長度、音量一致，給節拍器用。

不管是自己錄的還是語音合成的，八個字都不會自然一致：「三」「四」「七」
開頭有長長的摩擦音或送氣音，「二」「八」一開口就是母音；長度差兩三倍，
平均能量也差十幾 dB。

這支腳本做四件事：

  1. 裁掉頭尾靜音（門檻取峰值 2%，起音前留 3ms 免得削掉氣音頭）
  2. 變速到同一個長度（atempo，不變調）—— 這才是真的長度切齊，
     用裁切的話像「三」那種字會在母音中間被砍掉
  3. 對齊音量
  4. 頭 5ms 淡入、尾淡出，並補到剛好的長度

音量為什麼不用 EBU R128（ffmpeg 的 loudnorm）：R128 的量測區塊是
400ms，這些字只有 100~400ms，量出來會是 -inf，整個失效。也不能用峰值
（短又密實的字會過響）或整段平均能量（摩擦音多的字會被拉低而過度增益）。
這裡用的是**最響的 100ms 窗**的 RMS —— 耳朵判斷一個短音多大聲，
靠的就是它最響的那一段。

用法：
    python align_voice.py <來源資料夾> <輸出資料夾> [--target 毫秒] [--max-stretch 1.6]

來源資料夾裡要有檔名含 1~8 的音檔（wav/mp3/m4a/ogg 都可以）。
不給 --target 就取八個字裁切後長度的中位數。
--max-stretch 限制變速倍率，超過就不強求等長（變速太多會有金屬聲）。
"""

import argparse
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import wave

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIGITS = "一二三四五六七八"
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac")
SR = 44100


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("ffmpeg 失敗：\n" + " ".join(cmd) + "\n" + (r.stderr or "")[-800:])


def to_wav(src, dst, af=None):
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", src]
    if af:
        cmd += ["-af", af]
    cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", dst]
    run(cmd)


def read_wav(p):
    w = wave.open(p)
    n = w.getnframes()
    s = list(struct.unpack("<%dh" % n, w.readframes(n)))
    w.close()
    return s


def write_wav(p, s):
    w = wave.open(p, "w")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes(struct.pack("<%dh" % len(s), *s))
    w.close()


def trim(s, rel=0.02, pad_ms=3.0):
    peak = max((abs(v) for v in s), default=0)
    if peak == 0:
        return s
    thr = peak * rel
    first = next((i for i, v in enumerate(s) if abs(v) > thr), 0)
    last = next((i for i in range(len(s) - 1, -1, -1) if abs(s[i]) > thr), len(s) - 1)
    pad = int(SR * pad_ms / 1000)
    return s[max(0, first - pad): min(len(s), last + pad + 1)]


def loudness(s, win_ms=100.0, hop_ms=10.0):
    """最響的 100ms 窗的 RMS，回傳 dBFS。短於一個窗就量整段。"""
    if not s:
        return -120.0
    win = min(len(s), int(SR * win_ms / 1000))
    hop = max(1, int(SR * hop_ms / 1000))
    best = 0.0
    for i in range(0, max(1, len(s) - win + 1), hop):
        acc = 0.0
        for v in s[i:i + win]:
            acc += (v / 32768.0) ** 2
        best = max(best, math.sqrt(acc / win))
    return 20 * math.log10(best) if best > 0 else -120.0


def atempo_chain(ratio):
    parts, r = [], ratio
    while r > 2.0:
        parts.append("atempo=2.0"); r /= 2.0
    while r < 0.5:
        parts.append("atempo=0.5"); r /= 0.5
    parts.append("atempo=%.6f" % r)
    return ",".join(parts)


def shape(s, target_n, fade_in_ms=5.0, fade_out_ms=None):
    """補／切到剛好 target_n 個取樣，兩端做淡入淡出。"""
    s = s[:target_n] + [0] * max(0, target_n - len(s))
    fi = int(SR * fade_in_ms / 1000)
    fo = int(SR * (fade_out_ms if fade_out_ms else
                   min(30.0, target_n / SR * 1000 * 0.12)) / 1000)
    for i in range(min(fi, len(s))):
        s[i] = int(s[i] * i / fi)
    for i in range(min(fo, len(s))):
        s[-1 - i] = int(s[-1 - i] * i / fo)
    return s


def find_inputs(d):
    found = {}
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(AUDIO_EXT):
            continue
        base = os.path.splitext(name)[0]
        m = re.search(r"[1-8]", base)
        idx = int(m.group()) if m else next(
            (i + 1 for i in range(8) if DIGITS[i] in base), None)
        if idx and idx not in found:
            found[idx] = os.path.join(d, name)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--target", type=float, default=None, help="目標長度（毫秒），預設中位數")
    ap.add_argument("--max-stretch", type=float, default=1.6, help="變速倍率上限，預設 1.6")
    ap.add_argument("--bitrate", default="192k")
    a = ap.parse_args()

    os.makedirs(a.dst, exist_ok=True)
    files = find_inputs(a.src)
    missing = [DIGITS[i - 1] for i in range(1, 9) if i not in files]
    if missing:
        print("缺少：" + "、".join(missing))
    if not files:
        raise SystemExit("來源資料夾裡找不到 1~8 的音檔")

    tmp = tempfile.mkdtemp(prefix="align_")
    info = {}

    for i, src in sorted(files.items()):
        raw = os.path.join(tmp, "r%d.wav" % i)
        to_wav(src, raw)
        s = trim(read_wav(raw))
        cut = os.path.join(tmp, "c%d.wav" % i)
        write_wav(cut, s)
        info[i] = {"cut": cut, "orig_ms": len(read_wav(raw)) / SR * 1000,
                   "trim_ms": len(s) / SR * 1000}

    lens = sorted(v["trim_ms"] for v in info.values())
    target_ms = a.target if a.target else lens[len(lens) // 2]
    target_n = int(SR * target_ms / 1000)

    # 變速 + 量響度
    for i, v in sorted(info.items()):
        ratio = v["trim_ms"] / target_ms
        ratio = max(1 / a.max_stretch, min(a.max_stretch, ratio))
        v["ratio"] = ratio
        st = os.path.join(tmp, "s%d.wav" % i)
        if abs(ratio - 1.0) < 1e-4:
            st = v["cut"]
        else:
            to_wav(v["cut"], st, atempo_chain(ratio))
        v["pcm"] = read_wav(st)
        v["loud"] = loudness(v["pcm"])

    # 對齊到中位響度，再整組退避避免削波
    louds = sorted(v["loud"] for v in info.values())
    tgt_loud = louds[len(louds) // 2]
    headroom = 0.0
    for v in info.values():
        g = 10 ** ((tgt_loud - v["loud"]) / 20)
        pk = max((abs(x) for x in v["pcm"]), default=1) / 32768 * g
        headroom = max(headroom, pk)
    back = min(1.0, 0.89 / headroom) if headroom > 0 else 1.0

    print("目標長度 %.0fms　目標響度 %.1f dBFS（最響 100ms 窗）" % (target_ms, tgt_loud))
    if back < 1.0:
        print("整組退避 %.1f dB 以避免削波" % (20 * math.log10(back)))
    print("\n%-3s %9s %9s %8s %9s %11s" % ("字", "原長ms", "裁後ms", "變速", "成品ms", "響度dBFS"))

    outs = []
    for i, v in sorted(info.items()):
        g = 10 ** ((tgt_loud - v["loud"]) / 20) * back
        pcm = [max(-32768, min(32767, int(x * g))) for x in v["pcm"]]
        pcm = shape(pcm, target_n)
        fin = os.path.join(tmp, "f%d.wav" % i)
        write_wav(fin, pcm)
        out = os.path.join(a.dst, "%d.mp3" % i)
        run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", fin,
             "-ac", "1", "-ar", str(SR), "-c:a", "libmp3lame", "-b:a", a.bitrate, out])
        chk = os.path.join(tmp, "k%d.wav" % i)
        to_wav(out, chk)
        cp = read_wav(chk)
        ms, lo = len(cp) / SR * 1000, loudness(cp)
        outs.append((ms, lo))
        print("%-3s %9.0f %9.0f %7.2fx %9.0f %11.1f"
              % (DIGITS[i - 1], v["orig_ms"], v["trim_ms"], v["ratio"], ms, lo))

    ls = [o[0] for o in outs]; us = [o[1] for o in outs]
    print("\n長度差 %.0fms　響度差 %.1f dB" % (max(ls) - min(ls), max(us) - min(us)))
    print("輸出：" + a.dst)


if __name__ == "__main__":
    main()
