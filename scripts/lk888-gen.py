#!/usr/bin/env python3
"""shuohao-skills 专用 lk888.ai 出图命令（与 drama-skills 的 lk888_adapter.py 完全独立）。

用途：作为 character-refs 的 custom 模型，把 {prompt_file} + {refs} 交给
lk888 中转站的 /v1/media/generate，轮询 /v1/skills/task-status，把结果 PNG 写到 {out}。

用法（由 character-refs 按占位符模板调用）：
  python3 lk888-gen.py --prompt-file P.txt --ref a.png b.png \
      --size 1024x1024 --out /path/anchor.png

环境变量：
  LK888_API_KEY      必填，中转站 key（sk-…）
  LK888_BASE_URL     选填，默认 https://api.lk888.ai
  LK888_IMAGE_MODEL  选填，默认 banana-2.1（纳米香蕉 2.1 按次，约 ¥0.087/张）
  LK888_TIMEOUT      选填，轮询超时秒数，默认 600

退出码：0 成功；2 参数/环境错误；3 提交或任务失败。{out} 不存在即失败。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INLINE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

KNOWN_RATIOS = {
    (1, 1): "1:1", (4, 3): "4:3", (3, 4): "3:4",
    (16, 9): "16:9", (9, 16): "9:16",
    (3, 2): "3:2", (2, 3): "2:3", (21, 9): "21:9",
}


class GenError(Exception):
    def __init__(self, message: str, code: int = 3) -> None:
        super().__init__(message)
        self.code = code


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default or "").strip()
    if required and not value:
        raise GenError(f"missing environment variable {name}", code=2)
    return value


def request_json(url: str, *, method: str, token: str, body: dict | None = None) -> tuple[dict, int]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise GenError(f"lk888 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GenError(f"lk888 request failed: {exc.reason}") from exc


def data_uri(path: Path) -> str:
    mime = INLINE_MIME.get(path.suffix.lower())
    if mime is None:
        raise GenError(f"reference image {path} must be png/jpg/webp", code=2)
    if not path.is_file():
        raise GenError(f"reference image {path} does not exist", code=2)
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def pick_ratio(width: int, height: int) -> str:
    g = math.gcd(width, height)
    key = (width // g, height // g)
    if key in KNOWN_RATIOS:
        return KNOWN_RATIOS[key]
    # 就近归到常用比例，避免渠道拒单
    target = width / height
    best = min(KNOWN_RATIOS, key=lambda k: abs(k[0] / k[1] - target))
    return KNOWN_RATIOS[best]


def pick_size(width: int, height: int) -> str:
    return "2K" if max(width, height) >= 2048 else "1K"


def download(url: str, out: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "lk888-gen/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, out.open("wb") as fh:
        fh.write(resp.read())
    # character-refs 要求 {out} 是真 PNG（会校验文件头），而 lk888 常返回 JPEG 字节；
    # 用 macOS 自带的 sips 转码，零第三方依赖
    if out.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        tmp = out.with_suffix(".lk888-tmp" + out.suffix)
        out.replace(tmp)
        import subprocess
        proc = subprocess.run(
            ["sips", "-s", "format", "png", str(tmp), "--out", str(out)],
            capture_output=True, text=True,
        )
        tmp.unlink(missing_ok=True)
        if proc.returncode != 0 or not out.is_file():
            raise GenError(f"sips png conversion failed: {proc.stderr[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt-file")
    parser.add_argument("--prompt")
    parser.add_argument("--neg-file")  # banana 系不支持反向词，仅为兼容占位符
    parser.add_argument("--ref", nargs="*", default=[])
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--seed")
    parser.add_argument("--out", required=True)
    parser.add_argument("--model")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    elif args.prompt:
        prompt = args.prompt.strip()
    else:
        print("lk888-gen: need --prompt-file or --prompt", file=sys.stderr)
        return 2
    if not prompt:
        print("lk888-gen: prompt is empty", file=sys.stderr)
        return 2

    width = args.width or int((args.size.split("x", 1)[0] if "x" in args.size else 1024))
    height = args.height or int((args.size.split("x", 1)[1] if "x" in args.size else 1024))

    base = env("LK888_BASE_URL", "https://api.lk888.ai").rstrip("/")
    token = env("LK888_API_KEY", required=True)
    model = (args.model or env("LK888_IMAGE_MODEL", "banana-2.1")).strip()

    params: dict = {
        "aspectRatio": pick_ratio(width, height),
        "imageSize": pick_size(width, height),
    }
    refs = [Path(p) for p in args.ref if p]
    if refs:
        params["images"] = [data_uri(p) for p in refs]

    created, status = request_json(
        f"{base}/v1/media/generate",
        method="POST",
        token=token,
        body={"model": model, "prompt": prompt, "params": params},
    )
    if status != 200:
        raise GenError(f"lk888 submit failed (HTTP {status})")
    data = created.get("data") or {}
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise GenError(f"lk888 returned no task_id: {json.dumps(created, ensure_ascii=False)[:300]}")

    status_url = f"{base}/v1/skills/task-status?" + urllib.parse.urlencode({"task_id": task_id})
    timeout = float(env("LK888_TIMEOUT", "600"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document, _ = request_json(status_url, method="GET", token=token)
        is_final = document.get("is_final")
        state = document.get("state")
        if is_final is True or state in {"success", "failed"}:
            if state == "failed" or document.get("error"):
                raise GenError(
                    f"lk888 task {task_id} failed: {document.get('error') or document.get('error_code')}"
                )
            urls = document.get("result_urls")
            url = urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) and urls[0] \
                else document.get("result_url")
            if not isinstance(url, str) or not url:
                raise GenError(f"lk888 task {task_id} finished without a result URL")
            download(url, out)
            if not out.is_file() or out.stat().st_size == 0:
                raise GenError(f"downloaded file {out} is empty")
            print(f"lk888-gen: {model} task {task_id} -> {out}", file=sys.stderr)
            return 0
        time.sleep(5)
    raise GenError(f"lk888 task {task_id} poll timed out after {timeout}s")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenError as exc:
        print(f"lk888-gen: {exc}", file=sys.stderr)
        raise SystemExit(exc.code)
