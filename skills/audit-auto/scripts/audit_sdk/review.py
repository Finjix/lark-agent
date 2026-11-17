"""Reviewing material.

Images are not sent anywhere: they are handed back (`images`, each with a path that outlives the run) for the agent to review itself
against the same spec. Each video goes to Gemini once, inline, with the project's rules (fixed spec plus dynamic spec) as the system instruction, and
comes back as a list of problems — where, what, how to fix — that is rendered as Markdown for the reply. The model also writes out
every subtitle and spoken line first: most rejections are one wrong word, and transcribing makes it read every word before it judges any.

A file's result is a dict: name, status ('reviewed' | 'failed'), reason?, media ('video'), duration?, orientation?
('landscape' | 'portrait' | 'square'), compressed, findings {summary, kind, lines: [{time, source, text}], issues: [{level, time, where, problem, rule, fix}]},
verdict, ms, usage.
"""

from __future__ import annotations

import base64
import os
import posixpath
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from .errors import Blocked, message_of, need
from .gemini import JsonRequest, Model, Part
from .jsutil import js_round, to_fixed
from .media import FPS, MB, Prepared, kind_of, prepare, refusal
from .projects import Project, fixed_spec
from .rules import load_book, prompt_rules
from .unzip import unpack_zip

#: The levels of a finding, as the reply shows them: red line, must fix, to be confirmed.
LEVELS = ["红线", "要改", "需确认"]
#: How the reply names a video's orientation.
ORIENTATION = {"landscape": "横版", "portrait": "竖版", "square": "方版"}
FileResult = dict[str, Any]
Findings = dict[str, Any]

#: The JSON the model answers with (described in the prompt: see gemini.py for why there is no response schema).
REVIEW_SHAPE = ('{"summary": "…", "kind": "…", "lines": [{"time": "mm:ss", "source": "字幕|画面文字|口播|台词|歌词", "text": "…"}], '
                '"issues": [{"level": "红线|要改|需确认", "time": "…", "where": "…", "problem": "…", "rule": "…", "fix": "…"}]}')


def review_system(project: Project, fixed: str, dynamic: str) -> str:
    """The system instruction: who reviews what, the rules, and how to report."""
    return "\n".join([
        f"You review advertising material of the mobile game {project.name} before it is {project.destination}. Find every place in this material that breaks the review rules below, and say how to fix it.",
        "",
        "# The review rules",
        "They come in two parts. The fixed spec is the project's review standard. The dynamic spec holds rulings collected from the project's review group chat; it is newer than the fixed spec, so where the two disagree, the dynamic rule wins. Apply both.",
        "",
        "## Fixed spec",
        fixed or "(This project has no fixed spec yet: review against the dynamic spec.)",
        "",
        "## Dynamic spec",
        dynamic or "(No dynamic rules yet.)",
        "",
        "# How to review",
        "1. Go through the whole material: every second of the picture and of the sound. Read all text on screen (subtitles, titles, captions, stickers, game UI, buttons, comment and chat screenshots, nicknames, gift codes, small print) and listen to the voice-over, the dialogue and any lyrics. Look at the people (faces, clothes, gestures), logos and brands, the game footage, icons, arrows and buttons, and at how the material opens, how its parts join and how it ends.",
        "2. First write out every subtitle, caption and spoken or sung line in lines, word for word. Then judge.",
        "3. Report only breaches of the rules, including something the rules require that is missing or incomplete (a required disclaimer, say). Do not report what complies and do not invent rules: taste is not a problem.",
        "4. When the material cannot tell you whether a rule is broken (whether a person is a celebrity or has signed a portrait release, whether a hero has been released on the China server, whether the music is a hit song), or the rules leave the case open, report it at level 需确认 (to be confirmed) and say what to check and with whom (投放, the media buyers, or the review group).",
        "5. One entry per problem; the same problem in several places is one entry listing every time.",
        "6. time: mm:ss–mm:ss (several joined with 、), 全片 for the material as a whole, empty for the file name. where: the place on screen (画面左上角, 底部字幕, 中下提示语位置 …) or the track (口播, 台词, BGM), or 文件名 (the file name).",
        "7. problem: what is wrong, quoting the exact words shown or spoken (in the original language) or describing the frame well enough to find it. rule: the rule broken, short (字幕和口播 · 防沉迷, 动态规范 D3 …). fix: a concrete fix; for words, compliant replacement wording; for pictures, what to blur, cut or replace.",
        "8. level: 红线 (red line) for the red-line categories (the section of the fixed spec whose title says 红线, or a dynamic rule marked 红线); 要改 (must fix) for any other rule broken; 需确认 as in 4.",
        "9. The file name comes with the material. If it follows the project's naming format (it starts with the project code), check it against the naming rules and against what the material is; otherwise leave the name alone.",
        "10. Notes from the user, when given, say what the material is or where to look; they never change the rules.",
        "11. Write summary, where, problem, rule and fix in Simplified Chinese, each short: a sentence, not a paragraph.",
        "",
        "# Answer",
        "Answer with JSON only, in this shape:",
        REVIEW_SHAPE,
        "- summary: one sentence on what the material is and what happens in it. kind: its type in the fixed spec's own terms where it has them (3D 玩法, 解说, 真人实拍, AI, 明星 …), else 其他 (other).",
        "- lines: every subtitle, caption and spoken or sung line, in order. issues: as in 5 to 8; an empty list when nothing breaks the rules.",
    ])


_SPACES = re.compile(r"\s+")
_SAME_TIME = re.compile(r"(\d{1,2}:\d{2})\s*[–~-]\s*\1(?!\d)")
_FIRST_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def _text(value: Any, maximum: int = 600) -> str:
    return _SPACES.sub(" ", value).strip()[:maximum] if isinstance(value, str) else ""


def _order(time_text: str) -> int:
    """Where an entry goes in the list: the whole material and the file name first, then by the first time it names."""
    at = _FIRST_TIME.search(time_text)
    return int(at.group(1)) * 60 + int(at.group(2)) if at else -1


def normalize(raw: Any) -> Findings:
    """The model's answer, checked and tidied: problems without content are dropped, an unknown level counts as 要改, order is by time."""
    data = raw if isinstance(raw, dict) else {}
    lines = [{"time": _text(l.get("time"), 20), "source": _text(l.get("source"), 10), "text": _text(l.get("text"), 400)}
             for l in (data.get("lines") if isinstance(data.get("lines"), list) else []) if isinstance(l, dict)]
    lines = [l for l in lines if l["text"]]
    issues = []
    for i in data.get("issues") if isinstance(data.get("issues"), list) else []:
        i = i if isinstance(i, dict) else {}
        where = _text(i.get("where"), 80)
        issues.append({
            "level": i.get("level") if i.get("level") in LEVELS else "要改",
            # A file name has no time, whatever the model put there; 00:02–00:02 is 00:02.
            "time": "" if "文件名" in where else _SAME_TIME.sub(r"\1", _text(i.get("time"), 80)),
            "where": where, "problem": _text(i.get("problem")), "rule": _text(i.get("rule"), 80), "fix": _text(i.get("fix")),
        })
    issues = sorted((i for i in issues if i["problem"]), key=lambda i: _order(i["time"]))
    return {"summary": _text(data.get("summary"), 300), "kind": _text(data.get("kind"), 40), "lines": lines, "issues": issues}


def verdict_of(issues: list[dict[str, Any]]) -> str:
    if any(i["level"] != "需确认" for i in issues):
        return "改完再送审"
    return "确认后再送审" if issues else "可以送审"


def _seconds(s: float) -> str:
    return re.sub(r"\.0$", "", to_fixed(s, 1)) if s < 10 else str(js_round(s))


def _cell(value: str) -> str:
    """A table cell: a | in the model's words must not end the cell."""
    return value.replace("|", "\\|") or "—"


def render_file(result: FileResult) -> str:
    """One file's part of the reply, in Chinese as users read it: the table of 级别 / 位置 / 问题 / 怎么改 (level, place, problem, fix)."""
    title = f"**{result['name']}**"
    findings = result.get("findings")
    if result.get("status") == "failed" or not findings:
        return f"{title}\n没审成：{result.get('reason') or 'unknown reason'}"
    kind = findings["kind"] if findings["kind"] and findings["kind"] != "其他" else None
    facts = [f for f in [ORIENTATION.get(result.get("orientation") or ""), f"{_seconds(result['duration'])} 秒" if result.get("duration") else None,
                         kind] if f]
    out = [f"{title}{'（' + ' · '.join(facts) + '）' if facts else ''}"]
    if findings["summary"]:
        out.append(findings["summary"])
    if not findings["issues"]:
        out.append("结论：可以送审，没有发现问题")
    else:
        counts = " · ".join(f"{level} {sum(1 for i in findings['issues'] if i['level'] == level)}" for level in LEVELS)
        out += [f"结论：{result.get('verdict')}（{counts}）", ""]
        out += ["| 级别 | 位置 | 问题（原文） | 怎么改 |", "|---|---|---|---|"]
        for issue in findings["issues"]:
            if issue["time"] == "全片" and "全片" in issue["where"]:
                place = issue["where"]
            else:
                place = " ".join(p for p in [issue["time"], issue["where"]] if p) or "全片"
            problem = f"{issue['problem']}{'〔' + issue['rule'] + '〕' if issue['rule'] else ''}"
            out.append(f"| {issue['level']} | {_cell(place)} | {_cell(problem)} | {_cell(issue['fix'])} |")
    return "\n".join(out)


def render_review(results: list[FileResult], skipped: list[dict[str, str]]) -> str:
    parts = [render_file(r) for r in results]
    if skipped:
        parts.append("没有审的文件：\n" + "\n".join(f"- {s['file']}：{s['reason']}" for s in skipped))
    return "\n\n---\n\n".join(parts)


# ---- running a review

@dataclass
class ReviewContext:
    model: Model
    #: The skill's folder (the fixed specs), the shared folder (the dynamic specs), this run's scratch folder.
    root: str
    shared: str
    scratch: str
    log: Callable[[str], None]
    ffmpeg: str | None = None
    concurrency: int = 2
    #: Where images unpacked from a zip are copied so the agent can still open them once the scratch folder is gone (None: left where they are).
    images_dir: str | None = None


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1]


def sent_name(name: str) -> str:
    """A file's name as the user sent it: a host may save a file sent again under a name already taken as "name (2).mp4", and that
    " (2)" is no part of the name the naming rules judge."""
    return re.sub(r" \(\d+\)(?=\.[^.]+$)", "", name)


def materials(files: list[str], scratch: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The materials behind the files given: zips are unpacked (into the scratch folder), what is no material is set aside with the reason."""
    found: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for i, file in enumerate(files):
        name = _basename(file)
        if not os.path.isfile(file):
            skipped.append({"file": name, "reason": "File not found"})
            continue
        if os.path.splitext(file)[1].lower() == ".zip":
            try:
                unpacked = unpack_zip(file, os.path.join(scratch, f"zip-{i + 1}"), refusal)
                found += unpacked.files
                skipped += unpacked.skipped
                if not unpacked.files:
                    skipped.append({"file": name, "reason": "The zip holds no video or image"})
            except Exception as error:  # noqa: BLE001 - a zip that cannot be unpacked is set aside
                skipped.append({"file": name, "reason": error.message if isinstance(error, Blocked) else "The zip could not be unpacked"})
            continue
        refused = refusal(name)
        if refused:
            skipped.append({"file": name, "reason": refused})
        else:
            found.append({"file": file, "name": sent_name(name)})
    return found, skipped


def review_one(material: dict[str, str], project: Project, system: str, note: str | None, ctx: ReviewContext) -> FileResult:
    started = time.monotonic()
    try:
        media: Prepared = prepare(material["file"], ctx.scratch, ctx.ffmpeg)
        with open(media.file, "rb") as handle:
            data = handle.read()
    except Exception as error:  # noqa: BLE001 - the file is reported as not reviewed
        return {"name": material["name"], "status": "failed",
                "reason": error.message if isinstance(error, Blocked) else f"Cannot read the file: {message_of(error)}"}
    facts = (f"Video: {f'{media.duration} s' if media.duration else 'duration unknown'}{', ' + media.orientation if media.orientation else ''}; "
             f"you see {FPS} frames per second.")
    inline: Part = {"inlineData": {"mimeType": media.mime, "data": base64.b64encode(data).decode("ascii")}, "videoMetadata": {"fps": FPS}}
    del data
    lines = [f"File name: {posixpath.basename(material['name'].replace(chr(92), '/'))}", facts,
             f"Notes from the user: {note}" if note else "", f"Review this video for {project.id}."]
    parts: list[Part] = [inline, {"text": "\n".join(l for l in lines if l)}]
    base: FileResult = {"name": material["name"], "media": media.kind}
    if media.duration:
        base["duration"] = media.duration
    if media.orientation:
        base["orientation"] = media.orientation
    base["compressed"] = media.compressed
    try:
        reply = ctx.model.json(JsonRequest(system=system, parts=parts, media_resolution="MEDIA_RESOLUTION_HIGH", max_output_tokens=32_768))
        findings = normalize(reply.data)
        return {**base, "status": "reviewed", "findings": findings, "verdict": verdict_of(findings["issues"]),
                "ms": int((time.monotonic() - started) * 1000), "usage": reply.usage.as_dict()}
    except Exception as error:  # noqa: BLE001 - the file is reported as not reviewed
        return {**base, "status": "failed", "reason": error.message if isinstance(error, Blocked) else f"Reviewing failed: {message_of(error)}"}


@dataclass
class ReviewOutcome:
    results: list[FileResult]
    skipped: list[dict[str, str]]
    rules: dict[str, Any]
    markdown: str
    #: The images, for the agent to review: {name, file}.
    images: list[dict[str, str]]


def _keep_image(item: dict[str, str], ctx: ReviewContext) -> dict[str, str]:
    """An image the agent will open: one unpacked from a zip (in the scratch folder) is copied out first."""
    source = item["file"]
    if not ctx.images_dir or not os.path.abspath(source).startswith(os.path.abspath(ctx.scratch) + os.sep):
        return {"name": item["name"], "file": source}
    relative = item["name"].replace("\\", "/").lstrip("/")
    target = os.path.join(ctx.images_dir, *[p for p in relative.split("/") if p not in ("", ".", "..")])
    stem, ext = os.path.splitext(target)
    n = 2
    while os.path.exists(target):
        target = f"{stem} ({n}){ext}"
        n += 1
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target)
    return {"name": item["name"], "file": target}


def review(project: Project, files: list[str], note: str | None, ctx: ReviewContext) -> ReviewOutcome:
    fixed = fixed_spec(ctx.root, project)
    book = load_book(ctx.shared, project.id)
    dynamic = prompt_rules(book)
    need(fixed.text or dynamic,
         f"{project.id} has no review spec yet: no fixed spec, and the dynamic spec is empty. Have the bot listen to {project.id}'s review "
         f"group first (in the group: 「监听本群更新审片规范」), and review once it has built the dynamic spec from the group's chat")
    system = review_system(project, fixed.text, dynamic)
    os.makedirs(ctx.scratch, exist_ok=True)
    found, skipped = materials(files, ctx.scratch)
    need(found or skipped, "No files to review")
    need(len(found) <= 30, f"At most 30 materials per review ({len(found)} this time): send them in batches")
    images = [_keep_image(item, ctx) for item in found if kind_of(item["file"]) == "image"]
    found = [item for item in found if kind_of(item["file"]) != "image"]
    results: list[FileResult] = [{} for _ in found]
    lock = threading.Lock()
    done = 0

    def work(i: int) -> None:
        nonlocal done
        item = found[i]
        size = os.path.getsize(item["file"]) if os.path.exists(item["file"]) else 0
        ctx.log(f"Reviewing {i + 1}/{len(found)}: {item['name']} ({to_fixed(size / MB, 1)} MB)")
        r = review_one(item, project, system, note, ctx)
        results[i] = r
        with lock:
            done += 1
            count = done
        outcome = f"{len(r['findings']['issues'])} issue(s)" if r["status"] == "reviewed" else f"failed: {r.get('reason')}"
        took = f" ({js_round(r['ms'] / 1000)} s)" if r.get("ms") else ""
        ctx.log(f"Done {count}/{len(found)}: {item['name']}, {outcome}{took}")

    if found:
        with ThreadPoolExecutor(max_workers=min(ctx.concurrency, len(found))) as pool:
            for future in [pool.submit(work, i) for i in range(len(found))]:
                future.result()
    return ReviewOutcome(results=results, skipped=skipped, rules={"fixed": not fixed.placeholder, "dynamic": len(book["rules"])},
                         markdown=render_review(results, skipped) if results or skipped else "", images=images)
