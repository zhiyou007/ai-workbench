# -*- coding: utf-8 -*-
"""Agnes AI 免费全家桶 · 工作台后端
能力：文生图 / 图生图·多图合成 / 文生视频·图生视频 / 图像理解 / 文本聊天
任务系统：耗时任务（视频等）走持久化队列，后台 worker 轮询，服务重启不丢任务
模型（当前限时免费，实测可用）：agnes-image-2.5-flash · agnes-video-2.5-flash · agnes-3.0-flash · agnes-2.5-flash
用法：python app.py  →  http://127.0.0.1:8010
"""
import asyncio
import base64
import json
import os
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR                               # ai-workbench（独立项目）
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")  # 项目自身的接口配置（key/base_url）
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(DATA_DIR, "output")
VIDEO_DIR = os.path.join(DATA_DIR, "videos")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
TASKS_PATH = os.path.join(DATA_DIR, "tasks.json")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
DOCS_DIR = os.path.join(BASE_DIR, "docs")
os.makedirs(DOCS_DIR, exist_ok=True)

IMAGE_MODEL = "agnes-image-2.5-flash"   # 图像：实测可用（比 2.1 更强）
TEXT_MODEL = "agnes-3.0-flash"          # 聊天：实测可用
VISION_MODEL = "agnes-2.5-flash"        # 图像理解：官方文档明确支持 image_url 输入
VIDEO_MODEL = "agnes-video-2.5-flash"   # 视频：实测可用（¥0/秒）

LEVELS = ["1K", "2K", "3K", "4K"]
RATIOS = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]
VIDEO_SECONDS = ["4", "5", "6", "8", "10", "12"]
MAX_N = 4
MAX_REF = 5          # 参考图上限（图像/视频一致）
MAX_PROMPT = 2000
MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
TASK_TYPES = ("image", "video")
TASK_STATUS = ("queued", "running", "completed", "failed", "cancelled")
MAX_TASKS = 200                  # 任务列表上限：超出自动裁剪最旧的终态任务
MAX_VIDEO_RETRIES = 10           # 视频任务遇上游繁忙（队列满/5xx）最多自动重试 10 次，全失败则任务失败
VIDEO_RETRY_INTERVAL = 120       # 重试间隔（秒）：队列满等繁忙错误每 2 分钟重试一次

LONGVIDEO_MAX_SEGS = 10           # 长视频接力最多 10 段（10×12s = 120 秒）
LONGVIDEO_SECONDS = "12"         # 每段固定最长档 12 秒（段数最少、接缝最少）
XFADE_SECONDS = 0.5              # 段间交叉淡化时长
TOOLS_DIR = os.path.join(BASE_DIR, "tools")


def _find_ffmpeg() -> tuple:
    """返回 (ffmpeg, ffprobe) 可执行路径；未安装返回 (None, None)"""
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = shutil_which(name)
        if p:
            return p, (os.path.join(os.path.dirname(p), "ffprobe.exe")
                       if os.path.dirname(p) else None)
    if os.path.isdir(TOOLS_DIR):
        for root, _, files in os.walk(TOOLS_DIR):
            if "ffmpeg.exe" in files:
                return (os.path.join(root, "ffmpeg.exe"),
                        os.path.join(root, "ffprobe.exe"))
    return None, None


def shutil_which(name: str):
    try:
        import shutil
        return shutil.which(name)
    except Exception:
        return None


def _run_ff(args: list, timeout: int = 120) -> str:
    """运行 ffmpeg/ffprobe 子进程，返回 stdout"""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{os.path.basename(args[0])} 失败: {proc.stderr[-500:]}")
    return proc.stdout


def _seg_duration(path: str) -> float:
    ff, fp = _find_ffmpeg()
    if not fp:
        raise RuntimeError("未找到 ffprobe")
    out = _run_ff([fp, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(out.strip() or 0)


def _has_audio(path: str) -> bool:
    ff, fp = _find_ffmpeg()
    if not fp:
        return False
    out = _run_ff([fp, "-v", "error", "-select_streams", "a",
                   "-show_entries", "stream=index", "-of", "csv=p=0", path])
    return bool(out.strip())


def _grab_last_frame(video_path: str, jpg_path: str):
    """取视频最后 0.25 秒处的一帧作为尾帧图"""
    ff, _ = _find_ffmpeg()
    if not ff:
        raise RuntimeError("未找到 ffmpeg")
    _run_ff([ff, "-y", "-sseof", "-0.25", "-i", video_path,
             "-frames:v", "1", "-q:v", "3", jpg_path])


def _stitch_segments(seg_files: list, out_path: str):
    """ffmpeg xfade 交叉淡化拼接多个分段（每段 12s，淡化 0.5s）"""
    ff, _ = _find_ffmpeg()
    if not ff:
        raise RuntimeError("未找到 ffmpeg")
    n = len(seg_files)
    fade = XFADE_SECONDS
    durs = [_seg_duration(f) for f in seg_files]
    cmd = [ff, "-y"]
    for f in seg_files:
        cmd += ["-i", f]
    # 视频 xfade 链
    vf, vi = [], "v0"
    off = 0.0
    for i in range(1, n):
        off = sum(durs[:i]) - i * fade
        vf.append(f"[v{i-1}][{i}:v]xfade=transition=fade:duration={fade}:offset={off:.3f}[v{i}]")
    filters = ";".join(vf)
    # 音频 acrossfade 链（首段有音轨才处理；无音轨输出无声）
    af, ai = [], None
    has_aud = any(_has_audio(f) for f in seg_files)
    if has_aud:
        for i in range(1, n):
            af.append(f"[a{i-1}][{i}:a]acrossfade=d={fade}:c1=tri:c2=tri[a{i}]")
        filters = filters + ";" + ";".join(af)
        ai = f"[a{n-1}]"
    cmd += ["-filter_complex", filters]
    if n > 1:
        cmd += ["-map", f"[v{n-1}]"]
        if ai:
            cmd += ["-map", ai]
        else:
            cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    if ai:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += [out_path]
    _run_ff(cmd, timeout=600)


def _is_retryable(detail: str) -> bool:
    """上游错误是否值得自动重试（队列满 / 5xx / 限流），额度、模型、参数类错误不重试"""
    s = detail or ""
    low = s.lower()
    # 参数/格式类错误（400 invalid_request 等）：重试无意义，直接失败
    if "400" in s or "invalid_request" in low or "invalid" in low or "not_supported" in low:
        return False
    if "video_queue_full" in s:
        return True
    if any(c in s for c in ("429", "502", "503", "504", "500")):
        return True
    if "quota" in low or "insufficient" in low or "model_not_found" in low or "balance" in low:
        return False
    return False


def load_image_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        raise HTTPException(500, f"读取 config.json 失败: {str(e)[:120]}")
    img = cfg.get("image") or {}
    if not (img.get("base_url") and img.get("api_key")):
        raise HTTPException(500, "config.json 中未配置接口（base_url / api_key）")
    return img


def _call(img: dict, url: str, payload: dict = None, method: str = "POST", timeout: int = 300) -> dict:
    """统一调用 agnes OpenAI 兼容接口"""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {img['api_key']}"}
    try:
        with httpx.Client(timeout=timeout) as c:
            if method == "GET":
                r = c.get(url, headers=headers)
            else:
                r = c.post(url, json=payload or {}, headers=headers)
        if r.status_code != 200:
            raise HTTPException(502, f"上游接口返回 {r.status_code}：{r.text[:300]}")
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"调用接口失败：{str(e)[:200]}")


def _to_data_uri(path: str) -> str:
    mime = "image/png"
    if path.lower().endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif path.lower().endswith(".webp"):
        mime = "image/webp"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def _resolve_ref(ref: str) -> str:
    """把参考图引用归一化为可提交给 agnes 的 data URI / 公开 URL"""
    ref = (ref or "").strip()
    if ref.startswith("data:"):
        return ref
    if ref.startswith("/files/"):
        rel = ref[len("/files/"):].replace("/", os.sep)
        p = os.path.join(DATA_DIR, rel)
        if os.path.exists(p):
            return _to_data_uri(p)
        raise HTTPException(400, f"本地参考图不存在：{ref}")
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref  # 公开 URL 直接透传
    raise HTTPException(400, "参考图格式不支持")


def _resolve_refs(refs) -> list:
    items = refs or []
    if isinstance(items, str):
        items = [items]
    out, seen = [], set()
    for r in items[:MAX_REF]:
        uri = _resolve_ref(r)
        if uri not in seen:
            seen.add(uri)
            out.append(uri)
    return out


# ============ 历史 ============

def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history: list):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


def _store_results(data: dict, prompt: str, level: str, ratio: str, n: int, mode: str) -> dict:
    """解析图像上游返回，保存文件并写历史"""
    files, revised = [], ""
    for it in data.get("data", []):
        fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
        fpath = os.path.join(OUT_DIR, fname)
        if "b64_json" in it and it["b64_json"]:
            with open(fpath, "wb") as f:
                f.write(base64.b64decode(it["b64_json"]))
        elif "url" in it and it["url"]:
            try:
                with httpx.Client(timeout=180, follow_redirects=True) as c:
                    rr = c.get(it["url"])
                    rr.raise_for_status()
                    with open(fpath, "wb") as f:
                        f.write(rr.content)
            except Exception as e:
                raise HTTPException(502, f"下载上游图片失败：{str(e)[:150]}")
        else:
            continue
        files.append(f"/files/output/{fname}")
        if not revised and it.get("revised_prompt"):
            revised = it["revised_prompt"]
    if not files:
        raise HTTPException(502, "上游接口未返回图片数据")
    history = load_history()
    record = {
        "id": uuid.uuid4().hex[:12],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prompt": prompt,
        "level": level,
        "ratio": ratio,
        "n": len(files),
        "mode": mode,
        "files": files,
    }
    history.insert(0, record)
    save_history(history[:200])
    return {"ok": True, "files": files, "revised_prompt": revised or "", "record": record}


# ============ 任务队列（持久化 + 后台 worker） ============

_tasks_lock = threading.Lock()


def load_tasks() -> list:
    if not os.path.exists(TASKS_PATH):
        return []
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_tasks(tasks: list):
    # 调用方（add_task/update_task/delete_task）已持有 _tasks_lock，这里不再加锁（锁不可重入）
    with open(TASKS_PATH, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=1)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _now_plus(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))


def _trim_tasks(tasks: list):
    """任务列表超上限时，优先删除最旧的终态任务（活跃任务保留）"""
    while len(tasks) > MAX_TASKS:
        idx = next((i for i, t in enumerate(tasks)
                    if t.get("status") in ("completed", "failed", "cancelled")), None)
        if idx is None:
            break
        tasks.pop(idx)


def add_task(type_: str, mode: str, prompt: str, params: dict,
             status: str = "queued", progress: int = 0,
             error: str = None, result: dict = None, video_id: str = None) -> dict:
    t = {
        "id": uuid.uuid4().hex[:12],
        "type": type_,
        "mode": mode,
        "prompt": prompt,
        "params": params,
        "status": status,
        "progress": progress,
        "error": error,
        "result": result,
        "video_id": video_id,
        "retries": 0,
        "retry_at": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _tasks_lock:
        tasks = load_tasks()
        tasks.append(t)
        _trim_tasks(tasks)
        save_tasks(tasks)
    return t


def update_task(tid: str, **kw) -> dict | None:
    with _tasks_lock:
        tasks = load_tasks()
        for t in tasks:
            if t["id"] == tid:
                t.update(kw)
                t["updated_at"] = _now()
                save_tasks(tasks)
                return t
    return None


def get_task(tid: str) -> dict | None:
    for t in load_tasks():
        if t["id"] == tid:
            return t
    return None


def _fail_or_retry(tid: str, detail: str):
    """上游错误：可重试的进入退避重试（保持 queued），否则标记失败"""
    t = get_task(tid)
    if not t:
        return
    retries = t.get("retries") or 0
    if _is_retryable(detail) and retries < MAX_VIDEO_RETRIES:
        update_task(tid, retries=retries + 1,
                    retry_at=_now_plus(VIDEO_RETRY_INTERVAL),
                    error=f"上游繁忙（第 {retries + 1} 次重试等待中）：{detail[:200]}")
        return
    suffix = f"，已自动重试 {retries} 次仍失败" if retries else ""
    update_task(tid, status="failed", error=detail[:300] + suffix)


def _create_video_task(t: dict):
    """把 queued 视频任务真正提交给 agnes；失败按策略自动重试或标记失败"""
    img = load_image_cfg()
    params = t.get("params") or {}
    mode = params.get("mode", "text")
    payload = {
        "model": VIDEO_MODEL,
        "prompt": t.get("prompt", ""),
        "seconds": str(params.get("seconds", "5")),
        "mode": mode,
        "size": "720P",
        "aspect_ratio": params.get("aspect_ratio", "16:9"),
    }
    if mode == "reference":
        try:
            refs = _resolve_refs(params.get("refs") or [])
        except HTTPException as e:
            update_task(t["id"], status="failed", error=e.detail)
            return
        if not refs:
            update_task(t["id"], status="failed", error="reference 模式需要至少一张参考图")
            return
        payload["images"] = refs
    try:
        resp = _call(img, f"{img['base_url'].rstrip('/')}/v1/videos", payload, timeout=120)
    except HTTPException as e:
        _fail_or_retry(t["id"], e.detail)
        return
    vid = resp.get("video_id") or resp.get("id") or resp.get("task_id")
    if not vid:
        _fail_or_retry(t["id"], f"上游未返回 video_id：{str(resp)[:150]}")
        return
    update_task(t["id"], status="running", progress=10, video_id=vid, retry_at=None, error=None)


def _poll_video_task(t: dict):
    """轮询 running 视频任务；completed 后下载 mp4、写历史"""
    img = load_image_cfg()
    url = f"{img['base_url'].rstrip('/')}/agnesapi?video_id={t['video_id']}&model_name={VIDEO_MODEL}"
    try:
        resp = _call(img, url, method="GET", timeout=60)
    except HTTPException:
        return  # 网络抖动，保留 running 下次再轮询
    status = resp.get("status", "pending")
    if status == "completed" and resp.get("url"):
        try:
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.mp4"
            fpath = os.path.join(VIDEO_DIR, fname)
            with httpx.Client(timeout=300, follow_redirects=True) as c:
                rr = c.get(resp["url"])
                rr.raise_for_status()
                with open(fpath, "wb") as f:
                    f.write(rr.content)
            local = f"/files/videos/{fname}"
            update_task(t["id"], status="completed", progress=100,
                        result={"url": local, "files": [local]})
            history = load_history()
            record = {
                "id": uuid.uuid4().hex[:12],
                "time": _now(),
                "prompt": t.get("prompt", ""),
                "level": "720P",
                "ratio": (t.get("params") or {}).get("aspect_ratio", "16:9"),
                "n": 1,
                "mode": "video",
                "files": [local],
            }
            history.insert(0, record)
            save_history(history[:200])
        except Exception as e:
            update_task(t["id"], status="failed", error=f"下载视频失败：{str(e)[:150]}")
    elif status in ("failed", "error", "cancelled"):
        update_task(t["id"], status="failed", error=resp.get("error") or f"上游状态：{status}")
    else:
        progress = resp.get("progress") or 0
        if isinstance(progress, (int, float)) and progress > 0:
            update_task(t["id"], progress=min(99, int(progress)))


async def _tick():
    """每个 tick：优先轮询 running 视频/长视频任务，否则推进一个到期的 queued 任务"""
    for t in load_tasks():
        if t.get("type") == "video" and t.get("status") == "running" and t.get("video_id"):
            try:
                await asyncio.to_thread(_poll_video_task, t)
            except Exception:
                pass
            return
    for t in load_tasks():
        if t.get("type") == "longvideo" and t.get("status") == "running":
            try:
                await asyncio.to_thread(_poll_longvideo, t)
            except Exception:
                pass
            return
    now = _now()
    for t in load_tasks():
        if t.get("type") == "video" and t.get("status") == "queued":
            retry_at = t.get("retry_at")
            if retry_at and retry_at > now:
                continue  # 退避等待中，未到重试时间
            try:
                await asyncio.to_thread(_create_video_task, t)
            except Exception:
                pass
            return
    for t in load_tasks():
        if t.get("type") == "longvideo" and t.get("status") == "queued":
            retry_at = t.get("retry_at")
            if retry_at and retry_at > now:
                continue
            try:
                await asyncio.to_thread(_start_longvideo, t)
            except Exception:
                pass
            return


# ============ 长视频接力（分段 × 12s，尾帧接力 + ffmpeg 拼接） ============

def _start_longvideo(t: dict):
    update_task(t["id"], status="running", progress=10, error=None, retry_at=None)


def _longvideo_refs(t: dict, idx: int) -> list:
    """当前段参考图：首段用锚定图（可为空=文生视频），后续段用上一段尾帧"""
    segs = t.get("segs") or []
    if idx == 0:
        return t.get("anchor") or []
    prev = segs[idx - 1] if idx - 1 < len(segs) else {}
    last_frame = prev.get("last_frame") or ""
    return [last_frame] if last_frame else []


def _longvideo_progress(t: dict) -> int:
    segs = t.get("segs") or []
    idx = t.get("seg_idx") or 0
    total = max(1, len(segs))
    return min(90, int(10 + idx / total * 80))


def _fail_or_retry_longvideo(t: dict, detail: str):
    """长视频段提交失败：可重试则 2 分钟后再试当前段，超 10 次则任务失败"""
    retries = t.get("retries") or 0
    if _is_retryable(detail) and retries < MAX_VIDEO_RETRIES:
        segs = t.get("segs") or []
        idx = t.get("seg_idx") or 0
        if idx < len(segs):
            segs[idx]["video_id"] = None
        update_task(t["id"], segs=segs, retries=retries + 1,
                    retry_at=_now_plus(VIDEO_RETRY_INTERVAL),
                    error=f"第 {idx + 1}/{len(segs)} 段上游繁忙（第 {retries + 1} 次重试等待中）：{detail[:180]}")
        return
    suffix = f"，已自动重试 {retries} 次仍失败" if retries else ""
    update_task(t["id"], status="failed", error=detail[:300] + suffix)


def _submit_longvideo_seg(t: dict):
    """提交当前分镜段到 agnes 视频接口（参考图一律转 Base64，上游不支持本地路径）"""
    segs = t.get("segs") or []
    idx = t.get("seg_idx") or 0
    if idx >= len(segs):
        return
    seg = segs[idx]
    params = t.get("params") or {}
    refs = _longvideo_refs(t, idx)
    if refs:
        try:
            refs = _resolve_refs(refs)  # 本地尾帧图 → Base64 data URI
        except HTTPException as e:
            update_task(t["id"], status="failed", error=f"素材不可用：{e.detail}")
            return
    payload = {
        "model": VIDEO_MODEL,
        "prompt": seg.get("desc") or t.get("prompt", ""),
        "seconds": str(params.get("seconds", LONGVIDEO_SECONDS)),
        "mode": "reference" if refs else "text",
        "size": "720P",
        "aspect_ratio": params.get("aspect_ratio", "16:9"),
    }
    if refs:
        payload["images"] = refs
    img = load_image_cfg()
    try:
        resp = _call(img, f"{img['base_url'].rstrip('/')}/v1/videos", payload, timeout=120)
    except HTTPException as e:
        _fail_or_retry_longvideo(t, e.detail)
        return
    vid = resp.get("video_id") or resp.get("id") or resp.get("task_id")
    if not vid:
        _fail_or_retry_longvideo(t, f"上游未返回 video_id：{str(resp)[:150]}")
        return
    seg["video_id"] = vid
    segs[idx] = seg
    update_task(t["id"], segs=segs, status="running", progress=_longvideo_progress(t),
                error=None, retry_at=None)


def _poll_longvideo(t: dict):
    """推进长视频任务：轮询当前段 → 下载/抽尾帧 → 下一段；全部完成则拼接出片"""
    segs = t.get("segs") or []
    idx = t.get("seg_idx") or 0
    retry_at = t.get("retry_at")
    if retry_at and retry_at > _now():
        return  # 段提交失败后处于退避等待
    if idx >= len(segs):
        _finish_longvideo(t)
        return
    seg = segs[idx]
    if not seg.get("video_id"):
        _submit_longvideo_seg(t)
        return
    img = load_image_cfg()
    url = f"{img['base_url'].rstrip('/')}/agnesapi?video_id={seg['video_id']}&model_name={VIDEO_MODEL}"
    try:
        resp = _call(img, url, method="GET", timeout=60)
    except HTTPException:
        return  # 网络抖动，下轮再查
    status = resp.get("status", "pending")
    if status == "completed" and resp.get("url"):
        try:
            seg_file = os.path.join(VIDEO_DIR, f"_seg_{t['id']}_{idx}.mp4")
            with httpx.Client(timeout=300, follow_redirects=True) as c:
                rr = c.get(resp["url"])
                rr.raise_for_status()
                with open(seg_file, "wb") as f:
                    f.write(rr.content)
            seg["file"] = seg_file
            if idx + 1 < len(segs):
                last_frame = os.path.join(VIDEO_DIR, f"_seg_{t['id']}_{idx}_last.jpg")
                _grab_last_frame(seg_file, last_frame)
                seg["last_frame"] = f"/files/videos/{os.path.basename(last_frame)}"
            segs[idx] = seg
            update_task(t["id"], segs=segs, seg_idx=idx + 1, retry_at=None,
                        progress=_longvideo_progress({**t, "seg_idx": idx + 1, "segs": segs}))
        except Exception as e:
            _fail_or_retry_longvideo(t, f"段 {idx + 1} 下载/抽帧失败：{str(e)[:150]}")
    elif status in ("failed", "error", "cancelled"):
        _fail_or_retry_longvideo(t, resp.get("error") or f"上游状态：{status}")
    else:
        progress = resp.get("progress") or 0
        if isinstance(progress, (int, float)) and progress > 0:
            update_task(t["id"], progress=min(95, _longvideo_progress(t) + int(progress * 0.05)))


def _finish_longvideo(t: dict):
    """所有分段完成：ffmpeg 拼接 → 写历史 → 完成"""
    try:
        segs = t.get("segs") or []
        files = [s["file"] for s in segs if s.get("file")]
        if not files:
            update_task(t["id"], status="failed", error="分段文件缺失")
            return
        update_task(t["id"], progress=95)
        if len(files) == 1:
            final_file = files[0]
        else:
            final_file = os.path.join(
                VIDEO_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_long.mp4")
            _stitch_segments(files, final_file)
        local = f"/files/videos/{os.path.basename(final_file)}"
        update_task(t["id"], status="completed", progress=100,
                    result={"url": local, "files": [local]})
        history = load_history()
        record = {
            "id": uuid.uuid4().hex[:12],
            "time": _now(),
            "prompt": t.get("prompt", ""),
            "level": "720P",
            "ratio": (t.get("params") or {}).get("aspect_ratio", "16:9"),
            "n": 1,
            "mode": "video",
            "blocks": (t.get("params") or {}).get("blocks", []),
            "seg_seconds": (t.get("params") or {}).get("seconds", "12"),
            "files": [local],
        }
        history.insert(0, record)
        save_history(history[:200])
        # 清理分段临时文件
        for s in segs:
            p = s.get("file")
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception as e:
        update_task(t["id"], status="failed", error=f"拼接失败：{str(e)[:200]}")


async def _worker_loop():
    while True:
        await asyncio.sleep(2)
        try:
            await _tick()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    w = asyncio.create_task(_worker_loop())
    yield
    w.cancel()


app = FastAPI(title="Agnes AI 免费全家桶 · 工作台", lifespan=lifespan)


class GenRequest(BaseModel):
    prompt: str
    level: str = "1K"
    ratio: str = "1:1"
    n: int = 1


class EditRequest(BaseModel):
    prompt: str
    level: str = "1K"
    ratio: str = "1:1"
    n: int = 1
    refs: list = []


class VideoRequest(BaseModel):
    prompt: str
    mode: str = "text"          # text | reference
    seconds: str = "5"
    aspect_ratio: str = "16:9"
    refs: list = []


class LongVideoRequest(BaseModel):
    blocks: list = []           # 每段动作描述（行=段）
    refs: list = []             # 首段锚定图（可空=文生视频）
    aspect_ratio: str = "16:9"
    seconds: str = "12"         # 每段时长档位（4/5/6/8/10/12 秒）


class UnderstandRequest(BaseModel):
    question: str
    ref: str = ""


class ChatRequest(BaseModel):
    messages: list = []


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.post("/api/generate")
def generate(req: GenRequest):
    """文生图（agnes-image-2.5-flash，1K~4K 档位 + 比例）"""
    prompt = (req.prompt or "").strip()[:MAX_PROMPT]
    if not prompt:
        raise HTTPException(400, "提示词不能为空")
    n = max(1, min(MAX_N, int(req.n or 1)))
    level = req.level if req.level in LEVELS else "1K"
    ratio = req.ratio if req.ratio in RATIOS else "1:1"
    params = {"level": level, "ratio": ratio, "n": n}
    try:
        img = load_image_cfg()
        payload = {"model": IMAGE_MODEL, "prompt": prompt, "n": n, "size": level, "ratio": ratio, "return_base64": True}
        data = _call(img, f"{img['base_url'].rstrip('/')}/v1/images/generations", payload)
        res = _store_results(data, prompt, level, ratio, n, "text2img")
        add_task("image", "text2img", prompt, params, status="completed", progress=100,
                 result={"files": res["files"]})
        return res
    except HTTPException as e:
        add_task("image", "text2img", prompt, params, status="failed", error=e.detail)
        raise


@app.post("/api/edit")
def edit_image(req: EditRequest):
    """图生图 / 多图合成（refs 可传 1~5 张参考图）"""
    prompt = (req.prompt or "").strip()[:MAX_PROMPT]
    if not prompt:
        raise HTTPException(400, "修改指令不能为空")
    n = max(1, min(MAX_N, int(req.n or 1)))
    level = req.level if req.level in LEVELS else "1K"
    ratio = req.ratio if req.ratio in RATIOS else "1:1"
    params = {"level": level, "ratio": ratio, "n": n, "refs": req.refs}
    try:
        refs = _resolve_refs(req.refs)
        if not refs:
            raise HTTPException(400, "请提供至少一张参考图")
        img = load_image_cfg()
        payload = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "n": n,
            "size": level,
            "ratio": ratio,
            "extra_body": {"image": refs, "response_format": "b64_json"},
        }
        data = _call(img, f"{img['base_url'].rstrip('/')}/v1/images/generations", payload)
        res = _store_results(data, prompt, level, ratio, n, "edit")
        add_task("image", "edit", prompt, params, status="completed", progress=100,
                 result={"files": res["files"]})
        return res
    except HTTPException as e:
        add_task("image", "edit", prompt, params, status="failed", error=e.detail)
        raise


@app.post("/api/video")
def create_video(req: VideoRequest):
    """创建视频任务：进入持久化队列，worker 后台处理（立即返回 task_id）"""
    prompt = (req.prompt or "").strip()[:MAX_PROMPT]
    if not prompt:
        raise HTTPException(400, "视频描述不能为空")
    mode = req.mode if req.mode in ("text", "reference") else "text"
    seconds = str(req.seconds) if str(req.seconds) in VIDEO_SECONDS else "5"
    ar = req.aspect_ratio if req.aspect_ratio in RATIOS else "16:9"
    if mode == "reference" and not (req.refs or []):
        raise HTTPException(400, "reference 模式需要至少一张参考图")
    t = add_task("video", mode, prompt,
                 {"mode": mode, "seconds": seconds, "aspect_ratio": ar, "refs": req.refs})
    return {"task_id": t["id"], "status": t["status"]}


@app.post("/api/longvideo")
def create_longvideo(req: LongVideoRequest):
    """创建长视频接力任务：每段可选手长（4/5/6/8/10/12s），自动尾帧接力 + ffmpeg 拼接"""
    blocks = [str(b or "").strip()[:500] for b in (req.blocks or [])]
    blocks = [b for b in blocks if b]
    if not (1 <= len(blocks) <= LONGVIDEO_MAX_SEGS):
        raise HTTPException(400, f"分镜描述需要 1~{LONGVIDEO_MAX_SEGS} 段（每段 12 秒，最多 120 秒）")
    sec = str(req.seconds) if str(req.seconds) in VIDEO_SECONDS else LONGVIDEO_SECONDS
    ar = req.aspect_ratio if req.aspect_ratio in RATIOS else "16:9"
    try:
        anchor = _resolve_refs(req.refs)  # 首段锚定图（可空=文生视频）
    except HTTPException as e:
        raise HTTPException(400, f"锚定图无效：{e.detail}")
    params = {"blocks": blocks, "aspect_ratio": ar, "seconds": sec, "refs": req.refs}
    segs = [{"desc": b, "video_id": None, "file": None, "last_frame": None} for b in blocks]
    t = add_task("longvideo", "longvideo", blocks[0], params)
    with _tasks_lock:
        tasks = load_tasks()
        for x in tasks:
            if x["id"] == t["id"]:
                x["segs"] = segs
                x["seg_idx"] = 0
                x["anchor"] = anchor
                save_tasks(tasks)
                break
    return {"task_id": t["id"], "status": t["status"],
            "total_seconds": len(blocks) * int(sec)}


@app.get("/api/tasks")
def tasks():
    items = load_tasks()
    return list(reversed(items))[:100]


@app.get("/api/tasks/{tid}")
def task_detail(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.post("/api/tasks/{tid}/cancel")
def cancel_task(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] not in ("queued", "running"):
        raise HTTPException(400, f"当前状态 {t['status']} 不可取消")
    update_task(tid, status="cancelled", error="用户取消")
    return {"ok": True, "status": "cancelled"}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str):
    with _tasks_lock:
        tasks = load_tasks()
        kept = [t for t in tasks if t["id"] != tid]
        save_tasks(kept)
    return {"ok": True, "deleted": len(tasks) - len(kept)}


@app.post("/api/understand")
def understand(req: UnderstandRequest):
    """图像理解：agnes-2.5-flash 读图回答"""
    question = (req.question or "").strip()[:MAX_PROMPT]
    if not question:
        raise HTTPException(400, "问题不能为空")
    ref = _resolve_ref(req.ref) if req.ref else ""
    if not ref:
        raise HTTPException(400, "请选择或上传一张图片")
    content = [{"type": "text", "text": question}]
    content.append({"type": "image_url", "image_url": {"url": ref}})
    img = load_image_cfg()
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 1500,
    }
    resp = _call(img, f"{img['base_url'].rstrip('/')}/v1/chat/completions", payload)
    answer = resp["choices"][0]["message"]["content"]
    return {"answer": answer, "model": VISION_MODEL}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """文本聊天：agnes-3.0-flash"""
    msgs = req.messages or []
    msgs = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))[:4000]}
            for m in msgs[:20] if m.get("content")]
    if not msgs:
        raise HTTPException(400, "消息不能为空")
    img = load_image_cfg()
    payload = {"model": TEXT_MODEL, "messages": msgs, "max_tokens": 2048}
    resp = _call(img, f"{img['base_url'].rstrip('/')}/v1/chat/completions", payload)
    return {"reply": resp["choices"][0]["message"]["content"], "model": TEXT_MODEL}


@app.get("/api/history")
def history():
    return load_history()


@app.delete("/api/history/{hid}")
def delete_history(hid: str):
    history = load_history()
    kept = [h for h in history if h.get("id") != hid]
    save_history(kept)
    return {"ok": True, "deleted": len(history) - len(kept)}


@app.get("/api/config")
def api_config():
    img = load_image_cfg()
    return {
        "base_url": img.get("base_url"),
        "image_model": IMAGE_MODEL,
        "text_model": TEXT_MODEL,
        "vision_model": VISION_MODEL,
        "video_model": VIDEO_MODEL,
        "levels": LEVELS,
        "ratios": RATIOS,
        "video_seconds": VIDEO_SECONDS,
    }


app.mount("/files", StaticFiles(directory=DATA_DIR), name="files")
app.mount("/docs", StaticFiles(directory=DOCS_DIR), name="docs")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8010, log_level="warning")
